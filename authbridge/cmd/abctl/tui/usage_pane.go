package tui

import (
	"context"
	"errors"
	"fmt"
	"strings"
	"time"

	tea "github.com/charmbracelet/bubbletea"

	"github.com/rossoctl/cortex/authbridge/authlib/usage"
)

// usagePollInterval is how often the pane refetches while it is open.
//
// 20s is a deliberate compromise: the server buckets at one minute, so polling
// faster cannot reveal anything new except the current bucket filling, while
// polling slower makes the newest bar look stale to someone watching a live
// agent. The ticker is armed only while the pane is focused, so a background
// pane costs nothing.
const usagePollInterval = 20 * time.Second

// errUsageUnsupported marks a proxy with no usage aggregator wired — an older
// binary, or session tracking disabled. Distinguished from a transport error so
// the pane can explain the cause instead of showing a bare failure.
var errUsageUnsupported = errors.New("usage endpoint not available")

// usageLoadedMsg carries a fetched snapshot back to Update.
type usageLoadedMsg struct {
	snap *usage.Snapshot
	// session the request was made for, so a reply that arrives after the user
	// switched scope can be discarded rather than rendered under the wrong
	// heading.
	session string
	err     error
}

// usageTickMsg fires the periodic refetch.
type usageTickMsg time.Time

// usageState is the pane's view state: which metric, window and scope.
type usageState struct {
	metric    usageMetric
	windowIdx int    // index into usageWindows
	session   string // "" means all sessions
	snap      *usage.Snapshot
	err       error
	loading   bool
	lastFetch time.Time
}

func (u *usageState) window() (window, resolution time.Duration) {
	w := usageWindows[u.windowIdx%len(usageWindows)]
	return w.window, w.resolution
}

// cycleMetric advances [t]. Only the three count metrics are cycled here;
// latency gets its own renderer (mean-with-whiskers), which is a follow-up.
func (u *usageState) cycleMetric() {
	u.metric = (u.metric + 1) % 3
}

func (u *usageState) cycleWindow() {
	u.windowIdx = (u.windowIdx + 1) % len(usageWindows)
}

// fetchUsage requests a snapshot. Returns a tea.Cmd so the HTTP call happens off
// the render loop.
func (m *model) fetchUsage() tea.Cmd {
	if m.client == nil {
		return nil
	}
	client := m.client
	window, resolution := m.usage.window()
	session := m.usage.session
	return func() tea.Msg {
		ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		snap, err := client.GetUsage(ctx, window, resolution, session, usage.GroupNone)
		return usageLoadedMsg{snap: snap, session: session, err: err}
	}
}

// usageTick schedules the next poll.
func usageTick() tea.Cmd {
	return tea.Tick(usagePollInterval, func(t time.Time) tea.Msg { return usageTickMsg(t) })
}

// openUsage enters the pane, remembering where to return to.
//
// session is the scope: the events pane passes its selected session so the chart
// matches the timeline the operator was just reading; the sessions pane passes ""
// for an all-sessions view.
func (m *model) openUsage(session string) tea.Cmd {
	m.previousPane = m.pane
	m.pane = paneUsage
	m.usage.session = session
	m.usage.loading = true
	return tea.Batch(m.fetchUsage(), usageTick())
}

// renderUsage draws the pane.
func (m *model) renderUsage(width, height int) string {
	var b strings.Builder

	scope := "all sessions"
	if m.usage.session != "" {
		scope = "session: " + m.usage.session
	}
	window, resolution := m.usage.window()
	b.WriteString(fmt.Sprintf("  USAGE — %s — %s @ %s — %s\n\n",
		scope, window, resolution, m.usage.metric))

	switch {
	case m.usage.err != nil && errors.Is(m.usage.err, errUsageUnsupported):
		// The proxy has no aggregator: an older binary, or session tracking
		// disabled. Say which, rather than showing an empty chart that would
		// read as "no traffic".
		b.WriteString("  Usage aggregation is not available on this proxy.\n")
		b.WriteString("  It requires session tracking enabled and a build that serves /v1/usage.\n")
	case m.usage.err != nil:
		b.WriteString(fmt.Sprintf("  Error: %v\n", m.usage.err))
	case m.usage.snap == nil && m.usage.loading:
		b.WriteString("  Loading…\n")
	case m.usage.snap == nil:
		b.WriteString("  (no data)\n")
	default:
		for _, line := range renderBars(m.usage.snap.Buckets, m.usage.metric, width) {
			b.WriteString(line)
			b.WriteString("\n")
		}
		b.WriteString("\n")
		b.WriteString(renderUsageSummary(m.usage.snap))
		b.WriteString("\n")
		if !m.usage.lastFetch.IsZero() {
			b.WriteString(fmt.Sprintf("\n  updated %s ago (every %s)\n",
				time.Since(m.usage.lastFetch).Truncate(time.Second), usagePollInterval))
		}
	}

	b.WriteString("\n  [t] metric  [w] window  [r] refresh  [esc] back\n")
	return b.String()
}
