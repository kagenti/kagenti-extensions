package tui

import (
	"testing"
	"time"

	"github.com/rossoctl/cortex/authbridge/authlib/usage"
)

// A reply from a superseded request must be dropped. Two rapid `w` presses leave
// two requests in flight; without an id check an out-of-order response repaints a
// stale window under the current heading.
func TestUsage_StaleReplyIsDiscarded(t *testing.T) {
	m := &model{pane: paneUsage}
	m.usage.reqSeq = 2 // two requests issued; newest is 2

	stale := usageLoadedMsg{req: 1, snap: &usage.Snapshot{
		Totals: usage.Counts{Tokens: 111},
	}}
	u, _ := m.Update(stale)
	mm := u.(*model)
	if mm.usage.snap != nil {
		t.Errorf("stale reply (req=1 vs seq=2) was applied: %+v", mm.usage.snap.Totals)
	}

	fresh := usageLoadedMsg{req: 2, snap: &usage.Snapshot{
		Totals: usage.Counts{Tokens: 222},
	}}
	u, _ = mm.Update(fresh)
	mm = u.(*model)
	if mm.usage.snap == nil || mm.usage.snap.Totals.Tokens != 222 {
		t.Error("current reply (req=2) was not applied")
	}
	if mm.usage.loading {
		t.Error("loading should clear once the current reply lands")
	}
}

// Changing a view option must clear the old snapshot, not just set loading.
// Leaving it in place renders the previous window's bars under the new heading
// until the reply arrives — the same wrong-heading failure the discard guard
// exists to prevent, from the other direction.
func TestUsage_BeginFetchClearsStaleData(t *testing.T) {
	m := &model{pane: paneUsage}
	m.usage.snap = &usage.Snapshot{Totals: usage.Counts{Tokens: 999}}
	m.usage.err = errUsageUnsupported
	before := m.usage.reqSeq

	m.beginFetch() // client is nil, so no command; state changes are what matter

	if m.usage.snap != nil {
		t.Error("beginFetch left the previous snapshot on screen")
	}
	if m.usage.err != nil {
		t.Error("beginFetch left a stale error on screen")
	}
	if !m.usage.loading {
		t.Error("beginFetch did not mark the pane loading")
	}
	if m.usage.reqSeq != before+1 {
		t.Errorf("reqSeq = %d, want %d — a new request must invalidate older replies",
			m.usage.reqSeq, before+1)
	}
}

// The poll must stop doing work once the pane loses focus.
func TestUsage_TickIgnoredOffPane(t *testing.T) {
	m := &model{pane: paneEvents}
	_, cmd := m.Update(usageTickMsg(time.Now()))
	if cmd != nil {
		t.Error("tick scheduled more work while the pane was not focused")
	}
}

// Cycling window and metric must stay in range rather than growing an index.
func TestUsageState_CyclesWrap(t *testing.T) {
	var u usageState
	for i := 0; i < len(usageWindows)*3; i++ {
		u.cycleWindow()
		if w, r := u.window(); w <= 0 || r <= 0 {
			t.Fatalf("cycle %d produced window=%v resolution=%v", i, w, r)
		}
	}
	seen := map[usageMetric]bool{}
	for i := 0; i < 6; i++ {
		u.cycleMetric()
		seen[u.metric] = true
	}
	if len(seen) != 3 {
		t.Errorf("metric cycle covered %d of 3 metrics", len(seen))
	}
}
