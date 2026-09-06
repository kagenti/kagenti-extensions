// Package toolprune removes unused tool definitions from outbound inference
// requests.
//
// A Claude Code request carries the full tool manifest on every turn — tens of
// thousands of tokens of JSON schema, billed each time and largely for tools
// the agent will never call in a given deployment. The manifest is assembled by
// the client, so the only place to trim it without touching every client is in
// the proxy.
//
// The verdict is entirely configuration: `remove` names the tools to drop.
// There is no learning, no state and no storage dependency. `abctl tools scan`
// produces a candidate list from local transcripts, but the plugin itself only
// ever does what it was told.
//
// Safety is one-directional. Removing a tool the model needs is the harmful
// failure; carrying a few extra definitions is not. So every error path fails
// open, forwarding the original bytes untouched, and a tool named by a forced
// tool_choice is never removed — the manifest and tool_choice have to agree or
// the request is invalid.
//
// That is a promise about this plugin's own failure modes, not a claim that
// pruning is always safe: whether a provider or gateway accepts a validly
// pruned manifest is outside what the plugin can observe. on_error: observe
// exists to establish that empirically before any request changes.
package toolprune

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"sort"
	"strings"
	"sync"

	"github.com/tidwall/gjson"
	"github.com/tidwall/sjson"

	"github.com/rossoctl/cortex/authbridge/authlib/pipeline"
	"github.com/rossoctl/cortex/authbridge/authlib/plugins"
)

// defaultPaths are the inference endpoints the plugin acts on, matched by
// suffix as context-guru does.
var defaultPaths = []string{"/v1/chat/completions", "/v1/completions", "/v1/messages"}

type config struct {
	// Remove names the tools to delete from the manifest. Names not present
	// in a given request are ignored; names the plugin never observes are
	// reported as drift rather than failing.
	Remove []string `json:"remove" description:"Tool names to remove from the outbound manifest."`

	// Paths are the request paths this plugin acts on, matched exactly or by
	// suffix. Defaults to the three inference endpoints.
	Paths []string `json:"paths" description:"Request paths to act on (exact or suffix match)."`

	// Pricing gives per-token rates per model. Rates are per model because they
	// differ enormously: across the Claude family the input rate spans roughly
	// 5x (opus 1.0x, sonnet ~0.4x, haiku ~0.2x), so one flat rate misprices by
	// that factor depending on which model served the request.
	// Keys match the model name the parser records
	// (pctx.Extensions.Inference.Model), matched case-insensitively.
	Pricing map[string]modelRates `json:"pricing" description:"Rates keyed by model name or glob; prefer the per-million fields."`

	// pricing is Pricing with keys lower-cased; built by applyDefaults.
	pricing map[string]modelRates `json:"-"`
	// pricingGlobs are the Pricing keys containing glob metacharacters,
	// compiled in match order — the mechanism that makes a version bump need
	// no edit anywhere.
	pricingGlobs []patternRates `json:"-"`
	// flat is the normalized flat fallback, so ratesFor doesn't rebuild it per
	// request and the unit folding happens exactly once.
	flat modelRates `json:"-"`
	// pricingErr carries any pricing config fault — bad glob, or both units set
	// for one tier — for Configure to reject. Faults are not dropped: an unpriced
	// row from a typo and one from a genuinely unknown model must not look alike.
	pricingErr error `json:"-"`

	// The flat fields are the fallback for models absent from Pricing. Names and
	// semantics match litellm-budget-track. All optional; with nothing set no
	// cost is reported rather than a price being assumed.
	//
	// There is deliberately no output rate: pruning only ever shrinks the
	// prompt, so attributing output cost to it would be false.
	InputCostPerMillion      float64 `json:"input_cost_per_million" description:"Fallback USD per million uncached input tokens, for models absent from pricing."`
	CacheWriteCostPerMillion float64 `json:"cache_write_cost_per_million" description:"Fallback USD per million cache-write tokens; defaults to input_cost_per_million."`
	CacheReadCostPerMillion  float64 `json:"cache_read_cost_per_million" description:"Fallback USD per million cache-read tokens; defaults to input_cost_per_million."`

	InputCostPerToken      float64 `json:"input_cost_per_token" description:"Fallback USD per uncached input token. Alternative to input_cost_per_million; set one, not both."`
	CacheWriteCostPerToken float64 `json:"cache_write_cost_per_token" description:"Fallback USD per cache-write token; defaults to input rate."`
	CacheReadCostPerToken  float64 `json:"cache_read_cost_per_token" description:"Fallback USD per cache-read token; defaults to input rate."`
}

