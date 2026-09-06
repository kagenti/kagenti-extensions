// Package toolscan derives a tool-prune candidate list from local Claude Code
// transcripts.
//
// The central limitation is structural: transcripts record tools that were
// *called*, never tools that were *offered*. A configured-but-never-invoked
// tool leaves no trace at all. So the scan cannot enumerate the manifest — it
// can only intersect "tools we know Claude Code ships" with "tools this user
// never called".
//
// That shapes the safety rule: a name the scan has never heard of is always
// kept. Removing a tool the model needs is the harmful direction of failure;
// carrying a few extra definitions is merely expensive. Drift in the table
// below therefore costs savings, never correctness.
package toolscan

// knownTools is the set of Claude Code built-in tool names the scanner is
// willing to propose for removal. Membership is a claim that the tool is
// bundled and that its absence is safe when it is never called.
//
// Deliberately conservative. Tools that gate control flow (ExitPlanMode),
// carry state the model relies on (TodoWrite), or are the primary means of
// doing work (Bash, Read, Edit, Write, Glob, Grep) are omitted entirely, so
// they can never be proposed however long they sit unused in a window.
var knownTools = []string{
	"Artifact",
	"AskUserQuestion",
	"BashOutput",
	"CronCreate",
	"CronDelete",
	"CronList",
	"DesignSync",
	"EndConversation",
	"EnterWorktree",
	"ExitWorktree",
	"KillShell",
	"LSP",
	"ListAgents",
	"Monitor",
	"NotebookEdit",
	"PushNotification",
	"ReportFindings",
	"ScheduleWakeup",
	"SendFeedback",
	"SendMessage",
	"SlashCommand",
	"TaskOutput",
	"TaskStop",
	"WebFetch",
	"WebSearch",
	"Workflow",
}

// implies covers tools whose use is indirect: the transcript shows the driver
// being called, not the tool it depends on. Keeping the right-hand side
// whenever the left-hand side was called prevents the scan from proposing a
// tool that is reachable but never appears by name.
var implies = map[string][]string{
	"Agent":    {"SendMessage", "ListAgents", "TaskOutput", "TaskStop"},
	"Task":     {"SendMessage", "ListAgents", "TaskOutput", "TaskStop"},
	"Monitor":  {"TaskOutput", "TaskStop"},
	"Bash":     {"BashOutput", "KillShell"},
	"Workflow": {"TaskOutput", "TaskStop"},
	"Artifact": {"DesignSync"},
}

// KnownTools returns a copy of the candidate universe.
func KnownTools() []string { return append([]string(nil), knownTools...) }
