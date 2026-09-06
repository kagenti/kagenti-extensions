package main

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// TestDiscover_ExpectedTags fails when the shipped lite tag set changes —
// a plugin added/removed or liteKeep edited. Deliberate changes update
// the want string; unintended changes are caught here.
func TestDiscover_ExpectedTags(t *testing.T) {
	tags, err := discover(defaultPluginsDir)
	if err != nil {
		t.Fatalf("discover: %v", err)
	}
	got := strings.Join(tags, ",")
	want := "exclude_plugin_a2aparser,exclude_plugin_ibac,exclude_plugin_inferenceparser,exclude_plugin_lineage,exclude_plugin_mcpparser,exclude_plugin_opa,exclude_plugin_sparc,exclude_plugin_tokenbroker,exclude_plugin_toolprune"
	if got != want {
		t.Errorf("output changed\n got: %s\nwant: %s", got, want)
	}
}

// TestDiscover_FailsClosedOnEmptyTags — an empty result would flow to
// `go build -tags ""` and ship a full binary as "lite".
func TestDiscover_FailsClosedOnEmptyTags(t *testing.T) {
	dir := t.TempDir()
	writePlugin(t, dir, "plugins_jwtvalidation.go", "!exclude_plugin_jwtvalidation")

	if _, err := discover(dir); err == nil {
		t.Fatal("want error when every plugin is in liteKeep, got nil")
	}
}

// TestDiscover_CompoundBuildDirective — before dropping the $ anchor,
// `!exclude_plugin_X && !Y` silently failed to match and the plugin
// stayed in the lite build.
func TestDiscover_CompoundBuildDirective(t *testing.T) {
	dir := t.TempDir()
	writePlugin(t, dir, "plugins_examplecompound.go", "!exclude_plugin_examplecompound && !nocgo")
	writePlugin(t, dir, "plugins_jwtvalidation.go", "!exclude_plugin_jwtvalidation") // keep-listed, prevents fail-closed

	tags, err := discover(dir)
	if err != nil {
		t.Fatalf("discover: %v", err)
	}
	if got := strings.Join(tags, ","); got != "exclude_plugin_examplecompound" {
		t.Errorf("got %q, want exclude_plugin_examplecompound", got)
	}
}

// TestDiscover_SkipsTestFiles — the plugins_*.go glob would otherwise
// scan a future plugins_foo_test.go as a plugin definition.
func TestDiscover_SkipsTestFiles(t *testing.T) {
	dir := t.TempDir()
	writePlugin(t, dir, "plugins_ghost_test.go", "!exclude_plugin_ghost")
	writePlugin(t, dir, "plugins_a2aparser.go", "!exclude_plugin_a2aparser")

	tags, err := discover(dir)
	if err != nil {
		t.Fatalf("discover: %v", err)
	}
	if got := strings.Join(tags, ","); got != "exclude_plugin_a2aparser" {
		t.Errorf("got %q, want exclude_plugin_a2aparser (test file must be skipped)", got)
	}
}

func writePlugin(t *testing.T, dir, name, buildConstraint string) {
	t.Helper()
	content := "//go:build " + buildConstraint + "\n\npackage main\n"
	if err := os.WriteFile(filepath.Join(dir, name), []byte(content), 0o644); err != nil {
		t.Fatalf("write %s: %v", name, err)
	}
}
