# Daegam

A self-service platform that turns API specs into MCP servers on Kubernetes and manages what is built on them inside a
company: MCP gateways, the MCP hub, exposure, tenancy and agents.

Upload a WSDL, Swagger 2.0 or OpenAPI 3.x document and [spec2openapi](https://github.com/Seo-yul/spec2openapi) converts
it into OpenAPI that FastMCP can read and checks it. An MCP Server picks tools from that spec and deploys as an `MCPServer`
CR of the [ToolHive](https://github.com/stacklok/toolhive) operator, whose pods run the fixed runtime image `mcp-runtime`
(FastMCP). A Gateway that combines several MCP servers is a ToolHive `VirtualMCPServer`. An Agent is a canvas graph that
runs as a Deployment of the fixed image `agent-runtime` (LangChain deepagents). The platform never creates pods itself:
it applies the CRs, and for an Agent its Deployment, and reads their status.

```
Backend API ──▶ MCP Server (MCPServer CR, mcp-runtime pod)
                    │  published by hand
                    ▼
                 MCP Hub ──▶ Gateway (VirtualMCPServer CR)

Agent (canvas graph; Deployment, agent-runtime pod)
   tools = MCP Servers or Gateways, knowledge = Knowledge Bases, memory = a Database (Postgres or Redis)

Exposure (optional): an Ingress in front of an MCP Server (through a front MCPRemoteProxy), a Gateway or an Agent
```

MCP Servers, Gateways and Agents each keep the definition and the deployment in one document. Creating one saves a
`draft`; deploying puts its CRs or pods in a cluster; undeploying removes only what is in the cluster and keeps the
definition and its revisions.

## Repository layout

| Path | Content |
|---|---|
| `platform/` | The control plane, FastAPI and MongoDB. `daegam/api` (routers) → `daegam/services` (domain) → `daegam/store` (Mongo) and `daegam/k8s` (cluster clients, CR templates), with `daegam/auth`, `daegam/audit`, `daegam/pipeline` (spec conversion) and `daegam/documents` (document text extraction, run as the `daegam-docparse` service) |
| `ui/` | React 19 and Vite, served by the `daegam-ui` nginx image. Page files follow [ui/src/pages/README.md](ui/src/pages/README.md); `src/api/schema.d.ts` is generated from the server's OpenAPI |
| `mcp-runtime/` | The fixed image of MCP Server pods. The init container fetches the spec and `profile.json`, and the runtime serves them with FastMCP |
| `agent-runtime/` | The fixed image of Agent pods. It builds a deepagents graph from `/config/agent.json` and serves SSE chat ([agent-runtime/README.md](agent-runtime/README.md)) |
| `sandbox/` | The image of the per-session code execution sandbox: a small server on the standard library that runs an Agent's commands in its session workspace and proves its own isolation, with the data tools the image ships |
| `web-tools/` | The image of the platform's `web_fetch` server, run as a clone in each Gateway namespace that uses it. Its address rules are a byte copy of the platform's `urlfetch.py`, and `schema/` holds the tool contract and the shared result samples |
| `charts/daegam/` | The Helm chart that installs Daegam: the platform, the document parser, the UI, the Ingress, their configuration, Secrets and RBAC, and optionally a MongoDB and the LLM gateway's namespace; with `target: remote`, the account and rights the platform uses on a remote cluster ([charts/daegam/README.md](charts/daegam/README.md)) |
| `deploy/` | Values and development pieces: `install.env.example` (the install values described below), `cosign.pub` (the public key image signatures are checked against), `toolhive/` (the ToolHive operator's values), `dev/` (`values-local.yaml`, the chart's values for a local development cluster; the lab backends in [dev/lab](deploy/dev/lab/README.md), mock-oidc and other local helpers) |
| `scripts/` | The e2e gates (`e2e_*.sh`) and their library `scripts/lib/`, the build, push, deploy and verify scripts the `Makefile` calls, and `resolve-images.sh`, which pins the images to their digests before an install |
| `security/` | The accepted vulnerability list for image reports |

## Installing

Daegam installs from the Helm chart in `charts/daegam`, straight from this source tree: build the images, push them to
your own registry, write a values file with the host and the registry, and run `helm upgrade --install`. The
[chart's README](charts/daegam/README.md) covers the prerequisites, the values, the Secrets, upgrades and the order of an
uninstall. `scripts/resolve-images.sh` pins the images you pushed to their digests for your nodes' architecture,
checks their signatures and writes a values file to pass to `helm upgrade` with `-f`.

For development, `make deploy-local` installs the same chart on a local cluster with `deploy/dev/values-local.yaml`:
header authentication, which trusts every request and is for a cluster only you can reach, the locally built images, a
bundled MongoDB and Secrets the chart generates. `make deploy-ui-local` rolls only the UI, and `make undeploy-local`
removes the release with its namespace and data.

## Building and testing

The `Makefile` lists its targets in the comment at its top. `make verify` runs the lint and unit tests of the
Python packages and the UI, and checks the generated API types and the lock files. It needs
[uv](https://docs.astral.sh/uv/), Node.js with the UI's packages (`make ui-install`), shellcheck and Docker. The tests
that need MongoDB, Postgres or Redis run only when their test database addresses (`DAEGAM_TEST_MONGO_URL`,
`DAEGAM_TEST_PG_DSN`, `DAEGAM_TEST_REDIS_URL`) are set: `scripts/local-test-dbs.sh up` starts local stores with
Homebrew and `eval "$(scripts/local-test-dbs.sh env)"` sets the addresses. The e2e gates (`make e2e-*`) need a
cluster, and the images and the lab (`make image`, `make lab-up`) need a checkout of
[spec2openapi](https://github.com/Seo-yul/spec2openapi) at its `v0.5.0` tag in `../spec2openapi` (`SPEC2OPENAPI_DIR`
names another place).

The gates against an installed instance (`make e2e-install` and the `E2E_TARGET=install` branches) and the image
scripts read its install values (the platform host, the registry, the kubeconfig context and the e2e account) from a
copy of `deploy/install.env.example` at `~/.config/daegam/install.env`, or at the path in `INSTALL_ENV_FILE`.

## Built on

- [stacklok/toolhive](https://github.com/stacklok/toolhive): the operator that runs and manages MCP servers
  (Apache-2.0, Go).
- [Seo-yul/spec2openapi](https://github.com/Seo-yul/spec2openapi): converts legacy specs into OpenAPI for FastMCP
  (Apache-2.0, Python).
- [FastMCP](https://github.com/jlowin/fastmcp): the MCP server framework of mcp-runtime.
- [LangChain deepagents](https://github.com/langchain-ai/deepagents): the agent harness of agent-runtime.

The pinned versions are in `platform/pyproject.toml` (spec2openapi, FastMCP), the `Makefile` (`TOOLHIVE_VERSION`) and
`agent-runtime/pyproject.toml` (deepagents).

## License

Daegam is source-available, not open source. It is licensed under the [PolyForm Strict License 1.0.0](LICENSE): you may
use it for noncommercial purposes only, and you may not distribute it or make changes or new works based on it. For any
other use, such as commercial use or a modified version, ask the owner ([Seo-yul](https://github.com/Seo-yul),
janglab15@gmail.com). GitHub's terms of service still let anyone view the repository and fork it on GitHub, but a fork
stays under this license, so changing or distributing it still needs the owner's permission. Third-party material keeps
its own license ([NOTICE](NOTICE)), and security reports go through [SECURITY.md](SECURITY.md).
