# Plugin runtime config in abctl's plugin-detail pane

**Status:** Design — pending user review
**Date:** 2026-05-28

## Goal

`abctl`'s Pipeline pane shows each plugin's name, direction, position,
read/write slots, and event count. The plugin-detail view (Enter on a
row) shows those same fields plus a placeholder line:

> *(per-plugin runtime config will be added when the Plugin interface
> exposes a Config() method — tracked as a follow-up.)*

This spec replaces that placeholder with the plugin's actual runtime
config — the `config:` subtree from the runtime YAML, post-`${VAR}`
expansion — rendered as colorized pretty JSON. The most common
operator question when triaging an agent ("what is plugin X
configured to do?") is then answerable inside `abctl` without
reading the per-agent ConfigMap separately.

## Non-goals

- Per-field redaction. The convention (mirrored from the existing
  `:9093/config` endpoint) is that secrets live behind `*_file` paths,
  not inline. A redaction pass can ship as a separate follow-up if a
  known-sensitive field ever surfaces.
- Editing config from `abctl`. The TUI stays read-only.
- Diffing config across reloads. `:9093/reload/status` already serves
  that need.
- Surfacing config for non-`Configurable` plugins (those that don't
  declare `Configure(raw json.RawMessage) error`). They render as
  `Config: (none)`.
- Showing the pre-expansion ConfigMap form. Operators want the resolved
  values they'd see inside the pod; the framework already holds them.

## Source-of-truth choice

The runtime config lives in the per-agent ConfigMap (`authbridge-config-<agent>`)
in pre-expansion form. The pod's framework loads it, expands `${VAR}`
references against its env, and runs against the resolved form. We
expose the **resolved form from the running pod**, not the raw
ConfigMap, because:

- `${VAR}`-heavy configs render as useless template text from the ConfigMap.
- During hot-reload, the ConfigMap can be ahead of what the pod is
  actually running. The operator question is "what is this pod doing
  RIGHT NOW," which only the pod can answer.
- `:9094` is already port-forwarded by the picker; adding a field to
  `/v1/pipeline` (already there) is cheaper than introducing a second
  port-forward to `:9093/config` or a kubectl-shell-out for the
  ConfigMap.

## Architecture

```text
runtime YAML
    │
    ▼  (already exists)
PluginEntry{Name, Config json.RawMessage}
    │
    ▼  registry.Build()  — NEW: wrap Configurable plugins
configuredPlugin{Plugin; raw json.RawMessage}
    │
    ▼  pipeline.Holder.Plugins() returns []Plugin
    │
    ▼  /v1/pipeline → describePipeline()  — NEW: type-assert + populate Config
pipelinePluginView{ ..., Config json.RawMessage }
    │
    ▼  apiclient.PipelinePlugin.Config  — NEW: mirror wire shape
    │
    ▼  tui/plugin_detail_pane.go showPluginDetail()
"Config:\n  <pretty-JSON, colorized>"  — render via existing json_colorize.go
```

The framework wraps `Configurable` plugins automatically. **Zero
changes to any existing plugin.** Non-`Configurable` plugins flow
through unwrapped; the type-assert in describePipeline misses, and
the view renders `Config: (none)`.

## Components

### 1. `configuredPlugin` wrapper (`authlib/pipeline/configured.go`, new)

```go
package pipeline

import "encoding/json"

// configuredPlugin wraps a Configurable plugin with the raw config bytes
// it was constructed from, so the session API can surface them on
// /v1/pipeline. All Plugin interface methods forward to the embedded
// plugin — zero observable behavior change at the request hot path.
//
// The wrapper is constructed by the plugins/registry only for plugins
// that implement Configurable. Non-Configurable plugins pass through
// unwrapped.
type configuredPlugin struct {
    Plugin
    raw json.RawMessage
}

// RawConfig returns the raw config bytes the wrapped plugin was
// configured with. Used by the session API to populate the Config
// field on /v1/pipeline.
func (c *configuredPlugin) RawConfig() json.RawMessage { return c.raw }
```

The struct embeds `Plugin` so the four required methods (`Name()`,
`Capabilities()`, `OnRequest()`, `OnResponse()`) and the new
`RawConfig()` accessor are part of `*configuredPlugin`'s method set.

**Optional interfaces require explicit forwarding.** Go does NOT
promote method-set membership through an embedded interface — methods
declared on `Initializer` / `Shutdowner` / `Finisher` / `Readier`
(but absent from `Plugin`) are unreachable on `*configuredPlugin` even
when the wrapped concrete type implements them. The framework's
dispatchers (verified at `pipeline.go:292`, `:308`, `:364`, `:382`,
`:406`, `:474`) all do `p.(Initializer)`-shaped type assertions; a
naive wrapper would silently drop Init/Shutdown/Finish/Ready calls.

The wrapper therefore implements all four optional interfaces
unconditionally, each method forwarding to the wrapped plugin if it
implements the interface and no-opping otherwise:

```go
func (c *configuredPlugin) Init(ctx context.Context) error {
    if init, ok := c.Plugin.(Initializer); ok {
        return init.Init(ctx)
    }
    return nil
}

func (c *configuredPlugin) Shutdown(ctx context.Context) error {
    if sh, ok := c.Plugin.(Shutdowner); ok {
        return sh.Shutdown(ctx)
    }
    return nil
}

func (c *configuredPlugin) OnFinish(ctx context.Context, pctx *Context) {
    if fin, ok := c.Plugin.(Finisher); ok {
        fin.OnFinish(ctx, pctx)
    }
}

func (c *configuredPlugin) Ready() bool {
    if r, ok := c.Plugin.(Readier); ok {
        return r.Ready()
    }
    return true
}
```

**Behavioral implication:** every Configurable plugin appears to
implement all four optional interfaces after wrapping. The
framework's `if x, ok := p.(Initializer); ok` branches always succeed
for wrapped plugins; `Init()` is called on every wrapped plugin and
no-ops for those that don't really implement `Initializer`. Same for
the other three. The end-to-end behavior is identical to the
pre-wrap world — a non-Initializer plugin still has nothing to do at
Init time — but the dispatcher's "ok" branch is now a tautology for
wrapped plugins. The `Ready()` default of `true` preserves the
"plugins without Readier are considered always-ready" semantics
documented at `pipeline.go:287-289`.

This forwarding behavior is the load-bearing safety property and is
verified by `TestConfiguredPluginForwardsOptionalInterfaces` (see
Testing).

### 2. Registry wrapping (`authlib/plugins/registry.go`)

The two existing `c.Configure(e.Config)` call sites (around lines 119
and 167 in current code) become:

```go
if c, ok := plugin.(pipeline.Configurable); ok {
    if err := c.Configure(e.Config); err != nil {
        return nil, err
    }
    plugin = &pipeline.ConfiguredPlugin{Plugin: plugin, Raw: e.Config}
    // Or use the unexported helper described in Components §1; the
    // exact API surface is finalized during implementation.
}
```

Two open shape questions resolved during implementation:

- **Exported vs unexported.** Either `pipeline.configuredPlugin` (with
  a `pipeline.WrapConfigured(p, raw) Plugin` constructor) or an
  exported `pipeline.ConfiguredPlugin` struct. Implementation will
  pick whichever keeps the plugins package's call site cleaner; both
  are equivalent in capability.
- **Where the wrap happens.** Inside the existing Configure-success
  branch in registry.go. Adds 1 line per call site.

### 3. Wire shape (`authlib/sessionapi/server.go`)

```go
type pipelinePluginView struct {
    Name       string          `json:"name"`
    Direction  string          `json:"direction"`
    Position   int             `json:"position"`
    BodyAccess bool            `json:"bodyAccess"`
    Writes     []string        `json:"writes,omitempty"`
    Reads      []string        `json:"reads,omitempty"`
    Config     json.RawMessage `json:"config,omitempty"` // NEW
}
```

`describePipeline` populates Config via type-assertion:

```go
type rawConfigGetter interface{ RawConfig() json.RawMessage }

for i, pl := range plugins {
    caps := pl.Capabilities()
    view := pipelinePluginView{ /* existing fields */ }
    if rc, ok := pl.(rawConfigGetter); ok {
        view.Config = rc.RawConfig()
    }
    out[i] = view
}
```

`omitempty` drops the field from the JSON output when it's nil (i.e.,
non-Configurable plugins) so the wire payload stays the same shape
for those.

