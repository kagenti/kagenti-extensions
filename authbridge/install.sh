#!/bin/sh
# install.sh — one-line installer for Cortex on a local machine.
#
#   curl -fsSL https://raw.githubusercontent.com/rossoctl/cortex/main/authbridge/install.sh | sh
#
# Detects your OS/arch, downloads the prebuilt `abctl` and `authbridge-proxy`
# binaries for the newest release, verifies their SHA-256 checksums, installs
# them to ~/.local/bin, and starts Cortex in the background — then prints the
# commands to watch traffic and point an agent at it, plus how to stop it.
# macOS + Linux, amd64 + arm64. No cluster, Keycloak, or SPIRE needed.
#
# It installs, starts Cortex with its built-in config in ~/.cortex, and prints the
# command to send an agent through it. Traffic is decrypted and parsed for viewing;
# nothing is rewritten. Cutting Claude Code's token cost is one opt-in command
# afterwards, printed at the end.
#
# Options (pass through the pipe with `sh -s --`, e.g.
#   curl -fsSL ...install.sh | sh -s -- --install-only):
#
#   --install-only   install the binaries and stop
#   --claude-code    after starting, offer to write the three env vars Claude Code
#                    needs into ~/.claude/settings.json, so it runs as plain
#                    `claude`. Prompts before changing anything.
#
# There is deliberately only one config. It carries the parsers AND tool-prune,
# and the proxy preserves edits to it, so a second "cost-optimised" config had
# nothing to do that filling in one list did not already do — while costing a
# second CA, a second set of paths, and a second page of instructions that read
# identically to the first.
#
# No compatibility aliases here: this script accepted no flags at all until now,
# so there is no earlier spelling for anyone to still be using. (The proxy's
# --demo -> --local alias is different: that flag really did ship.)
#
# Flags rather than env vars: written `VAR=1 curl ... | sh` the variable reaches
# curl, not sh, so the script runs without it. `sh -s -- --flag` has no such
# failure mode. The env vars below still work.
#
# By default this script re-runs the copy from the newest RELEASE rather than
# executing whatever is currently on main — main is unstable by definition, and a
# `curl | sh` should not be the first thing to run a change nobody has released.
# --ref=main opts back in; --ref=vX.Y.Z pins.
#
# Environment:
#   AUTHBRIDGE_REF=REF          same as --ref
#   AUTHBRIDGE_VERSION=vX.Y.Z   install binaries from a specific release
#                               (default: the release this script came from)
#   AUTHBRIDGE_INSTALL_ONLY=1   same as --install-only
#   AUTHBRIDGE_SKIP_DOWNLOAD=1  use the already-installed binaries in ~/.local/bin
#                               instead of downloading (re-run setup offline)
# set -eu, not -euo pipefail: this is POSIX sh (the documented entry point is
# `curl ... | sh`), and `pipefail` is a bashism that would abort the script under
# dash/ash. The repo-wide `set -euo pipefail` convention applies to bash scripts.
set -eu

REPO="rossoctl/cortex"
BIN_DIR="${HOME}/.local/bin"
# Every file Cortex writes for this user lives here: config, CA, keys, logs,
# pidfiles. One directory to inspect, back up, or delete.
CORTEX_DIR="${HOME}/.cortex"
case "$(uname -s)" in
	Darwin) SUPERVISOR_NAME="launchd user agent" ;;
	*) SUPERVISOR_NAME="systemd user unit" ;;
esac

info() { printf '%s\n' "$*"; }
warn() { printf 'warning: %s\n' "$*" >&2; }
die()  { printf 'error: %s\n' "$*" >&2; exit 1; }

