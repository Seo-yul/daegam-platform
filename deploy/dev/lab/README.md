# Lab backends (`deploy/dev/lab`)

The local lab the e2e gates depend on, deployed to the namespace `daegam-spike`: `make image-lab` builds its images,
`make lab-up` deploys it and `make lab-clean` removes it. This README lists the files of the directory.

| Path | What it is |
|---|---|
| `manifests/` | The lab's namespace, mocks, Secret, Redis, Postgres and `MCPServer calculator`. The manifests are fixed to `daegam-spike`; `make lab-up` applies them and creates the ConfigMap `calculator-spec` |
| `images/` | The build contexts of `daegam-stub-spec-server:dev`, `daegam-mock-soap:dev`, `daegam-mock-petstore:dev`, `daegam-header-echo:dev`, a stand-in outside MCP server that requires one header, and `daegam-env-echo:dev`, a catalog-shaped stdio MCP server that echoes the `E2E_` env it was given (`make image-lab`) |
| `profile.json` | The calculator's `profile.json` (`Add` only), served by the stub spec server |
| `verify.sh` | The check `make lab-up` ends with: the `MCPServer` is Ready, the init container fetched the spec and the profile with the Bearer token, `tools/list` is exactly `[Add]`, `Add(3,4)` is 7 through mock-soap and `Divide` is refused |
| `mcp_client.py` | The MCP call CLI of the lab and the e2e gates, a wrapper around `platform/daegam/mcp_client.py` |
| `mcp_oauth_client.py` | A script client that connects to a Gateway through OAuth the way an MCP client does (P33, D59) |
| `agent_route.py` | The API server path CLI of `make e2e-agent` STEP 9r (P35) |
| `chat_samples.py` | Makes the sample files `make e2e-chat` uploads (P36) |
| `llm_echo.py` | The OpenAI- and Anthropic-compatible echo `make e2e-telemetry` and the P38 spike run in the runtime image (P37, P38 F0): chat, Anthropic messages and embeddings through `StubChatModel` and `hash_embedding`, with stream usage, cache, reasoning and thinking, one JSON log line per request with its `traceparent`, headers and body, and the `x-echo-control` control path (status, `Retry-After`, silence, chunk pacing, cut streams) |
| `llm-gateway/` | The P38 F0 lab for agentgateway standalone mode: `lab.sh` (namespaces `daegam-e2e-llm` and `daegam-llm`, echoes, gateway, collector, limit service), `render_config.py` (the gateway config), the manifests, and `probe.py` (the measurements, run in the pod `llm-client`) |
| `svc_token.py` | Mints a service token for calling an internal MCP proxy by hand (D41), or a token shaped like a turn token (`--act`, `--bound-to`, `--no-groups`) that e2e sends to Gateways and exposed Agents to see delegation tokens bound elsewhere refused (P53) |
| `tool_fill.py` | Fills MCP tool arguments by name pattern, for e2e calls to tools whose schema the gate does not know |
| `jwks_lab.py` | A JWKS and token test server that does not run permanently |

The Python scripts run in the platform environment, `uv run --project platform python deploy/dev/lab/<script>.py ...`,
except `tool_fill.py`, which uses only the standard library and runs with the system `python3`, and `llm_echo.py`,
which runs inside the agent runtime image. Each script's docstring
has its full usage.

To run only `verify.sh` again:

```sh
SPIKE_NS=daegam-spike KUBE_CONTEXT=orbstack bash deploy/dev/lab/verify.sh
```

- **`mcp_client.py`.** `mcp_client.py [--token TOKEN] [--host HOST] [--timeout SECONDS] list URL` and
  `mcp_client.py [...] call URL TOOL '{"a": 3, "b": 4}'`. `--token` adds `Authorization: Bearer`, `--host` the `Host`
  header (an Ingress called by IP), and `--timeout` bounds each request (30 seconds by default). It prints one JSON
  object with the time taken in `seconds` and exits non-zero on a protocol or tool error. The MCP client code exists once, in
  `platform/daegam/mcp_client.py`.
- **`mcp_oauth_client.py`.** Splits what a real client (Claude Code, VS Code, Cursor) does into one command per step: it
  finds the PRM and the authorization server metadata from an unauthenticated call's 401 (`discover`), registers with DCR
  or uses a CIMD client id (`register`, `cimd`), requests authorization with PKCE S256 (`authorize`), signs in through
  the API and makes the consent screen's calls (`inspect`, `approve`, `deny`), exchanges the code (`token`), calls the
  Gateway (`call`), refreshes (`refresh`) and revokes (`revoke`). One connection's state lives in the `--state` file,
  and `--resolve HOST:PORT:IP` reaches Traefik without `/etc/hosts`. `make e2e-oauth` runs the flow with it. The
  authorization server rules are written again from the specifications instead of borrowed from platform code, so a
  client cannot be wrong together with the server; only the MCP calls use `platform/daegam/mcp_client.py`.
- **`agent_route.py`.** `agent_route.py [--context CTX] NAMESPACE SERVICE TOKEN_SECRET` calls an Agent runtime's
  `/metrics` and one chat turn through pods/portforward with the kube context's client and the same
  `bearer_service_client` as the platform proxy, then cuts the next turn midway and checks that the cut turn runs on:
  the next chat in that session answers 409 `turn_in_progress`, the transcript gains the saved tool result and ends
  with `done`. It prints one JSON object.
- **`chat_samples.py`.** `samples`, `docx` and `hangul` commands. The document samples (docx, pdf, xlsx, hwpx) reuse the
  platform tests' sample builder (`platform/tests/helpers/documents.py`) and carry a run marker to find in the
  extracted text. It also makes a docx of an exact size (the 10 MiB upload of the run against an install) and Korean text that fills
  the session's attachment limit.
- **`jwks_lab.py`.** Keeps a key set, a response status and `Cache-Control` per channel and signs ES256 and RS256
  tokens. It runs on the platform image from a ConfigMap to measure when ToolHive's verifier refetches a JWKS.
