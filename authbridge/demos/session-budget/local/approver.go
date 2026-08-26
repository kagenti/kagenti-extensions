// approver.go is a demo-only HITL approver for the session-budget
// plugin's `on_exceed: pause` mode. It listens on an HTTP port, prints
// each incoming pause request, prompts the operator for [a]pprove or
// [d]eny, and returns the matching JSON response.
//
// Run:
//
//	go run demos/session-budget/local/approver.go
//	go run demos/session-budget/local/approver.go --auto-approve
//	go run demos/session-budget/local/approver.go --addr 127.0.0.1:7000
package main

import (
	"bufio"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"net/http"
	"os"
	"strings"
	"sync"
	"time"
)

// pauseRequest mirrors the wire type in
// authbridge/authlib/plugins/sessionbudget/plugin.go.
type pauseRequest struct {
	SessionID       string `json:"session_id"`
	Reason          string `json:"reason"`
	SpentTokens     int64  `json:"spent_tokens"`
	SpentCalls      int64  `json:"spent_calls"`
	TokenLimit      int64  `json:"token_limit"`
	CallLimit       int64  `json:"call_limit"`
	DurationSeconds int64  `json:"duration_seconds,omitempty"`
	DurationLimit   int64  `json:"duration_limit,omitempty"`
}

func main() {
	addr := flag.String("addr", "127.0.0.1:9099", "listen address")
	autoApprove := flag.Bool("auto-approve", false, "skip the prompt and always approve")
	autoDeny := flag.Bool("auto-deny", false, "skip the prompt and always deny")
	flag.Parse()

	if *autoApprove && *autoDeny {
		fmt.Fprintln(os.Stderr, "approver: --auto-approve and --auto-deny are mutually exclusive")
		os.Exit(2)
	}

	// Serialize prompts so concurrent pause requests queue rather than
	// interleave keystrokes on stdin.
	var promptMu sync.Mutex
	stdin := bufio.NewReader(os.Stdin)

	http.HandleFunc("/", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
			return
		}
		raw, err := io.ReadAll(io.LimitReader(r.Body, 64<<10))
		if err != nil {
			http.Error(w, "read body: "+err.Error(), http.StatusBadRequest)
			return
		}
		var pr pauseRequest
		if err := json.Unmarshal(raw, &pr); err != nil {
			http.Error(w, "decode: "+err.Error(), http.StatusBadRequest)
			return
		}

		action := decide(&pr, &promptMu, stdin, *autoApprove, *autoDeny)

		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]string{"action": action})
	})

	fmt.Printf("approver listening on %s (auto-approve=%v, auto-deny=%v)\n",
		*addr, *autoApprove, *autoDeny)
	srv := &http.Server{
		Addr:              *addr,
		ReadHeaderTimeout: 5 * time.Second,
		ReadTimeout:       10 * time.Second,
	}
	if err := srv.ListenAndServe(); err != nil {
		fmt.Fprintln(os.Stderr, "approver:", err)
		os.Exit(1)
	}
}

func decide(pr *pauseRequest, mu *sync.Mutex, stdin *bufio.Reader, autoApprove, autoDeny bool) string {
	mu.Lock()
	defer mu.Unlock()

	fmt.Println()
	fmt.Println("─── pause request ───")
	fmt.Printf("  session: %s\n", pr.SessionID)
	fmt.Printf("  reason:  %s\n", pr.Reason)
	fmt.Printf("  calls:   %d / %d\n", pr.SpentCalls, pr.CallLimit)
	fmt.Printf("  tokens:  %d / %d\n", pr.SpentTokens, pr.TokenLimit)
	if pr.DurationLimit > 0 {
		fmt.Printf("  age:     %ds / %ds\n", pr.DurationSeconds, pr.DurationLimit)
	}

	switch {
	case autoApprove:
		fmt.Println("  → approve (auto)")
		return "approve"
	case autoDeny:
		fmt.Println("  → deny (auto)")
		return "deny"
	}

	fmt.Print("  [a]pprove / [d]eny (default: approve): ")
	line, err := stdin.ReadString('\n')
	if err != nil {
		fmt.Printf("  (stdin closed: %v — failing closed to deny; use --auto-approve for unattended approvals)\n", err)
		return "deny"
	}
	line = strings.TrimSpace(strings.ToLower(line))
	if strings.HasPrefix(line, "d") {
		fmt.Println("  → deny")
		return "deny"
	}
	fmt.Println("  → approve")
	return "approve"
}
