package tui

import (
	"strings"
	"unicode"

	"github.com/charmbracelet/lipgloss"
)

// Series marks: a letter per series, on a coloured background.
//
// Replaces the shaded blocks (█ ▓ ▒ ░). Shading looked principled but failed in
// practice: █ against ▓ is nearly indistinguishable at a glance in most terminal
// fonts, so a reader could not tell which segment was which without counting
// against the legend. A letter is unambiguous at any size, and one derived from
// the label is self-describing — an `s` segment against a legend entry reading
// `s claude-sonnet-5` needs no decoding.
//
// Colour is layered on top for scanning speed, never as the encoding: the letter
// alone identifies the series, so the chart survives a monochrome terminal, a
// colour-vision deficiency, and a screenshot.
//
// seriesPalette is ordered so adjacent series differ in hue as well as letter.
// Reds are deliberately absent: red is reserved for the >= 400 status rule, and a
// series that happened to land on red would read as an error.
var seriesPalette = []lipgloss.AdaptiveColor{
	{Light: "#1D4ED8", Dark: "#93C5FD"}, // blue
	{Light: "#047857", Dark: "#6EE7B7"}, // green
	{Light: "#7C3AED", Dark: "#C4B5FD"}, // violet
	{Light: "#B45309", Dark: "#FCD34D"}, // amber
	{Light: "#0E7490", Dark: "#67E8F9"}, // cyan
	{Light: "#9D174D", Dark: "#F9A8D4"}, // magenta
}

// vendorPrefixes are leading tokens that identify a provider rather than a model,
// so they are skipped when deriving a letter. Without this every Anthropic model
// yields "c" for claude — the letters would collide precisely where a reader most
// needs them to differ.
//
// Matched case-insensitively against the first "-"/"_"/"/"/"." separated token.
var vendorPrefixes = map[string]bool{
	"claude": true, "anthropic": true, "openai": true, "gpt": true,
	"azure": true, "aws": true, "bedrock": true, "vertex": true,
	"google": true, "gemini": true, "meta": true, "llama": true,
	"mistral": true, "cohere": true, "ollama": true, "litellm": true,
}

// seriesLetter derives a one-character mark from a label.
//
// The rules, in order: skip a vendor prefix so sibling models differ; prefer a
// letter over a digit so "claude-sonnet-5" is `s` and not `5`; fall back to the
// first usable character; and use '?' when nothing qualifies. Callers dedupe the
// result — see assignLetters.
func seriesLetter(label string) rune {
	tokens := strings.FieldsFunc(label, func(r rune) bool {
		return r == '-' || r == '_' || r == '/' || r == '.'
	})
	if len(tokens) == 0 {
		return '?'
	}
	// Drop leading vendor tokens, not just one: "anthropic/claude-sonnet-5" has
	// two stacked, and stopping after the first still yields "c" for every Claude
	// model — the collision this exists to prevent. Always keep the last token so
	// a label that is nothing but vendor names still gets a mark.
	for len(tokens) > 1 && vendorPrefixes[strings.ToLower(tokens[0])] {
		tokens = tokens[1:]
	}
	// Prefer the first alphabetic character across the remaining tokens.
	for _, tok := range tokens {
		for _, r := range tok {
			if unicode.IsLetter(r) {
				return unicode.ToLower(r)
			}
		}
	}
	// No letters at all — a status code, say. Use its first character.
	for _, r := range tokens[0] {
		if unicode.IsPrint(r) {
			return r
		}
	}
	return '?'
}

// assignLetters maps each series label to a unique mark, in rank order.
//
// Uniqueness matters more than the mnemonic: two series sharing a letter is the
// failure the shaded blocks already had. When a derived letter collides, later
// characters of the label are tried, then the alphabet, then a digit — so the
// first (largest) series keeps the intuitive letter and the collision cost falls
// on the smaller one.
func assignLetters(series []seriesKey) map[string]rune {
	out := make(map[string]rune, len(series))
	used := make(map[rune]bool, len(series))

	claim := func(label string, r rune) bool {
		if r == 0 || used[r] {
			return false
		}
		used[r] = true
		out[label] = r
		return true
	}

	for _, s := range series {
		if claim(s.label, seriesLetter(s.label)) {
			continue
		}
		// Try the label's own remaining letters before falling back, so the mark
		// stays connected to the name where possible.
		done := false
		for _, r := range strings.ToLower(s.label) {
			if unicode.IsLetter(r) && claim(s.label, r) {
				done = true
				break
			}
		}
		if done {
			continue
		}
		for r := 'a'; r <= 'z'; r++ {
			if claim(s.label, r) {
				done = true
				break
			}
		}
		if done {
			continue
		}
		for r := '0'; r <= '9'; r++ {
			if claim(s.label, r) {
				break
			}
		}
	}
	return out
}

// seriesStyle returns the style for a series' mark: a coloured background with a
// readable foreground.
//
// An error series (>= 400 under group=status) always takes the error colour,
// overriding its palette slot — a 500 must look like a failure regardless of
// where it ranks.
func seriesStyle(rank int, isError bool) lipgloss.Style {
	bg := seriesPalette[rank%len(seriesPalette)]
	if isError {
		bg = colorError
	}
	// Bold on a coloured ground: terminals vary in how they render dim text on a
	// background, and the mark has to stay legible in all of them.
	return lipgloss.NewStyle().Background(bg).Foreground(colorOnSeries).Bold(true)
}
