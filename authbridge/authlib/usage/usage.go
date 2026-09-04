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
	// LatSamples is how many requests in this bucket carried a duration, which
	// is not always Requests: an unmeasured response counts as traffic but not
	// as a latency sample. Folding needs it to weight each bucket by its real
	// sample count, and a client showing a window-wide mean needs it for the
	// same reason.
	LatSamples int64 `json:"latSamples,omitempty"`
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
	// latN counts only the requests that actually carried a duration. Dividing
	// latSum by Requests instead reports a mean diluted by every unmeasured
	// response: one 2s response plus one unmeasured one reported 1s, not 2s.
	latN     int64
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
	sessions map[string]*sessionRing
	maxSess  int
	pricer   Pricer
	now      func() time.Time
}

// sessionRing is one session's buckets plus the last time it was written.
//
// lastSeen exists because the store evicts and expires sessions without telling
// the aggregator. Without reclamation, a pod that churns through session ids
// fills the map to the cap and then refuses every new session forever:
// /v1/sessions would list a live session while /v1/usage?session=<id> returned
// zeroed buckets, which looks exactly like "this session did nothing". Matching
// the store's own cap only delays that. Reusing the coldest ring instead bounds
// memory AND keeps the newest sessions answerable, which is what an operator is
// looking at.
type sessionRing struct {
	buckets  []bucket
	lastSeen time.Time
}

// Option configures an Aggregator.
type Option func(*Aggregator)

// WithPricer supplies cost rates. Without it, CostMicros stays zero.
func WithPricer(p Pricer) Option { return func(a *Aggregator) { a.pricer = p } }

// WithClock overrides time.Now, for deterministic tests.
func WithClock(now func() time.Time) Option { return func(a *Aggregator) { a.now = now } }

// WithMaxSessions bounds the number of per-session rings retained. Each ring is
// NumBuckets buckets, so this is the knob that bounds worst-case memory.
//
// n == 0 means track NO per-session rings: /v1/usage?session=... returns zeroed
// buckets, while the all-sessions aggregate keeps counting everything. That is
// the reading the name implies, and the safe one if this is ever wired to
// operator config — a 0 in a ConfigMap should not silently mean "unlimited".
// Pass a negative n to leave the default in place.
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
		sessions: make(map[string]*sessionRing),
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
		ring.lastSeen = at
		a.foldInto(ring.buckets, t, e)
		return
	}
	// maxSess == 0 means no per-session rings at all — see WithMaxSessions. The
	// event still counts toward the all-sessions total above.
	if a.maxSess == 0 {
		return
	}
	// At the cap, reclaim the least-recently-written ring rather than refuse the
	// new session. The store expires and evicts sessions without notifying us, so
	// the coldest ring is very likely one the store has already dropped; refusing
	// instead would make every session after the first maxSess unanswerable for
	// the life of the process.
	if len(a.sessions) >= a.maxSess {
		a.evictColdestLocked()
	}
	ring := &sessionRing{buckets: make([]bucket, NumBuckets), lastSeen: at}
	a.sessions[sessionID] = ring
	a.foldInto(ring.buckets, t, e)
}

// evictColdestLocked drops the ring with the oldest lastSeen. Caller holds mu.
//
// Linear scan rather than a heap: maxSess is a few dozen, this runs only when the
// map is full and a genuinely new session arrives, and a heap would need
// maintaining on every write instead.
func (a *Aggregator) evictColdestLocked() {
	var coldestID string
	var coldest time.Time
	for id, r := range a.sessions {
		if coldestID == "" || r.lastSeen.Before(coldest) {
			coldestID, coldest = id, r.lastSeen
		}
	}
	if coldestID != "" {
		delete(a.sessions, coldestID)
	}
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
		b.latN++
	}

	if model != "" {
		addLabel(&b.byMethod, truncateLabel(model), one)
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

// maxLabelsPerBucket caps distinct keys in one bucket's grouping map.
//
// The model name comes off the request body, so its cardinality is set by
// whatever the client sends, not by anything this process controls. Unbounded, a
// caller varying the model string every request would add a retained map entry
// and a retained string per request, in a ring that only frees a slot when it is
// reused a full lap later — 6h at one minute. That is memory growth driven from
// off-host, on the synchronous session-append path.
//
// 64 is well above any real deployment: a pod talks to a handful of models, a
// few status codes and its own plugin list. Past the cap, further keys fold into
// overflowLabel so the totals stay correct and the excess is visible rather than
// silently dropped.
const maxLabelsPerBucket = 64

// overflowLabel collects everything past maxLabelsPerBucket. Named rather than
// dropped so a chart's segments still sum to the bucket total, and so an
// operator can see that cardinality was capped instead of wondering why a model
// is missing.
const overflowLabel = "(other)"

// maxLabelLen bounds one retained label. The model name is request-controlled, so
// without this a caller could park a megabyte of string in a bucket that lives
// for a full ring lap. Long enough for any real model id, including provider
// prefixes and dated suffixes.
const maxLabelLen = 96

func truncateLabel(s string) string {
	if len(s) <= maxLabelLen {
		return s
	}
	return s[:maxLabelLen]
}

func addLabel(m *map[string]Counts, key string, c Counts) {
	if *m == nil {
		*m = make(map[string]Counts, 4)
	}
	// Reserve the last slot for overflowLabel: switching to it only once the map
	// is already full would make the overflow key itself the (cap+1)th entry.
	if _, ok := (*m)[key]; !ok && len(*m) >= maxLabelsPerBucket-1 {
		key = overflowLabel
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