# usage is a heredoc rather than sed over "$0": piped as `curl ... | sh -s -- --help`
# the script has no file to read ($0 is "sh"), so the previous version printed
# nothing at all — for the one flag someone is most likely to try before running an
# installer they piped from the internet.
usage() {
	cat <<'USAGE'
install.sh — install Cortex on a local machine (macOS/Linux, amd64/arm64).

Usage:
  curl -fsSL https://raw.githubusercontent.com/rossoctl/cortex/main/authbridge/install.sh | sh
  curl -fsSL ...install.sh | sh -s -- [option]

Installs abctl and authbridge-proxy to ~/.local/bin, starts the proxy with its
built-in config in ~/.cortex, and prints the command to send an agent through it.
Traffic is decrypted and parsed for viewing; nothing is rewritten.

Options:
  --install-only   install the binaries and stop
  --claude-code    after starting, offer to configure Claude Code to use it, so
                   it runs as plain `claude` with no environment variables
  --local          the default, spelled out
  --yes, -y        do not prompt; answer yes to configuring Claude Code
  --ref=REF        take THIS SCRIPT from a git ref instead of the newest release
                   (e.g. --ref=main for unreleased changes, --ref=v0.7.0-alpha.4
                   to pin). Binaries come from the same release unless
                   AUTHBRIDGE_VERSION says otherwise.
  -h, --help       this text

Environment:
  AUTHBRIDGE_VERSION=vX.Y.Z   install a specific release tag (default: newest)
  AUTHBRIDGE_INSTALL_ONLY=1   same as --install-only
  AUTHBRIDGE_SKIP_DOWNLOAD=1  use the binaries already in ~/.local/bin instead of
                              downloading (re-run setup offline)

After installing, to cut Claude Code's token cost:
  abctl tools scan --write ~/.cortex/config.yaml
USAGE
}

# --- mode selection ---
MODE=local
WIRE_CLAUDE_CODE=""
ASSUME_YES=""
for arg in "$@"; do
	case "$arg" in
		--install-only) MODE=install-only ;;
		--claude-code) WIRE_CLAUDE_CODE=1 ;;
		--yes | -y) ASSUME_YES=1 ;;
		--ref=*) AUTHBRIDGE_REF="${arg#*=}" ;;
		# --local is the default; accepted so writing it out explicitly works, and
		# so it mirrors the proxy flag of the same name.
		--local) MODE=local ;;
		-h | --help)
			usage
			exit 0
			;;
		*) die "unknown option: $arg (try --claude-code, --install-only, --local, --ref=REF, --yes, or no argument)" ;;
	esac
done
# Env form kept working; the flag wins if both are given.
if [ "${AUTHBRIDGE_INSTALL_ONLY:-}" = "1" ] && [ "$MODE" = "local" ]; then
	MODE=install-only
fi

command -v curl >/dev/null 2>&1 || die "curl is required"
command -v tar  >/dev/null 2>&1 || die "tar is required"

# newest_release prints the newest release tag, prereleases included.
# `releases/latest` excludes prereleases and this project ships them, so list
# releases (newest first) and take the first tag_name.
newest_release() {
	# Returns non-zero on empty. The pipeline ends in sed, which exits 0 for empty
	# input, so `version=$(newest_release) || die` could never fire on a
	# rate-limited or offline API — the caller got an empty tag and failed later
	# with an unactionable "download failed: abctl__darwin_arm64.tar.gz".
	_tag=$(curl -fsSL "https://api.github.com/repos/${REPO}/releases?per_page=1" 2>/dev/null \
		| grep -m1 '"tag_name"' | sed -e 's/.*"tag_name": *"//' -e 's/".*//')
	[ -n "${_tag}" ] || return 1
	printf '%s\n' "${_tag}"
}

# ere_escape quotes the ERE metacharacters in a literal so it matches exactly.
# Archive names contain dots, and an unescaped "." matches any character: the
# pattern for abctl_v0.7.0-alpha.3_..tar.gz also accepted
# abctl_v0X7X0-alpha_3_..Xtar.gz. Nothing exploitable followed — the count check
# or sha_check rejected it — but this script's whole subject is precision here.
ere_escape() {
	# shellcheck disable=SC2016 # the sed script is literal on purpose
	printf '%s' "$1" | sed 's/[].[^$()*+?{}|\\]/\\&/g'
}

