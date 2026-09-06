package tui

import (
	"strings"
	"testing"
	"time"

	"github.com/rossoctl/cortex/authbridge/authlib/usage"
)

// mkSeriesBuckets builds buckets whose Series carry the given per-label request
// counts, one bucket per map in the slice.
func mkSeriesBuckets(perBucket []map[string]int64) []usage.Bucket {
	base := time.Date(2026, 9, 6, 23, 24, 0, 0, time.UTC)
	out := make([]usage.Bucket, 0, len(perBucket))
	for i, labels := range perBucket {
		b := usage.Bucket{At: base.Add(time.Duration(i) * time.Minute)}
		series := map[string]usage.Counts{}
		for label, n := range labels {
			series[label] = usage.Counts{Requests: n, Tokens: n * 10}
			b.Counts.Requests += n
			b.Counts.Tokens += n * 10
		}
		if len(series) > 0 {
			b.Series = series
		}
		out = append(out, b)
	}
	return out
}

// stripANSI removes escape sequences so glyph assertions are not defeated by
// colour codes.
func stripANSI(s string) string {
	var b strings.Builder
	for i := 0; i < len(s); {
		if s[i] == 0x1b {
			for i < len(s) && s[i] != 'm' {
				i++
			}
			i++ // skip the 'm'
			continue
		}
		b.WriteByte(s[i])
		i++
	}
	return b.String()
}

// Each series must get its own texture, so the chart is readable with no colour
// at all — a terminal without colour support, a colour-vision deficiency, or a
// screenshot in an issue.
func TestRenderStacked_DistinctGlyphsPerSeries(t *testing.T) {
	buckets := mkSeriesBuckets([]map[string]int64{
		{"200": 10, "429": 5, "500": 2},
	})
	lines := renderStackedBars(buckets, metricRequests, usage.GroupStatus, 80)
	plot := stripANSI(strings.Join(lines, "\n"))

	// Three series, so three distinct glyphs from the densest end.
	for _, g := range []string{"█", "▓", "▒"} {
		if !strings.Contains(plot, g) {
			t.Errorf("expected glyph %q in the stack; got:\n%s", g, plot)
		}
	}
}

// Which series render as errors. Asserted on the DECISION, not on ANSI bytes:
// lipgloss strips colour when it detects no TTY, which is always the case under
// `go test`, so a byte-level assertion would pass vacuously and keep passing if
// the rule broke.
func TestRenderStacked_ErrorSeriesDecision(t *testing.T) {
	for _, tc := range []struct {
		label string
		group usage.Group
		want  bool
	}{
		{"500", usage.GroupStatus, true},
		{"429", usage.GroupStatus, true},
		{"400", usage.GroupStatus, true},
		{"denied", usage.GroupStatus, true},
		{"200", usage.GroupStatus, false},
		{"304", usage.GroupStatus, false},
		// Gated on the grouping: "429" is a plausible model name, and a method
		// chart must not turn red because a label happens to look like a status.
		{"429", usage.GroupMethod, false},
		{"500", usage.GroupPlugin, false},
		{"claude-sonnet-5", usage.GroupMethod, false},
	} {
		if got := isErrorSeries(tc.label, tc.group); got != tc.want {
			t.Errorf("isErrorSeries(%q, %s) = %v, want %v", tc.label, tc.group, got, tc.want)
		}
	}
}

// paintSegment must leave a non-error series byte-identical, so any styling it
// does apply is attributable to the error rule alone.
func TestPaintSegment_LeavesNonErrorsUntouched(t *testing.T) {
	const text = "████"
	if got := paintSegment(text, "200", usage.GroupStatus); got != text {
		t.Errorf("paintSegment styled a 2xx series: %q", got)
	}
	if got := paintSegment(text, "429", usage.GroupMethod); got != text {
		t.Errorf("paintSegment styled a method label: %q", got)
	}
}

