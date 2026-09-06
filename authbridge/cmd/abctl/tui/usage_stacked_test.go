package tui

import (
	"fmt"
	"strings"
	"testing"
	"time"
	"unicode"

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

// Each series must get a distinct mark, so the chart is readable with no colour
// at all — a terminal without colour support, a colour-vision deficiency, or a
// screenshot in an issue. Shaded blocks failed this in practice: █ against ▓ is
// nearly indistinguishable in most terminal fonts.
func TestRenderStacked_DistinctMarksPerSeries(t *testing.T) {
	buckets := mkSeriesBuckets([]map[string]int64{
		{"200": 10, "429": 5, "500": 2},
	})
	plot := stripANSI(strings.Join(renderStackedBars(buckets, metricRequests, usage.GroupStatus, 80), "\n"))

	// Status labels have no letters, so their marks are the leading digits.
	for _, want := range []string{"2", "4", "5"} {
		if !strings.Contains(plot, strings.Repeat(want, barWidth)) {
			t.Errorf("expected a %q band in the stack; got:\n%s", want, plot)
		}
	}
}

// The mark must be derived from the label so a band is self-describing, and
// sibling models must not collide on a shared vendor prefix — every claude-*
// yielding "c" would defeat the point.
func TestSeriesLetter(t *testing.T) {
	for _, tc := range []struct {
		label string
		want  rune
	}{
		{"claude-sonnet-5", 's'},
		{"claude-opus-5", 'o'},
		{"claude-haiku-4-5-20251001", 'h'},
		{"anthropic/claude-sonnet-5", 's'}, // provider prefix skipped too
		{"gpt-4o", 'o'},                    // gpt is a vendor token; 4 is not a letter
		{"inference-parser", 'i'},
		{"tool-prune", 't'},
		{"denied", 'd'},
		{"200", '2'}, // no letters at all: fall back to the first character
		{"429", '4'},
		{"(other)", 'o'},
		{"", '?'},
	} {
		if got := seriesLetter(tc.label); got != tc.want {
			t.Errorf("seriesLetter(%q) = %q, want %q", tc.label, string(got), string(tc.want))
		}
	}
}

// Two series sharing a mark is the failure the shaded blocks had. Uniqueness
// beats the mnemonic, and the largest series keeps the intuitive letter.
func TestAssignLetters_AreUnique(t *testing.T) {
	series := []seriesKey{
		{"claude-opus-5", 100},  // wants 'o'
		{"claude-sonnet-5", 90}, // wants 's'
		{"(other)", 80},         // also wants 'o' — must yield
		{"openai/gpt-4o", 70},   // 'o' taken as well
		{"ollama-llama3", 60},
	}
	letters := assignLetters(series)
	if len(letters) != len(series) {
		t.Fatalf("assigned %d marks for %d series", len(letters), len(series))
	}
	seen := map[rune]string{}
	for label, r := range letters {
		if prev, dup := seen[r]; dup {
			t.Errorf("mark %q assigned to both %q and %q", string(r), prev, label)
		}
		seen[r] = label
	}
	// The largest series keeps its derived letter.
	if letters["claude-opus-5"] != 'o' {
		t.Errorf("largest series lost its mnemonic: got %q", string(letters["claude-opus-5"]))
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
	joined := stripANSI(strings.Join(lines, "\n"))
	if !strings.Contains(joined, "more)") {
		t.Errorf("legend does not report series past the palette: %q", joined)
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
	if strings.Count(bottom, "2") < barWidth*2 {
		t.Errorf("the tiny bucket drew no segment:\n%q", bottom)
	}
}

// A series that is a rounding error of the bucket must still occupy a row.
// "claude-haiku at 912 tokens against 2.2M" is exactly the case an operator wants
// to spot, and a segment floored to zero rows is indistinguishable from absent.
//
// The per-series floor alone was not enough: with three series in a ten-row bar
// the largest took 9 rows and the two floored ones landed on rows 10 and 11, so
// the eleventh fell outside the bar and vanished anyway.
func TestRenderStacked_TinySeriesIsStillVisible(t *testing.T) {
	buckets := mkSeriesBuckets([]map[string]int64{{
		"claude-sonnet-5":           22000, // 2.2M tokens at 100x
		"claude-opus-5":             1050,
		"claude-haiku-4-5-20251001": 9, // 0.04% of the bucket
	}})
	plot := stripANSI(strings.Join(renderStackedBars(buckets, metricTokens, usage.GroupMethod, 80), "\n"))

	for _, want := range []string{"s", "o", "h"} {
		if !strings.Contains(plot, strings.Repeat(want, barWidth)) {
			t.Errorf("series %q drew no band despite being present:\n%s", want, plot)
		}
	}
}

// A bar's height comes from the bucket total, so traffic no label claims must get
// its own band rather than being absorbed by the named series. Absorbing it drew
// a bucket that is 10% claude-sonnet-5 as a solid `s` bar: the height said "lots
// of traffic" and every row claimed to be sonnet.
func TestAllotRows_UnlabelledTrafficGetsItsOwnBand(t *testing.T) {
	b := mkSeriesBuckets([]map[string]int64{{"claude-sonnet-5": 100}})[0]
	b.Counts.Requests = 1000 // only 10% of the bucket is labelled
	series := collectSeries([]usage.Bucket{b}, metricRequests)

	got := allotRows(b, metricRequests, series, 10, b.Requests)

	rows := map[string]int64{}
	var sum int64
	for _, a := range got {
		rows[a.label] = a.rows
		sum += a.rows
	}
	if sum != 10 {
		t.Fatalf("allotted %d rows, bar is 10 tall", sum)
	}
	if rows["claude-sonnet-5"] != 1 && rows["claude-sonnet-5"] != 2 {
		t.Errorf("sonnet has %d of 10 rows for 10%% of the bucket", rows["claude-sonnet-5"])
	}
	if rows[unlabelledLabel] == 0 {
		t.Error("the 90% no label claims drew no band")
	}
}

// Per-plugin attribution counts one request once per plugin, so byPlugin
// sub-totals intentionally sum to MORE than the bucket (see the aggregator's
// foldInto). Dividing by the bucket total over-allotted rows, `acc` ran past the
// bar height, and whole series fell off the top — on every by-plugin bucket.
func TestAllotRows_HandlesOverAttributedSeries(t *testing.T) {
	b := mkSeriesBuckets([]map[string]int64{{"p1": 1000, "p2": 1000, "p3": 1000}})[0]
	b.Counts.Requests = 1000 // each plugin credited the whole bucket
	series := collectSeries([]usage.Bucket{b}, metricRequests)

	got := allotRows(b, metricRequests, series, 10, b.Requests)

	var sum int64
	for _, a := range got {
		if a.rows < 1 {
			t.Errorf("series %q allotted %d rows", a.label, a.rows)
		}
		sum += a.rows
	}
	if sum != 10 {
		t.Errorf("allotted %d rows for a 10-row bar — series would fall off the chart", sum)
	}
	if len(got) != 3 {
		t.Errorf("allotted %d bands, want all 3 plugins present", len(got))
	}
}

// Every band drawn must have a legend entry. Marks come from the palette and
// repeat once it wraps, so a seventh series could draw with the first one's mark
// while the legend named neither — an undecodable band.
func TestRenderStacked_EveryDrawnBandIsInTheLegend(t *testing.T) {
	labels := map[string]int64{}
	for i := 0; i < 12; i++ {
		labels[fmt.Sprintf("series-%c", 'a'+i)] = int64(100 - i*5)
	}
	lines := renderStackedBars(mkSeriesBuckets([]map[string]int64{labels}), metricRequests, usage.GroupMethod, 120)

	// Split chart rows from legend rows at the axis.
	var axisAt int
	for i, l := range lines {
		if strings.ContainsRune(stripANSI(l), '┼') {
			axisAt = i
			break
		}
	}
	chart := stripANSI(strings.Join(lines[:axisAt], "\n"))
	legend := stripANSI(strings.Join(lines[axisAt:], "\n"))

	// Collect the distinct marks actually drawn.
	drawn := map[rune]bool{}
	for _, r := range chart {
		if unicode.IsLetter(r) || r == '·' {
			drawn[r] = true
		}
	}
	if len(drawn) == 0 {
		t.Fatal("no marks drawn")
	}
	for r := range drawn {
		// The legend lists each mark followed by its name.
		if !strings.ContainsRune(legend, r) {
			t.Errorf("mark %q is drawn on the chart but absent from the legend:\n%s", string(r), legend)
		}
	}
}

// Folding the tail must preserve the totals: bounding how many bands are drawn is
// the point, losing traffic is not.
func TestFoldTailSeries_PreservesTotals(t *testing.T) {
	labels := map[string]int64{}
	var want int64
	for i := 0; i < 10; i++ {
		v := int64(100 - i*5)
		labels[fmt.Sprintf("s%c", 'a'+i)] = v
		want += v
	}
	buckets := mkSeriesBuckets([]map[string]int64{labels})
	series := collectSeries(buckets, metricRequests)

	kept, folded := foldTailSeries(buckets, metricRequests, series, 4)
	if len(kept) != 5 { // 4 named + the fold
		t.Errorf("kept %d series, want 4 named plus the fold", len(kept))
	}
	var got int64
	for _, c := range folded[0].Series {
		got += c.Requests
	}
	if got != want {
		t.Errorf("folded buckets total %d, want %d — folding lost traffic", got, want)
	}
	// An existing "(other)" from the aggregator's own capping must merge, not
	// collide: two bands both meaning "the rest" would be indefensible.
	withOther := mkSeriesBuckets([]map[string]int64{{
		"a": 100, "b": 90, "c": 80, "d": 70, "e": 60, tailLabel: 50,
	}})
	s2 := collectSeries(withOther, metricRequests)
	kept2, _ := foldTailSeries(withOther, metricRequests, s2, 3)
	seen := 0
	for _, k := range kept2 {
		if k.label == tailLabel {
			seen++
		}
	}
	if seen != 1 {
		t.Errorf("%d %q bands after folding, want exactly 1", seen, tailLabel)
	}
}

// Allotment must fill the bar exactly: overshooting pushes top segments outside
// the frame, undershooting leaves a gap at the top.
func TestAllotRows_SumsToBarHeight(t *testing.T) {
	for _, tc := range []struct {
		name    string
		values  map[string]int64
		barRows int64
	}{
		{"tiny tail", map[string]int64{"a": 22000, "b": 1050, "c": 9}, 10},
		{"even split", map[string]int64{"a": 10, "b": 10, "c": 10}, 9},
		{"more series than rows", map[string]int64{"a": 5, "b": 4, "c": 3, "d": 2, "e": 1}, 3},
		{"single series", map[string]int64{"a": 7}, 10},
		{"one row", map[string]int64{"a": 7, "b": 3}, 1},
	} {
		b := mkSeriesBuckets([]map[string]int64{tc.values})[0]
		series := collectSeries([]usage.Bucket{b}, metricRequests)
		got := allotRows(b, metricRequests, series, tc.barRows, b.Requests)

		var sum int64
		for _, a := range got {
			if a.rows < 1 {
				t.Errorf("%s: series %q allotted %d rows", tc.name, a.label, a.rows)
			}
			sum += a.rows
		}
		if sum != tc.barRows {
			t.Errorf("%s: allotted %d rows, bar is %d tall", tc.name, sum, tc.barRows)
		}
	}
}

// The legend is the only key to a band, so a present series must never be elided
// for width — it wraps instead. Model names are long enough that three do not fit
// 80 columns on one line.
func TestRenderLegend_WrapsRatherThanElidingPresentSeries(t *testing.T) {
	series := []seriesKey{
		{"claude-sonnet-5", 4_600_000},
		{"claude-opus-5", 218_000},
		{"claude-haiku-4-5-20251001", 1900},
	}
	letters := assignLetters(series)
	rank := map[string]int{}
	for i, s := range series {
		rank[s.label] = i
	}

	lines := renderLegend(series, usage.GroupMethod, letters, rank, 80)
	joined := stripANSI(strings.Join(lines, "\n"))
	for _, s := range series {
		if !strings.Contains(joined, s.label) {
			t.Errorf("legend dropped %q:\n%s", s.label, joined)
		}
	}
	if strings.Contains(joined, "more)") {
		t.Errorf("legend elided a named series instead of wrapping:\n%s", joined)
	}
	for _, l := range lines {
		if got := len([]rune(stripANSI(l))); got > 80 {
			t.Errorf("legend line is %d columns:\n%q", got, stripANSI(l))
		}
	}
}

// A legend line wider than the terminal wraps and destroys the chart above it, so
// the width bound has to hold even for a single entry with nothing to wrap
// against — one long model name on a narrow terminal.
func TestRenderLegend_BoundsALoneOverWideEntry(t *testing.T) {
	series := []seriesKey{{"anthropic/claude-haiku-4-5-20251001-preview-experimental", 912}}
	letters := assignLetters(series)
	rank := map[string]int{series[0].label: 0}

	for _, width := range []int{10, 16, 20, 40, 80} {
		lines := renderLegend(series, usage.GroupMethod, letters, rank, width)
		for _, l := range lines {
			if got := len([]rune(stripANSI(l))); got > width {
				t.Errorf("width %d: legend line is %d columns:\n%q", width, got, stripANSI(l))
			}
		}
		// The total identifies how much the band is worth, so it survives
		// truncation wherever there is room for it at all.
		if width >= 20 {
			if !strings.Contains(stripANSI(strings.Join(lines, "")), "(912)") {
				t.Errorf("width %d: truncation dropped the total: %q", width, stripANSI(lines[0]))
			}
		}
	}
}

// The elision note is appended as sep+note, so the fit check must measure that.
// Counting the indent instead happened to agree at width 80 and was three columns
// short everywhere else.
func TestRenderLegend_ElisionNoteRespectsWidth(t *testing.T) {
	var series []seriesKey
	for i := 0; i < 9; i++ {
		series = append(series, seriesKey{fmt.Sprintf("series-%c-name", 'a'+i), int64(100 - i)})
	}
	letters := assignLetters(series)
	rank := map[string]int{}
	for i, s := range series {
		rank[s.label] = i
	}

	for width := 30; width <= 100; width++ {
		lines := renderLegend(series, usage.GroupMethod, letters, rank, width)
		joined := stripANSI(strings.Join(lines, "\n"))
		if !strings.Contains(joined, "more)") {
			t.Errorf("width %d: nine series past the palette but no elision note", width)
		}
		for _, l := range lines {
			if got := len([]rune(stripANSI(l))); got > width {
				t.Errorf("width %d: line is %d columns:\n%q", width, got, stripANSI(l))
			}
		}
	}
}

func TestTruncateLegendText(t *testing.T) {
	const text = " claude-sonnet-5 (2.2M)"
	for _, max := range []int{0, 1, 2, 5, 10, 22, 23, 40} {
		got := truncateLegendText(text, max)
		if len([]rune(got)) > max {
			t.Errorf("truncateLegendText(%d) = %q (%d runes), over budget", max, got, len([]rune(got)))
		}
	}
	if got := truncateLegendText(text, 40); got != text {
		t.Errorf("a text that already fits was altered: %q", got)
	}
}
