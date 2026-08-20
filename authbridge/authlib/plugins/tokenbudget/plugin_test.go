package tokenbudget

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"sync"
	"testing"
	"time"

	"github.com/rossoctl/cortex/authbridge/authlib/pipeline"
	"github.com/rossoctl/cortex/authbridge/authlib/storage"
)

// memStore is a minimal in-memory storage.Store for testing.
type memStore struct {
	mu     sync.Mutex
	hashes map[string]map[string]string
	kvs    map[string]string
	ttls   map[string]time.Duration
}

func newMemStore() *memStore {
	return &memStore{
		hashes: make(map[string]map[string]string),
		kvs:    make(map[string]string),
		ttls:   make(map[string]time.Duration),
	}
}

func (m *memStore) Get(_ context.Context, key string) (string, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	return m.kvs[key], nil
}

func (m *memStore) Set(_ context.Context, key, value string, ttl time.Duration) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.kvs[key] = value
	if ttl > 0 {
		m.ttls[key] = ttl
	}
	return nil
}

func (m *memStore) Incr(_ context.Context, key string, delta int64) (int64, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	var cur int64
	if v, ok := m.kvs[key]; ok {
		fmt.Sscanf(v, "%d", &cur)
	}
	cur += delta
	m.kvs[key] = fmt.Sprintf("%d", cur)
	return cur, nil
}

func (m *memStore) HashIncr(_ context.Context, key, field string, delta int64) (int64, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	if m.hashes[key] == nil {
		m.hashes[key] = make(map[string]string)
	}
	var cur int64
	if v, ok := m.hashes[key][field]; ok {
		fmt.Sscanf(v, "%d", &cur)
	}
	cur += delta
	m.hashes[key][field] = fmt.Sprintf("%d", cur)
	return cur, nil
}

func (m *memStore) HashGet(_ context.Context, key string) (map[string]string, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	h := m.hashes[key]
	if h == nil {
		return map[string]string{}, nil
	}
	out := make(map[string]string, len(h))
	for k, v := range h {
		out[k] = v
	}
	return out, nil
}

func (m *memStore) HashSetNX(_ context.Context, key, field, value string) (bool, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	if m.hashes[key] == nil {
		m.hashes[key] = make(map[string]string)
	}
	if _, exists := m.hashes[key][field]; exists {
		return false, nil
	}
	m.hashes[key][field] = value
	return true, nil
}

func (m *memStore) Expire(_ context.Context, key string, ttl time.Duration) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.ttls[key] = ttl
	return nil
}

func (m *memStore) Close() error { return nil }

var _ storage.Store = (*memStore)(nil)

// failingStore always returns errors (simulates total store unavailability).
type failingStore struct{}

func (failingStore) Get(context.Context, string) (string, error)              { return "", context.DeadlineExceeded }
func (failingStore) Set(context.Context, string, string, time.Duration) error { return context.DeadlineExceeded }
func (failingStore) Incr(context.Context, string, int64) (int64, error)       { return 0, context.DeadlineExceeded }
func (failingStore) HashIncr(context.Context, string, string, int64) (int64, error) { return 0, context.DeadlineExceeded }
func (failingStore) HashGet(context.Context, string) (map[string]string, error) { return nil, context.DeadlineExceeded }
func (failingStore) HashSetNX(context.Context, string, string, string) (bool, error) { return false, context.DeadlineExceeded }
func (failingStore) Expire(context.Context, string, time.Duration) error { return context.DeadlineExceeded }
func (failingStore) Close() error { return nil }

func init() {
	storage.Register("mem", func(_ string) (storage.Store, error) {
		return newMemStore(), nil
	})
}

