package tui

import (
	"strings"
	"testing"
	"time"

	"github.com/rossoctl/cortex/authbridge/authlib/usage"
)

func mkBuckets(vals []int64) []usage.Bucket {
	base := time.Date(2026, 9, 4, 23, 24, 0, 0, time.UTC)
	out := make([]usage.Bucket, 0, len(vals))
	for i, v := range vals {
		var reqs int64
		if v > 0 {
			reqs = 1
		}
		out = append(out, usage.Bucket{
			At:     base.Add(time.Duration(i) * time.Minute),
			Counts: usage.Counts{Requests: reqs, Tokens: v},
		})
	}
	return out
}

// The whole reason the value row exists: an idle bucket must print "0", and a
// very small one must print its value. A blank cell for both would make a gap in
// traffic indistinguishable from traffic too small to plot — the exact confusion
// an operator hits when usage looks wrong.
func TestRenderBars_ZeroBucketIsStatedNotBlank(t *testing.T) {
	lines := renderBars(mkBuckets([]int64{4100, 0, 300}), metricTokens, 80)
	values := lines[len(lines)-1]

	if !strings.Contains(values, "0") {
		t.Errorf("value row has no zero marker for the idle bucket:\n%q", values)
	}
	if !strings.Contains(values, "300") {
		t.Errorf("value row lost the small bucket's value:\n%q", values)
	}
	// The small bucket must also draw at least one glyph, or the chart implies
	// no traffic there.
	plot := strings.Join(lines[:len(lines)-3], "\n")
	if !strings.ContainsAny(plot, "▁▂▃▄▅▆▇█") {
		t.Errorf("no bar glyphs rendered at all:\n%s", plot)
	}
}

// A bucket far below the peak still has to be visible. Rounding it to zero rows
// would hide real traffic; the partial blocks exist for exactly this.
func TestRenderBars_SmallValueStillDrawsAGlyph(t *testing.T) {
	// 50 against a 50000 peak is 1/1000 — well under one full row.
	lines := renderBars(mkBuckets([]int64{50000, 50}), metricTokens, 80)
	bottom := lines[len(lines)-4] // last plot row before the axis

	// Two bars: the tall one full, the short one a partial block.
	if !strings.Contains(bottom, "█") {
		t.Errorf("tall bar missing from bottom row:\n%q", bottom)
	}
	if !strings.ContainsAny(bottom, "▁▂▃▄▅▆▇") {
		t.Errorf("small bar rendered no partial block, so it is invisible:\n%q", bottom)
	}
}

// Output must fit the terminal it was given. A line wider than the width wraps
// and destroys the chart.
func TestRenderBars_FitsWidth(t *testing.T) {
	for _, width := range []int{80, 100, 60, 40} {
		vals := make([]int64, 10)
		for i := range vals {
			vals[i] = int64(10000 * (i + 1))
		}
		for _, line := range renderBars(mkBuckets(vals), metricTokens, width) {
			if got := len([]rune(line)); got > width {
				t.Errorf("width %d: line is %d columns wide:\n%q", width, got, line)
			}
		}
	}
}

// A narrow terminal keeps the NEWEST buckets: an operator watching live traffic
// cares about now, not about the start of the window.
func TestRenderBars_NarrowKeepsNewest(t *testing.T) {
	vals := []int64{111, 222, 333, 444, 555, 666, 777, 888, 999, 1000}
	lines := renderBars(mkBuckets(vals), metricTokens, 30)
	values := lines[len(lines)-1]

	// 1000 humanizes to "1.0k".
	if !strings.Contains(values, "1.0k") {
		t.Errorf("newest bucket dropped on a narrow terminal:\n%q", values)
	}
	if strings.Contains(values, "111") {
		t.Errorf("oldest bucket kept instead of newest:\n%q", values)
	}
}

// All-zero data must render without panicking or dividing by a zero peak.
func TestRenderBars_AllZeroIsSafe(t *testing.T) {
	lines := renderBars(mkBuckets([]int64{0, 0, 0}), metricTokens, 80)
	if len(lines) == 0 {
		t.Fatal("no output for an all-idle window")
	}
	values := lines[len(lines)-1]
	if strings.Count(values, "0") < 3 {
		t.Errorf("all three idle buckets should be marked:\n%q", values)
	}
}

func TestRenderBars_EmptyInput(t *testing.T) {
	if got := renderBars(nil, metricTokens, 80); len(got) != 1 {
		t.Errorf("renderBars(nil) = %v, want a single placeholder line", got)
	}
}

// Cost is reserved but unpopulated. The summary must say so rather than render
// $0.00, which reads as "this traffic was free".
func TestRenderUsageSummary_UnpricedSaysUnavailable(t *testing.T) {
	snap := &usage.Snapshot{
		Totals:  usage.Counts{Requests: 100, Errors: 3, Tokens: 5000},
		Priced:  false,
		Buckets: mkBuckets([]int64{5000}),
	}
	got := renderUsageSummary(snap)
	if !strings.Contains(got, "COST unavailable") {
		t.Errorf("unpriced summary should say cost is unavailable, got:\n%q", got)
	}
	if strings.Contains(got, "$0.00") {
		t.Errorf("summary renders $0.00, which reads as free traffic:\n%q", got)
	}
	if !strings.Contains(got, "3.0%") {
		t.Errorf("error rate missing from summary:\n%q", got)
	}
}

// Window latency must be request-weighted, not a mean of per-bucket means — the
// same trap the server avoids when folding.
func TestRenderUsageSummary_LatencyIsRequestWeighted(t *testing.T) {
	snap := &usage.Snapshot{
		Totals: usage.Counts{Requests: 100},
		Buckets: []usage.Bucket{
			{Counts: usage.Counts{Requests: 99}, LatMeanMs: 1000},
			{Counts: usage.Counts{Requests: 1}, LatMeanMs: 5000},
		},
	}
	got := renderUsageSummary(snap)
	// (99*1000 + 1*5000)/100 = 1040ms = 1.04s. Mean-of-means would be 3.00s.
	if !strings.Contains(got, "1.04s") {
		t.Errorf("latency should be 1.04s (request-weighted), got:\n%q", got)
	}
	if strings.Contains(got, "3.00s") {
		t.Errorf("latency is the mean-of-means:\n%q", got)
	}
}

func TestHumanizeCount(t *testing.T) {
	for _, tc := range []struct {
		in   int64
		want string
	}{
		{0, "0"}, {999, "999"}, {1200, "1.2k"}, {9999, "10.0k"},
		{18900, "18k"}, {48200, "48k"}, {1_500_000, "1.5M"},
	} {
		if got := humanizeCount(tc.in); got != tc.want {
			t.Errorf("humanizeCount(%d) = %q, want %q", tc.in, got, tc.want)
		}
		if got := len(humanizeCount(tc.in)); got > 5 {
			t.Errorf("humanizeCount(%d) is %d chars, too wide for the gutter", tc.in, got)
		}
	}
}
