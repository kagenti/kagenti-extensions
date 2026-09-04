package sessionapi

import (
	"encoding/json"
	"log/slog"
	"net/http"

	"github.com/rossoctl/cortex/authbridge/authlib/session"
	"github.com/rossoctl/cortex/authbridge/authlib/usage"
)

// handleUsage serves GET /v1/usage — time-bucketed volume, error, latency and
// cost aggregates for charting.
//
// Query parameters:
//
//	window      10m (default), 1h, 6h — any multiple of the bucket width up to
//	            the retained maximum
//	resolution  bucket width to return, e.g. 5m for a 1h window rendered as 12
//	            bars. Defaults to the 1m storage resolution. Folding is done
//	            here, not in the client, so every consumer gets the same
//	            arithmetic — see usage.fold for why latency in particular cannot
//	            be folded naively.
//	session     session ID; omit for all sessions combined
//	group       none (default), method, status, plugin
//
// UNAUTHENTICATED, like every endpoint on this listener. Bind it on in-cluster
// addresses only, never behind ingress — the trust model is documented in
// authbridge/CLAUDE.md and applies here unchanged.
//
// This response is less sensitive than /v1/sessions, which serves raw prompts,
// completions and tool results. It carries no message content at all: only
// counts, timings and cost. But it is not free of information either, and two
// groupings leak deployment shape to anyone who can reach the port:
//
//   - group=method exposes the model names in use (claude-sonnet-5, and any
//     internal or preview model an operator is testing against).
//   - group=plugin exposes the active pipeline composition — though /v1/pipeline
//     already publishes that in full, so this adds no new exposure.
//
// Cost figures also disclose spend, which is business-sensitive in a way raw
// request counts are not. None of this changes the listener's existing posture;
// it is written down so the decision to expose it is a decision rather than an
// oversight.
//
// TODO(cost): costMicros is on the wire format but nothing populates it yet —
// no Pricer is wired at construction, so Snapshot reports priced:false and omits
// every cost field. The rates exist already, in the toolprune plugin's
// defaultPatterns table, but they are package-private there and explicitly
// documented as gateway-specific: that table measures the rossoctl LiteLLM
// gateway, which bills well below vendor list, so applying it to a
// direct-to-Anthropic deployment would understate cost by roughly 4x on the
// input tier. Publishing a number that wrong is worse than publishing none.
//
// The shape of the fix is either to promote those rates into a shared package
// both toolprune and this aggregator consume, or — better — to have a plugin
// that already knows the true cost report it per response, which
// litellm-budget-track effectively does: it reads LiteLLM's own
// X-Litellm-Response-Cost header, the authoritative post-discount figure. That
// would need the cost surfaced onto the session event, where the aggregator can
// see it; today it stays inside the plugin. Until then the field is reserved so
// adding it later is not a wire-format break, and priced:false tells a client to
// render "cost unavailable" rather than $0.00.
func (s *Server) handleUsage(w http.ResponseWriter, r *http.Request) {
	if s.usage == nil {
		// Aggregation not wired up (session store disabled, or an older binary
		// composition). 404 rather than an empty snapshot: "no such channel" and
		// "channel with nothing in it" are different answers, and a client that
		// gets zeros would render a flat chart implying idle traffic.
		http.Error(w, `{"error":"usage aggregation not enabled"}`, http.StatusNotFound)
		return
	}

	window, err := usage.ParseWindow(r.URL.Query().Get("window"))
	if err != nil {
		writeUsageError(w, err)
		return
	}
	resolution, err := usage.ParseResolution(r.URL.Query().Get("resolution"), window)
	if err != nil {
		writeUsageError(w, err)
		return
	}
	group, err := usage.ParseGroup(r.URL.Query().Get("group"))
	if err != nil {
		writeUsageError(w, err)
		return
	}
	sessionID := r.URL.Query().Get("session")
	if len(sessionID) > session.MaxSessionIDLen {
		writeUsageError(w, errSessionIDTooLong)
		return
	}

	snap := s.usage.Snapshot(window, resolution, sessionID, group)

	w.Header().Set("Content-Type", "application/json")
	if err := json.NewEncoder(w).Encode(snap); err != nil {
		slog.Debug("sessionapi: usage encode failed", "error", err)
	}
}

type usageError struct{ msg string }

func (e usageError) Error() string { return e.msg }

var errSessionIDTooLong = usageError{"session id too long"}

// writeUsageError returns 400 with the validation message. Every message it can
// carry is a fixed string authored in this package or in authlib/usage: none
// interpolates query input. That is a requirement, not an accident — this
// endpoint is unauthenticated, so reflecting caller-supplied bytes into a
// response body would hand an attacker a reflection primitive. Keep it that way
// when adding validation.
func writeUsageError(w http.ResponseWriter, err error) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(http.StatusBadRequest)
	if encErr := json.NewEncoder(w).Encode(struct {
		Error string `json:"error"`
	}{Error: err.Error()}); encErr != nil {
		slog.Debug("sessionapi: usage error encode failed", "error", encErr)
	}
}
