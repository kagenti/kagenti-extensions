# UC-1: Onboarding an agent and a tool

**Nobody wrote these access rules.** AIAC discovered a GitHub agent and a GitHub tool already
running in the cluster, read a two-line plain-English policy, and generated enforceable
least-privilege authorization for both — who may call the agent, and what the agent may do on the
tool on their behalf.

## The policy

This is the entire input a human wrote. No YAML, no scope tables, no per-endpoint rules:

```
Grant access on a least-privilege basis: allow only what this policy states; deny by default.

- Developers may read and modify source, and read issues.
- Testers may read and modify issues.
```

## What comes out the other side

AIAC turns that into two Rego policies per agent — one gating who may call it, one gating what it
may do downstream — derived from the policy text plus the realm-role descriptions already in
Keycloak and the tool's own discovered capabilities. Both use the fixed AuthBridge packages
(`authbridge.client.{inbound,outbound}.request`) the live OPA plugin evaluates, keyed on the
plugin's real input shape (`input.identity.subject`, `input.identity.service_id`,
`input.mcp.params.name`). An excerpt of the generated outbound gate:

```rego
package authbridge.client.outbound.request
import rego.v1

subject_role_scopes := {
    "developer": ["issues-read", "source-write", "source-read"],
    "tester": ["issues-read", "issues-write"],
}
target_scopes := {
    "spiffe://localtest.me/ns/team1/sa/github-tool": ["source-read", "source-write", "issues-read", "issues-write"],
}

subject_ok if {
    some role in subject_roles[input.identity.subject]
    input.mcp.params.name in subject_role_scopes[role]
}

target_ok if {
    input.mcp.params.name in target_scopes[input.identity.service_id]
}

default allow := false
allow if { subject_ok; target_ok }
```

Every access decision is a two-gate AND on the same invoked tool (`input.mcp.params.name`, the
**bare** MCP tool name such as `source-read`): the calling user's role must be granted the scope
(`subject_ok`), *and* the target service the exchanged token was minted for must expose it
(`target_ok`, keyed by the full `input.identity.service_id` SPIFFE id). A developer can read and
write source and read issues; a tester can read and write issues but never touches source — exactly
the two-line policy, and nothing it didn't say.

## Running it