// modelRates is one model's prompt-tier pricing. Cache rates fall back to the
// input rate, matching litellm-budget-track — though on Anthropic-family models
// that fallback is poor (a real cache read is 0.1x input), so set them when known.
type modelRates struct {
	// The per-million fields are the ones to reach for. Every provider publishes
	// prices per million tokens ("$3.80 / Mtok"), so this is the unit an operator
	// already has in hand — no dividing by a million by hand, and no
	// 0.0000038-vs-0.000038 typo that misprices by 10x and looks plausible in
	// either direction.
	InputCostPerMillion      float64 `json:"input_cost_per_million" description:"USD per million uncached input tokens (the unit providers publish)."`
	CacheWriteCostPerMillion float64 `json:"cache_write_cost_per_million" description:"USD per million cache-write tokens; defaults to input_cost_per_million."`
	CacheReadCostPerMillion  float64 `json:"cache_read_cost_per_million" description:"USD per million cache-read tokens; defaults to input_cost_per_million."`

	// The per-token fields remain accepted, for parity with
	// litellm-budget-track's config and with LiteLLM's own
	// model_prices_and_context_window.json — both are per-token, and rates get
	// copied straight out of them. Setting both units for one tier is an error,
	// not a precedence question: see normalize.
	InputCostPerToken      float64 `json:"input_cost_per_token" description:"USD per uncached input token. Alternative to input_cost_per_million; set one, not both."`
	CacheWriteCostPerToken float64 `json:"cache_write_cost_per_token" description:"USD per cache-write token; defaults to input rate."`
	CacheReadCostPerToken  float64 `json:"cache_read_cost_per_token" description:"USD per cache-read token; defaults to input rate."`
}

// tokensPerMillion converts the published unit to the per-token one all the
// downstream arithmetic uses.
const tokensPerMillion = 1_000_000

// normalize folds the per-million fields into the per-token ones, so everything
// after Configure deals in a single unit.
//
// Setting both units for the same tier is rejected rather than resolved by
// precedence. The two differ by 10^6, so picking a winner silently would either
// overstate a saving by a millionfold or bury it below rounding — and the
// readout gives an operator no way to tell which unit was honoured. A startup
// error naming the tier is the only outcome that can't be misread.
//
// what names the entry being normalized, so the error can point at it —
// `pricing["claude-opus-5"]` for a map entry, "config" for the flat fallback.
func (r modelRates) normalize(what string) (modelRates, error) {
	for _, f := range []struct {
		name    string
		million float64
		token   *float64
	}{
		{"input", r.InputCostPerMillion, &r.InputCostPerToken},
		{"cache_write", r.CacheWriteCostPerMillion, &r.CacheWriteCostPerToken},
		{"cache_read", r.CacheReadCostPerMillion, &r.CacheReadCostPerToken},
	} {
		if f.million <= 0 {
			continue
		}
		if *f.token > 0 {
			return r, fmt.Errorf("%s: %s rate set as both %s_cost_per_million and %s_cost_per_token; set one",
				what, f.name, f.name, f.name)
		}
		*f.token = f.million / tokensPerMillion
	}
	return r, nil
}

// rateFor returns the rate for a tier and whether one is actually available.
//
// The bool matters: set() is an OR across three fields, so a model configured
// with only cache_read_cost_per_token used to resolve as "priced" and then
// return 0 for a cache-write request — pricing it at zero while still counting
// toward the priced denominator, so the saving silently vanished with no
// `requests unpriced` row to show it had.
func (r modelRates) rateFor(t tier) (float64, bool) {
	switch t {
	case tierCacheWrite:
		if r.CacheWriteCostPerToken > 0 {
			return r.CacheWriteCostPerToken, true
		}
	case tierCacheRead:
		if r.CacheReadCostPerToken > 0 {
			return r.CacheReadCostPerToken, true
		}
	}
	return r.InputCostPerToken, r.InputCostPerToken > 0
}

