package main

import (
	"errors"
	"fmt"
	"log/slog"
	"os"
	"path/filepath"
)

// Everything Cortex writes for a user lives under ~/.cortex, so a laptop ends up
// with exactly one directory holding config, CA and keys rather than a CA
// scattered into whichever directory each command happened to run from.
const (
	cortexDirName = ".cortex"
	// localConfigName is the one config a local install has. There is deliberately
	// no second "cost-optimised" config: this one already carries both the parsers
	// and tool-prune, and writeBuiltinConfig preserves edits, so filling in
	// tool-prune's remove list is all the difference ever amounted to. Two configs
	// meant two CAs, two sets of paths, and two pages of instructions that read
	// identically.
	localConfigName = "config.yaml"
	// caDirName holds the bridge CA. Separate from the config so one directory
	// listing distinguishes "your settings" from "generated key material".
	caDirName = "ca"
	// localDirFallback is the directory --local used before the CA moved under
	// $HOME. It is no longer written to; it survives so main.go can spot a stale
	// one left in a working directory and warn that the client's trust anchor
	// needs updating.
	localDirFallback = "cortex-ca"
)

// defaultCortexDir returns ~/.cortex, or an error if there is no resolvable home
// directory.
//
// This used to be cwd-relative unconditionally, on the reasoning that no
// absolute path should be baked into the binary. Resolving $HOME at runtime
// satisfies that while keeping the private key in one predictable place — and
// the cwd default had a real cost: it dropped a CA and private key into
// whatever directory the proxy was started from, including checkouts.
//
// It returns an error rather than falling back to a relative path, because that
// fallback silently reintroduced exactly that bug: no $HOME meant the key landed
// in the working directory again, with nothing said about it. UserHomeDir only
// fails when $HOME is unset — a bare `env -i`, some systemd units, a scratch
// container — so failing loudly costs nothing anyone hits by accident, and
// --ca-dir remains available for those cases.
func defaultCortexDir() (string, error) {
	home, err := os.UserHomeDir()
	if err != nil || home == "" {
		return "", fmt.Errorf("cannot determine your home directory (is $HOME set?); "+
			"pass --ca-dir to choose where the CA is written: %w", err)
	}
	return filepath.Join(home, cortexDirName), nil
}

// builtinConfigYAML returns the built-in --local config with caDir interpolated: a
// forward-only proxy with the TLS bridge on (auto-generated CA in caDir) and
// the LLM / MCP / A2A parsers, so an agent's egress is decrypted and parsed.
// Kept in sync with the root README.
//
// Every listener is pinned to loopback on an uncommon port. This runs on a
// laptop, so (a) a wildcard bind would expose an open forward proxy, the stats
// endpoint, the health endpoint, and the unauthenticated session API (which
// carries decrypted bodies and any injected tokens) to the LAN, and (b) the
// usual 8081/909x ports collide with common dev tools. The preset only fills
// empty addresses, so these explicit values win — keep them in sync with the
// ports the installer probes and prints (authbridge/install.sh). The
// enforce-redirect transparent listener isn't used here (no iptables); --local
// skips it, and it is pinned anyway so that starting this same file with
// --config cannot bind it on every interface.
//
// The YAML body is flush-left on purpose — a raw string literal preserves
// leading whitespace, so indenting these lines in source would corrupt the YAML.
func builtinConfigYAML(caDir string) string {
	return `# Built-in config for: authbridge-proxy --local
# Forward-only proxy + TLS bridge (auto-generated CA) + LLM/MCP/A2A parsers.
# The running proxy watches this file — edit it to hot-reload.
mode: proxy-sidecar
listener:
  roles: [forward]
  # Every listener binds 127.0.0.1, including any this file does not name. The
  # explicit ports below pick a non-colliding range; this line is what makes an
  # unnamed or newly added listener safe on a laptop, where a wildcard bind means
  # the Wi-Fi.
  bind_loopback_only: true
  forward_proxy_addr: 127.0.0.1:47600
  session_api_addr: 127.0.0.1:47601
  # Without this the preset defaults health to ":9091" — every interface, and a
  # port common enough to collide with an unrelated service.
  health_addr: 127.0.0.1:47604
  # --local skips the enforce-redirect transparent listener, but --config does
  # not, and the troubleshooting docs tell people to start this same file with
  # --config. Unpinned it would then bind ":8082" on every interface. Pinning it
  # makes the config safe however it is launched.
  transparent_proxy_addr: 127.0.0.1:47603
stats:
  address: 127.0.0.1:47602
tls_bridge:
  mode: enabled
  ca_dir: "` + caDir + `"
  generate_ca: true
pipeline:
  outbound:
    plugins:
      - name: inference-parser
      - name: mcp-parser
      - name: a2a-parser
      # tool-prune drops unused tool definitions from the outbound manifest.
      # The empty remove list is the off switch: with nothing named it does
      # nothing at all. Fill it in and it takes effect immediately --
      #   abctl tools scan --write <this file>
      # -- and the config is hot-reloaded, so no restart.
      #
      # Watch the Metrics section of abctl's plugin pane for what it saved. If
      # you ever suspect the plugin of breaking a request, set
      # on_error: observe here: it then counts what it *would* remove while
      # leaving every byte on the wire untouched, which settles the question
      # without unconfiguring anything.
      #
      # Keep it last: it rewrites the request body, and body readers must
      # precede the mutator so they see the original bytes.
      - name: tool-prune
        on_error: enforce
        config:
          remove: []
`
}

