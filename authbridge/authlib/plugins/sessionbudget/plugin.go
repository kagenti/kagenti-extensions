// Package sessionbudget enforces per-session lifetime budgets on tokens,
// inference calls, and wall-clock duration. Must run before inference-parser
// in the declared plugin order (response path is reverse: inference-parser
// finalizes counts first, then this plugin reads them).
package sessionbudget

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"strconv"
	"sync"
	"time"

	"github.com/rossoctl/cortex/authbridge/authlib/pipeline"
	"github.com/rossoctl/cortex/authbridge/authlib/plugins"
	"github.com/rossoctl/cortex/authbridge/authlib/session"
	"github.com/rossoctl/cortex/authbridge/authlib/storage"
	"golang.org/x/sync/singleflight"
)

type config struct {
	RedisURL               string `json:"redis_url" required:"true" description:"Redis/Valkey connection URL."`
	MaxTokens              int64  `json:"max_tokens" description:"Cumulative token ceiling per session. 0 = no limit."`
	MaxCalls               int64  `json:"max_calls" description:"Max LLM/inference calls per session. Only inference-parser output increments this counter; MCP tool calls and other outbound traffic do not. Once the limit is reached, all subsequent outbound requests (including MCP tool calls) are blocked until the session resets. 0 = no limit."`
	MaxDurationSeconds     int64  `json:"max_duration_seconds" description:"Wall-clock session lifetime in seconds. 0 = no limit."`
	OnExceed               string `json:"on_exceed" description:"Action on breach: deny, observe (shadow), or pause (HITL webhook approval)." default:"deny" enum:"deny,observe,pause"`
	PauseWebhook           string `json:"pause_webhook" description:"URL to POST for approval when on_exceed=pause. Required when on_exceed=pause."`
	PauseTimeout           string `json:"pause_timeout" description:"How long to wait for webhook response." default:"30s"`
	PauseTimeoutAction     string `json:"pause_timeout_action" description:"Action on webhook timeout/error: deny or allow." default:"deny" enum:"deny,allow"`
	PauseGracePeriod       string `json:"pause_grace_period" description:"After approval, suppress further webhooks for this duration." default:"5m"`
	SessionTTLSeconds      int    `json:"session_ttl_seconds" description:"Redis key TTL; should be >= max_duration_seconds." default:"7200"`
	RefreshInterval        string `json:"refresh_interval" description:"How often to sync local cache from Redis." default:"5s"`
	RedisUnavailable       string `json:"redis_unavailable" description:"Behavior when Redis is unreachable. Only fail_open is supported; fail_closed is reserved." default:"fail_open"`
	DefaultSessionFallback bool   `json:"default_session_fallback" description:"Pool sessionless traffic into a shared 'default' bucket. Off by default. Single-workload only." default:"false"`
}

// approvalFlight carries the outcome of one webhook call. The leader writes
// approved before closing done; followers read it only after receiving from
// done, so the happens-before edge is safe without further synchronization.
// Attaching the result to the flight (instead of the cache entry) makes each
// waiter observe the outcome of the flight it actually waited on — a new
// leader that starts a second webhook after this one closes cannot clobber
// this flight's approved, and a refreshCache that deletes the cache entry
// mid-flight cannot make followers read a stale zero value.
type approvalFlight struct {
	done     chan struct{}
	approved bool
}

type counters struct {
	tokens         int64
	calls          int64
	startedAt      time.Time
	lastApprovedAt time.Time
	// pendingApproval is non-nil while a webhook call for this session is in
	// flight. Concurrent breaches wait on flight.done; the leader publishes
	// flight.approved before closing done, then clears this field.
	pendingApproval *approvalFlight
	// pendingWrites counts in-flight accumulate goroutines whose Redis writes
	// haven't landed yet. refreshCache leaves the local entry alone (rather
	// than deleting a Redis-missing session) while this is > 0, so a race
	// between accumulate and a refresh tick can't erase local counters.
	pendingWrites int
}

