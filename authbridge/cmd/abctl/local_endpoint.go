package main

import (
	"net"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"time"

	"github.com/rossoctl/cortex/authbridge/authlib/config"
)

// localProbeTimeout bounds the "is a local Cortex actually up?" check. It runs
// before the TUI starts, on the happy path of every bare `abctl`, so it has to be
// short enough not to feel like a hang.
const localProbeTimeout = 400 * time.Millisecond

// localSessionEndpoint returns the session API URL of the Cortex installed on
// this machine, or "" if there isn't one.
//
// Read from the config rather than hardcoded so it follows a port the operator
// changed. The in-cluster default is 9094 and a local install uses 47601, which
// is exactly the kind of difference a constant gets wrong.
func localSessionEndpoint() string {
	home, err := os.UserHomeDir()
	if err != nil || home == "" {
		return ""
	}
	cfg, err := config.Load(filepath.Join(home, ".cortex", "config.yaml"))
	if err != nil {
		return ""
	}
	addr := cfg.Listener.SessionAPIAddr
	if addr == "" {
		return ""
	}
	// SplitHostPort rather than strings.Cut, which splits at the first colon and
	// mangles "[::1]:9094" into host="[" — producing a URL that simply fails to
	// connect, after which abctl falls silently through to the cluster picker.
	host, port, err := net.SplitHostPort(addr)
	if err != nil || port == "" {
		return ""
	}
	// A bind address is not a dial address: ":9094", "0.0.0.0:9094" and
	// "[::]:9094" all need a host a client can connect to.
	if host == "" || host == "0.0.0.0" || host == "::" {
		host = "localhost"
	}
	return "http://" + net.JoinHostPort(host, port)
}

// localSessionAPIUp reports whether something is listening and answering there.
//
// Checked before choosing it over the cluster picker: a stale ~/.cortex/config.yaml
// left by an install that is no longer running must not hijack `abctl` away from
// the picker for someone working against a cluster.
func localSessionAPIUp(endpoint string) bool {
	if endpoint == "" {
		return false
	}
	c := &http.Client{Timeout: localProbeTimeout}
	resp, err := c.Get(endpoint + "/v1/sessions") //nolint:noctx // bounded by Timeout
	if err != nil {
		return false
	}
	defer resp.Body.Close()
	// Only 2xx. The session API answers GET /v1/sessions with 200, so anything
	// else — a 404 from an unrelated service that happens to hold the port — is
	// not ours, and selecting it would send abctl somewhere useless instead of to
	// the cluster picker.
	return resp.StatusCode >= 200 && resp.StatusCode < 300
}

// dialable is a cheap pre-check used only to keep the error message useful when
// the config exists but nothing is running.
func dialable(endpoint string) bool {
	hostport := strings.TrimPrefix(strings.TrimPrefix(endpoint, "http://"), "https://")
	conn, err := net.DialTimeout("tcp", hostport, localProbeTimeout)
	if err != nil {
		return false
	}
	_ = conn.Close()
	return true
}