// writeBuiltinConfig ensures the built-in --local config exists next to the CA (in
// caDir) and returns its path, so --local reuses the normal file-based load +
// hot-reload path. caDir is caller-resolved (cwd-relative by default, or
// --ca-dir); no absolute path is baked into the binary.
//
// An existing file is KEPT, not overwritten. The config's own header invites
// editing it, and `abctl tools scan --write` writes a prune list into it — and
// this function runs before any port is bound, so an unconditional write meant
// that even a --local start which then failed on a port clash silently destroyed
// those edits. Delete the file to regenerate the preset.
// writeBuiltinConfig ensures cortexDir/config.yaml exists, pointing at caDir for
// the CA, and returns its path.
//
// An existing file is never rewritten. That is what makes this the only config a
// local install needs: the prune list, the on_error policy and any hand edit all
// survive a restart, so there is nothing for a second "persistent" config to do.
func writeBuiltinConfig(cortexDir, caDir string) (string, error) {
	// 0700: caDir under here holds the CA's private key.
	if err := os.MkdirAll(cortexDir, 0o700); err != nil {
		return "", err
	}
	// MkdirAll leaves an existing directory's mode alone, so a ~/.cortex created
	// before this (or by another tool) would stay 0755 and the perms claim would
	// be true only for fresh installs. install.sh already chmods it; this makes a
	// bare `authbridge-proxy --local` match.
	if err := os.Chmod(cortexDir, 0o700); err != nil {
		return "", err
	}
	// Same reasoning one level down. tlsbridge creates caDir 0700, but MkdirAll does
	// not tighten an existing directory, so a caDir left at 0755 by an earlier build
	// stays 0755 while holding the CA's private key. The key file itself is 0600, so
	// this is defence in depth rather than the only guard — but it should not be the
	// parent's mode alone standing between a MITM CA key and every process on the
	// machine. Only when it already exists: letting tlsbridge create it keeps the
	// Kubernetes case (a mounted ca_dir, deliberately left alone) untouched.
	if fi, err := os.Stat(caDir); err == nil && fi.IsDir() {
		if err := os.Chmod(caDir, 0o700); err != nil {
			return "", err
		}
	}
	path := filepath.Join(cortexDir, localConfigName)
	if _, err := os.Stat(path); err == nil {
		slog.Info("local mode — keeping the existing config (edits and any prune list are preserved)",
			"path", path, "hint", "delete it to regenerate the built-in preset")
		return path, nil
	} else if !errors.Is(err, os.ErrNotExist) {
		return "", err
	}
	// 0600, not 0644: it names the CA's location and the whole listener layout, and
	// abctl's migration already rewrites it 0600 — a fresh install should not be the
	// looser of the two.
	if err := os.WriteFile(path, []byte(builtinConfigYAML(caDir)), 0o600); err != nil {
		return "", err
	}
	return path, nil
}
