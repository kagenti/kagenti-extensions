package toolprune

import (
	"sort"

	"github.com/gobwas/glob"
)

// patternRates is one pricing entry whose key may be a glob.
type patternRates struct {
	pattern string
	glob    glob.Glob
	rates   modelRates
}

// defaultPatterns holds per-token rates for the Claude families seen on the
// rossoctl LiteLLM gateway, measured from its own x-litellm-response-cost
// headers: send two non-streaming requests of differing prompt length and
// difference them, rate = Δcost / Δinput_tokens; the cache tiers were obtained
// the same way with a cache_control block sent twice.
//
// Keyed by FAMILY, not by version. Model names churn — opus 4.6, 4.7, 4.8, 5 —
// and a table of exact versions means a code change and a rebuild every time a
// provider ships one, which is not a thing an operator can be asked to do. One
// pattern per family survives version bumps, and matches provider prefixes
// (aws/, azure/) and dated suffixes (-20251001) alike.
//
// The tradeoff is stated plainly: this assumes a family bills at one rate. That
// has held across the Claude versions measured, but if a version ever differs,
// pin it with an exact `pricing:` key in config — exact always beats a pattern.
//
// These exist so `$ saved` works with no configuration. They are a starting
// point, not a fact about your account:
//
//   - Rates are gateway-specific. This gateway bills well below vendor list, so
//     a deployment talking straight to Anthropic pays more and these
//     UNDERSTATE its saving — by roughly 4x on the input tier at the time of
//     measurement. That is the common case for a laptop install, so the caveat
//     travels with every figure rather than living only here.
//   - Rates change. Nothing here refreshes them.
//
// Any `pricing` entry in config overrides the matching model.
var defaultPatterns = mustCompilePatterns(map[string]modelRates{
	// Written in the published unit — dollars per million tokens — and divided by
	// a CONSTANT, so the compiler folds each one exactly. A runtime division
	// lands a ulp low (3.7999999999999996e-06), which would make this table
	// disagree with the documented $3.80/Mtok in the last digit for no reason.
	"*claude-opus-*": {
		InputCostPerToken:      3.80 / tokensPerMillion,
		CacheWriteCostPerToken: 4.75 / tokensPerMillion, // 1.25x input
		CacheReadCostPerToken:  0.38 / tokensPerMillion, // 0.10x input
	},
	"*claude-sonnet-*": {
		InputCostPerToken:      1.52 / tokensPerMillion,
		CacheWriteCostPerToken: 1.90 / tokensPerMillion,
		CacheReadCostPerToken:  0.152 / tokensPerMillion,
	},
	"*claude-haiku-*": {
		InputCostPerToken:      0.76 / tokensPerMillion,
		CacheWriteCostPerToken: 0.95 / tokensPerMillion,
		CacheReadCostPerToken:  0.076 / tokensPerMillion,
	},
})

// compilePatterns compiles glob keys into match order. No separator is passed to
// glob.Compile: model names are delimited by "-" and "/", so "*" must span both
// — unlike the host globs elsewhere in authlib, which are "."-delimited.
//
// Sorted longest-pattern-first so the most specific glob wins deterministically
// when two match: "*claude-opus-4-8*" beats "*claude-opus-*".
func compilePatterns(in map[string]modelRates) ([]patternRates, error) {
	out := make([]patternRates, 0, len(in))
	for pat, r := range in {
		g, err := glob.Compile(pat)
		if err != nil {
			return nil, err
		}
		out = append(out, patternRates{pattern: pat, glob: g, rates: r})
	}
	sort.Slice(out, func(i, j int) bool {
		if len(out[i].pattern) != len(out[j].pattern) {
			return len(out[i].pattern) > len(out[j].pattern)
		}
		return out[i].pattern < out[j].pattern
	})
	return out, nil
}

func mustCompilePatterns(in map[string]modelRates) []patternRates {
	out, err := compilePatterns(in)
	if err != nil {
		panic("toolprune: invalid built-in pricing pattern: " + err.Error())
	}
	return out
}

// lookupPattern returns the rates for the first pattern matching model.
func lookupPattern(pats []patternRates, model string) (modelRates, bool) {
	for _, p := range pats {
		if p.glob.Match(model) && p.rates.set() {
			return p.rates, true
		}
	}
	return modelRates{}, false
}

func (s rateSource) String() string {
	switch s {
	case rateConfigured:
		return "configured"
	case rateDefault:
		return "default"
	default:
		return "none"
	}
}