// SessionBudget is the plugin state. Redis provides cross-pod durability;
// the local cache provides zero-I/O enforcement on the request path.
type SessionBudget struct {
	cfg          config
	store        storage.Store
	log          *slog.Logger
	httpClient   *http.Client
	gracePeriod  time.Duration
	pauseTimeout time.Duration

	mu           sync.RWMutex
	cache        map[string]*counters
	hydrateG     singleflight.Group
	stopCh       chan struct{}
	stopped      chan struct{}
	shutdownOnce sync.Once
	closeOnce    sync.Once
	closeErr     error
}

func New() *SessionBudget {
	return &SessionBudget{
		cache:   make(map[string]*counters),
		stopCh:  make(chan struct{}),
		stopped: make(chan struct{}),
		log:     slog.Default().With("plugin", "session-budget"),
	}
}

func init() {
	plugins.RegisterPlugin("session-budget", func() pipeline.Plugin { return New() })
}

func (p *SessionBudget) Name() string { return "session-budget" }

func (p *SessionBudget) Capabilities() pipeline.PluginCapabilities {
	return pipeline.PluginCapabilities{
		Description: "Enforce per-session token, call, and duration budgets via Redis.",
	}
}

func (p *SessionBudget) Configure(raw json.RawMessage) error {
	p.cfg = config{
		OnExceed:          "deny",
		SessionTTLSeconds: 7200,
		RefreshInterval:   "5s",
		RedisUnavailable:  "fail_open",
	}
	if err := json.Unmarshal(raw, &p.cfg); err != nil {
		return fmt.Errorf("session-budget config: %w", err)
	}
	if p.cfg.RedisURL == "" {
		return fmt.Errorf("session-budget: redis_url is required")
	}
	if p.cfg.MaxTokens <= 0 && p.cfg.MaxCalls <= 0 && p.cfg.MaxDurationSeconds <= 0 {
		return fmt.Errorf("session-budget: at least one limit (max_tokens, max_calls, max_duration_seconds) must be > 0")
	}
	if p.cfg.SessionTTLSeconds < 0 {
		return fmt.Errorf("session-budget: session_ttl_seconds must be > 0 (got %d)", p.cfg.SessionTTLSeconds)
	}
	if p.cfg.SessionTTLSeconds == 0 {
		// Explicit 0 (or unset via struct-marshal) — restore default.
		p.cfg.SessionTTLSeconds = 7200
	}
	if p.cfg.MaxDurationSeconds > 0 && int64(p.cfg.SessionTTLSeconds) < p.cfg.MaxDurationSeconds {
		return fmt.Errorf("session-budget: session_ttl_seconds (%d) must be >= max_duration_seconds (%d); Redis would expire counters mid-session and reopen enforcement gaps",
			p.cfg.SessionTTLSeconds, p.cfg.MaxDurationSeconds)
	}
	switch p.cfg.OnExceed {
	case "deny", "observe", "pause":
	default:
		return fmt.Errorf("session-budget: on_exceed must be \"deny\", \"observe\", or \"pause\" (got %q)", p.cfg.OnExceed)
	}
	if p.cfg.OnExceed == "pause" {
		if p.cfg.PauseWebhook == "" {
			return fmt.Errorf("session-budget: pause_webhook is required when on_exceed=\"pause\"")
		}
		if p.cfg.PauseTimeout == "" {
			p.cfg.PauseTimeout = "30s"
		}
		if d, err := time.ParseDuration(p.cfg.PauseTimeout); err != nil {
			return fmt.Errorf("session-budget: invalid pause_timeout %q: %w", p.cfg.PauseTimeout, err)
		} else if d <= 0 {
			return fmt.Errorf("session-budget: pause_timeout must be > 0 (got %q)", p.cfg.PauseTimeout)
		} else {
			p.pauseTimeout = d
		}
		if p.cfg.PauseTimeoutAction == "" {
			p.cfg.PauseTimeoutAction = "deny"
		}
		if p.cfg.PauseTimeoutAction != "deny" && p.cfg.PauseTimeoutAction != "allow" {
			return fmt.Errorf("session-budget: pause_timeout_action must be \"deny\" or \"allow\" (got %q)", p.cfg.PauseTimeoutAction)
		}
		if p.cfg.PauseGracePeriod == "" {
			p.cfg.PauseGracePeriod = "5m"
		}
		if d, err := time.ParseDuration(p.cfg.PauseGracePeriod); err != nil {
			return fmt.Errorf("session-budget: invalid pause_grace_period %q: %w", p.cfg.PauseGracePeriod, err)
		} else if d < 0 {
			return fmt.Errorf("session-budget: pause_grace_period must be >= 0 (got %q); use \"0s\" to fire the webhook on every breach", p.cfg.PauseGracePeriod)
		} else {
			p.gracePeriod = d
		}
	}
	if d, err := time.ParseDuration(p.cfg.RefreshInterval); err != nil {
		return fmt.Errorf("session-budget: invalid refresh_interval %q: %w", p.cfg.RefreshInterval, err)
	} else if d <= 0 {
		return fmt.Errorf("session-budget: refresh_interval must be > 0 (got %q)", p.cfg.RefreshInterval)
	}
	if p.cfg.RedisUnavailable == "fail_closed" {
		return fmt.Errorf("session-budget: redis_unavailable=fail_closed is not yet implemented; use fail_open")
	}
	return nil
}

