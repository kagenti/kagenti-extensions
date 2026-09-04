// Package usage aggregates session events into fixed-width time buckets so a
// client can chart volume, errors, latency and cost over wall-clock time.
//
// It lives server-side on purpose. An aggregate built inside a client would
// start empty when that client connected, so two operators watching the same
// pod would see different histories of the same traffic — and neither would see
// anything from before they attached. The store is the only place with the whole
// picture, so the arithmetic belongs next to it.
//
// Memory is O(buckets x distinct labels), independent of event volume: mean and
// standard deviation come from running sums (count, sum, sum-of-squares) rather
// than from retained samples, and every bucket is preallocated in a fixed ring.
// Nothing here grows with traffic.
package usage

import (
	"strconv"
	"sync"
	"time"

	"github.com/rossoctl/cortex/authbridge/authlib/pipeline"
)

const (
	// BucketWidth is the storage resolution. Every window the API offers is a
	// whole multiple of it, and clients fold buckets together for wider views
	// rather than asking the server to pre-aggregate — one storage shape, and a
	// client is free to pick its own on-screen resolution.
	BucketWidth = time.Minute

	// NumBuckets covers the longest window offered (6h). The ring is
	// preallocated at this size for both the all-sessions aggregate and each
	// tracked session.
	NumBuckets = 360

	// MaxWindow is the longest span Snapshot will return.
	MaxWindow = NumBuckets * BucketWidth
)

// defaultMaxSessions bounds how many per-session rings are tracked. Each ring
// is NumBuckets buckets, so this is the knob that bounds worst-case memory.
// Sessions beyond the cap still land in the all-sessions aggregate; only their
// individual breakdown is dropped.
const defaultMaxSessions = 64

// Counts is the per-label tuple accumulated in each bucket.
type Counts struct {
	Requests int64 `json:"requests"`
	Errors   int64 `json:"errors,omitempty"`
	Tokens   int64 `json:"tokens,omitempty"`
	// CostMicros is millionths of a US dollar. An integer unit keeps bucket
	// addition exact and JSON round-tripping lossless, which float dollars do
	// not; a client divides by 1e6 to display. Zero when no pricer is
	// configured, which is not the same as "this traffic was free" — the API
	// omits the field entirely in that case rather than asserting $0.
	CostMicros int64 `json:"costMicros,omitempty"`
}

func (c *Counts) add(o Counts) {
	c.Requests += o.Requests
	c.Errors += o.Errors
	c.Tokens += o.Tokens
	c.CostMicros += o.CostMicros
}

// Bucket is one BucketWidth slice of time, as served to clients.
//
// A bucket with no traffic is still emitted, with zeroed counts. That is
// deliberate: a client rendering a bar chart must be able to distinguish an idle
// minute from a minute that fell off the end of the ring, and inferring absent
// buckets from timestamps is exactly the kind of thing every client would get
// slightly differently.
type Bucket struct {
	At          time.Time `json:"at"`
	Counts                // totals across every label
	LatMeanMs   float64   `json:"latMeanMs,omitempty"`
	LatStdDevMs float64   `json:"latStdDevMs,omitempty"`
	// Series is the requested grouping, keyed by model / status / plugin name.
	// Nil when group=none.
	Series map[string]Counts `json:"series,omitempty"`
}

// bucket is the internal accumulator. It keeps all three groupings at once so
// the group= parameter is a read-time choice: an operator cycling groupings in a
// TUI sees the same history from each angle, instead of each grouping only
// having data from the moment it was first selected.
type bucket struct {
	start time.Time // truncated to BucketWidth; zero means never written
	Counts
	latSum   float64 // milliseconds
	latSumSq float64 // milliseconds squared, for stddev
	byMethod map[string]Counts
	byStatus map[string]Counts
	byPlugin map[string]Counts
}

// Pricer converts a model name and token count to millionths of a dollar.
// Optional: a nil Pricer leaves CostMicros zero and the API omits it.
//
// Injected rather than implemented here because rates are deployment-specific —
// a gateway bills differently from the vendor's list price — and authlib has no
// business asserting one.
//
// TODO(cost): no caller supplies one yet, so CostMicros is always zero and
// Snapshot reports priced:false. Two candidate sources, neither reachable from
// here today:
//
//   - toolprune's defaultPatterns table has per-family rates, but it is
//     package-private and measured against the rossoctl LiteLLM gateway, which
//     bills well below vendor list. Applying it to a direct-to-Anthropic
//     deployment understates cost by roughly 4x on the input tier.
//   - litellm-budget-track already reads the authoritative post-discount figure
//     from LiteLLM's X-Litellm-Response-Cost header, but keeps it inside the
//     plugin. Surfacing it onto the session event would let the aggregator use a
//     real number instead of a modelled one, which is the better fix.
//
// The field is reserved on the wire now so adding it later is not a breaking
// change.
type Pricer func(model string, tokens int64) int64

