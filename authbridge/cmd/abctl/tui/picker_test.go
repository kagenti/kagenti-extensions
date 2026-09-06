package tui

import (
	"context"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	tea "github.com/charmbracelet/bubbletea"

	"github.com/rossoctl/cortex/authbridge/cmd/abctl/apiclient"
	"github.com/rossoctl/cortex/authbridge/cmd/abctl/cluster"
)

// fakeLister returns a fixed []AgentNamespace and counts ListAgents calls.
type fakeLister struct {
	namespaces []cluster.AgentNamespace
	calls      int
}

func (f *fakeLister) ListAgents(ctx context.Context) ([]cluster.AgentNamespace, error) {
	f.calls++
	return f.namespaces, nil
}

// fixtureNamespaces is a small, deterministic dataset for picker tests.
var fixtureNamespaces = []cluster.AgentNamespace{
	{Name: "team1", Pods: []cluster.Pod{
		{Namespace: "team1", Name: "weather-agent-1", Phase: "Running", Ready: true},
	}},
	{Name: "team2", Pods: []cluster.Pod{
		{Namespace: "team2", Name: "billing-agent-1", Phase: "Pending", Ready: false},
	}},
}

func TestNamespacesPaneLoadsAndRenders(t *testing.T) {
	m := newPickerModel(context.Background(), &fakeLister{namespaces: fixtureNamespaces}, nil)
	// Init returns a Cmd that loads the agents.
	cmd := m.Init()
	if cmd == nil {
		t.Fatal("Init returned nil cmd; want loader cmd")
	}
	msg := cmd()
	loaded, ok := msg.(agentsLoadedMsg)
	if !ok {
		t.Fatalf("loader cmd produced %T, want agentsLoadedMsg", msg)
	}
	updated, _ := m.Update(loaded)
	mm := updated.(*model)
	if len(mm.namespaces) != 2 {
		t.Fatalf("model should hold 2 namespaces, got %d", len(mm.namespaces))
	}
	view := mm.View()
	if !strings.Contains(view, "team1") || !strings.Contains(view, "team2") {
		t.Fatalf("rendered view missing namespaces:\n%s", view)
	}
}

func TestNamespacesPaneEmptyState(t *testing.T) {
	// Lister returns no agents — the pane should render an actionable hint
	// instead of an empty table.
	m := newPickerModel(context.Background(), &fakeLister{namespaces: []cluster.AgentNamespace{}}, nil)
	loaded := m.Init()()
	updated, _ := m.Update(loaded)
	mm := updated.(*model)
	view := mm.View()
	if !strings.Contains(view, "No AuthBridge agents found") {
		t.Fatalf("empty-state hint missing from view:\n%s", view)
	}
	if !strings.Contains(view, "--endpoint") {
		t.Fatalf("empty-state hint should mention --endpoint:\n%s", view)
	}
}

func TestNamespacesPaneDrillsIntoPods(t *testing.T) {
	m := newPickerModel(context.Background(), &fakeLister{namespaces: fixtureNamespaces}, nil)
	loaded := m.Init()()
	updated, _ := m.Update(loaded)
	mm := updated.(*model)
	// Press Enter on the first row.
	updated, _ = mm.Update(tea.KeyMsg{Type: tea.KeyEnter})
	mm = updated.(*model)
	if mm.pane != panePods {
		t.Fatalf("after Enter, active pane should be panePods, got %v", mm.pane)
	}
	if mm.selectedNamespace != "team1" {
		t.Fatalf("selected namespace should be team1, got %q", mm.selectedNamespace)
	}
}

func TestPodsPaneListsPods(t *testing.T) {
	m := newPickerModel(context.Background(), &fakeLister{namespaces: fixtureNamespaces}, nil)
	loaded := m.Init()()
	updated, _ := m.Update(loaded)
	mm := updated.(*model)
	// Drill into team1.
	updated, _ = mm.Update(tea.KeyMsg{Type: tea.KeyEnter})
	mm = updated.(*model)
	view := mm.View()
	if !strings.Contains(view, "weather-agent-1") {
		t.Fatalf("Pods view missing pod name:\n%s", view)
	}
	if !strings.Contains(view, "Running") {
		t.Fatalf("Pods view missing phase column:\n%s", view)
	}
}