func (r modelRates) set() bool {
	return r.InputCostPerToken > 0 || r.CacheWriteCostPerToken > 0 || r.CacheReadCostPerToken > 0
}

// rateSource names where a request's rates came from, so a reported figure can
// carry its own provenance instead of looking equally authoritative either way.
type rateSource int

const (
	rateNone       rateSource = iota // no rates for this model
	rateConfigured                   // operator-supplied, for this model or via the flat fallback
	rateDefault                      // built-in table; see pricing.go
)

// ratesFor resolves rates for a model, most specific first: an explicit pricing
// entry, then the flat fallback, then the built-in defaults. Explicit config
// always wins so an operator on a different gateway can correct the defaults
// per model without deleting anything.
func (c *config) ratesFor(model string) (modelRates, rateSource) {
	key := strings.ToLower(model)
	// Exact config key first, so an operator can pin one version even when a
	// broader pattern would also match it.
	if r, ok := c.pricing[key]; ok && r.set() {
		return r, rateConfigured
	}
	// Then a config glob. This is what lets a model version bump need no edit at
	// all: one "*claude-opus-*" entry covers every opus release.
	if r, ok := lookupPattern(c.pricingGlobs, key); ok {
		return r, rateConfigured
	}
	// Then the built-in family patterns — before the flat fallback, because the
	// flat fields are documented as covering models "absent from pricing", and a
	// model the built-in table knows is not absent. Letting one flat rate shadow
	// every family default would reintroduce flat-rate mispricing, silently, and
	// the figure would claim to be operator-configured.
	if r, ok := lookupPattern(defaultPatterns, key); ok {
		return r, rateDefault
	}
	if c.flat.set() {
		return c.flat, rateConfigured
	}
	return modelRates{}, rateNone
}

