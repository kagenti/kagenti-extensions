package tui

import (
	"strings"
	"testing"

	"github.com/rossoctl/cortex/authbridge/cmd/abctl/apiclient"
)

func TestFormatMetricValue(t *testing.T) {
	tests := []struct {
		in   float64
		want string
	}{
		{1284, "1284"},       // counter: no decimal noise
		{9389184, "9389184"}, // byte total
		{0, "0"},             // a fresh counter is still a number
		{1830.5, "1830.50"},  // derived figure keeps precision
		{0.126, "0.13"},      // ratio rounds up
		{0.125, "0.12"},      // exact tie: strconv rounds half-to-even
		{-3, "-3"},           // negative whole
	}
	for _, tc := range tests {
		if got := formatMetricValue(tc.in); got != tc.want {
			t.Errorf("formatMetricValue(%v) = %q, want %q", tc.in, got, tc.want)
		}
	}
}

// TestFormatPluginMetrics_AlignsColumns: the point of the pane is comparing
// numbers by eye, so values must right-align into one column regardless of
// name length.
func TestFormatPluginMetrics_AlignsColumns(t *testing.T) {
	out := formatPluginMetrics([]apiclient.PluginMetric{
		{Name: "requests seen", Value: 7, Unit: "count"},
		{Name: "bytes removed / request", Value: 7312, Unit: "bytes"},
	})
	lines := strings.Split(strings.TrimRight(out, "\n"), "\n")
	if len(lines) != 2 {
		t.Fatalf("got %d lines, want 2:\n%s", len(lines), out)
	}
	// Both value strings must end at the same column.
	col0 := strings.Index(lines[0], "7")
	col1 := strings.Index(lines[1], "7312")
	if col0 < 0 || col1 < 0 {
		t.Fatalf("values not found in output:\n%s", out)
	}
	if end0, end1 := col0+len("7"), col1+len("7312"); end0 != end1 {
		t.Errorf("values not right-aligned: %q ends at %d, %q ends at %d\n%s",
			"7", end0, "7312", end1, out)
	}
	for i, l := range lines {
		if !strings.HasPrefix(l, "  ") {
			t.Errorf("line %d not indented: %q", i, l)
		}
	}
}

// TestFormatPluginMetrics_RendersNote: a derived number without its caveat
// reads as a measurement. The note must appear on the row.
func TestFormatPluginMetrics_RendersNote(t *testing.T) {
	out := formatPluginMetrics([]apiclient.PluginMetric{
		{Name: "tokens saved / request", Value: 1830, Unit: "tokens", Note: "estimate, n=1284"},
	})
	if !strings.Contains(out, "estimate, n=1284") {
		t.Errorf("note missing from output: %q", out)
	}
	if !strings.Contains(out, "tokens") {
		t.Errorf("unit missing from output: %q", out)
	}
}

func TestFormatPluginMetrics_EmptyIsEmptyString(t *testing.T) {
	if got := formatPluginMetrics(nil); got != "" {
		t.Errorf("formatPluginMetrics(nil) = %q, want empty (pane renders (none))", got)
	}
}

// TestLivePipelinePlugin_ResolvesAgainstRefreshedView: plugin Metrics are live
// counters riding on a view that was originally fetched once at startup, on the
// documented assumption that the pipeline composition never changes. That
// assumption held for composition and broke for counters — an open detail pane
// kept rendering its opening snapshot, so Metrics read "(none)" forever on a
// proxy that had counted nothing yet at connect time.
func TestLivePipelinePlugin_ResolvesAgainstRefreshedView(t *testing.T) {
	shown := &apiclient.PipelinePlugin{Name: "tool-prune", Direction: "outbound"}

	m := &model{pipeline: &apiclient.PipelineView{
		Outbound: []apiclient.PipelinePlugin{
			{Name: "inference-parser", Direction: "outbound"},
			{Name: "tool-prune", Direction: "outbound", Metrics: []apiclient.PluginMetric{
				{Name: "requests pruned", Value: 2, Unit: "count"},
			}},
		},
	}}

	got := m.livePipelinePlugin(shown)
	if got == nil {
		t.Fatal("tool-prune not resolved against the refreshed view")
	}
	if len(got.Metrics) != 1 || got.Metrics[0].Value != 2 {
		t.Errorf("resolved plugin carries no fresh metrics: %+v", got.Metrics)
	}
}

// TestLivePipelinePlugin_CatalogEntryHasNoLiveCounterpart: a catalog entry is
// synthesised with a blank direction and is not in the active chain, so there is
// nothing to refresh it from.
func TestLivePipelinePlugin_CatalogEntryHasNoLiveCounterpart(t *testing.T) {
	m := &model{pipeline: &apiclient.PipelineView{
		Outbound: []apiclient.PipelinePlugin{{Name: "tool-prune", Direction: "outbound"}},
	}}
	if got := m.livePipelinePlugin(&apiclient.PipelinePlugin{Name: "tool-prune"}); got != nil {
		t.Errorf("catalog entry (blank direction) should not resolve, got %+v", got)
	}
	if got := m.livePipelinePlugin(nil); got != nil {
		t.Error("nil input should return nil")
	}
	// No view fetched yet.
	if got := (&model{}).livePipelinePlugin(&apiclient.PipelinePlugin{Name: "x", Direction: "outbound"}); got != nil {
		t.Error("nil pipeline should return nil")
	}
}