func newTestPlugin(maxTokens, maxCalls, maxDuration int64) *TokenBudget {
	p := New()
	cfg := fmt.Sprintf(`{
		"redis_url": "mem://test",
		"max_tokens": %d,
		"max_calls": %d,
		"max_duration_seconds": %d,
		"refresh_interval": "100ms"
	}`, maxTokens, maxCalls, maxDuration)
	if err := p.Configure(json.RawMessage(cfg)); err != nil {
		panic(err)
	}
	store := newMemStore()
	p.store = store
	return p
}

func makePctx(sessionID string, totalTokens int) *pipeline.Context {
	pctx := &pipeline.Context{
		Direction: pipeline.Outbound,
		Headers:   http.Header{},
		Session:   &pipeline.SessionView{ID: sessionID},
		Extensions: pipeline.Extensions{
			Inference: &pipeline.InferenceExtension{
				TotalTokens: totalTokens,
			},
		},
	}
	return pctx
}

func TestOnRequest_UnderLimit(t *testing.T) {
	p := newTestPlugin(1000, 0, 0)
	pctx := makePctx("sess-1", 0)

	action := p.OnRequest(context.Background(), pctx)
	if action.Type != pipeline.Continue {
		t.Fatalf("expected Continue, got %v", action.Type)
	}
}


func TestOnResponseFrame_Accumulates(t *testing.T) {
	p := newTestPlugin(1000, 0, 0)
	pctx := makePctx("sess-1", 42)

	action := p.OnResponseFrame(context.Background(), pctx, nil, true)
	if action.Type != pipeline.Continue {
		t.Fatalf("expected Continue, got %v", action.Type)
	}

	// Check in-memory cache was updated.
	p.mu.RLock()
	c := p.cache["sess-1"]
	p.mu.RUnlock()

	if c == nil {
		t.Fatal("expected cache entry")
	}
	if c.tokens != 42 {
		t.Errorf("tokens = %d, want 42", c.tokens)
	}
	if c.calls != 1 {
		t.Errorf("calls = %d, want 1", c.calls)
	}
}

func TestOnResponseFrame_SkipsNonLast(t *testing.T) {
	p := newTestPlugin(1000, 0, 0)
	pctx := makePctx("sess-1", 42)

	action := p.OnResponseFrame(context.Background(), pctx, []byte("data"), false)
	if action.Type != pipeline.Continue {
		t.Fatalf("expected Continue, got %v", action.Type)
	}

	p.mu.RLock()
	_, ok := p.cache["sess-1"]
	p.mu.RUnlock()
	if ok {
		t.Error("expected no cache entry on non-last frame")
	}
}

func TestOnResponseFrame_NoInference(t *testing.T) {
	p := newTestPlugin(1000, 0, 0)
	pctx := &pipeline.Context{
		Direction: pipeline.Outbound,
		Headers:   http.Header{},
		Session:   &pipeline.SessionView{ID: "sess-1"},
	}

	action := p.OnResponseFrame(context.Background(), pctx, nil, true)
	if action.Type != pipeline.Continue {
		t.Fatalf("expected Continue, got %v", action.Type)
	}

	p.mu.RLock()
	_, ok := p.cache["sess-1"]
	p.mu.RUnlock()
	if ok {
		t.Error("expected no cache entry when no inference data")
	}
}

func TestOnRequest_NoSession(t *testing.T) {
	p := newTestPlugin(100, 0, 0)
	pctx := &pipeline.Context{
		Direction: pipeline.Outbound,
		Headers:   http.Header{},
	}

	action := p.OnRequest(context.Background(), pctx)
	if action.Type != pipeline.Continue {
		t.Fatalf("expected Continue for nil session, got %v", action.Type)
	}
}

func TestAccumulate_WritesToStore(t *testing.T) {
	p := newTestPlugin(1000, 0, 0)
	store := newMemStore()
	p.store = store

	p.accumulate("sess-1", 100)

	fields, _ := store.HashGet(context.Background(), "token-budget:sess-1")
	if fields["tokens"] != "100" {
		t.Errorf("tokens in store = %q, want 100", fields["tokens"])
	}
	if fields["calls"] != "1" {
		t.Errorf("calls in store = %q, want 1", fields["calls"])
	}
	if fields["started_at"] == "" {
		t.Error("started_at not set in store")
	}
}

