package tui

import (
	"fmt"
	"math"
	"strconv"
	"strings"

	"github.com/rossoctl/cortex/authbridge/cmd/abctl/apiclient"
)

// formatMetricValue renders a metric value without lying about precision.
// Counters and byte totals are whole numbers and print as integers; a derived
// figure (a ratio, a per-request average) keeps two decimals. Very large
// values fall back to %g rather than printing 20 digits of float noise.
func formatMetricValue(v float64) string {
	switch {
	case math.IsNaN(v) || math.IsInf(v, 0):
		return "—"
	case math.Abs(v) >= 1e15:
		return strconv.FormatFloat(v, 'g', 6, 64)
	case v == math.Trunc(v):
		return strconv.FormatInt(int64(v), 10)
	default:
		return strconv.FormatFloat(v, 'f', 2, 64)
	}
}

// formatPluginMetrics lays out metric rows as name / right-aligned value /
// unit / note. Columns are sized to the widest entry so the numbers line up
// and can be compared by eye, which is the whole reason an operator opens
// this pane. Note renders in styleHint, as Description does in the header —
// it carries the caveat (sample size, "estimate") that keeps a derived
// number from being read as a measurement.
func formatPluginMetrics(metrics []apiclient.PluginMetric) string {
	nameW, valW := 0, 0
	vals := make([]string, len(metrics))
	for i, m := range metrics {
		vals[i] = formatMetricValue(m.Value)
		if len(m.Name) > nameW {
			nameW = len(m.Name)
		}
		if len(vals[i]) > valW {
			valW = len(vals[i])
		}
	}

	var b strings.Builder
	for i, m := range metrics {
		fmt.Fprintf(&b, "  %-*s  %*s", nameW, m.Name, valW, vals[i])
		if m.Unit != "" {
			fmt.Fprintf(&b, "  %s", styleMuted.Render(m.Unit))
		}
		if m.Note != "" {
			fmt.Fprintf(&b, "  %s", styleHint.Render(m.Note))
		}
		b.WriteString("\n")
	}
	return b.String()
}