### 4. apiclient (`cmd/abctl/apiclient/client.go`)

```go
type PipelinePlugin struct {
    Name       string          `json:"name"`
    Direction  string          `json:"direction"`
    Position   int             `json:"position"`
    BodyAccess bool            `json:"bodyAccess"`
    Writes     []string        `json:"writes"`
    Reads      []string        `json:"reads"`
    Config     json.RawMessage `json:"config,omitempty"` // NEW
}
```

### 5. TUI rendering (`cmd/abctl/tui/plugin_detail_pane.go`)

`showPluginDetail()` replaces:

```go
b.WriteString(styleHint.Render(
    "(per-plugin runtime config will be added when the Plugin interface\n" +
    "exposes a Config() method — tracked as a follow-up.)"))
```

with:

```go
b.WriteString(styleMuted.Render("Config:    "))
if len(p.Config) == 0 {
    b.WriteString(" (none)\n")
} else {
    b.WriteString("\n")
    b.WriteString(ColorizeJSONBytes(p.Config))
    b.WriteString("\n")
}
```

`ColorizeJSONBytes` is the existing helper in
`cmd/abctl/tui/json_colorize.go`. It parses, indents, colorizes, and
falls back to a muted raw render on parse failure — no caller-side
fallback needed. Long configs scroll via the existing detail
viewport; no layout changes.

