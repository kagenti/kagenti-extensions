# github-agent

An autonomous A2A agent that acts on a user's behalf against GitHub **source repositories** and an **issue/PR tracker**, using the [`github-tool`](https://github.com/kagenti/kagenti-extensions) MCP server.

This agent implements the canonical `github-agent` used by the AIAC policy-pipeline integration test — the two skills match the policy scenario's `source-operator` and `issues-operator` roles.

## Skills

| Skill id | Name | Description |
|---|---|---|
| `source_operations` | Source repository operations | Browse and search code; read, create, and modify repository file contents, branches, and commits. |
| `issue_operations` | Issue & PR tracker operations | Read, search, create, and update issues, comments, sub-issues, and pull requests. |

## Prerequisite: `github-tool-mcp` (production tool)

This agent connects to **`github-tool-mcp:9090/mcp`** — the production 44-tool MCP server — at `MCP_URL`.
Deploy it before starting the agent:

```
authbridge/demos/github-issue/k8s/github-tool-deployment.yaml
```

> **Not the same as `demo/tools/github_tool/`.**
> `demo/tools/github_tool/` is a simplified 4-tool stub (`source-read`, `source-write`, `issues-read`,
> `issues-write`) deployed as Service `github-tool` for **UC-1 onboarding discovery** only.
> The agent never connects to it — it connects to the production `github-tool-mcp` server which
> exposes the 44-tool GitHub API federation.

## Configuration

All settings are read from environment variables (or a `.env` file). Copy one of the presets:

| Preset | Description |
|---|---|
| `.env.ollama` | Default — local Ollama (ibm/granite4) |
| `.env.openai` | OpenAI gpt-4o-mini |
| `.env.claude` | Anthropic Claude Sonnet |
| `.env.template` | Documented placeholder for all vars |

### Variables

| Variable | Description | Default |
|---|---|---|
| `TASK_MODEL_ID` | litellm model id | `ollama/ibm/granite4:latest` |
| `LLM_API_BASE` | OpenAI-compatible base URL | `http://host.docker.internal:11434` |
| `LLM_API_KEY` | LLM API key | `my_api_key` |
| `MODEL_TEMPERATURE` | Sampling temperature | `0` |
| `EXTRA_HEADERS` | Extra LLM headers (JSON) | `{}` |
| `MCP_URL` | MCP tool endpoint | `http://github-tool-mcp:9090/mcp` |
| `MCP_TIMEOUT` | MCP connect timeout (s) | `600` |
| `ENABLED_TOOLS` | Override the curated tool allow-list (comma-separated) | (unset → default set) |
| `PORT` | A2A listen port | `8000` |
| `LOG_LEVEL` | Log level | `INFO` |
| `GITHUB_TOKEN` | Static Bearer to MCP (else inbound passthrough) | (unset) |
| `ISSUER` | Expected `iss` of inbound JWTs (informational) | (unset) |
| `AGENT_ENDPOINT` | Override the URL advertised in the card | (unset) |

## Running locally

```bash
cd aiac/demo/agents/github_agent
cp .env.ollama .env          # or another preset
uv sync
uv run server
# In another terminal:
curl -s localhost:8000/.well-known/agent-card.json | python3 -m json.tool
```

Optionally, run `expect -f test_startup.exp` instead to check startup automatically.

## Deploying to Kagenti (Kind cluster)

Prerequisites: a running Kagenti cluster (Keycloak realm `kagenti`, namespace `team1`) with `github-tool` already deployed.

1. **Build the image:**
   ```bash
   cd aiac/demo/agents/github_agent
   podman build -t github-agent:latest .
   # or: docker build -t github-agent:latest .
   ```

2. **Load into the Kind cluster:**
   ```bash
   kind load docker-image github-agent:latest --name kagenti
   ```

3. **Apply manifests:**
   ```bash
   kubectl apply -f k8s/configmaps.yaml
   kubectl apply -f k8s/github-agent-deployment.yaml
   ```

4. **Confirm AuthBridge injection:**
   ```bash
   kubectl get pod -n team1 -l app.kubernetes.io/name=github-agent -o jsonpath='{.items[0].spec.containers[*].name}'
   ```
   You should see the `authbridge-proxy` (or `envoy-proxy`) sidecar alongside `agent`.

5. **Port-forward and send a message:**
   ```bash
   kubectl port-forward svc/github-agent 8080:8080 -n team1 &
   # Send an A2A message/send request:
   curl -s http://localhost:8080/.well-known/agent-card.json | python3 -m json.tool
   ```

## Architecture

```
A2A client ──(JSON-RPC /)──► github-agent (:8000)
                                 │  CrewAI: prereq extract → researcher
                                 └──(streamable-http, MCP_URL)──► github-tool-mcp:9090/mcp ──► GitHub
      (AuthBridge sidecar: inbound JWT validation; outbound RFC-8693 token exchange for MCP_URL host)
```