# --- run the released copy of this script, not the one from main ---
#
# The documented command fetches this file from main, which is whatever landed
# last: an unreviewed or half-finished change there runs on someone's laptop
# immediately. Releases are tested, so by default this bootstrap re-runs the copy
# from the newest release and hands it the same arguments.
#
# SCRIPT_REF names the ref this copy came from and doubles as the recursion guard:
# the child sees it set and does not bootstrap again.
SCRIPT_REF="${AUTHBRIDGE_SCRIPT_REF:-}"
if [ -z "${SCRIPT_REF}" ]; then
	want_ref="${AUTHBRIDGE_REF:-}"
	if [ -z "${want_ref}" ]; then
		want_ref="$(newest_release)" || true
	fi
	if [ -z "${want_ref}" ]; then
		warn "could not resolve the newest release; continuing with the copy from main"
		SCRIPT_REF="main"
	elif [ "${want_ref}" = "main" ]; then
		# Explicitly asked for main: this copy already is main.
		SCRIPT_REF="main"
	else
		# Rebuild the argument list without --ref: it is meta, consumed here, and a
		# released script from before --ref existed rejects it as an unknown option.
		# Rotating the positional parameters keeps arguments with spaces intact,
		# which building a string would not.
		argc=$#
		argi=0
		while [ "${argi}" -lt "${argc}" ]; do
			a="$1"
			shift
			argi=$((argi + 1))
			case "$a" in
				--ref=*) ;;
				*) set -- "$@" "$a" ;;
			esac
		done

		boot=$(mktemp)
		url="https://raw.githubusercontent.com/${REPO}/${want_ref}/authbridge/install.sh"
		# Capture the status code rather than collapsing every failure into one
		# branch. A 404 means that ref genuinely predates this script — fall back.
		# A transport error means we could not ask, and silently dropping to main
		# there would break the exact guarantee this bootstrap exists to give.
		# On a transport failure curl still prints "000" via -w AND exits non-zero,
		# so appending our own default produced "HTTP 000000". Overwrite instead.
		http=$(curl -sSL -o "${boot}" -w '%{http_code}' "${url}" 2>/dev/null) || http="000"
		[ -n "${http}" ] || http="000"
		if [ "${http}" = "200" ] && [ -s "${boot}" ]; then
			info "Using the installer from ${want_ref}."
			# set -e would abort the parent on a non-zero child before any of the
			# lines below ran, leaking the downloaded script on every failed
			# install. The if/else keeps the status and still cleans up.
			if AUTHBRIDGE_SCRIPT_REF="${want_ref}" sh "${boot}" "$@"; then
				status=0
			else
				status=$?
			fi
			rm -f "${boot}"
			exit "${status}"
		fi
		rm -f "${boot}"
		if [ "${http}" = "404" ]; then
			# A release from before this script existed under that name. Falling
			# back beats refusing to install, but name the copy that is running so
			# a surprise is attributable.
			warn "${want_ref} has no authbridge/install.sh (HTTP 404); continuing with the copy from main"
			SCRIPT_REF="main"
		else
			# Blocked, offline, rate-limited, proxied, 5xx. We cannot tell whether a
			# released installer exists, so do not quietly run main instead.
			die "could not fetch the installer for ${want_ref} (HTTP ${http}) from ${url}.
  Check the network, or choose explicitly:
    --ref=main       run the copy from main (unreleased changes)
    --ref=vX.Y.Z     use a specific release"
		fi
	fi
fi

# Verify the checklist file passed as $1 (run from the directory holding the
# files). shasum is preferred: it's always present on macOS and its -c reads the
# GNU-style checksums.txt reliably, whereas some non-GNU sha256sum builds reject
# -c. Linux without shasum falls back to sha256sum (GNU coreutils).
sha_check() {
	if command -v shasum >/dev/null 2>&1; then
		shasum -a 256 -c "$1"
	elif command -v sha256sum >/dev/null 2>&1; then
		sha256sum -c "$1"
	else
		die "need shasum or sha256sum to verify downloads"
	fi
}

# Demo listener ports — loopback, and deliberately uncommon to avoid colliding
# with common dev tools. Keep in sync with the built-in config in
# authbridge/cmd/authbridge-proxy/local.go.
DEMO_FORWARD_PORT=47600
DEMO_SESSION_PORT=47601
DEMO_STATS_PORT=47602
# Bound too, and previously missing from the preflight — an occupied 47604 let the
# download finish and then killed the proxy during startup.
DEMO_HEALTH_PORT=47604

# port_in_use exits 0 if something is already listening on the given loopback
# port. Best-effort: uses lsof, then nc; if neither exists, it assumes free.
port_in_use() {
	if command -v lsof >/dev/null 2>&1; then
		lsof -nP -iTCP@127.0.0.1:"$1" -sTCP:LISTEN >/dev/null 2>&1
	elif command -v nc >/dev/null 2>&1; then
		nc -z 127.0.0.1 "$1" >/dev/null 2>&1
	else
		return 1
	fi
}