func (p *SessionBudget) Init(_ context.Context) error {
	// "redis" driver handles both Redis and Valkey (wire-compatible); URL must use redis:// scheme.
	store, err := storage.Open("redis", p.cfg.RedisURL)
	if err != nil {
		return fmt.Errorf("session-budget: redis connect: %w", err)
	}
	p.store = store

	if p.cfg.OnExceed == "pause" && p.httpClient == nil {
		// Timeout: 0 — per-request deadline is set via context in callPauseWebhook.
		p.httpClient = &http.Client{Timeout: 0}
	}

	interval, _ := time.ParseDuration(p.cfg.RefreshInterval)
	go p.refreshLoop(interval)
	return nil
}

// In-flight accumulate goroutines get ErrClosed after store.Close — bounded by their 2s ctx.
// Safe to call multiple times: both the stopCh close and store.Close are guarded by sync.Once.
func (p *SessionBudget) Shutdown(ctx context.Context) error {
	p.shutdownOnce.Do(func() { close(p.stopCh) })
	select {
	case <-p.stopped:
	case <-ctx.Done():
		// refreshLoop is still running and calls p.store.HashGet; closing the
		// store here would race that call. Leave the store open and let the
		// process exit reclaim it — refreshLoop terminates when p.stopCh
		// closes, and the shutdown-deadline is already exceeded.
		p.log.Warn("shutdown timed out waiting for refresh loop; leaving store open")
		return ctx.Err()
	}
	if p.store != nil {
		p.closeOnce.Do(func() { p.closeErr = p.store.Close() })
		return p.closeErr
	}
	return nil
}

