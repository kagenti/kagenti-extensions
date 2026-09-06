package tui

import (
	"fmt"
	"sort"
	"strings"

	"github.com/rossoctl/cortex/authbridge/authlib/usage"
)

// maxNamedSeries is how many series get their own mark and legend entry before
// the rest fold together. Bounded by the palette so no two named series share a
// colour.
var maxNamedSeries = len(seriesPalette)

// seriesKey is one label's total across the window, used to decide segment order
// and which labels get their own glyph.
type seriesKey struct {
	label string
	total int64
}

// collectSeries totals each label across every bucket and returns them largest
// first, with ties broken by label so the legend and the stack order are stable
// between renders. An unstable order would make the chart appear to reshuffle on
// each 20s poll even when nothing changed.
func collectSeries(buckets []usage.Bucket, m usageMetric) []seriesKey {
	totals := map[string]int64{}
	for _, b := range buckets {
		for label, c := range b.Series {
			totals[label] += m.valueOf(c)
		}
	}
	out := make([]seriesKey, 0, len(totals))
	for label, total := range totals {
		out = append(out, seriesKey{label: label, total: total})
	}
	sort.Slice(out, func(i, j int) bool {
		if out[i].total != out[j].total {
			return out[i].total > out[j].total
		}
		return out[i].label < out[j].label
	})
	return out
}

// isErrorStatus reports whether a group=status label denotes a failure, so it can
// be drawn in red.
//
// Only meaningful for GroupStatus: a model name or plugin name has no status to
// read. Callers gate on the grouping rather than this returning false for
// everything else, because "429" is a plausible model name in a way that should
// not silently colour a method chart red.
func isErrorStatus(label string) bool {
	if label == "denied" {
		return true // a rejected request never got a status code
	}
	// Labels are produced by strconv.Itoa on the status code, so a leading 4 or 5
	// with three digits is the whole test.
	if len(label) != 3 {
		return false
	}
	return label[0] == '4' || label[0] == '5'
}

// renderStackedBars draws one bar per bucket, split into segments by series.
//
// Segments are whole cells, not eighths: a fractional top cell cannot also encode
// a segment boundary, so the ungrouped renderer keeps sub-row precision and this
// one trades it for the breakdown. That is the reason "ungrouped" is its own
// cycle state rather than a special case of grouping.
func renderStackedBars(buckets []usage.Bucket, m usageMetric, group usage.Group, width int) []string {
	if len(buckets) == 0 {
		return []string{"  (no data)"}
	}
	maxBars := (width - axisLabel) / barStride
	if maxBars < 1 {
		maxBars = 1
	}
	if len(buckets) > maxBars {
		buckets = buckets[len(buckets)-maxBars:]
	}

	series := collectSeries(buckets, m)
	if len(series) == 0 {
		// Grouped view with no labelled traffic yet: fall back to the ungrouped
		// bars rather than an empty frame, so the pane still shows the volume it
		// does know about.
		return renderBars(buckets, m, width)
	}

	// Rank each label once so segment order is identical in every bucket. A stack
	// whose layers reorder between adjacent bars is unreadable.
	rank := make(map[string]int, len(series))
	for i, s := range series {
		rank[s.label] = i
	}
	// One mark per series, assigned once for the whole chart so a letter means the
	// same thing in every bucket and in the legend.
	letters := assignLetters(series)

	var peak int64
	for _, b := range buckets {
		if v := m.value(b); v > peak {
			peak = v
		}
	}

	out := make([]string, 0, plotRows+4)
	for row := plotRows; row >= 1; row-- {
		var sb strings.Builder
		if row%2 == 0 && peak > 0 {
			sb.WriteString(fmt.Sprintf("%5s ", humanizeCount(peak*int64(row)/int64(plotRows))))
		} else {
			sb.WriteString(strings.Repeat(" ", axisLabel))
		}
		for _, b := range buckets {
			sb.WriteString(stackedCell(b, m, group, series, rank, letters, peak, row))
			sb.WriteString(strings.Repeat(" ", barGap))
		}
		out = append(out, strings.TrimRight(sb.String(), " "))
	}

	out = append(out, renderAxis(len(buckets)))
	out = append(out, renderTimeLabels(buckets))
	out = append(out, renderValues(buckets, m))
	out = append(out, "")
	out = append(out, renderLegend(series, group, letters, rank, width)...)
	return out
}

// stackedCell renders one bar's glyphs for one row, choosing the segment whose
// cumulative height covers this row.
func stackedCell(b usage.Bucket, m usageMetric, group usage.Group,
	series []seriesKey, rank map[string]int, letters map[string]rune,
	peak int64, row int) string {

	total := m.value(b)
	if total <= 0 || peak <= 0 {
		return strings.Repeat(" ", barWidth)
	}
	// Whole rows only — see the renderStackedBars comment on why segments cannot
	// use partial blocks.
	barRows := total * int64(plotRows) / peak
	if barRows == 0 {
		barRows = 1 // non-zero traffic must never render as an empty column
	}
	if int64(row) > barRows {
		return strings.Repeat(" ", barWidth)
	}

	// Walk the allotment bottom-up until we pass this row.
	var acc int64
	for _, alloc := range allotRows(b, m, series, barRows, total) {
		acc += alloc.rows
		if int64(row) <= acc {
			return paintMark(alloc.label, letters, rank, group)
		}
	}
	// Rounding left this row uncovered: attribute it to the largest series rather
	// than punching a hole in the middle of a bar.
	return paintMark(series[0].label, letters, rank, group)
}

// rowAlloc is one series' share of a bar, in whole rows.
type rowAlloc struct {
	label string
	rows  int64
}

