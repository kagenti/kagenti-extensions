package edit

import (
	"strings"
	"testing"
)

func TestDiff_Equal(t *testing.T) {
	got := Diff([]byte("a\nb\nc\n"), []byte("a\nb\nc\n"))
	if got != "" {
		t.Fatalf("equal inputs should produce empty diff, got %q", got)
	}
}

func TestDiff_OneLineChange(t *testing.T) {
	old := []byte("a\nb\nc\n")
	new := []byte("a\nB\nc\n")
	got := Diff(old, new)
	if !strings.Contains(got, "-b") {
		t.Fatalf("missing -b line:\n%s", got)
	}
	if !strings.Contains(got, "+B") {
		t.Fatalf("missing +B line:\n%s", got)
	}
}

func TestDiff_AddAndRemove(t *testing.T) {
	old := []byte("a\nb\nc\n")
	new := []byte("a\nc\nd\n")
	got := Diff(old, new)
	if !strings.Contains(got, "-b") {
		t.Fatalf("missing -b:\n%s", got)
	}
	if !strings.Contains(got, "+d") {
		t.Fatalf("missing +d:\n%s", got)
	}
}

func TestDiff_PreservesContext(t *testing.T) {
	old := []byte("line1\nline2\nline3\nline4\n")
	new := []byte("line1\nline2\nLINE3\nline4\n")
	got := Diff(old, new)
	if !strings.Contains(got, "line1") || !strings.Contains(got, "line4") {
		t.Fatalf("missing context lines:\n%s", got)
	}
	if !strings.Contains(got, "-line3") || !strings.Contains(got, "+LINE3") {
		t.Fatalf("missing change markers:\n%s", got)
	}
}