// Aggregator is a fixed ring of per-minute buckets. Safe for concurrent use.
//
// Expiry is implicit: a slot is indexed by minutes-since-epoch modulo
// NumBuckets, so a write whose timestamp does not match the slot's recorded
// start has landed on a stale bucket from a previous lap and resets it. No
// sweeper goroutine, no cleanup path. The tradeoff is that a large backwards
// clock jump can reset buckets that were still current; for observability
// counters that is acceptable, and it is preferable to a timer that has to be
// stopped on shutdown.
type Aggregator struct {
	mu       sync.RWMutex
	all      []bucket
	sessions map[string][]bucket
	maxSess  int
	pricer   Pricer
	now      func() time.Time
}

// Option configures an Aggregator.
type Option func(*Aggregator)

// WithPricer supplies cost rates. Without it, CostMicros stays zero.
func WithPricer(p Pricer) Option { return func(a *Aggregator) { a.pricer = p } }

// WithClock overrides time.Now, for deterministic tests.
func WithClock(now func() time.Time) Option { return func(a *Aggregator) { a.now = now } }

// WithMaxSessions bounds the number of per-session rings retained.
func WithMaxSessions(n int) Option {
	return func(a *Aggregator) {
		if n >= 0 {
			a.maxSess = n
		}
	}
}

// New returns an empty Aggregator.
func New(opts ...Option) *Aggregator {
	a := &Aggregator{
		all:      make([]bucket, NumBuckets),
		sessions: make(map[string][]bucket),
		maxSess:  defaultMaxSessions,
		now:      time.Now,
	}
	for _, o := range opts {
		o(a)
	}
	return a
}

// Record folds one event into the aggregate.
//
// Response events only: a request event carries no status, no duration and no
// usage, so counting it would double every request and pull the latency mean
// toward zero. Denials (phase "denied") are counted as errors — they are
// requests that happened and failed, and omitting them would make an
// authentication outage look like a traffic drop.
func (a *Aggregator) Record(sessionID string, e *pipeline.SessionEvent) {
	if e == nil {
		return
	}
	if e.Phase != pipeline.SessionResponse && e.Phase != pipeline.SessionDenied {
		return
	}

	at := e.At
	if at.IsZero() {
		at = a.now()
	}
	t := at.Truncate(BucketWidth)

	a.mu.Lock()
	defer a.mu.Unlock()

	a.foldInto(a.all, t, e)

	if ring, ok := a.sessions[sessionID]; ok {
		a.foldInto(ring, t, e)
		return
	}
	// Cap reached: the event still counts toward the all-sessions total above,
	// it just gets no per-session breakdown.
	if a.maxSess > 0 && len(a.sessions) >= a.maxSess {
		return
	}
	ring := make([]bucket, NumBuckets)
	a.sessions[sessionID] = ring
	a.foldInto(ring, t, e)
}

func (a *Aggregator) foldInto(ring []bucket, t time.Time, e *pipeline.SessionEvent) {
	b := &ring[slot(t)]
	if !b.start.Equal(t) {
		*b = bucket{start: t} // stale lap: reset rather than accumulate onto old data
	}

	var tokens, cost int64
	var model string
	if e.Inference != nil {
		tokens = int64(e.Inference.TotalTokens)
		model = e.Inference.Model
		if a.pricer != nil && tokens > 0 {
			cost = a.pricer(model, tokens)
		}
	}

	one := Counts{Requests: 1, Tokens: tokens, CostMicros: cost}
	if e.StatusCode >= 400 || e.Phase == pipeline.SessionDenied {
		one.Errors = 1
	}
	b.Counts.add(one)

	// Latency: only from events that actually carry one. A zero duration is
	// "not measured", not "instant", and folding it in would drag the mean down.
	if ms := float64(e.Duration.Milliseconds()); ms > 0 {
		b.latSum += ms
		b.latSumSq += ms * ms
	}

	if model != "" {
		addLabel(&b.byMethod, model, one)
	}
	if e.StatusCode > 0 {
		addLabel(&b.byStatus, strconv.Itoa(e.StatusCode), one)
	} else if e.Phase == pipeline.SessionDenied {
		addLabel(&b.byStatus, "denied", one)
	}
	// Per-plugin attribution counts the request once per plugin that ran, so
	// these sub-totals intentionally sum to more than Requests when several
	// plugins touched one message. Tokens are attributed whole to each plugin
	// for the same reason: there is no defensible way to split one response's
	// usage between the plugins that observed it.
	// Invocations is a POINTER and is nil whenever no plugin appended a record —
	// which is the common case for a plain proxied response. Dereferencing it
	// unguarded panics inside Store.Append, i.e. on the request hot path.
	if e.Invocations != nil {
		for _, inv := range e.Invocations.Outbound {
			if inv.Plugin != "" {
				addLabel(&b.byPlugin, inv.Plugin, one)
			}
		}
		for _, inv := range e.Invocations.Inbound {
			if inv.Plugin != "" {
				addLabel(&b.byPlugin, inv.Plugin, one)
			}
		}
	}
}

func addLabel(m *map[string]Counts, key string, c Counts) {
	if *m == nil {
		*m = make(map[string]Counts, 4)
	}
	cur := (*m)[key]
	cur.add(c)
	(*m)[key] = cur
}

func slot(t time.Time) int {
	s := int(t.Unix()/int64(BucketWidth/time.Second)) % NumBuckets
	if s < 0 {
		s += NumBuckets
	}
	return s
}