Everything below is a real cluster, a real Keycloak, a real LLM call, and a real RFC 8693 token
exchange — there is no offline mode. Bring up a rossoctl cluster with SPIRE + Keycloak + the
rossoctl operator first (see [../../assets/INSTALL.md](../../assets/INSTALL.md) and
[../../../k8s/aiac-deployment-guide.md](../../../k8s/aiac-deployment-guide.md) for reference, not as a
manual checklist — `make prereqs` below verifies and, where safe, installs what's missing).

You do **not** need to export the Keycloak variables by hand. `make keycloak`
(`init/00-discover-keycloak.sh`) port-forwards the in-cluster Keycloak to a local port and reads the
admin credentials from the `keycloak-admin-secret`, exporting `KEYCLOAK_URL` /
`KEYCLOAK_ADMIN_USERNAME` / `KEYCLOAK_ADMIN_PASSWORD` for the step that runs it. Every
Keycloak-touching target self-runs it first, so you rarely call it directly — the forward is set up
once and reused across the run. If you already export those three variables (e.g. to point at a
Keycloak this can't reach), your values win.

```bash
make keycloak  # (optional) port-forward Keycloak + discover admin creds; the targets below self-run it
make prereqs   # verify/install cluster + AIAC stack + demo workloads; wait for Keycloak registration
make clear     # reset to a clean slate
make setup     # provision demo users/roles, mount policy.md, configure token exchange
```

To tear down the port-forward afterwards: `pkill -f 'port-forward .*keycloak-service'`.

**Pause 1 — baseline.** `make show` reports three users with roles, no `github-*` roles or scopes
yet, and no generated `.rego` at all. Nothing has been onboarded; there is nothing to enforce yet.

```bash
make agent   # AIAC discovers github-agent, reads policy.md, generates the inbound gate
make show
```

**Pause 2 — the agent alone.** The inbound gate is now populated: developers and testers can reach
the agent's discovered scopes. The outbound gate exists but every map in it is still empty —
there's no tool yet for the agent to act on.

```bash
make tool    # AIAC discovers github-tool's capabilities and completes the agent's outbound gate
make show
```

**Pause 3 — both onboarded.** `make diff PRIOR=01-after-agent`
shows the outbound gate's maps filling in: `target_scopes` keyed by the tool's SPIFFE identity, and
per-role grants for every discovered tool scope. This is the moment least-privilege access to a
downstream tool exists — generated, not hand-written.

Now drive real users through it:

```bash
make dev      # dev-user: read a file, commit a fix, read an issue (allowed) / close an issue (denied)
make test     # test-user: read/file issues (allowed) / read source (denied)
make devops   # devops-user: blocked at the inbound gate — no role sources any agent scope
```

Each target does a real `grant_type=password` login, checks the inbound gate, performs a real RFC
8693 token exchange for the tool's audience, and checks the outbound gate per intent — printing a
result table. `devops-user`'s inbound denial is the intended story, not a failure: nothing in the
policy grants devops-user access to the agent at all.

### One-shot: `make demo`

The individual targets above are grouped into three phase aggregates, so you can run a whole phase
at once or the entire demo end to end:

| Phase target | Steps | What it does |
|--------------|-------|--------------|
| `make init`    | `00`–`03` | discover Keycloak → verify prereqs → clear → setup |
| `make onboard` | `04`–`05` | onboard the agent, then the tool |
| `make run`     | —         | drive all three users (developer, tester, devops) |

`make demo` chains all three (`init → onboard → run`) with no narrated pauses — use it when you just
want the full run:

```bash
make demo     # init (00-03) -> onboard (04-05) -> run (dev/test/devops)
```

Or drive one phase — or one user — at a time:

```bash
make init     # or step-by-step: make prereqs / clear / setup
make onboard  # or: make agent / make tool
make run      # or a single user: make dev / make test / make devops
```

## Architecture

```
 dev-user/test-user/devops-user
        │  grant_type=password
        ▼
   Keycloak  ──────────────────────────────┐
        │  access_token                    │ RFC 8693 token exchange
        ▼                                  │ (subject token -> tool-audience token)
  [inbound gate: may this user call        │
   the agent? — generated from policy.md]  ▼
        │                            [outbound gate: may the agent reach
        ▼                             this tool scope, for this user? —
   github-agent                       generated from policy.md + tool capabilities]
        │                                  │
        └──────────────────────────────────┴──► github-tool
```

The gates are plain Rego, evaluated with `opa eval` against the policy content AIAC's writer
produces — this demo runs them the same way a live enforcement point would query them, but does
not itself sit in the request path (see the appendix).

### How the demo sources the generated Rego

The reworked PDP Policy Writer is **CR-backed**: for each onboarded agent it server-side-applies a
single `AuthorizationPolicy` custom resource (`agent.rossoctl.dev/v1alpha1`, named
`<name>` in namespace `<ns>` — here `github-agent` in `team1`) whose `spec.policies[]` carry the
inbound and outbound Rego as `content`. In production it writes **CRs only** — no `.rego` files on
disk (`k8s/pdp-interface-deployment.yaml` keeps `POLICY_WRITER_DUMP_REGO` off and mounts no
`/rego`).

So this demo reads its Rego **straight from the CR** — the same artifact a live enforcement point
consumes — rather than from a debug file dump:

```bash
kubectl get authorizationpolicies.agent.rossoctl.dev github-agent -n team1 -o json
```

`onboard/04`/`05` fetch that CR and write each `spec.policies[].content` into
`generated/<snapshot>/team1/github-agent/{inbound,outbound}/request.rego` (mirroring the CR's
`policies[].path`), then `opa eval` those files. `make clear` deletes the CR (a re-onboard
server-side-applies a fresh one). This keeps the demo honest against the real artifact and needs no
demo-only deployment overlay to re-enable the optional `.rego` dump.

> The committed snapshots under `generated/` are produced from the **real** reworked generator
> (`src/aiac/pdp/service/policy/opa/rego.py`) against a hand-built model of this scenario, so they
> match the CR content the live writer emits (byte-for-byte with `docs/examples/opa-team1-policy.yaml`
> for the after-tool state, modulo the cluster's actual trust domain). A live `make onboard` against
> a cluster running the reworked writer overwrites them and is the authoritative source of truth.

## Troubleshooting

- **`make prereqs` hangs waiting on client registration** — Keycloak client registration is async
  after the operator injects a workload; give it a couple of minutes, then check the operator's
  webhook logs.
- **`make agent`/`make tool` times out** — onboarding drives the Policy Rules
  Builder's LLM calls and can genuinely take minutes; re-run with a larger `AIAC_ONBOARD_TIMEOUT` if
  your LLM endpoint is slow.
- **`make setup` / `make dev` fails with a Keycloak profile error** — Keycloak 26's declarative user
  profile requires `email`/`firstName`/`lastName` before `grant_type=password` succeeds; `03-setup.py`
  sets these, so this points at a realm that was provisioned some other way.
- **A `run-*` target aborts with "no policy found"** — the drivers always run against
  `generated/02-after-tool/`; run `make agent && make tool` first.

## Appendix: known gaps

- The generated Rego is enforced by evaluating it directly with `opa eval`, mirroring how a gateway
  would query it — this demo does not itself sit in front of live agent/tool traffic (that gateway
  integration is separate, ongoing work).
- `run-*.py` performs a real token exchange to prove the RFC 8693 flow end to end, but does not feed
  the exchanged token into a live call against `github-tool` — the outbound verdict is read from the
  same generated Rego, not from an intercepted request.
