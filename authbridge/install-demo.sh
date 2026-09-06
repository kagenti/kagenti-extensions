#!/bin/sh
# install-demo.sh — renamed to install.sh.
#
# This shim exists because the old name was published in release notes and docs,
# so a command already in someone's shell history or notes would otherwise 404.
# It forwards every argument to the current script and will be removed once the
# old URL stops being fetched.
#
# Local installs are no longer framed as a "demo" — they are the supported way to
# run Cortex on a machine — hence the rename.
# set -eu, not -euo pipefail: this is POSIX sh (the documented entry point is
# `curl ... | sh`), and `pipefail` is a bashism that would abort the script under
# dash/ash. The repo-wide `set -euo pipefail` convention applies to bash scripts.
set -eu

URL="https://raw.githubusercontent.com/rossoctl/cortex/main/authbridge/install.sh"

printf '%s\n' "note: install-demo.sh is now install.sh; forwarding to it." >&2
printf '%s\n' "      update your command to: curl -fsSL ${URL} | sh" >&2

command -v curl >/dev/null 2>&1 || { printf 'error: curl is required\n' >&2; exit 1; }

# Download to a file first rather than piping into sh, so a truncated or failed
# fetch cannot execute as a partial script.
tmp=$(mktemp)
trap 'rm -f "$tmp"' EXIT
curl -fsSL "$URL" -o "$tmp" || { printf 'error: could not fetch %s\n' "$URL" >&2; exit 1; }
sh "$tmp" "$@"