// OnRequest evaluates cached counters against limits. On cold cache the first
// miss hydrates from Redis so pre-existing sessions enforce immediately.
func (p *SessionBudget) OnRequest(ctx context.Context, pctx *pipeline.Context) pipeline.Action {
	sessionID := p.sessionID(pctx)
	if sessionID == "" {
		pctx.Skip("no_session_id")
		return pipeline.Action{Type: pipeline.Continue}
	}

	p.mu.Lock()
	c, ok := p.cache[sessionID]
	if !ok {
		p.mu.Unlock()
		// Cold cache handling is mode-dependent:
		//   - pause: synchronously hydrate from Redis so pre-existing sessions
		//     (seeded by another pod) fire the webhook on request #1. Pause is
		//     the only mode where a one-request-per-pod overshoot would defeat
		//     the point — HITL only works if we ask before continuing.
		//   - deny / observe: skip with cold_cache. The local counters populate
		//     via OnResponseFrame + the background refresh loop; a single pod
		//     may under-enforce by up to one request for a pre-existing session,
		//     which is the same tradeoff these modes have always had. This
		//     avoids putting Redis on the request path for the common modes.
		if p.cfg.OnExceed == "pause" {
			// Hydrate returns true when Redis had the session; the second
			// cache lookup can still miss if refreshCache raced in and
			// evicted the freshly-hydrated entry between the two locks.
			// One retry closes that window without unbounded looping —
			// singleflight ensures both hydrate calls share one Redis read.
			for attempt := 0; attempt < 2; attempt++ {
				if !p.hydrateCache(sessionID) {
					pctx.Skip("cold_cache")
					return pipeline.Action{Type: pipeline.Continue}
				}
				p.mu.Lock()
				c, ok = p.cache[sessionID]
				if ok {
					break
				}
				p.mu.Unlock()
			}
			if !ok {
				pctx.Skip("cold_cache")
				return pipeline.Action{Type: pipeline.Continue}
			}
		} else {
			pctx.Skip("cold_cache")
			return pipeline.Action{Type: pipeline.Continue}
		}
	}
	snap := *c
	if reason := p.evaluate(&snap); reason != "" {
		switch p.cfg.OnExceed {
		case "observe":
			p.mu.Unlock()
			pctx.Observe("shadow_budget_exceeded")
			p.log.Warn("budget exceeded (shadow mode)",
				"session", sessionID,
				"reason", reason,
				"tokens", snap.tokens,
				"calls", snap.calls)
			return pipeline.Action{Type: pipeline.Continue}

		case "pause":
			// Grace window: skip webhook if recently approved.
			if p.gracePeriod > 0 && !c.lastApprovedAt.IsZero() && time.Since(c.lastApprovedAt) < p.gracePeriod {
				p.mu.Unlock()
				pctx.Allow("pause_grace_window")
				return pipeline.Action{Type: pipeline.Continue}
			}
			if c.pendingApproval != nil {
				// Another goroutine is already calling the webhook — wait for
				// its outcome so followers honor a deny instead of racing past.
				flight := c.pendingApproval
				p.mu.Unlock()
				select {
				case <-flight.done:
				case <-ctx.Done():
					pctx.Record(pipeline.Invocation{Action: pipeline.ActionDeny, Reason: "pause_wait_canceled"})
					return pipeline.DenyWithDetails("budget.exceeded", reason+" (client canceled during pause)", p.buildDetails(&snap))
				}
				if flight.approved {
					pctx.Allow("pause_follower_approved")
					return pipeline.Action{Type: pipeline.Continue}
				}
				pctx.Record(pipeline.Invocation{Action: pipeline.ActionDeny, Reason: "pause_follower_denied"})
				return pipeline.DenyWithDetails("budget.exceeded", reason+" (approval denied)", p.buildDetails(&snap))
			}
			flight := &approvalFlight{done: make(chan struct{})}
			c.pendingApproval = flight
			p.mu.Unlock()
			p.log.Info("budget exceeded, requesting approval",
				"session", sessionID,
				"reason", reason)
			// Deferred cleanup so a panic (or runtime.Goexit) in
			// callPauseWebhook can't wedge the session: without this,
			// pendingApproval would stay non-nil forever and every
			// future request for this session would block on the dead
			// flight.done. Order matters: publish outcome to the flight
			// object first, then close done (channel-close is the
			// happens-before edge for followers), then clear
			// pendingApproval under the lock.
			approved := false
			defer func() {
				flight.approved = approved
				close(flight.done)
				p.mu.Lock()
				if cc, ok := p.cache[sessionID]; ok {
					cc.pendingApproval = nil
					if approved {
						cc.lastApprovedAt = time.Now()
					}
				}
				p.mu.Unlock()
			}()
			approved = p.callPauseWebhook(sessionID, reason, &snap)
			if approved {
				pctx.Allow("pause_approved")
				return pipeline.Action{Type: pipeline.Continue}
			}
			details := p.buildDetails(&snap)
			pctx.Record(pipeline.Invocation{Action: pipeline.ActionDeny, Reason: "pause_denied"})
			return pipeline.DenyWithDetails("budget.exceeded", reason+" (approval denied)", details)

		default: // "deny"
			p.mu.Unlock()
			details := p.buildDetails(&snap)
			pctx.Record(pipeline.Invocation{Action: pipeline.ActionDeny, Reason: "budget_exceeded"})
			return pipeline.DenyWithDetails("budget.exceeded", reason, details)
		}
	}
	p.mu.Unlock()
	// Counts are incremented in OnResponseFrame when inference lands, so
	// max_calls only counts LLM/inference calls (see plugin doc).
	pctx.Allow("under_budget")
	return pipeline.Action{Type: pipeline.Continue}
}