func TestIsErrorStatus(t *testing.T) {
	for _, tc := range []struct {
		label string
		want  bool
	}{
		{"200", false}, {"201", false}, {"304", false},
		{"400", true}, {"429", true}, {"500", true}, {"503", true},
		{"denied", true},
		{"claude-sonnet-5", false},
		{"4", false}, {"40", false}, {"4000", false}, // wrong length
		{"", false},
		{"(other)", false},
	} {
		if got := isErrorStatus(tc.label); got != tc.want {
			t.Errorf("isErrorStatus(%q) = %v, want %v", tc.label, got, tc.want)
		}
	}
}

// Segment order must be stable across renders, or the chart appears to reshuffle
// on every 20s poll even when nothing changed.
func TestRenderStacked_SeriesOrderIsStable(t *testing.T) {
	buckets := mkSeriesBuckets([]map[string]int64{
		{"a": 5, "b": 5, "c": 5}, // equal totals: ties must break deterministically
	})
	first := stripANSI(strings.Join(renderStackedBars(buckets, metricRequests, usage.GroupStatus, 80), "\n"))
	for i := 0; i < 5; i++ {
		again := stripANSI(strings.Join(renderStackedBars(buckets, metricRequests, usage.GroupStatus, 80), "\n"))
		if again != first {
			t.Fatal("render is not deterministic for equal-total series")
		}
	}
}

// A grouped view with no labelled traffic must still show the volume it knows
// about rather than an empty frame.
func TestRenderStacked_NoSeriesFallsBackToBars(t *testing.T) {
	base := time.Date(2026, 9, 6, 23, 24, 0, 0, time.UTC)
	buckets := []usage.Bucket{
		{At: base, Counts: usage.Counts{Requests: 3, Tokens: 300}}, // no Series
	}
	lines := renderStackedBars(buckets, metricTokens, usage.GroupStatus, 80)
	if !strings.ContainsAny(strings.Join(lines, "\n"), "▁▂▃▄▅▆▇█") {
		t.Error("no bars drawn when Series is absent; the frame is empty")
	}
}

// Every line must fit the terminal, colour codes excluded — they occupy no
// columns but do inflate len().
func TestRenderStacked_FitsWidth(t *testing.T) {
	buckets := mkSeriesBuckets([]map[string]int64{
		{"200": 100, "429": 50, "500": 25, "503": 10, "418": 5},
		{"200": 80, "429": 40},
	})
	for _, width := range []int{80, 100, 60} {
		for _, line := range renderStackedBars(buckets, metricRequests, usage.GroupStatus, width) {
			if got := len([]rune(stripANSI(line))); got > width {
				t.Errorf("width %d: line is %d columns:\n%q", width, got, stripANSI(line))
			}
		}
	}
}

// Series past the glyph set fold into the overflow marker, and the legend says
// how many were elided rather than running off the terminal.
func TestRenderStacked_OverflowSeriesAreMarked(t *testing.T) {
	labels := map[string]int64{}
	for i := 0; i < 12; i++ {
		labels[string(rune('a'+i))] = int64(12 - i)
	}
	lines := renderStackedBars(mkSeriesBuckets([]map[string]int64{labels}), metricRequests, usage.GroupMethod, 80)
	legend := stripANSI(lines[len(lines)-1])
	if !strings.Contains(legend, "more)") {
		t.Errorf("legend does not report elided series: %q", legend)
	}
}

// Non-zero traffic must never render as an empty column, matching the bar chart.
func TestRenderStacked_SmallBucketStillDraws(t *testing.T) {
	buckets := mkSeriesBuckets([]map[string]int64{
		{"200": 10000},
		{"200": 1}, // 1/10000 of the peak
	})
	lines := renderStackedBars(buckets, metricRequests, usage.GroupStatus, 80)
	bottom := stripANSI(lines[plotRows-1]) // last plot row
	if strings.Count(bottom, "█") < barWidth*2 {
		t.Errorf("the tiny bucket drew no segment:\n%q", bottom)
	}
}