func TestPodsPaneEscBacksOut(t *testing.T) {
	m := newPickerModel(context.Background(), &fakeLister{namespaces: fixtureNamespaces}, nil)
	updated, _ := m.Update(m.Init()())
	mm := updated.(*model)
	updated, _ = mm.Update(tea.KeyMsg{Type: tea.KeyEnter}) // → panePods
	updated, _ = updated.(*model).Update(tea.KeyMsg{Type: tea.KeyEsc})
	mm = updated.(*model)
	if mm.pane != paneNamespaces {
		t.Fatalf("Esc should back out to Namespaces, got pane %v", mm.pane)
	}
}

func TestRefreshKeybindReloadsAgents(t *testing.T) {
	lister := &fakeLister{namespaces: fixtureNamespaces}
	m := newPickerModel(context.Background(), lister, nil)
	// Initial load: 1 call.
	updated, _ := m.Update(m.Init()())
	mm := updated.(*model)
	if lister.calls != 1 {
		t.Fatalf("after Init: lister.calls = %d, want 1", lister.calls)
	}
	mm.pickerErr = "stale error from earlier"

	// `r` from paneNamespaces should re-run loadAgentsCmd and clear the error.
	_, cmd := mm.Update(tea.KeyMsg{Type: tea.KeyRunes, Runes: []rune{'r'}})
	if cmd == nil {
		t.Fatal("`r` on paneNamespaces should produce a Cmd to reload")
	}
	if mm.pickerErr != "" {
		t.Fatalf("`r` should clear pickerErr, got %q", mm.pickerErr)
	}
	// Run the Cmd and feed its message through Update so the post-reload
	// transition (clear m.loading, rebuild table) actually runs.
	updated, _ = mm.Update(cmd())
	mm = updated.(*model)
	if lister.calls != 2 {
		t.Fatalf("after r on paneNamespaces: lister.calls = %d, want 2", lister.calls)
	}
	if mm.loading {
		t.Fatal("agentsLoadedMsg should clear m.loading")
	}

	// Drill into team1, press `r` from panePods. Same effect.
	updated, _ = mm.Update(tea.KeyMsg{Type: tea.KeyEnter})
	mm = updated.(*model)
	if mm.pane != panePods {
		t.Fatalf("setup: not on panePods, got %v", mm.pane)
	}
	_, cmd = mm.Update(tea.KeyMsg{Type: tea.KeyRunes, Runes: []rune{'r'}})
	if cmd == nil {
		t.Fatal("`r` on panePods should produce a Cmd to reload")
	}
	updated, _ = mm.Update(cmd())
	mm = updated.(*model)
	if lister.calls != 3 {
		t.Fatalf("after r on panePods: lister.calls = %d, want 3", lister.calls)
	}
	// Pane stays on panePods (selectedNamespace still exists in the new data).
	if mm.pane != panePods {
		t.Fatalf("after r on panePods with stable data: pane = %v, want panePods", mm.pane)
	}
}

func TestRefreshKeybindIgnoredWhileLoading(t *testing.T) {
	lister := &fakeLister{namespaces: fixtureNamespaces}
	m := newPickerModel(context.Background(), lister, nil)
	// Init dispatches loadAgentsCmd and sets m.loading = true. We don't
	// execute the Cmd yet — that simulates the load being in flight.
	initCmd := m.Init()
	if initCmd == nil {
		t.Fatal("Init should return a Cmd")
	}
	if !m.loading {
		t.Fatal("Init should set m.loading = true")
	}
	// `r` while loading: the gate is purely on m.loading, regardless of
	// whether the in-flight Cmd has run yet. No new dispatch.
	_, cmd := m.Update(tea.KeyMsg{Type: tea.KeyRunes, Runes: []rune{'r'}})
	if cmd != nil {
		t.Fatal("`r` while loading should return nil Cmd")
	}
	// Complete the original load; loading flag clears.
	_, _ = m.Update(initCmd())
	if m.loading {
		t.Fatal("agentsLoadedMsg should clear m.loading")
	}
	if lister.calls != 1 {
		t.Fatalf("only the original load should have run: lister.calls = %d, want 1", lister.calls)
	}
	// Now `r` works again.
	_, cmd = m.Update(tea.KeyMsg{Type: tea.KeyRunes, Runes: []rune{'r'}})
	if cmd == nil {
		t.Fatal("`r` after load completes should produce a Cmd")
	}
	_ = cmd()
	if lister.calls != 2 {
		t.Fatalf("after second r: lister.calls = %d, want 2", lister.calls)
	}
}