# --- detect platform ---
os=$(uname -s)
case "$os" in
	Darwin) os=darwin ;;
	Linux) os=linux ;;
	*) die "unsupported OS: $os (the installer supports macOS and Linux)" ;;
esac

arch=$(uname -m)
case "$arch" in
	x86_64 | amd64) arch=amd64 ;;
	arm64 | aarch64) arch=arm64 ;;
	*) die "unsupported architecture: $arch (supported: amd64, arm64)" ;;
esac

# --- preflight: fail early (before downloading) if a listener port is taken ---
if [ "$MODE" = "local" ]; then
	# A Cortex of ours holding these ports is fine — `abctl service install` adopts
	# it, and keeping a second copy of that narrow "is this pid really ours" check
	# here would only let the two drift. This probe is for a FOREIGN listener, and
	# it runs before the download so the failure is early and cheap.
	for p in "$DEMO_FORWARD_PORT" "$DEMO_SESSION_PORT" "$DEMO_STATS_PORT" "$DEMO_HEALTH_PORT"; do
		if port_in_use "$p"; then
			if [ -f "${CORTEX_DIR}/config.yaml" ]; then
				# Ours, most likely: let abctl adopt it rather than refusing here.
				continue
			fi
			die "port ${p} is already in use by something else. Free it, or change the ports in ${CORTEX_DIR}/config.yaml, then re-run."
		fi
	done
fi

# --- skip the download entirely when asked (offline re-run) ---
if [ "${AUTHBRIDGE_SKIP_DOWNLOAD:-}" = "1" ]; then
	for b in abctl authbridge-proxy; do
		[ -x "${BIN_DIR}/${b}" ] || die "AUTHBRIDGE_SKIP_DOWNLOAD=1 but ${BIN_DIR}/${b} is missing"
	done
	version="already installed"
	info "Using the binaries already in ${BIN_DIR}"
else

# --- resolve the release tag ---
version="${AUTHBRIDGE_VERSION:-}"
if [ -z "$version" ]; then
	# Default the binaries to the same release this script came from, so the
	# script and the binaries it installs are one tested set rather than two
	# independently-moving things.
	case "${SCRIPT_REF}" in
		v*) version="${SCRIPT_REF}" ;;
		*)
			info "Resolving newest release..."
			version=$(newest_release) || die "could not resolve the newest release (set AUTHBRIDGE_VERSION=vX.Y.Z)"
			;;
	esac
fi
info "Release: $version"

# --- download + verify ---
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

base="https://github.com/${REPO}/releases/download/${version}"
abctl_tgz="abctl_${version}_${os}_${arch}.tar.gz"
proxy_tgz="authbridge-proxy_${version}_${os}_${arch}.tar.gz"

info "Downloading binaries for ${os}/${arch}..."
curl -fsSL "${base}/${abctl_tgz}" -o "${tmp}/${abctl_tgz}" || die "download failed: ${abctl_tgz}"
curl -fsSL "${base}/${proxy_tgz}" -o "${tmp}/${proxy_tgz}" || die "download failed: ${proxy_tgz}"
curl -fsSL "${base}/checksums.txt" -o "${tmp}/checksums.txt" || die "download failed: checksums.txt"

info "Verifying checksums..."
# One grep per archive, not an alternation. An alternation SUCCEEDS on a single
# match, so a checksums.txt missing one entry — a truncated or partly-generated
# release build — passed the guard, sha_check verified only the file that was
# listed, and the UNVERIFIED binary was installed anyway. This is the one step
# whose whole job is not to fail open.
#
# Matching is anchored to end-of-line so an unrelated future artifact in
# checksums.txt can't make verification fail on a file we never fetched.
: > "${tmp}/checksums.filtered"
for archive in "${abctl_tgz}" "${proxy_tgz}"; do
	# The name may be preceded by whitespace, sha256sum's binary-mode "*", or a
	# path component: the release workflow runs `sha256sum ./*.tar.gz`, so every
	# real line reads "HASH  ./abctl_....tar.gz". An earlier version of this
	# pattern required the name immediately after whitespace or "*", which matched
	# nothing against an actual release and refused every install.
	# Anchored to the whole line and to the exact shape our own workflow emits:
	# "HASH  ./name" (from `cd dist && sha256sum ./*.tar.gz`), with a bare name and
	# binary-mode "*" also accepted.
	#
	# Deliberately NOT any path. sha_check runs from ${tmp}, so a permissive class
	# let a crafted entry like "HASH  ../name" or "HASH  /etc/name" match and be
	# verified against a file outside the download directory — passing verification
	# for something other than the archive we then extract. Only ./ and a bare name
	# are ours, so nothing else is accepted.
	archive_re=$(ere_escape "${archive}")
	grep -E "^[0-9a-fA-F]+[[:space:]]+\*?(\./)?${archive_re}\$" "${tmp}/checksums.txt" \
		>> "${tmp}/checksums.filtered" \
		|| die "checksums.txt has no usable entry for ${archive} — refusing to install it unverified"
