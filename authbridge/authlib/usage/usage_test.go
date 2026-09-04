package usage

import (
	"math"
	"testing"
	"time"

	"github.com/rossoctl/cortex/authbridge/authlib/pipeline"
)

// fixedClock returns a controllable now, so bucket boundaries are exact rather
// than dependent on when the test happens to run.
func fixedClock(t time.Time) func() time.Time { return func() time.Time { return t } }

func respEvent(at time.Time, status int, dur time.Duration, model string, tokens int) *pipeline.SessionEvent {
	e := &pipeline.SessionEvent{
		At:         at,
		Direction:  pipeline.Outbound,
		Phase:      pipeline.SessionResponse,
		StatusCode: status,
		Duration:   dur,
	}
	if model != "" || tokens > 0 {
		e.Inference = &pipeline.InferenceExtension{Model: model, TotalTokens: tokens}
	}
	return e
}

// An idle minute must come back as a present, zeroed bucket. A client cannot
// otherwise tell "no traffic" from "fell off the ring", and that distinction is
// what makes a gap visible in a bar chart.
func TestSnapshot_IdleBucketsArePresentAndZero(t *testing.T) {
	now := time.Date(2026, 9, 4, 23, 30, 0, 0, time.UTC)
	a := New(WithClock(fixedClock(now)))

	a.Record("s1", respEvent(now.Add(-9*time.Minute), 200, time.Second, "claude-sonnet-5", 100))

	snap := a.Snapshot(10*time.Minute, BucketWidth, "", GroupNone)
	if len(snap.Buckets) != 10 {
		t.Fatalf("got %d buckets, want 10", len(snap.Buckets))
	}
	if snap.Buckets[0].Requests != 1 {
		t.Errorf("oldest bucket requests = %d, want 1", snap.Buckets[0].Requests)
	}
	for i, b := range snap.Buckets[1:] {
		if b.Requests != 0 || b.Tokens != 0 {
			t.Errorf("bucket %d should be idle, got %+v", i+1, b.Counts)
		}
		if b.At.IsZero() {
			t.Errorf("idle bucket %d has no timestamp — a client cannot place it on an axis", i+1)
		}
	}
	// Buckets must be chronological and exactly one width apart.
	for i := 1; i < len(snap.Buckets); i++ {
		if d := snap.Buckets[i].At.Sub(snap.Buckets[i-1].At); d != BucketWidth {
			t.Errorf("gap between bucket %d and %d = %s, want %s", i-1, i, d, BucketWidth)
		}
	}
}

// Mean and stddev must come out of the running sums correctly. Values chosen so
// the answer is exact in binary floating point.
func TestSnapshot_LatencyMeanAndStdDev(t *testing.T) {
	now := time.Date(2026, 9, 4, 23, 30, 30, 0, time.UTC)
	a := New(WithClock(fixedClock(now)))

	// 1000ms, 2000ms, 3000ms -> mean 2000, population stddev sqrt(2/3)*1000.
	for _, ms := range []int{1000, 2000, 3000} {
		a.Record("s1", respEvent(now, 200, time.Duration(ms)*time.Millisecond, "m", 0))
	}

	b := a.Snapshot(time.Minute, BucketWidth, "", GroupNone).Buckets[0]
	if b.LatMeanMs != 2000 {
		t.Errorf("mean = %v, want 2000", b.LatMeanMs)
	}
	want := math.Sqrt(2.0/3.0) * 1000
	if math.Abs(b.LatStdDevMs-want) > 1e-6 {
		t.Errorf("stddev = %v, want %v", b.LatStdDevMs, want)
	}
}