func TestAccumulate_ZeroTokens(t *testing.T) {
	p := newTestPlugin(1000, 10, 0)
	store := newMemStore()
	p.store = store

	p.accumulate("sess-1", 0)

	fields, _ := store.HashGet(context.Background(), "token-budget:sess-1")
	if fields["tokens"] != "" {
		t.Errorf("tokens in store = %q, want empty (no HINCRBY for 0)", fields["tokens"])
	}
	if fields["calls"] != "1" {
		t.Errorf("calls in store = %q, want 1", fields["calls"])
	}
	if fields["started_at"] == "" {
		t.Error("started_at not set in store")
	}
}

func TestOnResponseFrame_ZeroTokensCountsCalls(t *testing.T) {
	p := newTestPlugin(1000, 5, 0)
	pctx := makePctx("sess-1", 0)

	action := p.OnResponseFrame(context.Background(), pctx, nil, true)
	if action.Type != pipeline.Continue {
		t.Fatalf("expected Continue, got %v", action.Type)
	}

	p.mu.RLock()
	c := p.cache["sess-1"]
	p.mu.RUnlock()
	if c == nil {
		t.Fatal("expected cache entry for zero-token response")
	}
	if c.calls != 1 {
		t.Errorf("calls = %d, want 1", c.calls)
	}
	if c.tokens != 0 {
		t.Errorf("tokens = %d, want 0", c.tokens)
	}
}

func TestConfigure_Validation(t *testing.T) {
	tests := []struct {
		name    string
		cfg     string
		wantErr bool
	}{
		{"valid", `{"redis_url":"redis://localhost","max_tokens":100}`, false},
		{"missing redis_url", `{"max_tokens":100}`, true},
		{"no limits", `{"redis_url":"redis://localhost"}`, true},
		{"invalid json", `{broken}`, true},
		{"zero refresh_interval", `{"redis_url":"redis://localhost","max_tokens":100,"refresh_interval":"0s"}`, true},
		{"negative refresh_interval", `{"redis_url":"redis://localhost","max_tokens":100,"refresh_interval":"-1s"}`, true},
		{"unparseable refresh_interval", `{"redis_url":"redis://localhost","max_tokens":100,"refresh_interval":"abc"}`, true},
		{"fail_closed rejected", `{"redis_url":"redis://localhost","max_tokens":100,"redis_unavailable":"fail_closed"}`, true},
		{"invalid on_exceed", `{"redis_url":"redis://localhost","max_tokens":100,"on_exceed":"block"}`, true},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			p := New()
			err := p.Configure(json.RawMessage(tt.cfg))
			if (err != nil) != tt.wantErr {
				t.Errorf("Configure() error = %v, wantErr = %v", err, tt.wantErr)
			}
		})
	}
}

func TestOnRequest_ShadowMode(t *testing.T) {
	p := New()
	cfg := `{
		"redis_url": "mem://test",
		"max_tokens": 100,
		"on_exceed": "observe",
		"refresh_interval": "100ms"
	}`
	if err := p.Configure(json.RawMessage(cfg)); err != nil {
		t.Fatalf("Configure: %v", err)
	}
	p.store = newMemStore()

	p.OnResponseFrame(context.Background(), makePctx("sess-1", 60), nil, true)
	p.OnResponseFrame(context.Background(), makePctx("sess-1", 60), nil, true)

	p.mu.RLock()
	c := p.cache["sess-1"]
	p.mu.RUnlock()
	if c.tokens != 120 {
		t.Errorf("tokens = %d, want 120", c.tokens)
	}

	action := p.OnRequest(context.Background(), makePctx("sess-1", 0))
	if action.Type != pipeline.Continue {
		t.Fatalf("shadow mode: expected Continue past limit, got %v", action.Type)
	}

	// Verify observe mode still reserves a call slot.
	p.mu.RLock()
	calls := p.cache["sess-1"].calls
	p.mu.RUnlock()
	if calls != 2 {
		t.Errorf("calls after shadow OnRequest = %d, want 2 (1 from cold-cache response + 1 observe reservation)", calls)
	}
}