func TestRefreshDropsBackOutWhenSelectedNamespaceVanishes(t *testing.T) {
	// Drill into team1, then the lister's underlying data shifts to
	// only-team2. After `r`, the user should land back on paneNamespaces
	// since team1 no longer exists.
	lister := &fakeLister{namespaces: fixtureNamespaces}
	m := newPickerModel(context.Background(), lister, nil)
	updated, _ := m.Update(m.Init()())
	mm := updated.(*model)
	updated, _ = mm.Update(tea.KeyMsg{Type: tea.KeyEnter}) // → panePods on team1
	mm = updated.(*model)
	if mm.pane != panePods {
		t.Fatalf("setup: not on panePods, got %v", mm.pane)
	}

	// Cluster state changes — only team2 remains.
	lister.namespaces = []cluster.AgentNamespace{
		{Name: "team2", Pods: []cluster.Pod{
			{Namespace: "team2", Name: "billing-agent-1", Phase: "Pending", Ready: false},
		}},
	}
	// Press `r`, deliver the new agentsLoadedMsg.
	_, cmd := mm.Update(tea.KeyMsg{Type: tea.KeyRunes, Runes: []rune{'r'}})
	updated, _ = mm.Update(cmd())
	mm = updated.(*model)

	if mm.pane != paneNamespaces {
		t.Fatalf("after vanished-namespace reload, pane should be paneNamespaces, got %v", mm.pane)
	}
	if mm.selectedNamespace != "" {
		t.Fatalf("selectedNamespace should be cleared, got %q", mm.selectedNamespace)
	}
}

// fakePortForwarder returns a no-op PortForward.
type fakePortForwarder struct {
	startedNs  string
	startedPod string
	endpoint   string
	startErr   error
	closeCount int
}

func (f *fakePortForwarder) Start(ctx context.Context, ns, pod string) (cluster.PortForward, error) {
	if f.startErr != nil {
		return nil, f.startErr
	}
	f.startedNs, f.startedPod = ns, pod
	return &fakePortForward{endpoint: f.endpoint, parent: f}, nil
}

type fakePortForward struct {
	endpoint string
	parent   *fakePortForwarder
}

func (p *fakePortForward) Endpoint() string       { return p.endpoint }
func (p *fakePortForward) StatusEndpoint() string { return "" } // unused by current picker tests
func (p *fakePortForward) Close() error           { p.parent.closeCount++; return nil }

func TestPodEnterStartsPortForwardAndTransitions(t *testing.T) {
	pf := &fakePortForwarder{endpoint: "http://127.0.0.1:60000"}
	m := newPickerModel(context.Background(), &fakeLister{namespaces: fixtureNamespaces}, pf)
	updated, _ := m.Update(m.Init()())
	mm := updated.(*model)
	updated, _ = mm.Update(tea.KeyMsg{Type: tea.KeyEnter}) // → panePods
	mm = updated.(*model)
	updated, cmd := mm.Update(tea.KeyMsg{Type: tea.KeyEnter}) // start PF
	mm = updated.(*model)
	if cmd == nil {
		t.Fatal("Enter on pod should produce a Cmd to start the PF")
	}
	msg := cmd()
	conn, ok := msg.(portForwardReadyMsg)
	if !ok {
		t.Fatalf("PF cmd produced %T, want portForwardReadyMsg", msg)
	}
	updated, _ = mm.Update(conn)
	mm = updated.(*model)
	if mm.pane != paneSessions {
		t.Fatalf("after PF ready, pane should be paneSessions, got %v", mm.pane)
	}
	if mm.endpoint != "http://127.0.0.1:60000" {
		t.Fatalf("model endpoint not set: %q", mm.endpoint)
	}
	if pf.startedNs != "team1" || pf.startedPod != "weather-agent-1" {
		t.Fatalf("PortForwarder.Start not called with selection: ns=%q pod=%q", pf.startedNs, pf.startedPod)
	}
}

