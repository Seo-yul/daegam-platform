# Daegam Helm chart

This chart installs Daegam on a Kubernetes cluster: the platform API, the document parsing service, the web UI, the
Ingress that serves them on one host, the platform's configuration and Secrets, its RBAC and, optionally, a MongoDB and
the namespace of the LLM gateway. With `target: remote` it installs instead only what the platform needs on another
cluster it deploys into (see [Remote clusters](#remote-clusters)).

The repository publishes source only. There are no published images and no packaged chart: you build the images, push
them to your own registry and install the chart from this source tree.

## What the chart installs

| Object | Notes |
|---|---|
| Deployments and Services `daegam-platform`, `daegam-docparse`, `daegam-ui` | Two replicas each by default, with PodDisruptionBudgets, probes, resources and restricted pod security |
| NetworkPolicies `daegam-docparse`, `daegam-ui` | The parser takes connections from the platform only; neither pod may open a connection |
| Ingress `daegam-platform` | `/api/`, `/.well-known/` and `/healthz` go to the platform; everything else goes to the UI |
| ConfigMap `daegam-platform-config` | Rendered from the values below |
| Secrets `daegam-platform-secrets`, `daegam-docparse-token`, `daegam-mongo-auth` | Generated once and kept, unless you name your own (see [Secrets](#secrets)) |
| ServiceAccount, Role, RoleBinding, ClusterRoles `daegam-platform`, `daegam-tls-reader`, `daegam-tenant-objects`, ClusterRoleBinding | The permissions the platform needs; verbs and resources are listed one by one |
| PriorityClass `daegam-sandbox`, namespace `daegam-probe` and its RoleBinding | Code-execution sandboxes and their isolation check |
| StatefulSet and Service `mongodb` | Only with `mongodb.bundled: true` (the default) |
| LimitRange `daegam-defaults` | Only with `limitRange.enabled: true` |
| Namespace `llmGateway.namespace` (`daegam-llm`), Role and RoleBinding `daegam-llm-gateway` | Only with `llmGateway.mode: agentgateway`; kept by `helm uninstall` (see [LLM gateway](#llm-gateway)) |

The chart installs one release per cluster, in the namespace `daegam-system`; it refuses any other namespace. It does
not create that namespace, so `helm uninstall` never deletes the namespace that holds the MongoDB volume and the kept
Secrets. Tenant namespaces and everything in them are created by the platform at run time, not by the chart.

## Prerequisites

- Kubernetes 1.30 or later.
- Helm 3.17 or later; the repository tests with Helm 3.21.4. Helm 4 is not supported.
- The ToolHive operator and its CRDs, installed from the upstream ToolHive charts.
  `make operator-install KUBE_CONTEXT=<your context>` installs them with the repository's
  `deploy/toolhive/operator-values.yaml`.
- An Ingress controller. The UI Service carries Traefik's sticky-cookie annotations, which keep one browser on one UI
  pod during a rollout. With another controller, set `ui.service.traefikStickyCookie: false` and put that
  controller's session-affinity annotations in `ui.service.annotations`.
- A default StorageClass for the bundled MongoDB, or `mongodb.storageClass`.
- cert-manager, only if instances are exposed with certificates it issues.
- Redis and Postgres are not part of the chart. Run your own and register them as Data Services in the platform.
- Optionally, label the namespace with `pod-security.kubernetes.io/enforce: restricted` before the install
  (`kubectl label namespace daegam-system pod-security.kubernetes.io/enforce=restricted`). Every pod the chart renders
  meets that profile.

## Build and push the images

The chart pulls six images from `images.registry`: `daegam-platform` (also used by the document parser),
`daegam-ui`, and four images the platform runs in tenant namespaces: `daegam-mcp-runtime`, `daegam-agent-runtime`,
`daegam-web-tools` and `daegam-sandbox`.

1. Make a signing key pair with `cosign generate-key-pair`. Keep the private key off the cluster and out of the
   repository.
2. Log in to your registry with `docker login`.
3. From the repository root, build, push and sign the images for your nodes' architecture:

   ```
   make push-images REGISTRY=registry.example.com/daegam PUSH_PLATFORMS=linux/amd64 \
     PUSH_IMAGES="platform ui runtime agent web-tools sandbox" \
     COSIGN_KEY=/path/to/cosign.key COSIGN_PUB=/path/to/cosign.pub
   ```

   It asks for the key's password once.

   Each image gets two tags: the version tag (`0.1.0`, the chart's `appVersion`) and a build stamp that names the
   code it was built from. The command refuses uncommitted changes.

   After the push it reports each image's signature and the vulnerability scan the registry made of it. That report
   reads the scan through zot's search extension (`/v2/_zot/ext/search`), so with a zot registry it waits for each
   scan and ends with exit 1 when one is missing or finds an unaccepted CRITICAL. With any other registry, add
   `SKIP_IMAGE_REPORT=1`: the images are still pushed and signed, and the command prints a warning that the report
   was skipped. Check the images with your registry's own scanner then.

The version tag moves with every push, so without a digest the chart sets `imagePullPolicy: Always`. To pin an image,
give its digest in `images.<component>.digest` (and the stamp in `images.<component>.tag`); a pinned image is pulled
with `IfNotPresent`. `scripts/resolve-images.sh` writes those values for all six images from the same checkout:

```
scripts/resolve-images.sh --registry registry.example.com/daegam --arch amd64 --out images.json \
  --key /path/to/cosign.pub
```

For each image it asks the registry for the stamp tag's index, requires an image for the architecture you name (there
is no default), checks the signature against the public key (by default `deploy/cosign.pub`) together with the image
name and stamp it carries, and writes `images.registry` and each image's `tag` and `digest`. It refuses a checkout with
uncommitted changes and writes nothing when any image fails. Pass the file to `helm upgrade` with `-f images.json`.

## Install

Write a values file with at least the host and the registry:

```yaml
host: daegam.example.com
images:
  registry: registry.example.com/daegam
ingress:
  className: traefik
```

Then install from the repository root:

```
helm upgrade --install daegam charts/daegam -n daegam-system --create-namespace -f my-values.yaml
```

The notes printed after the install show how to read the first admin's name and password. The platform creates that
admin on its first start and asks for a new password at the first sign-in.

### TLS

`scheme: https` (the default) means browsers reach the host over HTTPS, so something must end TLS:

- a proxy or load balancer in front of the Ingress controller: set `ingress.tls: edge`;
- the Ingress itself: set `ingress.tls` to the name of a TLS Secret in `daegam-system`.

With `ingress.tls: none` and `scheme: https`, nothing in the release ends TLS, and the notes print a warning. For a
trial without TLS, use `scheme: http`; it goes only with `ingress.tls: none`.

## Values

Every key is typed in `values.schema.json`, and unknown keys are rejected (except `global`). The schema also holds
the rules between values (header mode needs `auth.allowHeaderMode`, `oidc` needs an issuer and a client id, `http`
needs `ingress.tls: none`, and the others in the table), so `helm lint` refuses them too. The defaults are in
[values.yaml](values.yaml).

| Key | Default | Meaning |
|---|---|---|
| `target` | `home` | `home` installs the platform; `remote` installs only the account and rights on a cluster the platform deploys into ([Remote clusters](#remote-clusters)) and reads no other value except `llmGateway.mode`, which must stay `off` |
| `host` | required | The platform host: the public URL, the Ingress host, and a host that exposures may not take |
| `scheme` | `https` | The scheme browsers use. With `http`, the UI's sticky cookie is not `Secure` |
| `auth.mode` | `local` | `local` (Daegam checks users), `oidc` (tokens from your IdP) or `header` (development only; trusts every request as platform-admin and needs `auth.allowHeaderMode: true`) |
| `auth.adminUsername` | `admin-<random>` | Name of the first admin, used only when the chart first generates `daegam-platform-secrets` |
| `auth.bootstrapAdminGroup` | empty | IdP group that counts as platform-admin while no admin mapping exists |
| `auth.mcpAudience` | `daegam-mcp` | Audience of the platform's MCP tokens |
| `auth.oidc.issuer`, `audience`, `insecureHttp` | empty, `daegam-api`, `false` | The IdP for `oidc` mode; the UI's content security policy also opens the issuer's origin |
| `auth.oidc.clientId` | empty | The UI's public PKCE client registered at the IdP. The UI signs in with it. Issuing a personal or service-account token and approving a login connection need a recent sign-in through it. Required in `oidc` mode, where nobody can sign in without it. Set it here: `platform.extraConfig` refuses `DAEGAM_OIDC_CLIENT_ID` |
| `auth.oidc.dataPlaneIssuer` | `platform` | In `oidc` mode, whose person tokens the Gateways, exposed MCP Servers and exposed Agents accept. With `platform`, people sign in at the IdP and call MCP with tokens Daegam issues (personal tokens, login connections, service accounts), and IdP access tokens are refused there; exposed Agents accept both. `idp` returns to the IdP's tokens and revokes nothing Daegam issued |
| `auth.oidc.confirmSeconds` | `604800` (7 days) | In `oidc` mode, how long after a person's last sign-in their personal tokens and login connections keep working and runs act as them. At least `4500` |
| `ingress.enabled`, `className`, `annotations` | `true`, cluster default, none | The Ingress for `host` |
| `ingress.tls` | `none` | `none`, `edge` when a proxy in front ends TLS, or the name of a TLS Secret in `daegam-system` |
| `home.exposure.*` | exposure off | Defaults for exposing instances to the internet: `enabled`, `className`, `controllerNamespace` (where the Ingress controller pods run), `domain`, `allowUnauthenticated`, `tls` |
| `home.networkPolicy` | detected | `enforced` or `unenforced` when you know whether your network plugin enforces NetworkPolicies |
| `home.blockMetadata` | empty | `true` or `false` to keep tenant pods away from the instance metadata address `169.254.169.254` or not; empty leaves the value set in the platform's screen |
| `home.resources`, `home.limitRange` | unset | Default resources of the pods the platform runs (`cpu` and `memory` under both `requests` and `limits`), and the LimitRange of tenant namespaces; quantities are quoted strings |
| `home.shippedRedis` | unset | For an operator who runs a Redis with TLS next to the platform: its hosts, ports, CA ConfigMap and whether the plain port is open. An upgrade that leaves it out while the live ConfigMap records that Redis is refused |
| `home.allowRedisPlainReopen` | `false` | Allow `home.shippedRedis.plain: true` while the live ConfigMap records the plain port as closed; without it such an upgrade is refused |
| `images.registry` | required | Registry host and project of your images, without spaces |
| `images.pullPolicy` | by digest | Forces one pull policy for every image |
| `images.pullSecret` | none | Pull Secret for the platform's pods and the pods it runs |
| `images.<component>.repository`, `tag`, `digest` | image name, `appVersion`, none | Per image: `platform`, `ui`, `runtime`, `agent`, `webTools`, `sandbox` |
| `platform.replicas`, `docparse.replicas`, `ui.replicas` | `2` | Replicas |
| `platform.resources`, `docparse.resources`, `ui.resources` | see the values file | Requests and limits |
| `platform.pdb.enabled`, `docparse.pdb.enabled`, `ui.pdb.enabled` | `true` | PodDisruptionBudgets with `minAvailable: 1`, rendered only when the workload has two or more replicas. Turn them off on a single-node cluster, where they block a drain |
| `platform.extraEnv`, `docparse.extraEnv`, `ui.extraEnv` | none | Extra container environment variables. Names the chart sets itself and names that come only through the ConfigMap (`reservedKeys` and `configOnlyKeys` in `values.schema.json`) are refused, because an environment variable would override the ConfigMap |
| `platform.extraConfig` | none | Extra keys of the ConfigMap, as text or `true`/`false` (quote numbers). Names the chart sets itself (`reservedKeys`) are refused |
| `ui.service.traefikStickyCookie`, `ui.service.annotations` | `true`, none | Annotations of the UI Service |
| `mongodb.bundled` | `true` | Run MongoDB with authentication in the release. With `false`, `secrets.platform.existingSecret` must hold `DAEGAM_MONGO_URL` of your MongoDB |
| `mongodb.image`, `storageClass`, `size`, `wiredTigerCacheSizeGB`, `resources` | `mongo:7.0`, default class, `10Gi`, `0.25`, see the values file | The bundled MongoDB. Storage class and size cannot change after the first install |
| `secrets.platform`, `secrets.docparse`, `secrets.mongo` | generated | `existingSecret: <name>` to use a Secret you made |
| `limitRange.enabled`, `limits` | `false` | A LimitRange for `daegam-system`, for clusters that require requests and limits on every container |
| `llmGateway.mode` | `"off"` | `"off"` or `agentgateway`. Quote `"off"` in YAML, where a bare `off` reads as `false` |
| `llmGateway.namespace` | `daegam-llm` | The gateway's namespace; tenants cannot take it. Not a namespace of the platform or of Kubernetes |
| `llmGateway.pdb.enabled` | `true` | A PodDisruptionBudget for the gateway's two replicas. Turn it off on a single-node cluster |
| `llmGateway.images.<name>.image`, `signer` | see the values file | The four third-party images of the gateway (`gateway`, `ratelimit`, `redis`, `collector`), each `repository:tag@sha256:<index digest>`, and the GitHub Actions workflow that signed it keylessly, where there is one |
| `llmGateway.images.registryPrefix`, `pullSecret` | none | A registry prefix for copies of those images under the same names, tags and digests, and their pull Secret |

## Secrets

Each of the three Secrets has two modes.

- **Generated** (the default). On the first install the chart generates hex values; on every later upgrade it reads the
  live Secret and keeps its values. The Secrets carry `helm.sh/resource-policy: keep`, so `helm uninstall` leaves them.
  Their values are stored in Helm's release records, as with any Secret a chart renders.
- **Existing**: `existingSecret: <name>`. You create the Secret and the chart only refers to it.

| Secret | Keys |
|---|---|
| `secrets.platform` | `DAEGAM_MONGO_URL`, `DAEGAM_LOCAL_BOOTSTRAP_USERNAME`, `DAEGAM_LOCAL_BOOTSTRAP_PASSWORD` |
| `secrets.docparse` | `token`, shared by the platform and the document parser |
| `secrets.mongo` | `username`, `password` of the MongoDB root user |

A generated `secrets.platform` needs a generated `secrets.mongo` and the bundled MongoDB, because its MongoDB URL
carries that password.

Pods read an edited existing Secret at their next restart:

```
kubectl -n daegam-system rollout restart deploy/daegam-platform deploy/daegam-docparse
```

MongoDB creates its root user only on its first start with an empty volume. If the PVC `data-mongodb-0` exists while a
generated `daegam-mongo-auth` is missing, the chart refuses to render: a new password would not match the existing
user. Restore the old Secret, or delete the PVC to start with an empty database.

## LLM gateway

The optional LLM gateway (agentgateway) holds every provider key and lets Agents call providers with a short token of
their own. With `llmGateway.mode: agentgateway` the chart renders its namespace, labelled
`daegam.devops.ai.kr/component: llm-gateway` and enforcing PodSecurity `restricted`, and the platform's Role and
RoleBinding there; the platform renders the gateway itself. The chart refuses a namespace that carries a tenant's label.

The four images are pinned by digest in `llmGateway.images` and written into the platform's configuration whatever the
mode. To pull copies from your own registry instead, copy them with the same names, tags and digests under one prefix
(`make mirror-images REGISTRY=<registry>` copies them under `<registry>/third-party` after checking the signatures) and
set `llmGateway.images.registryPrefix`, the copies' `name:tag@digest` references and `pullSecret`.

Turning the gateway off is an upgrade with `llmGateway.mode: "off"`. Agents keep calling the gateway until the platform
moves them back, so the namespace, the Role and the RoleBinding carry `helm.sh/resource-policy: keep`: neither that
upgrade nor `helm uninstall` deletes them. Once no Agent calls the gateway, `make llm-gateway-uninstall` deletes the
namespace.

## Remote clusters

The platform can deploy into other clusters, which you register on the platform. On each such cluster, install the
chart with only the target:

```
helm upgrade --install daegam charts/daegam -n daegam-system --create-namespace --set target=remote
```

It renders the ServiceAccount `daegam-deployer` and its token Secret `daegam-deployer-token`, the ClusterRoles
`daegam-deployer`, `daegam-tls-reader` and `daegam-tenant-objects`, the ClusterRoleBinding `daegam-deployer`, the
PriorityClass `daegam-sandbox` and the namespace `daegam-probe` with its RoleBinding. The rules are the same as the
platform's on its own cluster. The notes printed after the install show how to read the token and the CA to register
the cluster. Run the same command after upgrading the chart, so new rights reach the cluster. The chart refuses
`target: remote` on a cluster that runs the platform (where `daegam-system` holds the ConfigMap
`daegam-platform-config`), since that release must stay `target: home`.

A cluster whose objects were applied with `kubectl apply` before this chart existed is taken over once by adding
`--take-ownership` (Helm 3.17 or later); the objects keep their names, so nothing is replaced.

## Rendering without a cluster

A generated Secret needs the live one, so `helm template`, `helm lint` and any render without cluster access cannot
produce it. `helm template` stops with a message that names the `existingSecret` values to set; `helm lint` only logs
that message. To render offline, name every Secret as `existingSecret`, as
[ci/offline-values.yaml](ci/offline-values.yaml) does.

Tools that install by running `helm template` and applying its output (Argo CD, for example) render without the
cluster lookup, so they must use `existingSecret` for all three Secrets.

## Upgrading

Run the same `helm upgrade --install` with your values file. To change one value and keep the rest, use
`--reset-then-reuse-values`, which also picks up new defaults of a newer chart.

The platform's pod template carries a checksum of the ConfigMap, and the UI's a checksum of the two keys it reads (the
authentication mode and the OIDC issuer), so a change of values rolls the pods that read it. An upgrade that renders
nothing new restarts nothing; restart the Deployments yourself when you need it, for example after editing an existing
Secret.

## Uninstalling

First remove the tenants' MCP Servers, Gateways, Agents and exposures through the platform. Then:

```
helm uninstall daegam -n daegam-system
```

This deletes the ClusterRoles, the PriorityClass `daegam-sandbox` and the namespace `daegam-probe`. Tenant pods left
behind would keep serving with nothing to manage them, and sandboxes could not start, which is why the platform's
objects go first. What stays: the namespace `daegam-system`, the MongoDB volume, the Secrets the chart generated and
the LLM gateway's namespace (`make llm-gateway-uninstall` deletes it once the gateway is off and unused).
