package edit

import (
	"strings"

	"github.com/charmbracelet/lipgloss"
)

var (
	diffStyleAdd     = lipgloss.NewStyle().Foreground(lipgloss.Color("2")) // green
	diffStyleRemove  = lipgloss.NewStyle().Foreground(lipgloss.Color("1")) // red
	diffStyleContext = lipgloss.NewStyle().Faint(true)
)

// Diff renders a line-based diff of old vs new with lipgloss styling.
// Returns the empty string when inputs are byte-equal. The algorithm is
// LCS-on-lines; output is one rendered line per diff entry, terminated
// by newlines.
//
// LCS is O(N*M) in the line count; pipeline subtrees are typically <50
// lines so the cost is negligible. The diff is intended for the abctl
// edit confirmation overlay — it shows the user every line that
// changed, in original order.
func Diff(old, new []byte) string {
	if string(old) == string(new) {
		return ""
	}
	oldLines := splitLinesKeepNewline(old)
	newLines := splitLinesKeepNewline(new)
	ops := lcsDiff(oldLines, newLines)
	var b strings.Builder
	for _, op := range ops {
		switch op.kind {
		case opEqual:
			b.WriteString(diffStyleContext.Render(" " + strings.TrimRight(op.line, "\n")))
			b.WriteByte('\n')
		case opRemove:
			b.WriteString(diffStyleRemove.Render("-" + strings.TrimRight(op.line, "\n")))
			b.WriteByte('\n')
		case opAdd:
			b.WriteString(diffStyleAdd.Render("+" + strings.TrimRight(op.line, "\n")))
			b.WriteByte('\n')
		}
	}
	return b.String()
}

func splitLinesKeepNewline(b []byte) []string {
	if len(b) == 0 {
		return nil
	}
	s := string(b)
	out := strings.SplitAfter(s, "\n")
	// strings.SplitAfter on "a\n" returns ["a\n", ""] — drop trailing empty.
	if len(out) > 0 && out[len(out)-1] == "" {
		out = out[:len(out)-1]
	}
	return out
}

type opKind int

const (
	opEqual opKind = iota
	opRemove
	opAdd
)

type diffOp struct {
	kind opKind
	line string
}

// lcsDiff is a textbook LCS-then-backtrack implementation. Returns the
// edit script as a sequence of equal/remove/add ops in order.
func lcsDiff(old, new []string) []diffOp {
	m, n := len(old), len(new)
	if m == 0 {
		out := make([]diffOp, n)
		for i, l := range new {
			out[i] = diffOp{opAdd, l}
		}
		return out
	}
	if n == 0 {
		out := make([]diffOp, m)
		for i, l := range old {
			out[i] = diffOp{opRemove, l}
		}
		return out
	}
	dp := make([][]int, m+1)
	for i := range dp {
		dp[i] = make([]int, n+1)
	}
	for i := 1; i <= m; i++ {
		for j := 1; j <= n; j++ {
			if old[i-1] == new[j-1] {
				dp[i][j] = dp[i-1][j-1] + 1
			} else if dp[i-1][j] >= dp[i][j-1] {
				dp[i][j] = dp[i-1][j]
			} else {
				dp[i][j] = dp[i][j-1]
			}
		}
	}
	var rev []diffOp
	i, j := m, n
	for i > 0 && j > 0 {
		switch {
		case old[i-1] == new[j-1]:
			rev = append(rev, diffOp{opEqual, old[i-1]})
			i--
			j--
		case dp[i-1][j] >= dp[i][j-1]:
			rev = append(rev, diffOp{opRemove, old[i-1]})
			i--
		default:
			rev = append(rev, diffOp{opAdd, new[j-1]})
			j--
		}
	}
	for i > 0 {
		rev = append(rev, diffOp{opRemove, old[i-1]})
		i--
	}
	for j > 0 {
		rev = append(rev, diffOp{opAdd, new[j-1]})
		j--
	}
	out := make([]diffOp, len(rev))
	for k, op := range rev {
		out[len(rev)-1-k] = op
	}
	return out
}
