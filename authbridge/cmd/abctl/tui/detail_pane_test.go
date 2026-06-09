package tui

import (
	"encoding/json"
	"strings"
	"testing"

	"github.com/kagenti/kagenti-extensions/authbridge/authlib/pipeline"
)

// tlsHeader returns "" for plaintext events so callers can prepend
// unconditionally. Locks the contract that the header is invisible
// on non-TLS connections.
func TestTLSHeader_NilProducesEmpty(t *testing.T) {
	if got := tlsHeader(nil); got != "" {
		t.Errorf("tlsHeader(nil) = %q, want empty", got)
	}
}

// Full TLS state — the header includes version, cipher, and peer.
func TestTLSHeader_FullState(t *testing.T) {
	got := tlsHeader(&pipeline.EventTLS{
		Version:      "TLS 1.3",
		CipherSuite:  "TLS_AES_128_GCM_SHA256",
		PeerSPIFFEID: "spiffe://kagenti.local/ns/team1/sa/caller-agent",
	})
	for _, want := range []string{
		"TLS:",
		"version: TLS 1.3",
		"cipher: TLS_AES_128_GCM_SHA256",
		"peer:    spiffe://kagenti.local/ns/team1/sa/caller-agent",
	} {
		if !strings.Contains(got, want) {
			t.Errorf("tlsHeader missing %q\ngot:\n%s", want, got)
		}
	}
}

// Partial state — version+cipher only, no peer (e.g. peer cert had
// no SPIFFE URI). The peer line should be absent rather than showing
// "peer: " with an empty value.
func TestTLSHeader_NoPeerSPIFFEID(t *testing.T) {
	got := tlsHeader(&pipeline.EventTLS{
		Version:     "TLS 1.3",
		CipherSuite: "TLS_AES_128_GCM_SHA256",
	})
	if strings.Contains(got, "peer:") {
		t.Errorf("tlsHeader unexpectedly included peer line on no-SPIFFE cert\ngot:\n%s", got)
	}
	if !strings.Contains(got, "version: TLS 1.3") {
		t.Errorf("tlsHeader missing version on partial state\ngot:\n%s", got)
	}
}

// Empty version + cipher but with peer — the version/cipher line
// is omitted, but peer still shows. Tolerates partial wire data
// gracefully.
func TestTLSHeader_PeerOnly(t *testing.T) {
	got := tlsHeader(&pipeline.EventTLS{
		PeerSPIFFEID: "spiffe://test/example",
	})
	if !strings.Contains(got, "spiffe://test/example") {
		t.Errorf("tlsHeader missing peer\ngot:\n%s", got)
	}
	if strings.Contains(got, "version:") || strings.Contains(got, "cipher:") {
		t.Errorf("tlsHeader unexpectedly included version/cipher on peer-only state\ngot:\n%s", got)
	}
}

// eventScopedToPlugin must restrict both the Invocations slices and the
// per-plugin Plugins map to the selected plugin, and must NOT mutate the
// original event (the shallow copy aliases the slices/map otherwise).
func TestEventScopedToPlugin_FiltersToSelectedPlugin(t *testing.T) {
	ev := &pipeline.SessionEvent{
		Invocations: &pipeline.Invocations{
			Inbound: []pipeline.Invocation{
				{Plugin: "jwt-validation", Action: pipeline.ActionAllow},
				{Plugin: "a2a-parser", Action: pipeline.ActionObserve},
			},
		},
		Plugins: map[string]json.RawMessage{
			"jwt-validation": json.RawMessage("{}"),
			"a2a-parser":     json.RawMessage("{}"),
		},
	}

	scoped := eventScopedToPlugin(ev, "jwt-validation")

	if got := len(scoped.Invocations.Inbound); got != 1 {
		t.Fatalf("scoped inbound invocations = %d, want 1", got)
	}
	if got := scoped.Invocations.Inbound[0].Plugin; got != "jwt-validation" {
		t.Errorf("scoped invocation plugin = %q, want %q", got, "jwt-validation")
	}
	if got := len(scoped.Plugins); got != 1 {
		t.Fatalf("scoped Plugins entries = %d, want 1", got)
	}
	if _, ok := scoped.Plugins["jwt-validation"]; !ok {
		t.Errorf("scoped Plugins missing jwt-validation key: %v", scoped.Plugins)
	}
	if _, ok := scoped.Plugins["a2a-parser"]; ok {
		t.Errorf("scoped Plugins unexpectedly retained a2a-parser")
	}

	// Original event must be untouched (no aliasing of slice/map).
	if got := len(ev.Invocations.Inbound); got != 2 {
		t.Errorf("original inbound invocations mutated: = %d, want 2", got)
	}
	if got := len(ev.Plugins); got != 2 {
		t.Errorf("original Plugins map mutated: = %d, want 2", got)
	}
}

// An empty plugin string means "no specific invocation" — the helper returns
// the event unchanged (same pointer) so old whole-event behavior is preserved.
func TestEventScopedToPlugin_EmptyPluginReturnsUnchanged(t *testing.T) {
	ev := &pipeline.SessionEvent{
		Invocations: &pipeline.Invocations{
			Inbound: []pipeline.Invocation{
				{Plugin: "jwt-validation", Action: pipeline.ActionAllow},
			},
		},
	}
	if got := eventScopedToPlugin(ev, ""); got != ev {
		t.Errorf("eventScopedToPlugin(ev, \"\") = %p, want original %p", got, ev)
	}
}
