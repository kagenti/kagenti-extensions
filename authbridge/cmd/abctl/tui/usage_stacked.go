package tui

import (
	"fmt"
	"sort"
	"strings"

	"github.com/rossoctl/cortex/authbridge/authlib/usage"
)

// segmentGlyphs are the textures a stacked bar cycles through, densest first.
//
// Redundant with color on purpose. Color alone fails three ways here: a terminal
// with no color support, a viewer with a colour-vision deficiency, and a
// screenshot pasted into an issue. A distinct glyph per series survives all
// three, so the chart is readable from texture alone and color is an accelerant
// rather than the encoding.
//
// Ordered densest-to-sparsest so the largest series (sorted first below) is also
// the visually heaviest, which matches what a reader expects from a stack.
var segmentGlyphs = [...]rune{'█', '▓', '▒', '░'}

// overflowGlyph marks the series past len(segmentGlyphs) — including the
// aggregator's own "(other)" bucket. Distinct from the four above so "several
// small series folded together" never looks like a named one.
const overflowGlyph = '·'

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

// glyphFor assigns each series its texture by rank.
func glyphFor(rank int) rune {
	if rank < len(segmentGlyphs) {
		return segmentGlyphs[rank]
	}
	return overflowGlyph
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
			sb.WriteString(stackedCell(b, m, group, series, rank, peak, row))
			sb.WriteString(strings.Repeat(" ", barGap))
		}
		out = append(out, strings.TrimRight(sb.String(), " "))
	}

	out = append(out, renderAxis(len(buckets)))
	out = append(out, renderTimeLabels(buckets))
	out = append(out, renderValues(buckets, m))
	out = append(out, "")
	out = append(out, renderLegend(series, group, width))
	return out
}

// stackedCell renders one bar's glyphs for one row, choosing the segment whose
// cumulative height covers this row.
func stackedCell(b usage.Bucket, m usageMetric, group usage.Group,
	series []seriesKey, rank map[string]int, peak int64, row int) string {

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

	// Walk the series in rank order, accumulating rows, until we pass this row.
	// Rows are allotted proportionally to each series' share of the bucket.
	var acc int64
	for _, s := range series {
		v := m.valueOf(b.Series[s.label])
		if v <= 0 {
			continue
		}
		segRows := v * barRows / total
		if segRows == 0 {
			segRows = 1 // a present series must occupy at least one cell
		}
		acc += segRows
		if int64(row) <= acc {
			cell := strings.Repeat(string(glyphFor(rank[s.label])), barWidth)
			return paintSegment(cell, s.label, group)
		}
	}
	// Rounding left this row uncovered: attribute it to the largest series rather
	// than punching a hole in the middle of a bar.
	cell := strings.Repeat(string(glyphFor(0)), barWidth)
	return paintSegment(cell, series[0].label, group)
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

// paintSegment applies the error style when the series calls for it.
func paintSegment(text, label string, group usage.Group) string {
	if isErrorSeries(label, group) {
		return styleError.Render(text)
	}
	return text
}

// renderLegend names each series with its glyph and total. Error statuses are
// coloured to match their segments, so the legend is the key to the chart rather
// than a separate vocabulary.
func renderLegend(series []seriesKey, group usage.Group, width int) string {
	const sep = "   "
	var parts []string
	// Track the printable width separately: paintSegment may add ANSI escapes,
	// which occupy no columns but do inflate len(). Measuring the styled string
	// would under-fill the line; measuring the plain text keeps it honest.
	plain := 2 // leading indent
	elided := 0

	for i, s := range series {
		entry := fmt.Sprintf("%c %s (%s)", glyphFor(i), s.label, humanizeCount(s.total))
		cost := len([]rune(entry))
		if len(parts) > 0 {
			cost += len(sep)
		}
		// Reserve room for the "(+N more)" note so the line cannot overflow while
		// admitting the very entry that would need eliding.
		if plain+cost+len("   (+99 more)") > width && len(parts) > 0 {
			elided = len(series) - i
			break
		}
		parts = append(parts, paintSegment(entry, s.label, group))
		plain += cost
		// Past the glyph set every series shares the overflow marker, so naming
		// them individually stops being informative.
		if i+1 >= len(segmentGlyphs) {
			elided = len(series) - i - 1
			break
		}
	}
	out := "  " + strings.Join(parts, sep)
	if elided > 0 {
		out += fmt.Sprintf("%s(+%d more)", sep, elided)
	}
	return out
}