// OnResponse is a no-op; see OnResponseFrame.
func (p *SessionBudget) OnResponse(_ context.Context, _ *pipeline.Context) pipeline.Action {
	return pipeline.Action{Type: pipeline.Continue}
}

// OnResponseFrame accumulates token counts on finalization (last=true).
func (p *SessionBudget) OnResponseFrame(_ context.Context, pctx *pipeline.Context, _ []byte, last bool) pipeline.Action {
	if !last {
		return pipeline.Action{Type: pipeline.Continue}
	}

	sessionID := p.sessionID(pctx)
	if sessionID == "" {
		return pipeline.Action{Type: pipeline.Continue}
	}

	inf := pctx.Extensions.Inference
	if inf == nil {
		return pipeline.Action{Type: pipeline.Continue}
	}

	tokens := int64(inf.TotalTokens)

	p.mu.Lock()
	c, ok := p.cache[sessionID]
	if !ok {
		c = &counters{startedAt: time.Now()}
		p.cache[sessionID] = c
	}
	c.tokens += tokens
	c.calls++
	c.pendingWrites++
	p.mu.Unlock()

	go p.accumulate(sessionID, tokens)

	return pipeline.Action{Type: pipeline.Continue}
}

func (p *SessionBudget) buildDetails(snap *counters) map[string]any {
	details := map[string]any{
		"spent_tokens": snap.tokens,
		"spent_calls":  snap.calls,
		"token_limit":  p.cfg.MaxTokens,
		"call_limit":   p.cfg.MaxCalls,
	}
	if p.cfg.MaxDurationSeconds > 0 && !snap.startedAt.IsZero() {
		details["duration_seconds"] = int64(time.Since(snap.startedAt).Seconds())
		details["duration_limit"] = p.cfg.MaxDurationSeconds
	}
	return details
}

type pauseRequest struct {
	SessionID       string `json:"session_id"`
	Reason          string `json:"reason"`
	SpentTokens     int64  `json:"spent_tokens"`
	SpentCalls      int64  `json:"spent_calls"`
	TokenLimit      int64  `json:"token_limit"`
	CallLimit       int64  `json:"call_limit"`
	DurationSeconds int64  `json:"duration_seconds,omitempty"`
	DurationLimit   int64  `json:"duration_limit,omitempty"`
}

type pauseResponse struct {
	Action string `json:"action"`
}

func (p *SessionBudget) callPauseWebhook(sessionID, reason string, snap *counters) bool {
	// Decouple from the inbound request ctx so a client disconnect can't
	// cancel the webhook out from under waiting followers.
	ctx, cancel := context.WithTimeout(context.Background(), p.pauseTimeout)
	defer cancel()

	body := pauseRequest{
		SessionID:   sessionID,
		Reason:      reason,
		SpentTokens: snap.tokens,
		SpentCalls:  snap.calls,
		TokenLimit:  p.cfg.MaxTokens,
		CallLimit:   p.cfg.MaxCalls,
	}
	if p.cfg.MaxDurationSeconds > 0 && !snap.startedAt.IsZero() {
		body.DurationSeconds = int64(time.Since(snap.startedAt).Seconds())
		body.DurationLimit = p.cfg.MaxDurationSeconds
	}

	payload, _ := json.Marshal(body)
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, p.cfg.PauseWebhook, bytes.NewReader(payload))
	if err != nil {
		p.log.Warn("pause webhook request build failed", "session", sessionID, "err", err)
		return p.cfg.PauseTimeoutAction == "allow"
	}
	req.Header.Set("Content-Type", "application/json")

	resp, err := p.httpClient.Do(req)
	if err != nil {
		p.log.Warn("pause webhook call failed", "session", sessionID, "err", err)
		return p.cfg.PauseTimeoutAction == "allow"
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		responseBody, _ := io.ReadAll(io.LimitReader(resp.Body, 512))
		p.log.Warn("pause webhook non-200", "session", sessionID, "status", resp.StatusCode, "response_bytes", len(responseBody))
		return p.cfg.PauseTimeoutAction == "allow"
	}

	var result pauseResponse
	if err := json.NewDecoder(io.LimitReader(resp.Body, 4096)).Decode(&result); err != nil {
		p.log.Warn("pause webhook response decode failed", "session", sessionID, "err", err)
		return p.cfg.PauseTimeoutAction == "allow"
	}
	switch result.Action {
	case "approve":
		return true
	case "deny":
		return false
	default:
		p.log.Warn("pause webhook unknown action; treating as deny", "session", sessionID, "action", result.Action)
		return false
	}
}