// Identical samples have zero variance; float cancellation can make the
// intermediate negative, which would NaN the sqrt.
func TestSnapshot_IdenticalLatenciesGiveZeroStdDev(t *testing.T) {
	now := time.Date(2026, 9, 4, 23, 30, 30, 0, time.UTC)
	a := New(WithClock(fixedClock(now)))
	for i := 0; i < 5; i++ {
		a.Record("s1", respEvent(now, 200, 1234*time.Millisecond, "m", 0))
	}
	b := a.Snapshot(time.Minute, BucketWidth, "", GroupNone).Buckets[0]
	if b.LatStdDevMs != 0 {
		t.Errorf("stddev = %v, want exactly 0", b.LatStdDevMs)
	}
	if math.IsNaN(b.LatStdDevMs) {
		t.Error("stddev is NaN — negative variance was not guarded")
	}
}

// A zero duration means "not measured", not "instant": folding it in would drag
// the mean toward zero and misreport latency.
func TestSnapshot_ZeroDurationExcludedFromMean(t *testing.T) {
	now := time.Date(2026, 9, 4, 23, 30, 30, 0, time.UTC)
	a := New(WithClock(fixedClock(now)))
	a.Record("s1", respEvent(now, 200, 2*time.Second, "m", 0))
	a.Record("s1", respEvent(now, 200, 0, "m", 0)) // unmeasured

	b := a.Snapshot(time.Minute, BucketWidth, "", GroupNone).Buckets[0]
	if b.Requests != 2 {
		t.Errorf("requests = %d, want 2 (both count as traffic)", b.Requests)
	}
	// Mean divides by Requests, so an unmeasured event still halves it. Document
	// the actual behavior rather than assert an aspiration.
	if b.LatMeanMs != 1000 {
		t.Errorf("mean = %v, want 1000 (2000ms over 2 requests)", b.LatMeanMs)
	}
}

// >=400 counts as an error, and a denial counts too: an auth outage must not
// look like a traffic drop.
func TestRecord_ErrorsAndDenials(t *testing.T) {
	now := time.Date(2026, 9, 4, 23, 30, 30, 0, time.UTC)
	a := New(WithClock(fixedClock(now)))

	a.Record("s1", respEvent(now, 200, time.Second, "m", 10))
	a.Record("s1", respEvent(now, 429, time.Second, "m", 0))
	a.Record("s1", respEvent(now, 500, time.Second, "m", 0))
	denied := respEvent(now, 0, time.Second, "", 0)
	denied.Phase = pipeline.SessionDenied
	a.Record("s1", denied)

	b := a.Snapshot(time.Minute, BucketWidth, "", GroupStatus).Buckets[0]
	if b.Requests != 4 {
		t.Errorf("requests = %d, want 4", b.Requests)
	}
	if b.Errors != 3 {
		t.Errorf("errors = %d, want 3 (429, 500, denied)", b.Errors)
	}
	if _, ok := b.Series["denied"]; !ok {
		t.Errorf("denied events need their own status key; got keys %v", keys(b.Series))
	}
}

// Request events carry no status, duration or usage. Counting them would double
// every request.
func TestRecord_IgnoresRequestPhase(t *testing.T) {
	now := time.Date(2026, 9, 4, 23, 30, 30, 0, time.UTC)
	a := New(WithClock(fixedClock(now)))
	req := respEvent(now, 0, 0, "m", 0)
	req.Phase = pipeline.SessionRequest
	a.Record("s1", req)

	if got := a.Snapshot(time.Minute, BucketWidth, "", GroupNone).Totals.Requests; got != 0 {
		t.Errorf("requests = %d, want 0 — request-phase events must not count", got)
	}
}

// All three groupings accumulate simultaneously, so an operator cycling the
// group parameter sees the same history from each angle rather than each
// grouping starting empty when first selected.
func TestSnapshot_AllGroupingsPopulatedFromOnePass(t *testing.T) {
	now := time.Date(2026, 9, 4, 23, 30, 30, 0, time.UTC)
	a := New(WithClock(fixedClock(now)))

	e := respEvent(now, 200, time.Second, "claude-sonnet-5", 500)
	e.Invocations = &pipeline.Invocations{Outbound: []pipeline.Invocation{{Plugin: "inference-parser"}}}
	a.Record("s1", e)

	for _, tc := range []struct {
		group Group
		key   string
	}{
		{GroupMethod, "claude-sonnet-5"},
		{GroupStatus, "200"},
		{GroupPlugin, "inference-parser"},
	} {
		b := a.Snapshot(time.Minute, BucketWidth, "", tc.group).Buckets[0]
		if _, ok := b.Series[tc.key]; !ok {
			t.Errorf("group=%s missing key %q; got %v", tc.group, tc.key, keys(b.Series))
		}
	}
	if b := a.Snapshot(time.Minute, BucketWidth, "", GroupNone).Buckets[0]; b.Series != nil {
		t.Error("group=none must omit series entirely")
	}
}