func TestPodEnterSurfacesPortForwardError(t *testing.T) {
	pf := &fakePortForwarder{startErr: fmt.Errorf("forbidden")}
	m := newPickerModel(context.Background(), &fakeLister{namespaces: fixtureNamespaces}, pf)
	updated, _ := m.Update(m.Init()())
	mm := updated.(*model)
	updated, _ = mm.Update(tea.KeyMsg{Type: tea.KeyEnter}) // → panePods
	mm = updated.(*model)
	updated, cmd := mm.Update(tea.KeyMsg{Type: tea.KeyEnter}) // start PF
	mm = updated.(*model)
	updated, _ = mm.Update(cmd())
	mm = updated.(*model)
	if mm.pane != panePods {
		t.Fatalf("PF error should keep us on panePods, got %v", mm.pane)
	}
	if !strings.Contains(mm.pickerErr, "forbidden") {
		t.Fatalf("error not surfaced in pickerErr: %q", mm.pickerErr)
	}
}

func TestRunOptionsWiringEndpointBypass(t *testing.T) {
	// Endpoint set → no Lister/PF needed; the function should not panic
	// and should return promptly when the context is cancelled.
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	opts := RunOptions{Endpoint: "http://127.0.0.1:1"}
	// Run will exit because ctx is already cancelled; we just verify
	// it doesn't dereference nil Lister/PortForwarder.
	_ = Run(ctx, opts)
}

func TestEscFromSessionsReturnsToPods(t *testing.T) {
	pf := &fakePortForwarder{endpoint: "http://127.0.0.1:60001"}
	m := newPickerModel(context.Background(), &fakeLister{namespaces: fixtureNamespaces}, pf)
	// Drill: agents loaded → namespaces → pods → port-forward → sessions
	updated, _ := m.Update(m.Init()())
	mm := updated.(*model)
	updated, _ = mm.Update(tea.KeyMsg{Type: tea.KeyEnter})
	mm = updated.(*model)
	updated, cmd := mm.Update(tea.KeyMsg{Type: tea.KeyEnter})
	mm = updated.(*model)
	updated, _ = mm.Update(cmd())
	mm = updated.(*model)
	if mm.pane != paneSessions {
		t.Fatalf("setup failed: not in paneSessions, got %v", mm.pane)
	}
	if mm.activePF == nil {
		t.Fatal("setup failed: activePF should be set")
	}

	// Esc from Sessions should return to Pods.
	updated, _ = mm.Update(tea.KeyMsg{Type: tea.KeyEsc})
	mm = updated.(*model)
	if mm.pane != panePods {
		t.Fatalf("after Esc, pane should be panePods, got %v", mm.pane)
	}
	if mm.activePF != nil {
		t.Fatal("activePF should have been closed and cleared")
	}
	if pf.closeCount != 1 {
		t.Fatalf("PortForward.Close should have been called once, got %d", pf.closeCount)
	}
	if mm.client != nil {
		t.Fatal("m.client should have been cleared")
	}
	if mm.cancel == nil {
		t.Fatal("m.cancel should have been re-derived from parentCtx, not left nil")
	}
}

func TestEscFromSessionsNoOpInBypassMode(t *testing.T) {
	// Build a bypass-mode model directly (no picker).
	ctx := context.Background()
	c := apiclient.New("http://127.0.0.1:1")
	m := New(ctx, c).(*model)
	// No parentCtx. Esc on paneSessions should be a no-op.
	updated, _ := m.Update(tea.KeyMsg{Type: tea.KeyEsc})
	mm := updated.(*model)
	if mm.pane != paneSessions {
		t.Fatalf("bypass mode Esc should NOT change pane, got %v", mm.pane)
	}
}

func TestRefreshTickAfterEscDoesNotPanic(t *testing.T) {
	pf := &fakePortForwarder{endpoint: "http://127.0.0.1:60002"}
	m := newPickerModel(context.Background(), &fakeLister{namespaces: fixtureNamespaces}, pf)
	// Drill all the way to paneSessions.
	updated, _ := m.Update(m.Init()())
	mm := updated.(*model)
	updated, _ = mm.Update(tea.KeyMsg{Type: tea.KeyEnter})
	mm = updated.(*model)
	updated, cmd := mm.Update(tea.KeyMsg{Type: tea.KeyEnter})
	mm = updated.(*model)
	updated, _ = mm.Update(cmd())
	mm = updated.(*model)
	if mm.pane != paneSessions {
		t.Fatalf("setup failed: not in paneSessions, got %v", mm.pane)
	}
	// Esc back to Pods. m.client is now nil.
	updated, _ = mm.Update(tea.KeyMsg{Type: tea.KeyEsc})
	mm = updated.(*model)
	if mm.pane != panePods {
		t.Fatalf("after Esc, expected panePods, got %v", mm.pane)
	}
	// A late refreshTickMsg should not panic.
	defer func() {
		if r := recover(); r != nil {
			t.Fatalf("tick/stream msg in picker pane panicked: %v", r)
		}
	}()
	_, _ = mm.Update(refreshTickMsg(time.Now()))
	// A late tickMsg should not panic.
	_, _ = mm.Update(tickMsg(time.Now()))
	// A late streamMsg should not panic.
	_, _ = mm.Update(streamMsg{})
	// A late streamClosedMsg should not panic.
	_, _ = mm.Update(streamClosedMsg{})
}

