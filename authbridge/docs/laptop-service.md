# Running Cortex: start, stop, and remove it

Cortex runs as a service, so there is nothing to launch by hand and nothing to keep
in a terminal. Everything below is `abctl service`; run it with no action to see the
list.

```sh
abctl service status      # is it running, and is it answering?
abctl service start       # start it
abctl service stop        # stop it, and keep it stopped
abctl service restart     # stop and start
abctl service install     # set it up in the first place (the installer does this)
abctl service uninstall   # stop it and remove the service
```

**Never use `kill` or `pkill` to stop it.** The proxy is supervised, so killing it gets
it restarted within a couple of seconds, which looks like a process refusing to die.
`abctl service stop` is the stop that works.

That restart is the point, though, and it is worth seeing once:

```sh
kill -9 $(pgrep -f 'authbridge-proxy --config')   # comes back within ~2s
abctl service status                              # healthy again
```

On macOS you will see **two** `authbridge-proxy` processes: a supervisor (the one
launchd starts, holding no ports) and the proxy itself. launchd does not restart user
agents added mid-session — verified across `KeepAlive`, `StartInterval` and
`RunAtLoad` — so the supervisor is what makes crash recovery work. On Linux there is
one process; systemd handles it.

## Is it working?

```sh
abctl service status
```

`installed:` names the unit file, `healthy:` names the endpoint that answered. If it
says `NOT answering`, the proxy is loaded but not serving — check `~/.cortex/proxy.log`.

To see traffic rather than status, run `abctl` with no arguments.

## Start and stop

```sh
abctl service start
abctl service stop
```

A stop persists: Cortex stays down across logouts and reboots until you start it
again. That is deliberate — a stop that quietly undoes itself at your next login is
worse than none.

`stop` also reports how many connections it cut, because a Claude Code session that is
already running cannot recover on its own: `HTTPS_PROXY` is fixed in its environment
when it starts, so it has no way to fall back to a direct connection. Restart any
session that begins failing to connect.

## Three ways to turn it off

They are different, so pick deliberately:

### Pause it

```sh
abctl service stop
```

Claude Code fails while Cortex is stopped, because its settings still point at the
proxy. Either start Cortex again or unwire Claude Code (below).

**A Claude Code session that is already running cannot recover on its own.**
`HTTPS_PROXY` is fixed in its environment when it starts, so it has no way to fall
back to a direct connection, and `claude-code disable` cannot reach it. Restart any
session that starts failing to connect. `service stop` tells you how many
connections it cut, for exactly this reason.

Use `abctl service stop`, not `kill` or `pkill` — the supervisor restarts the
process within seconds, which looks like it refusing to die.

### Unwire Claude Code

```sh
abctl claude-code disable
```

This removes only the three keys Cortex added to `~/.claude/settings.json`
(`HTTPS_PROXY`, `NODE_EXTRA_CA_CERTS`, `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC`)
and leaves anything else in that file alone. Claude Code goes straight to the API
again. Restart `claude` to pick it up.

Cortex keeps running; nothing sends traffic to it. `abctl claude-code enable` puts it
back.

### Remove it

```sh
abctl claude-code disable     # 1. unwire Claude Code
abctl service uninstall       # 2. stop it and remove the service
rm -rf ~/.cortex              # 3. config, CA, logs
rm -f ~/.local/bin/abctl ~/.local/bin/authbridge-proxy
```

Order matters for the first two: `claude-code disable` needs to read the config that
step 3 deletes.

#### Check nothing is left

```sh
abctl claude-code status                    # should say "not enabled"
pgrep -fl authbridge-prox                   # should print nothing
ls ~/.cortex 2>/dev/null                    # should print nothing
```

The CA that step 3 removes was only ever trusted through `NODE_EXTRA_CA_CERTS` in
`~/.claude/settings.json` — Cortex never adds it to the system or login keychain, so
there is nothing to clean up there.

#### If `abctl` is already gone

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