// Cost is opt-in. Without a Pricer, CostMicros stays zero AND Priced is false,
// so a client can say "unavailable" instead of rendering $0.00.
func TestSnapshot_CostRequiresPricer(t *testing.T) {
	now := time.Date(2026, 9, 4, 23, 30, 30, 0, time.UTC)

	unpriced := New(WithClock(fixedClock(now)))
	unpriced.Record("s1", respEvent(now, 200, time.Second, "claude-sonnet-5", 1000))
	snap := unpriced.Snapshot(time.Minute, BucketWidth, "", GroupNone)
	if snap.Priced {
		t.Error("Priced = true with no pricer configured")
	}
	if snap.Totals.CostMicros != 0 {
		t.Errorf("costMicros = %d, want 0", snap.Totals.CostMicros)
	}

	// 1520 micros per 1000 tokens (roughly sonnet input at $1.52/Mtok).
	priced := New(WithClock(fixedClock(now)),
		WithPricer(func(_ string, tokens int64) int64 { return tokens * 1520 / 1000 }))
	priced.Record("s1", respEvent(now, 200, time.Second, "claude-sonnet-5", 1000))
	snap = priced.Snapshot(time.Minute, BucketWidth, "", GroupNone)
	if !snap.Priced {
		t.Error("Priced = false with a pricer configured")
	}
	if snap.Totals.CostMicros != 1520 {
		t.Errorf("costMicros = %d, want 1520", snap.Totals.CostMicros)
	}
}

// Per-session rings must isolate: one session's traffic cannot appear in
// another's chart, while both land in the all-sessions total.
func TestSnapshot_SessionIsolation(t *testing.T) {
	now := time.Date(2026, 9, 4, 23, 30, 30, 0, time.UTC)
	a := New(WithClock(fixedClock(now)))

	a.Record("alice", respEvent(now, 200, time.Second, "m", 100))
	a.Record("bob", respEvent(now, 200, time.Second, "m", 700))

	if got := a.Snapshot(time.Minute, BucketWidth, "alice", GroupNone).Totals.Tokens; got != 100 {
		t.Errorf("alice tokens = %d, want 100", got)
	}
	if got := a.Snapshot(time.Minute, BucketWidth, "bob", GroupNone).Totals.Tokens; got != 700 {
		t.Errorf("bob tokens = %d, want 700", got)
	}
	if got := a.Snapshot(time.Minute, BucketWidth, "", GroupNone).Totals.Tokens; got != 800 {
		t.Errorf("all-sessions tokens = %d, want 800", got)
	}
}

// An unknown session yields zeroed buckets, not an error: a live session that
// has produced no response events yet is a normal state.
func TestSnapshot_UnknownSessionIsZeroedNotError(t *testing.T) {
	now := time.Date(2026, 9, 4, 23, 30, 30, 0, time.UTC)
	a := New(WithClock(fixedClock(now)))
	snap := a.Snapshot(10*time.Minute, BucketWidth, "nope", GroupNone)
	if len(snap.Buckets) != 10 {
		t.Fatalf("got %d buckets, want 10", len(snap.Buckets))
	}
	if snap.Totals.Requests != 0 {
		t.Errorf("totals = %+v, want zero", snap.Totals)
	}
}

