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

AIAC turns that into two Rego files per agent — one gating who may call it, one gating what it may
do downstream — derived from the policy text plus the realm-role descriptions already in Keycloak
and the tool's own discovered capabilities. An excerpt of the generated outbound gate:

```rego
package authz.team1_github_agent.outbound

subject_role_scopes := {
    "developer": ["github-tool.issues-read", "github-tool.source-write", "github-tool.source-read"],
    "tester": ["github-tool.issues-write", "github-tool.issues-read"],
}

subject_ok if {
    some role in subject_roles[input.subject]
    input.function_name in subject_role_scopes[role]
}

target_ok if {
    input.function_name in target_scopes[input.target]
}

default allow := false
allow if { subject_ok; target_ok }
```

Every access decision is a two-gate AND: the calling user's role must be granted the scope
(`subject_ok`), *and* the agent's own discovered capabilities must reach it (`target_ok`). A
developer can read and write source and read issues; a tester can read and write issues but never
touches source — exactly the two-line policy, and nothing it didn't say.

## Running it

Everything below is a real cluster, a real Keycloak, a real LLM call, and a real RFC 8693 token
exchange — there is no offline mode. Bring up a rossoctl cluster with SPIRE + Keycloak + the
rossoctl operator first (see [../../assets/INSTALL.md](../../assets/INSTALL.md) and
[../../../k8s/aiac-deployment-guide.md](../../../k8s/aiac-deployment-guide.md) for reference, not as a
manual checklist — `make prereqs` below verifies and, where safe, installs what's missing) and
export `KEYCLOAK_URL` / `KEYCLOAK_ADMIN_USERNAME` / `KEYCLOAK_ADMIN_PASSWORD`.

```bash
make prereqs   # verify/install cluster + AIAC stack + demo workloads; wait for Keycloak registration
make clear     # reset to a clean slate
make setup     # provision demo users/roles, mount policy.md, configure token exchange
```

**Pause 1 — baseline.** `make show` reports three users with roles, no `github-*` roles or scopes
yet, and no generated `.rego` at all. Nothing has been onboarded; there is nothing to enforce yet.

```bash
make onboard-agent   # AIAC discovers github-agent, reads policy.md, generates the inbound gate
make show
```

**Pause 2 — the agent alone.** The inbound gate is now populated: developers and testers can reach
the agent's discovered scopes. The outbound gate exists but every map in it is still empty —
there's no tool yet for the agent to act on.

```bash
make onboard-tool    # AIAC discovers github-tool's capabilities and completes the agent's outbound gate
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

Run `make demo` for the whole provisioning ladder (`prereqs` through the second `show`) in one
shot, then drive the three users separately.

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

The gates are plain Rego, evaluated with `opa eval` against the files AIAC's Policy Computation
Engine writes — this demo runs them the same way a live enforcement point would query them, but
does not itself sit in the request path (see the appendix).

## Troubleshooting

- **`make prereqs` hangs waiting on client registration** — Keycloak client registration is async
  after the operator injects a workload; give it a couple of minutes, then check the operator's
  webhook logs.
- **`make onboard-agent`/`make onboard-tool` times out** — onboarding drives the Policy Rules
  Builder's LLM calls and can genuinely take minutes; re-run with a larger `AIAC_ONBOARD_TIMEOUT` if
  your LLM endpoint is slow.
- **`make setup` / `make dev` fails with a Keycloak profile error** — Keycloak 26's declarative user
  profile requires `email`/`firstName`/`lastName` before `grant_type=password` succeeds; `02-setup.py`
  sets these, so this points at a realm that was provisioned some other way.
- **A `run-*` target aborts with "no policy found"** — the drivers always run against
  `generated/02-after-tool/`; run `make onboard-agent && make onboard-tool` first.

## Appendix: known gaps

- The generated Rego is enforced by evaluating it directly with `opa eval`, mirroring how a gateway
  would query it — this demo does not itself sit in front of live agent/tool traffic (that gateway
  integration is separate, ongoing work).
- `run-*.py` performs a real token exchange to prove the RFC 8693 flow end to end, but does not feed
  the exchanged token into a live call against `github-tool` — the outbound verdict is read from the
  same generated Rego, not from an intercepted request.