func (p *SessionBudget) evaluate(c *counters) string {
	if p.cfg.MaxTokens > 0 && c.tokens >= p.cfg.MaxTokens {
		return fmt.Sprintf("token limit reached: %d/%d", c.tokens, p.cfg.MaxTokens)
	}
	if p.cfg.MaxCalls > 0 && c.calls >= p.cfg.MaxCalls {
		return fmt.Sprintf("call limit reached: %d/%d", c.calls, p.cfg.MaxCalls)
	}
	if p.cfg.MaxDurationSeconds > 0 && !c.startedAt.IsZero() {
		elapsed := time.Since(c.startedAt).Seconds()
		if int64(elapsed) >= p.cfg.MaxDurationSeconds {
			return fmt.Sprintf("duration limit reached: %ds/%ds", int64(elapsed), p.cfg.MaxDurationSeconds)
		}
	}
	return ""
}

// accumulate writes counters to Redis. On failure, writes are dropped (fail-open).
func (p *SessionBudget) accumulate(sessionID string, tokens int64) {
	defer func() {
		p.mu.Lock()
		if cc, ok := p.cache[sessionID]; ok && cc.pendingWrites > 0 {
			cc.pendingWrites--
		}
		p.mu.Unlock()
	}()

	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()

	key := p.redisKey(sessionID)
	ttl := time.Duration(p.cfg.SessionTTLSeconds) * time.Second

	if tokens > 0 {
		if _, err := p.store.HashIncr(ctx, key, "tokens", tokens); err != nil {
			p.log.Warn("redis HashIncr tokens failed", "session", sessionID, "err", err)
		}
	}

	if _, err := p.store.HashIncr(ctx, key, "calls", 1); err != nil {
		p.log.Warn("redis HashIncr calls failed", "session", sessionID, "err", err)
	}

	if _, err := p.store.HashSetNX(ctx, key, "started_at", strconv.FormatInt(time.Now().Unix(), 10)); err != nil {
		p.log.Warn("redis HashSetNX started_at failed", "session", sessionID, "err", err)
	}
	// Refresh TTL on every accumulate. Redis EXPIRE is idempotent and this
	// self-heals keys where a prior Expire failed after HashSetNX succeeded —
	// without it, one Expire failure would leave the key TTL-less forever
	// (HashSetNX only fires TTL on its first success per key).
	if err := p.store.Expire(ctx, key, ttl); err != nil {
		p.log.Warn("redis Expire failed", "session", sessionID, "err", err)
	}
}

func (p *SessionBudget) refreshLoop(interval time.Duration) {
	defer close(p.stopped)
	ticker := time.NewTicker(interval)
	defer ticker.Stop()

	for {
		select {
		case <-p.stopCh:
			return
		case <-ticker.C:
			p.refreshCache()
		}
	}
}

// hydrateCache pulls one session's counters from Redis on cold-cache miss.
// Concurrent callers for the same session share one Redis lookup via singleflight.
func (p *SessionBudget) hydrateCache(sessionID string) bool {
	v, _, _ := p.hydrateG.Do(sessionID, func() (any, error) {
		// Decouple from the caller's ctx: singleflight shares one flight
		// across concurrent callers, so a leader's client disconnect would
		// otherwise cancel the lookup for every follower and let a burst
		// of pause-mode requests skip enforcement together.
		lookupCtx, cancel := context.WithTimeout(context.Background(), 200*time.Millisecond)
		defer cancel()
		fields, err := p.store.HashGet(lookupCtx, p.redisKey(sessionID))
		if err != nil {
			p.log.Warn("hydrate: redis lookup failed", "session", sessionID, "err", err)
			return false, nil
		}
		if len(fields) == 0 {
			return false, nil
		}
		tokens, _ := strconv.ParseInt(fields["tokens"], 10, 64)
		calls, _ := strconv.ParseInt(fields["calls"], 10, 64)
		var startedAt time.Time
		if ts, err := strconv.ParseInt(fields["started_at"], 10, 64); err == nil {
			startedAt = time.Unix(ts, 0)
		}
		p.mu.Lock()
		// Do not overwrite: OnResponseFrame may have seeded an entry between
		// our HashGet and this lock. Its counters are fresher than Redis.
		if _, exists := p.cache[sessionID]; !exists {
			p.cache[sessionID] = &counters{tokens: tokens, calls: calls, startedAt: startedAt}
		}
		p.mu.Unlock()
		return true, nil
	})
	return v.(bool)
}