// allotRows divides a bar's rows among the series present in it.
//
// Every present series gets at least one row, so a model with a rounding-error
// share of the traffic is still visible — "claude-haiku at 912 tokens against
// 2.2M" is exactly the case an operator wants to spot, and a segment floored to
// zero rows makes it indistinguishable from absent.
//
// The floor cannot simply be applied per series, which is what the previous
// version did: with three series in a ten-row bar the largest took 9 rows and the
// two floored ones landed on rows 10 and 11, so the eleventh fell outside the bar
// and its series vanished anyway. Guaranteed rows are reserved FIRST and the
// remainder shared out proportionally, so the total always fits.
func allotRows(b usage.Bucket, m usageMetric, series []seriesKey, barRows, total int64) []rowAlloc {
	present := make([]rowAlloc, 0, len(series))
	for _, s := range series {
		if m.valueOf(b.Series[s.label]) > 0 {
			present = append(present, rowAlloc{label: s.label})
		}
	}
	if len(present) == 0 {
		return nil
	}
	// More series than rows: the bar cannot show them all, so give a row each to
	// as many as fit, largest first (series is already sorted). The legend still
	// names the rest.
	if int64(len(present)) >= barRows {
		out := present[:barRows]
		for i := range out {
			out[i].rows = 1
		}
		return out
	}

	// One row reserved per series, the rest shared by proportion of the surplus.
	surplus := barRows - int64(len(present))
	var assigned int64
	for i := range present {
		v := m.valueOf(b.Series[present[i].label])
		extra := v * surplus / total
		present[i].rows = 1 + extra
		assigned += present[i].rows
	}
	// Integer division leaves rows unassigned; give them to the largest series so
	// the bar reaches its full height.
	if rem := barRows - assigned; rem > 0 {
		present[0].rows += rem
	}
	return present
}

// segmentStyle reports whether a series should be drawn as an error, separated
// from the rendering so tests can assert the DECISION rather than the bytes.
//
// lipgloss strips colour when it detects no TTY, which is always true under `go
// test`, so asserting on ANSI escapes cannot verify this. Returning the intent
// keeps the rule testable and leaves styling to one call site.
func isErrorSeries(label string, group usage.Group) bool {
	return group == usage.GroupStatus && isErrorStatus(label)
}

// paintMark renders one series' cell: its letter, repeated across the bar width,
// on the series background.
//
// Repeated rather than centred so the segment reads as a solid band — a single
// letter floating in a 4-column cell looks like a data point, not a share of a
// stack.
func paintMark(label string, letters map[string]rune, rank map[string]int, group usage.Group) string {
	r, ok := letters[label]
	if !ok {
		r = '?'
	}
	return seriesStyle(rank[label], isErrorSeries(label, group)).
		Render(strings.Repeat(string(r), barWidth))
}

// paintSegment applies the error style to legend text when the series calls for
// it. Foreground only: a coloured background belongs on the chart marks, where it
// encodes the series, not on a line of prose.
func paintSegment(text, label string, group usage.Group) string {
	if isErrorSeries(label, group) {
		return styleError.Render(text)
	}
	return text
}

// renderLegend names each series with its glyph and total. Error statuses are
// coloured to match their segments, so the legend is the key to the chart rather
// than a separate vocabulary.
// renderLegend keys the chart: each series' mark, name and total.
//
// Returns one or more lines. Wrapping rather than eliding matters because the mark
// is the only way to identify a band — a series dropped from the legend leaves an
// unreadable segment on the chart, and model names are long enough
// ("claude-haiku-4-5-20251001") that three of them do not fit 80 columns on one
// line. Only series past maxNamedSeries fold into a count, and those share a
// colour anyway.
func renderLegend(series []seriesKey, group usage.Group,
	letters map[string]rune, rank map[string]int, width int) []string {
	const sep = "   "
	const indent = "  "

	named := series
	elided := 0
	if len(named) > maxNamedSeries {
		elided = len(named) - maxNamedSeries
		named = named[:maxNamedSeries]
	}

	var lines []string
	var parts []string
	plain := len(indent)

	flush := func() {
		if len(parts) > 0 {
			lines = append(lines, indent+strings.Join(parts, sep))
			parts = nil
			plain = len(indent)
		}
	}

	for _, s := range named {
		mark := seriesStyle(rank[s.label], isErrorSeries(s.label, group)).
			Render(string(letters[s.label]))
		text := fmt.Sprintf(" %s (%s)", s.label, humanizeCount(s.total))
		// Measured on the plain text: the mark is one column however many bytes of
		// escape sequence it carries.
		cost := 1 + len([]rune(text))
		if len(parts) > 0 {
			cost += len(sep)
		}
		if plain+cost > width && len(parts) > 0 {
			flush()
			cost = 1 + len([]rune(text))
		}
		parts = append(parts, mark+paintSegment(text, s.label, group))
		plain += cost
	}
	flush()

	if elided > 0 {
		note := fmt.Sprintf("%s(+%d more)", indent, elided)
		// Append to the last line when it fits, so a single extra series does not
		// cost a whole row.
		if n := len(lines); n > 0 && len([]rune(stripANSIWidth(lines[n-1])))+len(note) <= width {
			lines[n-1] += sep + strings.TrimPrefix(note, indent)
		} else {
			lines = append(lines, note)
		}
	}
	if len(lines) == 0 {
		return nil
	}
	return lines
}

// stripANSIWidth returns text with escape sequences removed, for measuring how
// many columns a styled string occupies.
func stripANSIWidth(s string) string {
	var b strings.Builder
	for i := 0; i < len(s); {
		if s[i] == 0x1b {
			for i < len(s) && s[i] != 'm' {
				i++
			}
			i++
			continue
		}
		b.WriteByte(s[i])
		i++
	}
	return b.String()
}