done
# Both entries present, and exactly the two we asked for.
lines=$(wc -l < "${tmp}/checksums.filtered" | tr -d '[:space:]')
[ "${lines}" = "2" ] \
	|| die "expected 2 checksum entries, got ${lines} — refusing to install"
( cd "$tmp" && sha_check checksums.filtered ) || die "checksum verification failed"

# --- extract + install ---
info "Installing to ${BIN_DIR}..."
mkdir -p "$BIN_DIR"
tar -xzf "${tmp}/${abctl_tgz}" -C "$tmp"
tar -xzf "${tmp}/${proxy_tgz}" -C "$tmp"
for b in abctl authbridge-proxy; do
	[ -f "${tmp}/${b}" ] || die "archive did not contain expected binary: ${b}"
	chmod +x "${tmp}/${b}"
	mv -f "${tmp}/${b}" "${BIN_DIR}/${b}"
done

# macOS: clear the quarantine flag so Gatekeeper doesn't block the unsigned binaries.
if [ "$os" = "darwin" ] && command -v xattr >/dev/null 2>&1; then
	xattr -dr com.apple.quarantine "${BIN_DIR}/abctl" "${BIN_DIR}/authbridge-proxy" 2>/dev/null || true
fi

rm -rf "$tmp"
trap - EXIT
fi # end of download block

# --- report ---
proxy="${BIN_DIR}/authbridge-proxy"
ca_dir="${CORTEX_DIR}/ca" # matches defaultCortexDir()+caDirName in local.go
case ":${PATH}:" in
	*":${BIN_DIR}:"*) abctl_cmd="abctl" proxy_cmd="authbridge-proxy" ;;
	*) abctl_cmd="${BIN_DIR}/abctl" proxy_cmd="$proxy" ;;
esac

info ""
info "Installed abctl and authbridge-proxy (${version}) to ${BIN_DIR}"
case ":${PATH}:" in
	*":${BIN_DIR}:"*) ;;
	*)
		warn "${BIN_DIR} is not on your PATH."
		warn "Add it for future sessions:  export PATH=\"${BIN_DIR}:\$PATH\""
		;;
esac

if [ "$MODE" = "install-only" ]; then
	info ""
	info "Install-only mode. Start it with:  ${proxy_cmd} --local"
	exit 0
fi

# --- run it as a service, not a background process ---
#
# There is no "start it with nohup" path any more. A backgrounded process survives
# neither a crash nor a logout, and once Claude Code's settings point at the proxy
# that means Claude Code silently stops working — most reliably right after a
# reboot. Handing it to the OS supervisor removes that whole class of problem, and
# removes any reason for anyone to reach for kill or pkill.
# This script now starts the proxy ONLY through `abctl service`, so an abctl that
# predates that command cannot be driven by it. Say which mismatch it is, rather
# than letting `unknown subcommand "service"` surface as a bare non-zero exit after
# the binaries are already installed.
if ! "${BIN_DIR}/abctl" service status >/dev/null 2>&1 &&
	"${BIN_DIR}/abctl" service 2>&1 | grep -q "unknown subcommand"; then
	# Only offer the matching-release URL when $version really is a tag: under
	# AUTHBRIDGE_SKIP_DOWNLOAD it reads "already installed", which would otherwise
	# be spliced into a nonsense URL.
	case "${version}" in
		v*)
			die "the ${version} abctl has no 'service' command, which this installer needs
  in order to start Cortex. Either use the installer that shipped with it:
    curl -fsSL https://raw.githubusercontent.com/${REPO}/${version}/authbridge/install.sh | sh
  or install newer binaries with this script:
    AUTHBRIDGE_VERSION=<newer tag>"
			;;
		*)
			die "the abctl in ${BIN_DIR} has no 'service' command, which this installer
  needs in order to start Cortex. Install a newer one — drop
  AUTHBRIDGE_SKIP_DOWNLOAD, or point AUTHBRIDGE_VERSION at a release that has it."
			;;
	esac