func (c *config) applyDefaults() {
	if len(c.Paths) == 0 {
		c.Paths = append([]string(nil), defaultPaths...)
	}
	// Fold model keys to lower case once, so lookup is case-insensitive
	// without allocating per request. Gateways vary in how they echo model
	// names, and a case mismatch would silently unprice the traffic.
	c.pricing = make(map[string]modelRates, len(c.Pricing))
	globs := map[string]modelRates{}
	// Sorted so that when several entries are malformed the reported one is
	// stable across restarts, instead of whichever map iteration reached first.
	keys := make([]string, 0, len(c.Pricing))
	for k := range c.Pricing {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	for _, k := range keys {
		lk := strings.ToLower(k)
		v, err := c.Pricing[k].normalize(fmt.Sprintf("pricing[%q]", k))
		if err != nil && c.pricingErr == nil {
			c.pricingErr = err
		}
		if strings.ContainsAny(lk, "*?[") {
			globs[lk] = v
			continue
		}
		c.pricing[lk] = v
	}
	flat, err := modelRates{
		InputCostPerMillion:      c.InputCostPerMillion,
		CacheWriteCostPerMillion: c.CacheWriteCostPerMillion,
		CacheReadCostPerMillion:  c.CacheReadCostPerMillion,
		InputCostPerToken:        c.InputCostPerToken,
		CacheWriteCostPerToken:   c.CacheWriteCostPerToken,
		CacheReadCostPerToken:    c.CacheReadCostPerToken,
	}.normalize("config")
	if err != nil && c.pricingErr == nil {
		c.pricingErr = err
	}
	c.flat = flat

	globsCompiled, err := compilePatterns(globs)
	if err != nil && c.pricingErr == nil {
		c.pricingErr = fmt.Errorf("invalid pricing pattern: %w", err)
	}
	c.pricingGlobs = globsCompiled
}

// ToolPrune is the plugin. Counters live in metrics, guarded by its own mutex;
// everything else is read-only after Configure.
type ToolPrune struct {
	cfg    config
	raw    json.RawMessage
	remove map[string]struct{}

	m         metrics
	driftOnce sync.Once
	// driftChecked records that the stale-list check actually ran, so a test can
	// tell "guard consumed" from "guard consumed without checking anything".
	driftChecked bool
}

func New() *ToolPrune { return &ToolPrune{} }

func init() {
	plugins.RegisterPlugin("tool-prune", func() pipeline.Plugin { return New() })
}

func (p *ToolPrune) Name() string { return "tool-prune" }

func (p *ToolPrune) Capabilities() pipeline.PluginCapabilities {
	return pipeline.PluginCapabilities{
		// Request-only: the response is never touched, so SSE relay stays
		// incremental. That distinction is the reason WritesResponseBody
		// exists as a separate capability.
		WritesRequestBody: true,
		RequiresAny:       []string{"inference-parser"},
		Description:       "Removes unused tool definitions from inference requests.",
	}
}

// ConfigSchema implements pipeline.SchemaProvider.
func (p *ToolPrune) ConfigSchema() []pipeline.FieldSchema {
	return pipeline.SchemaOf(config{})
}

// RawConfig implements pipeline.RawConfigProvider.
func (p *ToolPrune) RawConfig() json.RawMessage { return p.raw }

func (p *ToolPrune) Configure(raw json.RawMessage) error {
	var c config
	if len(raw) > 0 {
		dec := json.NewDecoder(bytes.NewReader(raw))
		dec.DisallowUnknownFields()
		if err := dec.Decode(&c); err != nil {
			return fmt.Errorf("tool-prune config: %w", err)
		}
	}
	c.applyDefaults()
	if c.pricingErr != nil {
		return fmt.Errorf("tool-prune config: %w", c.pricingErr)
	}

	p.cfg = c
	p.raw = raw
	p.remove = make(map[string]struct{}, len(c.Remove))
	for _, n := range c.Remove {
		if n != "" {
			p.remove[n] = struct{}{}
		}
	}
	if len(p.remove) == 0 {
		slog.Info("tool-prune: configured with an empty remove list — no-op until names are added",
			"hint", "abctl tools scan")
	}
	return nil
}

// gated reports whether the request path is one the plugin acts on.
//
// The query string is stripped first. Providers accept query parameters on
// these endpoints — /v1/messages?beta=true is a real request Claude Code makes —
// and a suffix match against the raw target silently misses every one of them,
// which reads as the plugin doing nothing for no visible reason.
func (p *ToolPrune) gated(path string) bool {
	path = pathOnly(path)
	for _, s := range p.cfg.Paths {
		if path == s || strings.HasSuffix(path, s) {
			return true
		}
	}
	return false
}

// pathOnly drops a query string and any trailing slash, so the configured
// suffixes match the endpoint rather than the exact request target.
func pathOnly(target string) string {
	if i := strings.IndexAny(target, "?#"); i >= 0 {
		target = target[:i]
	}
	if len(target) > 1 && strings.HasSuffix(target, "/") {
		target = strings.TrimRight(target, "/")
	}
	return target
}

// toolNameAt extracts a tool's name from raw manifest element i, covering both
// dialects: Anthropic puts it at tools.i.name, OpenAI at tools.i.function.name.
func toolNameAt(body []byte, i int) string {
	if n := gjson.GetBytes(body, fmt.Sprintf("tools.%d.name", i)); n.Exists() {
		return n.String()
	}
	return gjson.GetBytes(body, fmt.Sprintf("tools.%d.function.name", i)).String()
}

// forcedToolChoice reports the tool a forced tool_choice names, and whether the
// tool_choice could be interpreted at all.
//
// resolvable is false only when tool_choice is an object from which no name can
// be read. That is the dangerous case: the request forces *some* tool the plugin
// cannot identify, so pruning risks removing it and producing an invalid request.
// Dialects nest this differently — Anthropic tool_choice.name, OpenAI
// tool_choice.function.name, Bedrock Converse tool_choice.tool.name — and an
// unknown shape must not be read as "nothing is forced".
//
// A string form ("auto", "none", "any", "required") forces no *specific* tool, so
// it is resolvable with an empty name: pruning is safe.
func forcedToolChoice(body []byte) (name string, resolvable bool) {
	tc := gjson.GetBytes(body, "tool_choice")
	if !tc.Exists() {
		return "", true
	}
	if !tc.IsObject() {
		return "", true // "auto" / "none" / "any" / "required"
	}
	for _, path := range []string{"name", "function.name", "tool.name"} {
		if n := tc.Get(path); n.Type == gjson.String && n.String() != "" {
			return n.String(), true
		}
	}
	// An object naming nothing we recognise. It may still be a plain
	// {"type":"auto"}, which is safe — accept only that narrow shape.
	if t := tc.Get("type"); t.Type == gjson.String {
		switch t.String() {
		case "auto", "none", "any", "required":
			return "", true
		}
	}
	return "", false
}

// OnRequest prunes the manifest. Every failure path returns Continue with the
// body untouched.
func (p *ToolPrune) OnRequest(_ context.Context, pctx *pipeline.Context) (action pipeline.Action) {
	action = pipeline.Action{Type: pipeline.Continue}
	if len(p.remove) == 0 {
		return action
	}
	// A panic here would fail a request to save tokens. Never worth it.
	defer func() {
		if r := recover(); r != nil {
			slog.Warn("tool-prune: recovered, forwarding original body", "panic", r)
			action = pipeline.Action{Type: pipeline.Continue}
		}
	}()

	if !p.gated(pctx.Path) {
		// Distinguish "this is not an HTTP request at all" from "the path did
		// not match". A CONNECT tunnel has no path, and reporting it as a path
		// mismatch sends an operator hunting for a routing problem when the
		// real answer is that TLS is not being decrypted — so the client does
		// not trust the bridge CA and nothing downstream can see the request.
		reason := "path_not_inference"
		if pctx.Path == "" {
			reason = "no_path_tunnelled"
		}
		pctx.Record(pipeline.Invocation{
			Action: pipeline.ActionSkip,
			Reason: reason,
			Path:   pctx.Path,
		})
		return action
	}
	// inference-parser establishes that this is an inference call at all. Its
	// absence means the chain is misconfigured; RequiresAny catches that at
	// build time, so treat it as a skip rather than an error.
	if pctx.Extensions.Inference == nil {
		pctx.Record(pipeline.Invocation{Action: pipeline.ActionSkip, Reason: "no_inference_extension"})
		return action
	}
	body := pctx.Body
	if len(body) == 0 {
		pctx.Record(pipeline.Invocation{Action: pipeline.ActionSkip, Reason: "no_body"})
		return action
	}
	// gjson parses leniently: on a truncated document it still resolves
	// fields, and sjson then rewrites the fragment into garbage. Refuse to
	// touch anything that is not well-formed JSON to begin with.
	if !gjson.ValidBytes(body) {
		pctx.Record(pipeline.Invocation{Action: pipeline.ActionSkip, Reason: "invalid_json"})
		return action
	}
	tools := gjson.GetBytes(body, "tools")
	if !tools.IsArray() {
		pctx.Record(pipeline.Invocation{Action: pipeline.ActionSkip, Reason: "no_tool_manifest"})
		return action
	}

	raw := tools.Array()
	p.noteDrift(pctx.Extensions.Inference.Tools)
	p.m.seen()

	// Resolve indices from the raw bytes rather than from the parsed manifest:
	// inference-parser drops unnamed tools, so manifest position does not
	// reliably map back to array position.
	forced, resolvable := forcedToolChoice(body)
	if !resolvable {
		// tool_choice is an object but names no tool we recognise — e.g. a
		// dialect that nests it differently (Bedrock Converse's
		// {"tool":{"name":X}}). Treating that as "nothing is forced" risks
		// pruning the one tool the request requires, so decline instead. A
		// missed saving is the cheap direction of failure.
		pctx.Record(pipeline.Invocation{
			Action: pipeline.ActionSkip,
			Reason: "tool_choice_unresolved",
			Path:   pctx.Path,
		})
		return action
	}
	// Tools the conversation already used must stay in the manifest. A provider
	// may reject a tool_use / tool_result block that references a tool the
	// request no longer defines, and enabling the plugin mid-conversation (the
	// config hot-reloads) is exactly when history can cite a tool the scan
	// proposed — the scan only looks at a rolling window, so a tool used earlier
	// in this very session can be on the remove list.
	//
	// Not reproducible against every provider (one gateway accepts it), but the
	// cost of the guard is a few unpruned definitions and the cost of being wrong
	// is a failed request, so it is not a trade worth making.
	used := toolsCitedByHistory(body)

	var victims []int
	var anyNameResolved bool
	names := make([]string, 0, len(raw))
	for i := range raw {
		name := toolNameAt(body, i)
		if name == "" {
			continue
		}
		anyNameResolved = true
		if name == forced {
			// Removing the tool tool_choice forces would make the request
			// invalid. Keep it and prune the rest.
			slog.Debug("tool-prune: keeping tool forced by tool_choice", "tool", name)
			continue
		}
		if _, cited := used[name]; cited {
			slog.Debug("tool-prune: keeping tool cited by conversation history", "tool", name)
			continue
		}
		if _, ok := p.remove[name]; ok {
			victims = append(victims, i)
			names = append(names, name)
		}
	}
	if len(victims) == 0 {
		// Distinguish "the manifest had none of the configured tools" from "no
		// tool name could be read at all" — the latter means an unrecognised
		// dialect (Gemini, Bedrock toolSpec nesting), where the plugin is inert
		// for a reason an operator would want to know about.
		reason := "no_configured_tool_present"
		if len(names) == 0 && !anyNameResolved {
			reason = "names_unresolved"
		}
		pctx.Record(pipeline.Invocation{Action: pipeline.ActionSkip, Reason: reason, Path: pctx.Path})
		return action
	}

	out := body
	var err error
	if len(victims) == len(raw) {
		// Emptying the array is not safe — OpenAI rejects `tools: []`, and
		// tool_choice without tools. Drop both keys instead.
		if out, err = sjson.DeleteBytes(out, "tools"); err != nil {
			slog.Warn("tool-prune: delete tools failed, forwarding original", "err", err)
			return action
		}
		if gjson.GetBytes(out, "tool_choice").Exists() {
			if out, err = sjson.DeleteBytes(out, "tool_choice"); err != nil {
				slog.Warn("tool-prune: delete tool_choice failed, forwarding original", "err", err)
				return action
			}
		}
	} else {
		// A prompt-cache breakpoint rides on one element (Claude Code marks the
		// last tool). Deleting that element deletes the breakpoint, and losing
		// it turns every subsequent turn into a full cache write — which costs
		// far more than the definitions saved. Carry the marker to the last
		// surviving tool instead.
		victimSet := make(map[int]bool, len(victims))
		for _, v := range victims {
			victimSet[v] = true
		}
		// Last marker wins: if two pruned tools each carried a breakpoint, only
		// one can move to the single last survivor. Claude Code marks exactly one
		// tool, so this is not a shape seen in practice — but a future reader
		// should know the overwrite is deliberate, not an oversight.
		var orphanedCacheControl gjson.Result
		for _, v := range victims {
			if cc := gjson.GetBytes(body, fmt.Sprintf("tools.%d.cache_control", v)); cc.Exists() {
				orphanedCacheControl = cc
			}
		}
		lastSurvivor := -1
		for i := len(raw) - 1; i >= 0; i-- {
			if !victimSet[i] {
				lastSurvivor = i
				break
			}
		}
		if orphanedCacheControl.Exists() && lastSurvivor >= 0 &&
			!gjson.GetBytes(body, fmt.Sprintf("tools.%d.cache_control", lastSurvivor)).Exists() {
			if out, err = sjson.SetRawBytes(out,
				fmt.Sprintf("tools.%d.cache_control", lastSurvivor),
				[]byte(orphanedCacheControl.Raw)); err != nil {
				slog.Warn("tool-prune: could not preserve cache_control, forwarding original", "err", err)
				return action
			}
			slog.Debug("tool-prune: moved cache_control to the last surviving tool", "index", lastSurvivor)
		}
		// Descending, so an earlier deletion never shifts a later index.
		for i := len(victims) - 1; i >= 0; i-- {
			if out, err = sjson.DeleteBytes(out, fmt.Sprintf("tools.%d", victims[i])); err != nil {
				slog.Warn("tool-prune: delete failed, forwarding original", "index", victims[i], "err", err)
				return action
			}
		}
	}
	if len(out) >= len(body) {
		// Nothing shrank: treat as a no-op rather than emitting a rewrite.
		pctx.Record(pipeline.Invocation{Action: pipeline.ActionSkip, Reason: "no_bytes_removed"})
		return action
	}
	// Post-conditions. The edit is surgical, so verify it actually did what
	// was intended before putting it on the wire: still valid JSON, and
	// exactly the intended number of tools left standing.
	if !gjson.ValidBytes(out) {
		slog.Warn("tool-prune: rewrite produced invalid JSON, forwarding original")
		return action
	}
	want := len(raw) - len(victims)
	if got := len(gjson.GetBytes(out, "tools").Array()); got != want {
		slog.Warn("tool-prune: unexpected tool count after rewrite, forwarding original",
			"got", got, "want", want)
		return action
	}

	removedBytes := len(body) - len(out)
	// Publish the per-request saving so a UI can show it on the row rather than
	// only in an aggregate pane. Emitted here, in OnRequest, because the
	// listener records the response session event before the deferred
	// RunFinish, so anything published from OnFinish arrives too late to appear.
	//
	// Everything except the token tier is known now: inference-parser runs
	// earlier in the chain and has already set the model, so the applicable
	// rates resolve here. The consumer pairs this with the response event
	// (matching on RequestID) to get the prompt token total and which tier the
	// saving came out of, and finishes the arithmetic.
	rates, src := p.cfg.ratesFor(inferenceModel(pctx))
	rateInput, _ := rates.rateFor(tierInput)
	rateWrite, okW := rates.rateFor(tierCacheWrite)
	rateRead, okR := rates.rateFor(tierCacheRead)
	if !okW {
		rateWrite = 0
	}
	if !okR {
		rateRead = 0
	}
	// SetBody BEFORE publishing, so the event can report what was actually sent.
	// Under ErrorPolicyObserve it is a no-op on bytes and leaves bodyMutated
	// false — this same code path measures without enforcing.
	pctx.SetBody(out)
	applied := pctx.BodyMutated()
	// The body upstream actually sees: the rewrite when it was applied, the
	// original when it was only measured.
	bodySent := len(out)
	if !applied {
		bodySent = len(body)
	}
	p.publish(pctx, pruneEvent{
		ToolsRemoved:   names,
		BytesRemoved:   removedBytes,
		BodyBytesAfter: bodySent,
		Projected:      !applied,
		Model:          inferenceModel(pctx),
		RateInput:      rateInput,
		RateCacheWrite: rateWrite,
		RateCacheRead:  rateRead,
		RateSource:     src.String(),
	})
	// Carry the saving to OnFinish, where the response reveals which token tier
	// it came out of. SetState keeps it private to this plugin, unlike
	// Extensions.Custom which is shared.
	pipeline.SetState(pctx, p.Name(), &requestState{bytesRemoved: removedBytes})
	if applied {
		p.m.pruned(names, removedBytes)
	} else {
		p.m.projected(names, removedBytes)
	}
	return action
}

func (p *ToolPrune) OnResponse(_ context.Context, _ *pipeline.Context) pipeline.Action {
	return pipeline.Action{Type: pipeline.Continue}
}

// requestState carries the per-request byte saving from OnRequest to OnFinish.
type requestState struct{ bytesRemoved int }

// OnFinish converts the request's byte saving into tokens and attributes it to
// the token tier it actually came out of.
//
// Two things make a single "tokens saved" number wrong, which is why this is
// per-tier. First, the ratio: rather than bundling a tokenizer or assuming
// bytes-per-token, it is calibrated on this request — prompt tokens over request
// bytes, both post-pruning, so the two sides are consistent. Second, and larger:
// providers price prompt tiers very differently. Anthropic charges 1.25x the
// input rate for a cache write and 0.1x for a cache read, so identical saved
// bytes are worth more than 12x more on a cache miss than on a hit. Reporting
// one blended figure would hide a factor of twelve.
//
// The tool manifest sits inside the cached prefix — Claude Code puts
// cache_control on the tool block — so on a cache-miss request the saving comes
// out of cache writes, and on a hit out of cache reads. That is the assumption
// this attribution rests on; it is stated here because it is the one thing that
// would need revisiting for a client that lays out its prompt differently.
func (p *ToolPrune) OnFinish(_ context.Context, pctx *pipeline.Context) {
	st := pipeline.GetState[requestState](pctx, p.Name())
	if st == nil || st.bytesRemoved <= 0 {
		return
	}
	inf := pctx.Extensions.Inference
	if inf == nil || len(pctx.Body) == 0 {
		return
	}
	promptTotal := inf.InputTokens + inf.CacheReadTokens + inf.CacheWriteTokens
	if promptTotal <= 0 {
		// Fall back to the aggregate when a provider reports only a total.
		promptTotal = inf.PromptTokens
	}
	if promptTotal <= 0 {
		return
	}
	tokens := float64(st.bytesRemoved) * float64(promptTotal) / float64(len(pctx.Body))
	if tokens <= 0 {
		return
	}
	t := tierOf(inf)
	rates, src := p.cfg.ratesFor(inf.Model)
	rate, ok := rates.rateFor(t)
	if !ok {
		// No usable rate for the tier this request actually used. Count it
		// unpriced rather than charging zero into the total.
		src = rateNone
	}
	p.m.observeSaving(tokens, t, tokens*rate, src, inf.Model)
}

// tier names which prompt token tier a request's saving came out of.
type tier int

const (
	tierInput tier = iota
	tierCacheWrite
	tierCacheRead
)

// tierOf picks the tier the pruned manifest belonged to. The manifest is in the
// cached prefix, so a write-dominant request wrote it and a read-dominant one
// read it; with no cache tokens reported at all it was plain input.
func tierOf(inf *pipeline.InferenceExtension) tier {
	switch {
	case inf.CacheWriteTokens > inf.CacheReadTokens && inf.CacheWriteTokens > 0:
		return tierCacheWrite
	case inf.CacheReadTokens > 0:
		return tierCacheRead
	default:
		return tierInput
	}
}

// noteDrift logs, once, any configured name absent from the first manifest the
// plugin actually sees. A stale list costs savings rather than correctness, so
// it surfaces as a warning instead of a failure.
func (p *ToolPrune) noteDrift(observed []pipeline.InferenceTool) {
	// Check the precondition BEFORE consuming the Once. sync.Once marks itself
	// done however the closure returns, so an early return on an empty manifest
	// used to disable this warning permanently — and an empty first manifest is
	// the norm on the dialects the plugin already knows it cannot read names
	// from (Gemini functionDeclarations, Bedrock toolSpec nesting), which is a
	// live path here. The result was that a stale remove list stayed silent in
	// exactly the deployments most likely to have one.
	if len(observed) == 0 {
		return
	}
	p.driftOnce.Do(func() {
		p.driftChecked = true
		present := make(map[string]struct{}, len(observed))
		for _, t := range observed {
			present[t.Name] = struct{}{}
		}
		var missing []string
		for _, n := range p.cfg.Remove {
			if _, ok := present[n]; !ok {
				missing = append(missing, n)
			}
		}
		if len(missing) > 0 {
			slog.Warn("tool-prune: configured tools not present in the observed manifest — list may be stale",
				"missing", strings.Join(missing, ","),
				"hint", "re-run abctl tools scan")
		}
	})
}

// Metrics implements pipeline.MetricsProvider.
func (p *ToolPrune) Metrics() []pipeline.Metric { return p.m.snapshot() }