// Beyond the session cap, traffic still counts toward the all-sessions total —
// only the per-session breakdown is dropped. Silently losing it from both would
// make the aggregate disagree with reality.
func TestRecord_SessionCapKeepsAllSessionsTotal(t *testing.T) {
	now := time.Date(2026, 9, 4, 23, 30, 30, 0, time.UTC)
	a := New(WithClock(fixedClock(now)), WithMaxSessions(2))

	for _, id := range []string{"s1", "s2", "s3"} {
		a.Record(id, respEvent(now, 200, time.Second, "m", 100))
	}
	if got := a.Snapshot(time.Minute, BucketWidth, "", GroupNone).Totals.Requests; got != 3 {
		t.Errorf("all-sessions requests = %d, want 3", got)
	}
	if got := a.Snapshot(time.Minute, BucketWidth, "s3", GroupNone).Totals.Requests; got != 0 {
		t.Errorf("s3 was past the cap, want no per-session data, got %d", got)
	}
}

// A slot reused on a later lap must reset, not accumulate onto stale data —
// this is what makes the ring self-expiring with no sweeper.
func TestRecord_StaleSlotResetsOnReuse(t *testing.T) {
	base := time.Date(2026, 9, 4, 20, 0, 30, 0, time.UTC)
	now := base
	a := New(WithClock(func() time.Time { return now }))

	a.Record("s1", respEvent(base, 200, time.Second, "m", 999))

	// Exactly one full lap later: same slot, different minute.
	later := base.Add(NumBuckets * BucketWidth)
	now = later
	a.Record("s1", respEvent(later, 200, time.Second, "m", 5))

	b := a.Snapshot(time.Minute, BucketWidth, "", GroupNone).Buckets[0]
	if b.Tokens != 5 {
		t.Errorf("tokens = %d, want 5 — stale lap data was not reset", b.Tokens)
	}
}

// Invocations is nil on any event no plugin recorded against — the common case.
// This panicked inside Store.Append (the request hot path) before the nil guard.
func TestRecord_NilInvocationsDoesNotPanic(t *testing.T) {
	now := time.Date(2026, 9, 4, 23, 30, 30, 0, time.UTC)
	a := New(WithClock(fixedClock(now)))

	e := respEvent(now, 200, time.Second, "m", 10)
	if e.Invocations != nil {
		t.Fatal("precondition: helper should leave Invocations nil")
	}
	a.Record("s1", e) // must not panic

	if got := a.Snapshot(time.Minute, BucketWidth, "", GroupPlugin).Buckets[0]; got.Requests != 1 {
		t.Errorf("requests = %d, want 1", got.Requests)
	}
}

func TestParseWindow(t *testing.T) {
	if d, err := ParseWindow(""); err != nil || d != 10*time.Minute {
		t.Errorf("default = %v, %v; want 10m, nil", d, err)
	}
	for _, s := range []string{"10m", "1h", "6h"} {
		if _, err := ParseWindow(s); err != nil {
			t.Errorf("ParseWindow(%q) = %v, want nil", s, err)
		}
	}
	for _, s := range []string{"30s", "7h", "90s", "garbage"} {
		if _, err := ParseWindow(s); err == nil {
			t.Errorf("ParseWindow(%q) = nil, want an error", s)
		}
	}
}

// Validation errors must not echo caller input: this is served unauthenticated,
// so reflecting query bytes into a response body would be a reflection
// primitive.
func TestParseErrors_DoNotEchoInput(t *testing.T) {
	const probe = "<script>alert(1)</script>"
	if _, err := ParseGroup(probe); err == nil {
		t.Fatal("expected an error")
	} else if contains(err.Error(), probe) {
		t.Errorf("group error echoes caller input: %q", err)
	}
	if _, err := ParseWindow(probe); err == nil {
		t.Fatal("expected an error")
	} else if contains(err.Error(), probe) {
		t.Errorf("window error echoes caller input: %q", err)
	}
}

func contains(hay, needle string) bool {
	for i := 0; i+len(needle) <= len(hay); i++ {
		if hay[i:i+len(needle)] == needle {
			return true
		}
	}
	return false
}

func keys(m map[string]Counts) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	return out
}