// refreshCache replaces local counters with authoritative Redis values.
func (p *SessionBudget) refreshCache() {
	p.mu.RLock()
	keys := make([]string, 0, len(p.cache))
	for k := range p.cache {
		keys = append(keys, k)
	}
	p.mu.RUnlock()

	for _, sessionID := range keys {
		ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
		fields, err := p.store.HashGet(ctx, p.redisKey(sessionID))
		cancel()

		if err != nil {
			p.log.Warn("redis refresh failed", "session", sessionID, "err", err)
			continue
		}

		if len(fields) == 0 {
			p.mu.Lock()
			// Preserve entries with in-flight work — deleting mid-flight would
			// lose state the flight's completion writes back:
			//   - pendingWrites > 0: a slow accumulate is about to land counters
			//   - pendingApproval != nil: a webhook is mid-call; its defer will
			//     write lastApprovedAt, and dropping the entry would defeat
			//     pause_grace_period for the next request post-approval.
			if existing, ok := p.cache[sessionID]; !ok || (existing.pendingWrites == 0 && existing.pendingApproval == nil) {
				delete(p.cache, sessionID)
			}
			p.mu.Unlock()
			continue
		}

		tokens, _ := strconv.ParseInt(fields["tokens"], 10, 64)
		calls, _ := strconv.ParseInt(fields["calls"], 10, 64)
		var startedAt time.Time
		if ts, err := strconv.ParseInt(fields["started_at"], 10, 64); err == nil {
			startedAt = time.Unix(ts, 0)
		}

		p.mu.Lock()
		var lastApprovedAt time.Time
		var pendingApproval *approvalFlight
		var pendingWrites int
		if existing, ok := p.cache[sessionID]; ok {
			// Take the max of local and Redis to avoid regressing counters when
			// in-flight accumulate goroutines haven't committed to Redis yet.
			if tokens < existing.tokens {
				tokens = existing.tokens
			}
			if calls < existing.calls {
				calls = existing.calls
			}
			if startedAt.IsZero() && !existing.startedAt.IsZero() {
				startedAt = existing.startedAt
			}
			lastApprovedAt = existing.lastApprovedAt
			// Preserve mid-webhook: dropping would let a concurrent breach fire a duplicate.
			pendingApproval = existing.pendingApproval
			pendingWrites = existing.pendingWrites
		}
		p.cache[sessionID] = &counters{
			tokens:          tokens,
			calls:           calls,
			startedAt:       startedAt,
			lastApprovedAt:  lastApprovedAt,
			pendingApproval: pendingApproval,
			pendingWrites:   pendingWrites,
		}
		p.mu.Unlock()
	}
}

func (p *SessionBudget) sessionID(pctx *pipeline.Context) string {
	if pctx.Session != nil && pctx.Session.ID != "" {
		return pctx.Session.ID
	}
	// Opt-in fallback for single-workload deployments where all sessionless
	// egress should share one bucket. Off by default; callers with no
	// session then skip enforcement (existing no_session_id path).
	if p.cfg.DefaultSessionFallback {
		return session.DefaultSessionID
	}
	return ""
}

func (p *SessionBudget) redisKey(sessionID string) string {
	return "session-budget:" + sessionID
}

var (
	_ pipeline.Plugin             = (*SessionBudget)(nil)
	_ pipeline.Configurable       = (*SessionBudget)(nil)
	_ pipeline.Initializer        = (*SessionBudget)(nil)
	_ pipeline.Shutdowner         = (*SessionBudget)(nil)
	_ pipeline.StreamingResponder = (*SessionBudget)(nil)
)
