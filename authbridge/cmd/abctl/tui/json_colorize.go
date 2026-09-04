package tui

import (
	"encoding/json"
	"sort"
	"strconv"
	"strings"

	"github.com/charmbracelet/lipgloss"
)

// Minimal JSON pretty-printer with lipgloss styling. Takes any value and
// emits indented, colorized text. Used by the detail pane to render a
// SessionEvent. Stdlib-only — no deps on go-prettyjson / colorjson etc.
//
// Style choices mirror common editor palettes: keys in one color, strings
// another, numbers/booleans/null each distinct so the eye can scan a dense
// payload quickly.
var (
	styleJSONKey    = lipgloss.NewStyle().Foreground(colorAccent)
	styleJSONString = lipgloss.NewStyle().Foreground(lipgloss.AdaptiveColor{Light: "#047857", Dark: "#6EE7B7"})
	styleJSONNumber = lipgloss.NewStyle().Foreground(lipgloss.AdaptiveColor{Light: "#B45309", Dark: "#FCD34D"})
	styleJSONBool   = lipgloss.NewStyle().Foreground(lipgloss.AdaptiveColor{Light: "#B91C1C", Dark: "#FCA5A5"})
	styleJSONNull   = lipgloss.NewStyle().Foreground(colorMuted).Italic(true)
	styleJSONPunct  = lipgloss.NewStyle().Foreground(colorMuted)
)

// ColorizeJSON takes a Go value (typically something decoded from json) and
// returns an indented, colorized string. For inputs that are already raw
// bytes, round-trip through json.Unmarshal to normalize key order and
// whitespace before styling.
func ColorizeJSON(v any) string {
	var b strings.Builder
	writeJSONValue(&b, v, 0)
	return b.String()
}

// ColorizeJSONBytes parses data then colorizes. Falls back to a muted raw
// string on parse failure so the caller always gets something renderable.
func ColorizeJSONBytes(data []byte) string {
	var v any
	dec := json.NewDecoder(strings.NewReader(string(data)))
	dec.UseNumber()
	if err := dec.Decode(&v); err != nil {
		return styleJSONPunct.Render(string(data))
	}
	return ColorizeJSON(v)
}

func writeJSONValue(b *strings.Builder, v any, indent int) {
	switch x := v.(type) {
	case nil:
		b.WriteString(styleJSONNull.Render("null"))
	case bool:
		b.WriteString(styleJSONBool.Render(strconv.FormatBool(x)))
	case json.Number:
		b.WriteString(styleJSONNumber.Render(string(x)))
	case float64:
		b.WriteString(styleJSONNumber.Render(strconv.FormatFloat(x, 'g', -1, 64)))
	case int, int64, int32:
		b.WriteString(styleJSONNumber.Render(strconv.FormatInt(toInt64(x), 10)))
	case string:
		b.WriteString(styleJSONString.Render(strconv.Quote(x)))
	case []any:
		writeJSONArray(b, x, indent)
	case map[string]any:
		writeJSONObject(b, x, indent)
	default:
		// Fallback: marshal with stdlib and emit muted.
		raw, err := json.Marshal(x)
		if err != nil {
			b.WriteString(styleJSONPunct.Render("<unrenderable>"))
			return
		}
		b.WriteString(styleJSONPunct.Render(string(raw)))
	}
}

func writeJSONObject(b *strings.Builder, m map[string]any, indent int) {
	if len(m) == 0 {
		b.WriteString(styleJSONPunct.Render("{}"))
		return
	}
	b.WriteString(styleJSONPunct.Render("{"))
	b.WriteByte('\n')

	// Sort keys for deterministic output.
	keys := make([]string, 0, len(m))
	for k := range m {
		keys = append(keys, k)
	}
	sort.Strings(keys)

	pad := strings.Repeat("  ", indent+1)
	closePad := strings.Repeat("  ", indent)
	for i, k := range keys {
		b.WriteString(pad)
		b.WriteString(styleJSONKey.Render(strconv.Quote(k)))
		b.WriteString(styleJSONPunct.Render(": "))
		writeJSONValue(b, m[k], indent+1)
		if i < len(keys)-1 {
			b.WriteString(styleJSONPunct.Render(","))
		}
		b.WriteByte('\n')
	}
	b.WriteString(closePad)
	b.WriteString(styleJSONPunct.Render("}"))
}

func writeJSONArray(b *strings.Builder, a []any, indent int) {
	if len(a) == 0 {
		b.WriteString(styleJSONPunct.Render("[]"))
		return
	}
	b.WriteString(styleJSONPunct.Render("["))
	b.WriteByte('\n')
	pad := strings.Repeat("  ", indent+1)
	closePad := strings.Repeat("  ", indent)
	for i, v := range a {
		b.WriteString(pad)
		writeJSONValue(b, v, indent+1)
		if i < len(a)-1 {
			b.WriteString(styleJSONPunct.Render(","))
		}
		b.WriteByte('\n')
	}
	b.WriteString(closePad)
	b.WriteString(styleJSONPunct.Render("]"))
}

func toInt64(v any) int64 {
	switch x := v.(type) {
	case int:
		return int64(x)
	case int32:
		return int64(x)
	case int64:
		return x
	}
	return 0
}
