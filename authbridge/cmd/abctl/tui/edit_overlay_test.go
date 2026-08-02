package tui

import (
	"strings"
	"testing"
)

func TestEditOverlayRender_Fetching(t *testing.T) {
	s := editState{phase: editPhaseFetching}
	out := renderEditOverlay(s, 80, 20)
	if !strings.Contains(out, "Fetching ConfigMap") {
		t.Fatalf("fetching phase missing message:\n%s", out)
	}
}

func TestEditOverlayRender_Diff(t *testing.T) {
	s := editState{
		phase: editPhaseDiff,
		diff:  "-old line\n+new line\n",
	}
	out := renderEditOverlay(s, 80, 20)
	if !strings.Contains(out, "old line") || !strings.Contains(out, "new line") {
		t.Fatalf("diff content missing:\n%s", out)
	}
	if !strings.Contains(out, "(y/N)") {
		t.Fatalf("confirm prompt missing:\n%s", out)
	}
}

func TestEditOverlayRender_Applying(t *testing.T) {
	s := editState{phase: editPhaseApplying}
	out := renderEditOverlay(s, 80, 20)
	if !strings.Contains(out, "Applying") {
		t.Fatalf("applying phase missing message:\n%s", out)
	}
}

func TestEditOverlayRender_Waiting(t *testing.T) {
	s := editState{phase: editPhaseWaiting}
	out := renderEditOverlay(s, 80, 20)
	if !strings.Contains(out, "reload") {
		t.Fatalf("waiting phase should mention reload:\n%s", out)
	}
}

func TestEditOverlayRender_Error(t *testing.T) {
	s := editState{phase: editPhaseError, err: "kubectl: forbidden"}
	out := renderEditOverlay(s, 80, 20)
	if !strings.Contains(out, "forbidden") {
		t.Fatalf("error message not surfaced:\n%s", out)
	}
}