func TestOnRequest_OptimisticReservation(t *testing.T) {
	p := newTestPlugin(1000, 10, 0)

	// Seed cache so OnRequest finds the session.
	p.mu.Lock()
	p.cache["sess-1"] = &counters{tokens: 50, calls: 7, startedAt: time.Now()}
	p.mu.Unlock()

	// Three sequential requests each reserve a slot; calls should reach the limit.
	for i := 0; i < 3; i++ {
		action := p.OnRequest(context.Background(), makePctx("sess-1", 0))
		if action.Type != pipeline.Continue {
			t.Fatalf("request %d: expected Continue, got %v", i+1, action.Type)
		}
	}

	// Fourth request should be denied (7+3 = 10, limit is 10).
	action := p.OnRequest(context.Background(), makePctx("sess-1", 0))
	if action.Type != pipeline.Reject {
		t.Fatalf("request 4: expected Reject at limit, got %v", action.Type)
	}

	p.mu.RLock()
	calls := p.cache["sess-1"].calls
	p.mu.RUnlock()
	if calls != 10 {
		t.Errorf("calls = %d, want 10", calls)
	}
}

// TestOnRequest_ConcurrentCallLimit verifies that concurrent goroutines racing
// through OnRequest cannot exceed the call limit. This is the scenario the
// RLock→Lock upgrade and optimistic reservation were designed to prevent.
// Distinct from TestOnRequest_OptimisticReservation which tests serial ordering.
func TestOnRequest_ConcurrentCallLimit(t *testing.T) {
	p := newTestPlugin(1000, 10, 0)

	p.mu.Lock()
	p.cache["sess-1"] = &counters{tokens: 0, calls: 0, startedAt: time.Now()}
	p.mu.Unlock()

	const n = 20
	var wg sync.WaitGroup
	results := make([]pipeline.ActionType, n)
	wg.Add(n)
	for i := 0; i < n; i++ {
		go func(idx int) {
			defer wg.Done()
			action := p.OnRequest(context.Background(), makePctx("sess-1", 0))
			results[idx] = action.Type
		}(i)
	}
	wg.Wait()

	var continued, rejected int
	for _, r := range results {
		switch r {
		case pipeline.Continue:
			continued++
		case pipeline.Reject:
			rejected++
		}
	}
	if continued != 10 {
		t.Errorf("continued = %d, want exactly 10 (call limit)", continued)
	}
	if rejected != 10 {
		t.Errorf("rejected = %d, want 10", rejected)
	}

	p.mu.RLock()
	calls := p.cache["sess-1"].calls
	p.mu.RUnlock()
	if calls != 10 {
		t.Errorf("final calls = %d, want 10", calls)
	}
}

func TestEvaluate_MultipleLimits(t *testing.T) {
	p := newTestPlugin(100, 10, 60)

	tests := []struct {
		name    string
		c       *counters
		wantDeny bool
	}{
		{"all under", &counters{tokens: 50, calls: 5, startedAt: time.Now()}, false},
		{"tokens over", &counters{tokens: 100, calls: 5, startedAt: time.Now()}, true},
		{"calls over", &counters{tokens: 50, calls: 10, startedAt: time.Now()}, true},
		{"duration over", &counters{tokens: 50, calls: 5, startedAt: time.Now().Add(-90 * time.Second)}, true},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			reason := p.evaluate(tt.c)
			if tt.wantDeny && reason == "" {
				t.Error("expected denial reason, got empty")
			}
			if !tt.wantDeny && reason != "" {
				t.Errorf("expected no denial, got %q", reason)
			}
		})
	}
}