// silence unused-import nag if test build trims this file later
var _ = time.Second

// --- [l] localhost:9094 shortcut -----------------------------------------

// sessionAPIStub serves the minimum /v1/sessions response the `[l]` probe
// needs, so the shortcut can be exercised end-to-end against a real HTTP
// endpoint rather than a mocked client.
func sessionAPIStub(t *testing.T) *httptest.Server {
	t.Helper()
	mux := http.NewServeMux()
	mux.HandleFunc("/v1/sessions", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"sessions":[]}`))
	})
	mux.HandleFunc("/v1/pipeline", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"inbound":[],"outbound":[]}`))
	})
	srv := httptest.NewServer(mux)
	t.Cleanup(srv.Close)
	return srv
}

func TestLocalhostKeybindConnects(t *testing.T) {
	srv := sessionAPIStub(t)
	m := newPickerModel(context.Background(), &fakeLister{namespaces: fixtureNamespaces}, nil)
	updated, _ := m.Update(m.Init()())
	mm := updated.(*model)

	// `l` should dispatch a probe Cmd and mark the model loading.
	_, cmd := mm.Update(keyRune('l'))
	if cmd == nil {
		t.Fatal("`l` on paneNamespaces should produce a connect Cmd")
	}
	if !mm.loading {
		t.Fatal("`l` should set m.loading while the probe is in flight")
	}

	// Run the probe against the stub (not the real localhost:9094) and feed
	// the result back through Update.
	msg := connectLocalCmd(context.Background(), srv.URL)()
	lc, ok := msg.(localConnectedMsg)
	if !ok {
		t.Fatalf("connectLocalCmd produced %T, want localConnectedMsg", msg)
	}
	if lc.err != nil {
		t.Fatalf("probe against stub failed: %v", lc.err)
	}
	updated, _ = mm.Update(lc)
	mm = updated.(*model)

	if mm.pane != paneSessions {
		t.Fatalf("after `l` connect, pane should be paneSessions, got %v", mm.pane)
	}
	if mm.client == nil {
		t.Fatal("after `l` connect, m.client should be set")
	}
	if mm.endpoint != srv.URL {
		t.Fatalf("endpoint = %q, want %q", mm.endpoint, srv.URL)
	}
	if !mm.localDirect {
		t.Fatal("localDirect should be true after connecting via `l`")
	}
	if mm.activePF != nil {
		t.Fatal("`l` must not create a port-forward")
	}
	if mm.loading {
		t.Fatal("localConnectedMsg should clear m.loading")
	}
}

func TestLocalhostKeybindProbeFailureStaysInPicker(t *testing.T) {
	// Point at a closed port so the probe fails fast.
	srv := sessionAPIStub(t)
	dead := srv.URL
	srv.Close()

	m := newPickerModel(context.Background(), &fakeLister{namespaces: fixtureNamespaces}, nil)
	updated, _ := m.Update(m.Init()())
	mm := updated.(*model)

	msg := connectLocalCmd(context.Background(), dead)()
	lc := msg.(localConnectedMsg)
	if lc.err == nil {
		t.Fatal("probe against a closed port should fail")
	}
	updated, _ = mm.Update(lc)
	mm = updated.(*model)

	if mm.pane != paneNamespaces {
		t.Fatalf("failed probe should leave the user in the picker, got pane %v", mm.pane)
	}
	if mm.client != nil {
		t.Fatal("failed probe must not set a client")
	}
	if mm.pickerErr == "" {
		t.Fatal("failed probe should surface an error in the footer")
	}
	if !strings.Contains(mm.pickerErr, "localhost:9094") {
		t.Fatalf("picker error should name the endpoint it tried, got %q", mm.pickerErr)
	}
	if mm.loading {
		t.Fatal("failed probe should clear m.loading")
	}
}

