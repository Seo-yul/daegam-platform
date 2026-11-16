import type { Messages } from "../types";
import { plural } from "./plural";

/** Cluster 의 목록, 등록, 상세 (F6). 칸 이름(Default IngressClass 등)은 필드 이름이라 코드에 영어로 남는다. */
export const clusters: Messages["clusters"] = {
  readOnly: {
    notice: "You can only view Clusters. Ask a platform admin to register, change or delete one.",
    title: "Only platform admins can register or change Clusters.",
  },
  list: {
    register: "Register Cluster",
    columns: { name: "Name", connection: "Connection" },
    noMatch: (query: string): string => `No Clusters match "${query}".`,
    emptyWritable: "No Clusters are registered. Add one with the Register Cluster button above.",
    emptyReadOnly: "No Clusters are registered. Ask a platform admin to register one.",
    title: "Clusters",
    search: "Search clusters…",
    home: "home",
  },
  form: {
    backToList: "Back to Clusters",
    placeholders: { display: "Production cluster", name: "prod-cluster" },
    nameHint: "'home' is reserved for the cluster that the platform registers automatically.",
    platformUrlHint: "The address the pods of this cluster use to reach the platform API.",
    kubeconfigFileHint: "The kubeconfig used to connect to this cluster. Its content goes into a K8s Secret, and MongoDB keeps only the Secret's name.",
    kubeconfigHint: "Upload a file or paste the content. It is stored safely in a platform Secret.",
    registering: "Registering… (preflight)",
    register: "Register",
    title: "Register Cluster",
    platformUrl: "Platform internal URL",
    kubeconfigFile: "kubeconfig file",
    loaded: (name: string): string => `loaded ${name}`,
    kubeconfig: "kubeconfig",
  },
  detail: {
    resource: "Cluster",
    rerunning: "Running…",
    rerun: "Run preflight again",
    usageMetrics: { on: "on", off: "off" },
    networkPolicy: {
      enforced: (controller: string): string => `Enforced (${controller})`,
      presumed: (controller: string): string => `Presumed enforced (${controller})`,
      none: "No enforcing controller",
    },
    presumedNote:
      "k3s enforces NetworkPolicy inside k3s itself, so no controller pod is visible. Enforcement is presumed, but k3s may have been started without policies. After you confirm that traffic is actually blocked, set the platform setting DAEGAM_HOME_NETWORK_POLICY, and this shows as confirmed instead of presumed.",
    noControllerNote:
      "This cluster has no controller that enforces NetworkPolicy, so the NetworkPolicies the platform creates block nothing here. The platform does not rely on them for protection, so installs and deployments proceed as usual.",
    secretRights: {
      bound: (count: number): string =>
        count === 1 ? "RoleBinding in the one namespace" : `RoleBinding in all ${count} namespaces`,
      unbound: (unbound: number, total: number): string =>
        `No RoleBinding in ${unbound} of ${plural(total, "namespace", "namespaces")}`,
      noNamespaces: "No managed namespaces yet",
      roleWithoutSecrets: "daegam-tenant-objects grants no Secret rights",
      clusterWideNote:
        "The platform's account can still read Secrets in every namespace of this cluster. Check that the row above shows the RoleBinding in every namespace, then apply this release's RBAC file to remove this right.",
      unboundNote:
        "This cluster gives the platform's account no cluster-wide Secret rights, so the platform cannot read or write Secrets in a namespace without the RoleBinding. Deploy to that namespace or wait for the platform to create the RoleBinding, then run preflight again.",
    },
    shippedRedisCertNote: (date: string): string =>
      `The TLS server certificate of the Redis deployed with the platform expires on ${date}. Renew it with this installation's deploy procedure, and replace the CA first if it also expires soon.`,
    notRun: "Preflight has not run yet.",
    referencesTitle: "Deployed to this cluster",
    referencesHint: "Shows only the current tenant's resources.",
    missing: {
      connect: "Cannot connect to the cluster. Check the server address and the certificates in the kubeconfig.",
      crd: "This cluster has no ToolHive CRDs. Install the ToolHive operator first.",
      operator: "The ToolHive operator is not running. Install it first.",
      rbac: "The service account lacks permissions. Install or upgrade the Daegam chart on this cluster with target: remote.",
      other: "Items that did not pass preflight.",
    },
    missingCount: (count: number): string => plural(count, "missing item", "missing items"),
    rawLines: "Show raw lines",
    rbac: {
      deniedOnly: (denied: number, total: number): string => `Show denied permissions only (${denied}/${total})`,
      columns: { group: "API group", resource: "Resource", verb: "Verb", namespace: "Namespace", allowed: "Allowed" },
      coreGroup: "core",
    },
    envManaged: (fields: string): string =>
      `The platform settings (DAEGAM_HOME_*) set these fields and rewrite them at every start, so you cannot change them here. To change them, edit the deployment's ConfigMap: ${fields}`,
    labels: {
      connection: "connection",
      secret: "secret",
      platformUrl: "platform internal url",
      checked: "checked",
      usageMetrics: "usage metrics",
      networkPolicy: "NetworkPolicy",
      secretRights: "tenant Secrets",
    },
  },
  ingress: {
    hint: "Defaults and allowed values for the deploy form. Existing deployments keep their own exposure snapshot.",
    enabled: "Allow external exposure",
    allowUnauthenticated: "Allow unauthenticated exposure",
    allowUnauthenticatedNote: "(Turn on to expose in header auth mode. Keep it off on production clusters)",
    classHint: "The IngressClass preselected when you create an exposure on this cluster.",
    allowedClassesHint: "Separate with commas. Leave empty to allow every IngressClass in the cluster.",
    domainHint: "The domain in the default host <name>.<tenant>.<domain>.",
    tlsHint:
      "The default TLS mode of the exposure form. Use edge for a cluster where a proxy in front of the Ingress terminates TLS. Exposures on such a cluster can only use edge.",
    tlsEdge: "edge (terminated at the front)",
    issuerHint: "The default ClusterIssuer for cert-manager mode.",
    controllerNamespaceHint: "The namespace of the Ingress controller pods. The exposure NetworkPolicy allows traffic from it.",
    ipBlocksHint: "CIDR list for hostNetwork controllers. Separate with commas.",
    reservedHostsHint: "Hosts that deployments cannot use. The platform's own Ingress host goes here.",
    annotationsHint: "One key: value per line. Applied to every rendered Ingress.",
    annotationLine: (line: string): string => `Each annotation line must have the form "key: value": ${line}`,
    example: "An example for nginx-based controllers. Sessions over streamable-http stay open for a long time, so this raises the proxy timeouts.",
    applyExample: "Insert example",
    save: "Save exposure defaults",
    title: "Exposure defaults (ingress)",
    classPlaceholder: "nginx",
    allowedClasses: "Allowed IngressClasses",
    domain: "Domain",
    tls: "Default TLS",
    tlsNone: "none",
    tlsSecret: "secret",
    tlsCertManager: "cert-manager",
    issuer: "Default ClusterIssuer",
    controllerNamespace: "Controller namespace",
    controllerNamespacePlaceholder: "ingress-nginx",
    ipBlocks: "Controller IP blocks",
    reservedHosts: "Reserved hosts",
    annotations: "Ingress annotations",
    labels: {
      ingressClass: "Default IngressClass",
    },
  },
  metadata: {
    title: "Instance metadata",
    enabled: "Block tenant pods from the instance metadata address (169.254.169.254)",
    hint: "Turn this on where nodes are cloud VMs that serve node metadata at that address, such as OCI. Leave it off where pods get credentials from that address, such as GKE Workload Identity, kube2iam or kiam.",
    save: "Save metadata block",
  },
  defaults: {
    /** default_redis 의 TLS 끝점(P61)을 JSON 으로 적는 법. */
    redisTlsHint:
      "Add tls (port and a PEM ca) to default_redis, and MCP Server session storage connects to that TLS port on ToolHive 0.52 and later. Keep the plain port in address. The CA is copied into every tenant namespace as the Secret daegam-redis-ca. Copying the CA and using TLS need the tenant-object rights.",
    save: "Save defaults",
    title: "Deployment defaults",
  },
  preflight: {
    ok: "ok",
    failed: "failed",
    notRun: "not run",
    title: "Preflight",
    kubernetes: "Kubernetes",
    toolhive: "ToolHive",
  },
  sandbox: {
    title: "Code execution isolation",
    check: "Run isolation check",
    passed: (time: string): string => `Passed (${time})`,
    failedNetwork: "NetworkPolicy does not actually block connections.",
    failedUserns: "Pods with user namespaces do not start.",
    failedSelfCheck: "A sandbox's self-check found a connection open that should be blocked. Fix the cause and run the check again.",
    couldNotRun: (reason: string): string => `The check could not run (${reason}).`,
    voided: "The cluster changed after the check, so it must be checked again.",
    capacity: (count: number): string => `Workspaces this cluster can hold at once: ${count}`,
    assumedLimits: (count: number): string => `${count} pods without a memory limit were counted by their request or the default.`,
    sharedNodes: "Sandboxes run on the same nodes as the platform or Agents. This isolation does not stop kernel flaws, so give sandboxes their own nodes in production.",
    acceptSharedNodes: "I understand that sandboxes share nodes with the platform or Agents, and I allow code execution.",
    accepted: (name: string, time: string): string => `Allowed by ${name} at ${time}.`,
    reason: {
      unchecked: "This cluster has not run or not passed the isolation check, so code execution is unavailable. Ask a platform admin to run the isolation check on the Cluster screen.",
      unaccepted:
        "Code execution is unavailable because sandboxes on this cluster share nodes with other workloads and a platform admin has not allowed that. Ask a platform admin to allow it on the Cluster screen.",
      rights: "Code execution is unavailable because the platform lacks the rights to create workspaces on this cluster. Tell a platform admin.",
      capacity: "Code execution is unavailable because this cluster's node memory cannot hold a single workspace.",
    },
  },
};
