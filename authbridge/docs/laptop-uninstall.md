# Turning Cortex off, and removing it

Two different things, so pick the one you want:

- **Pause it** — stop Cortex, keep everything installed.
- **Unwire Claude Code** — leave Cortex running, stop routing Claude Code through it.
- **Remove it** — take it all off the machine.

## Pause it

```sh
abctl service stop      # start / restart when you want it back
```

Claude Code fails while Cortex is stopped, because its settings still point at the
proxy. Either start Cortex again or unwire Claude Code (below).

Use `abctl service stop`, not `kill` or `pkill` — the supervisor restarts the
process within seconds, which looks like it refusing to die.

## Unwire Claude Code

```sh
abctl claude-code disable
```

This removes only the three keys Cortex added to `~/.claude/settings.json`
(`HTTPS_PROXY`, `NODE_EXTRA_CA_CERTS`, `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC`)
and leaves anything else in that file alone. Claude Code goes straight to the API
again. Restart `claude` to pick it up.

Cortex keeps running; nothing sends traffic to it. `abctl claude-code enable` puts it
back.

## Remove it

```sh
abctl claude-code disable     # 1. unwire Claude Code
abctl service uninstall       # 2. stop it and remove the service
rm -rf ~/.cortex              # 3. config, CA, logs
rm -f ~/.local/bin/abctl ~/.local/bin/authbridge-proxy
```

Order matters for the first two: `claude-code disable` needs to read the config that
step 3 deletes.

### Check nothing is left

```sh
abctl claude-code status                    # should say "not enabled"
pgrep -fl authbridge-prox                   # should print nothing
ls ~/.cortex 2>/dev/null                    # should print nothing
```

The CA that step 3 removes was only ever trusted through `NODE_EXTRA_CA_CERTS` in
`~/.claude/settings.json` — Cortex never adds it to the system or login keychain, so
there is nothing to clean up there.

### If `abctl` is already gone

The service can be removed by hand:

```sh
# macOS
launchctl bootout "gui/$(id -u)/io.rossoctl.cortex"
rm -f ~/Library/LaunchAgents/io.rossoctl.cortex.plist

# Linux
systemctl --user disable --now cortex.service
rm -f ~/.config/systemd/user/cortex.service
```

Then delete the three Cortex keys from the `env` block of
`~/.claude/settings.json` yourself.