// Esc out of a session entered via `[l]` has no pod to return to, so it
// must land on Namespaces rather than an empty Pods table.
func TestLocalhostEscReturnsToNamespaces(t *testing.T) {
	srv := sessionAPIStub(t)
	m := newPickerModel(context.Background(), &fakeLister{namespaces: fixtureNamespaces}, nil)
	updated, _ := m.Update(m.Init()())
	mm := updated.(*model)
	updated, _ = mm.Update(connectLocalCmd(context.Background(), srv.URL)())
	mm = updated.(*model)
	if mm.pane != paneSessions {
		t.Fatalf("setup: expected paneSessions, got %v", mm.pane)
	}
	updated, _ = mm.Update(tea.KeyMsg{Type: tea.KeyEsc})
	mm = updated.(*model)
	if mm.pane != paneNamespaces {
		t.Fatalf("Esc from an `l`-entered session should return to Namespaces, got %v", mm.pane)
	}
	if mm.localDirect {
		t.Fatal("localDirect should be cleared on back-out")
	}
}

// The namespaces footer and empty state must advertise `[l]`, otherwise
// the shortcut is undiscoverable.
func TestLocalhostKeybindIsAdvertised(t *testing.T) {
	m := newPickerModel(context.Background(), &fakeLister{namespaces: fixtureNamespaces}, nil)
	updated, _ := m.Update(m.Init()())
	mm := updated.(*model)
	updated, _ = mm.Update(tea.WindowSizeMsg{Width: 120, Height: 40})
	mm = updated.(*model)
	if !strings.Contains(mm.View(), "[l] localhost:9094") {
		t.Fatalf("namespaces footer should advertise [l]:\n%s", mm.View())
	}

	// Empty-state hint: the no-agents case is exactly when `l` is most
	// useful, so it should be mentioned there too.
	m2 := newPickerModel(context.Background(), &fakeLister{namespaces: []cluster.AgentNamespace{}}, nil)
	u2, _ := m2.Update(m2.Init()())
	mm2 := u2.(*model)
	if !strings.Contains(mm2.View(), "[l]") {
		t.Fatalf("empty-state hint should mention [l]:\n%s", mm2.View())
	}
}

// `l` must not be swallowed when the picker is mid-load, and must not
// hijack the vim-right binding in the session panes.
func TestLocalhostKeybindScoping(t *testing.T) {
	srv := sessionAPIStub(t)
	m := newPickerModel(context.Background(), &fakeLister{namespaces: fixtureNamespaces}, nil)
	updated, _ := m.Update(m.Init()())
	mm := updated.(*model)
	mm.loading = true
	_, cmd := mm.Update(keyRune('l'))
	if cmd != nil {
		t.Fatal("`l` while loading should be a no-op")
	}
	mm.loading = false

	// In the sessions pane, `l` is vim-right (drill in), not connect-local.
	updated, _ = mm.Update(connectLocalCmd(context.Background(), srv.URL)())
	mm = updated.(*model)
	mm.pane = paneSessions
	before := mm.endpoint
	_, _ = mm.Update(keyRune('l'))
	if mm.endpoint != before {
		t.Fatal("`l` in the sessions pane should not re-trigger a local connect")
	}
}

// A session entered via `[l]` has no pod/namespace, so pipeline editing
// must report the limitation rather than opening a broken edit. This is
// the same guard `--endpoint` mode relies on; asserted here because the
// README documents the behavior for `[l]` specifically.
func TestLocalhostEditIsUnavailable(t *testing.T) {
	srv := sessionAPIStub(t)
	m := newPickerModel(context.Background(), &fakeLister{namespaces: fixtureNamespaces}, nil)
	updated, _ := m.Update(m.Init()())
	mm := updated.(*model)
	updated, _ = mm.Update(connectLocalCmd(context.Background(), srv.URL)())
	mm = updated.(*model)
	mm.pane = panePipeline

	updated, cmd := mm.Update(keyRune('e'))
	mm = updated.(*model)
	if cmd != nil {
		t.Fatal("`e` after an `l` connect should not start an edit")
	}
	if mm.editState.phase != editPhaseDone {
		t.Fatalf("`e` should not enter an edit phase, got %v", mm.editState.phase)
	}
	if !strings.Contains(mm.flash, "picker") {
		t.Fatalf("`e` should flash the picker-required hint, got %q", mm.flash)
	}
}