## Testing

| File | What |
|---|---|
| `authlib/pipeline/configured_test.go` (new) | Verify `configuredPlugin.RawConfig()` returns the bytes passed in; verify pass-through of `Name() / Capabilities() / OnRequest() / OnResponse()` to the embedded plugin (a fake plugin that records each call). |
| `authlib/pipeline/configured_test.go` | `TestConfiguredPluginForwardsOptionalInterfaces`: for each of `Initializer`/`Shutdowner`/`Finisher`/`Readier`, a concrete plugin implementing both `Configurable` AND that optional interface is wrapped; the wrapper's corresponding method is reachable via type-assertion AND forwards to the wrapped plugin's implementation (verified by spy counters / return values). For each optional interface, also test the no-op case: a Configurable plugin that does NOT implement the optional interface; the wrapper's method type-asserts true (because of unconditional embedding) but the underlying call is the documented no-op (Init/Shutdown return nil; OnFinish does nothing; Ready returns true). |
| `authlib/plugins/registry_test.go` | Extend an existing test: a Configurable plugin built via the registry exposes `RawConfig()` returning the config bytes from the YAML; a non-Configurable plugin does not (type-assert returns false). |
| `authlib/sessionapi/server_test.go` | Extend `TestHandlePipeline`: a Configurable plugin's response JSON contains the `config` field with the expected raw bytes; a non-Configurable plugin's response omits the field. |
| `cmd/abctl/apiclient/client_test.go` | Decode a pipeline JSON payload that includes `config`; assert the field is preserved as `json.RawMessage` round-trip. |
| `cmd/abctl/tui/plugin_detail_pane_test.go` (extend) | Render with a Configurable-shape `PipelinePlugin` carrying a small JSON config; assert the rendered output contains the indented JSON. Render with `Config: nil`; assert "(none)" appears. |

## Acceptance criteria

1. A Configurable plugin's `config:` subtree from the runtime YAML
   appears in `abctl`'s plugin-detail pane (post-`${VAR}` expansion,
   colorized JSON).
2. A non-Configurable plugin shows `Config: (none)`.
3. Hot-reload: editing the per-agent ConfigMap causes `abctl`'s
   plugin-detail pane to reflect the new config after the framework
   completes its reload (verified via `/v1/pipeline` returning the
   new raws). No abctl-side polling needed beyond the existing
   `/v1/pipeline` fetch on pane entry.
4. Existing `/v1/pipeline` consumers (operators using `curl` directly,
   any future tooling) continue to work — the new field is additive
   and `omitempty`-elided when absent.
5. Zero changes to existing plugin source files.
6. `go test ./...` from `authbridge/` and `authbridge/cmd/abctl/`
   passes including the new tests.