fi

# Materialise the config before handing the proxy to the supervisor. This used to
# happen as a side effect of starting `--local` in the background; with the service
# doing the starting, nothing else creates the file, and `abctl service install`
# refuses to run without it.
if [ ! -f "${CORTEX_DIR}/config.yaml" ]; then
	if ! "${BIN_DIR}/authbridge-proxy" --local --write-config; then
		die "could not write ${CORTEX_DIR}/config.yaml.
  Run this to see why:
    \"${proxy_cmd}\" --local --write-config"
	fi
fi

info ""
info "Setting Cortex up as a ${SUPERVISOR_NAME} so it restarts on failure and"
info "comes back at login..."
set +e
"${BIN_DIR}/abctl" service install --yes
svc_status=$?
set -e
if [ "${svc_status}" != "0" ]; then
	die "could not set up the service (exit ${svc_status}).
  Cortex is NOT running. Inspect the unit it would install with:
    \"${abctl_cmd}\" service install --print-unit
  or run the proxy in the foreground to see what it says:
    \"${proxy_cmd}\" --local"
fi

# tool-prune is in the config but INERT: its remove list is empty, so it does
# nothing until a name is added. That is deliberate for an install.
#
# Filling it here would mean a quickstart whose job is to *observe* traffic
# silently starts *rewriting* it. It is also Claude-Code-specific — the scan reads
# ~/.claude/projects — so for anyone driving a different agent it would be a
# mutation with no upside. Opting in is one command, and it belongs to the person
# who knows whether they want it.
local_cfg="${CORTEX_DIR}/config.yaml"
if [ -f "${local_cfg}" ]; then
	info "  Skip the env vars below — configure Claude Code once, then just run \`claude\`:"
	info "    ${abctl_cmd} claude-code enable"
	info ""
	info "  Using Claude Code? Cut its token cost by pruning tools you never call:"
	info "    \"${abctl_cmd}\" tools scan --write \"${local_cfg}\""
	info "    (proposes tools absent from your last 30 days of transcripts;"
	info "     add --all to spare anything you have ever called; hot-reloaded)"
	info ""
fi
# --claude-code: hand off to abctl, which owns the JSON merge (a shell-side edit
# of a file holding API tokens is not worth attempting) and prompts on /dev/tty —
# stdin here is the script itself when piped, so it cannot be read for an answer.
if [ -n "${WIRE_CLAUDE_CODE:-}" ]; then
	info ""
	set +e
	if [ -n "${ASSUME_YES}" ]; then
		"${BIN_DIR}/abctl" claude-code enable --yes
	else
		"${BIN_DIR}/abctl" claude-code enable
	fi
	cc_status=$?
	set -e
	case "${cc_status}" in
		0)
			# The service is already installed above — it is how the proxy runs now,
			# not an option — so there is nothing to offer here.
			info ""
			info "  Run Claude Code:   claude"
			info "  Watch traffic:     \"${abctl_cmd}\""
			info "  Undo:              \"${abctl_cmd}\" claude-code disable"
			info "  Stop Cortex:       \"${abctl_cmd}\" service stop"
			info ""
			exit 0
			;;
		3)
			# Declined, or no terminal to ask on. A normal outcome — fall through to
			# the manual instructions below.
			info ""
			info "  Claude Code left unchanged. To do it later:"
			info "    \"${abctl_cmd}\" claude-code enable"
			info ""
			;;
		*)
			# Anything else went wrong (a foreign HTTPS_PROXY, unparseable settings).
			# Reporting that as "left unchanged" and exiting 0 would claim a success
			# that did not happen.
			die "abctl claude-code enable failed (exit ${cc_status}); Cortex is running but Claude Code is not configured for it"
			;;
	esac
fi
info "  Watch traffic:   \"${abctl_cmd}\""
info "  Send traffic through it (e.g. Claude Code):"
info "    HTTPS_PROXY=http://localhost:${DEMO_FORWARD_PORT} \\"
info "      NODE_EXTRA_CA_CERTS=${ca_dir}/ca.crt \\"
info "      CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 claude"
info ""
info "  Stop it:         \"${abctl_cmd}\" service stop      (start / restart / status too)"
info ""
