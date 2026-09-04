package tui

import (
	"testing"

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
	m.usage.tickGen = 1
	_, cmd := m.Update(usageTickMsg{gen: 1})
	if cmd != nil {
		t.Error("tick scheduled more work while the pane was not focused")
	}
}

// A tick left over from an earlier visit must not reschedule itself. Without a
// generation check, a quick exit and re-entry leaves two chains alive — each
// rescheduling its own successor — and the request rate doubles for the life of
// the session.
func TestUsage_StaleTickChainDoesNotReschedule(t *testing.T) {
	m := &model{pane: paneUsage}
	m.usage.tickGen = 2 // second visit

	if _, cmd := m.Update(usageTickMsg{gen: 1}); cmd != nil {
		t.Error("tick from the first visit rescheduled itself, doubling the poll rate")
	}
	// The current generation still polls. m.client is nil so fetchUsage yields no
	// command, but the tick half of the batch must still be scheduled.
	if _, cmd := m.Update(usageTickMsg{gen: 2}); cmd == nil {
		t.Error("current-generation tick did not reschedule")
	}
}

// esc must return to the pane Usage was opened from, even after the catalog
// overlay has been opened on top. model.previousPane is shared with the catalog,
// so relying on it sent esc to Sessions instead of Events.
func TestUsage_ReturnPaneSurvivesCatalogOverlay(t *testing.T) {
	m := &model{pane: paneEvents, selectedSess: "s1", previousPane: paneNone}
	m.openUsage("s1")
	if m.usage.returnPane != paneEvents {
		t.Fatalf("returnPane = %v, want paneEvents", m.usage.returnPane)
	}

	// Simulate opening the catalog from Usage and escaping back out of it: the
	// catalog uses previousPane and clears it on the way out.
	m.previousPane = paneUsage
	m.pane = paneCatalog
	m.previousPane = paneNone
	m.pane = paneUsage

	// Usage's own esc must still know where it came from.
	if m.usage.returnPane != paneEvents {
		t.Errorf("returnPane = %v after a catalog round trip, want paneEvents", m.usage.returnPane)
	}
}

// Switching pods must invalidate in-flight work: a reply describing the old pod
// must not land as if it described the new one, and the old polling chain must
// stop. backToPodsPane needs a fully wired model (contexts, port-forward), so
// this asserts the reset a stale reply would have to get past — that a bumped
// sequence makes the previous request's reply undeliverable.
func TestUsage_PodSwitchInvalidatesInFlight(t *testing.T) {
	m := &model{pane: paneUsage}
	m.usage.reqSeq = 5
	m.usage.tickGen = 3
	m.usage.snap = &usage.Snapshot{Totals: usage.Counts{Tokens: 42}}

	// The reset backToPodsPane performs on the usage fields.
	m.usage.snap = nil
	m.usage.err = nil
	m.usage.reqSeq++
	m.usage.tickGen++

	// A reply issued against the old pod must now be rejected.
	u, _ := m.Update(usageLoadedMsg{req: 5, snap: &usage.Snapshot{
		Totals: usage.Counts{Tokens: 999},
	}})
	if got := u.(*model); got.usage.snap != nil {
		t.Errorf("a reply from the previous pod was applied: %+v", got.usage.snap.Totals)
	}
	// And the old polling chain must not reschedule.
	if _, cmd := m.Update(usageTickMsg{gen: 3}); cmd != nil {
		t.Error("the previous pod's polling chain is still alive")
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
