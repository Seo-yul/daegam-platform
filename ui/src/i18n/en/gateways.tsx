import type { ReactNode } from "react";

import type { Messages } from "../types";
import { plural } from "./plural";

const tools = (count: number): string => plural(count, "tool", "tools");

/** Gateway 의 목록, 폼, 상세와 삭제 경고 (F6). 다시 배포하는 일은 deploy again 으로 적는다. */
export const gateways: Messages["gateways"] = {
  placeholder: "Customer support gateway",
  columns: {
    name: "Name",
    state: "Status",
    revision: "Revision",
    backend: "Backend",
    backends: "Backends",
    conflict: "Conflict resolution",
    location: "Cluster / Namespace",
    ready: "Ready",
    error: "Error",
    workload: "Workload",
    kind: "Kind",
    failures: "Failures",
    message: "Message",
    failureMode: "Failure mode",
    created: "Created",
  },
  degradedBadge: { all_backends_down: "All backends down", some_backends_down: "Some backends down" },
  list: {
    create: "Create Gateway",
    noMatch: (query: string): string => `No Gateways match "${query}".`,
    noneDeployed: 'No Gateways are deployed. Turn off "Deployed only" to see the ones not deployed yet.',
    // 링크 글자가 문장 가운데 들어가므로 소문자로 시작한다
    empty: (tenant: string, link: ReactNode): ReactNode => (
      <>
        Tenant {tenant} has no Gateways. A Gateway connects to hub items, so first {link}.
      </>
    ),
    emptyLink: "publish an MCP Server or register an external MCP",
    title: "Gateways",
    search: "Search gateways…",
    url: "URL",
  },
  form: {
    revisionNotice: (revision: number): string => `Saving creates revision r${revision}.`,
    backendsHint: "The selection order sets the priority. If you select no Tools for an item, all of its Tools are advertised.",
    noHubItems: "The hub has no items. Create an MCP Server or register an external MCP.",
    oidcBlocked: "While Gateways accept only IdP tokens, a published Gateway cannot be a backend. It can be selected once a platform admin switches to platform tokens.",
    proxyBlocked: "A Gateway published in proxy mode cannot be a backend. Select an item that publishes the original Gateway in entry mode.",
    publishedNamesUnknown: "This published Gateway's Tool names are unknown, so it cannot be selected. Run Tool refresh on the original Gateway's backends and deploy it again.",
    notConsumable: "You do not have permission to select this item as a backend.",
    notConsumableError: (name: string): string => `${name}: You do not have permission to select this item as a backend. Check the item's access policy (consume).`,
    backendError: (name: string, text: string, source: string): string => (source ? `${name}: ${text} The original Gateway is ${source}.` : `${name}: ${text}`),
    toolsPick: "Tools (leave empty for all)",
    noTools: (link: ReactNode): ReactNode => <>This item has no Tool list. Run {link} first. For now, all of its Tools are included.</>,
    conflictHint:
      "auto: priority if every backend's Tool list is known, otherwise prefix / priority: earlier backends win, and only conflicting Tools become <item>_<tool> / prefix: every Tool becomes <workload>_<tool>",
    unknownTools: (names: string): string =>
      `The Tool lists of ${names} are unknown, so conflicting Tools cannot be renamed. On a conflict, the earlier backend wins without a warning. Run Refresh Tools, or switch to prefix mode.`,
    failureHint:
      "Currently, ToolHive keeps serving from the remaining backends with either value. With best_effort, the Gateway shows a badge when a backend drops out.",
    authzHint: "One line of Cedar policy, added to the tenant's default policy. Leave empty to apply only the default policy.",
    runtimeHint: "Replicas and resource requests and limits for the Gateway pods.",
    timeout: "Timeout (s)",
    timeoutHint:
      "The longest the Gateway waits for a Tool call to this backend. Leave empty to let the deploy choose: an internal MCP Server gets its longest backend timeout plus 5 seconds, anything else 60 seconds.",
    surfaceResources: "Expose resources",
    surfacePrompts: "Expose prompts",
    surfaceHint: "When on, this Gateway lists the backends' resources (or prompts) and lets clients read them. When off, they leave the lists and reads are refused.",
    codeMode: "Code mode",
    codeModeHint:
      "When on, this Gateway also offers the execute_tool_script Tool. An Agent sends it one script that calls several of this Gateway's Tools in turn or at once and gets the results back together. Calls inside a script meet the caller's rights and the Tool rules like any other call. It can be turned on only when every Tool this Gateway offers is read-only, and not on an exposed Gateway.",
    title: "New Gateway",
    backendsTitle: "Backends (MCP Hub)",
    toolCount: (count: number): string => tools(count),
    aggregation: "Aggregation",
    auth: "auth",
    conflict: "Conflict resolution",
    conflicts: { auto: "auto", priority: "priority", prefix: "prefix" },
    failure: "Failure mode",
    failFast: "fail",
    authz: "Authz policy (Cedar)",
    runtime: "Runtime",
    replicas: "Replicas",
    selected: (count: number): string => `selected ${count}`,
  },
  detail: {
    backToList: "Back to Gateways",
    deployTitle: "Deploy Gateway",
    deployBody: (revision: number): string =>
      `Deploys the Gateway and its group and Entry cluster resources from definition r${revision}. Every backend hub item must be Ready.`,
    notReady: "Every backend must be Ready before you can deploy.",
    cycle: (path: ReactNode): ReactNode => <>The published items lead back to this Gateway: {path}</>,
    redeployBehind: (deployed: number, revision: number): string =>
      `r${deployed} is running now. Deploying again moves it to the latest definition r${revision}, so its backends may change. Connections drop briefly while the Gateway pods restart.`,
    redeploySame: (revision: number): string =>
      `Recreates the Gateway pods. The definition stays at r${revision}. Connections drop briefly while the new pods start.`,
    undeployBody:
      "Removes the Gateway and its group and Entry cluster resources. The definition and revision history stay, so you can deploy it again. Clones of shared hub items are not deleted right away; they are cleaned up shortly after no Gateway uses them.",
    clonePlaceholder: "Customer support gateway copy",
    revokedBackends: (warning: string, names: string): string => `${warning} The blocked backends are ${names}.`,
    overridesOnlyVmcp: "Overrides can be saved only on the VirtualMCPServer document.",
    advancedNote: "Shows the Gateway (vMCP) together with its group and Entry documents. Overrides are saved only on the VirtualMCPServer document.",
    allTools: "(all Tools)",
    labels: {
      group: "group",
      entry: "entry",
      proxy: "proxy",
      backends: "backends",
      conflict: "conflict resolution",
      failure: "failure mode",
      codeMode: "code mode",
      auth: "auth",
      authz: "authz policy",
      runtime: "runtime",
      preview: "Gateway preview",
      namespace: "namespace",
      revisions: "Revisions",
      tools: "Tools",
      accessRevoked: (name: string): string => `access revoked: ${name}`,
      renames: (count: number): string => `, ${plural(count, "tool rename", "tool renames")}`,
      toolCount: (total: number, conflict: string): string => `${tools(total)}, conflict ${conflict}`,
    },
  },
  overview: {
    deployedSummary: (revision: number, backends: number): string =>
      `Definition r${revision} is saved with ${plural(backends, "backend", "backends")}. Once you deploy, the Gateway starts, and its endpoint and backend status appear here.`,
    innerHint: "One endpoint for the Tools of all its backends.",
    auth: (badge: ReactNode): ReactNode => <>The auth boundary is {badge}. The platform auth_mode sets it, and you cannot change it here.</>,
    summary: { conflict: "Name conflicts", failure: "Backend failures" },
    alive: (alive: number | string, expected: number): string => `${alive}/${expected} running`,
    noBackendRows: "No backend rows yet.",
  },
  definition: {
    authFixed: "Set by the platform auth_mode. You cannot change it here.",
  },
  deployment: {
    emptyBody: (revision: number): string =>
      `Choose the cluster, the namespace and external exposure to deploy the Gateway from definition r${revision}.`,
    hint: "Where it was deployed, and the backends resolved at that time.",
    resolvedBackends: "Resolved backends",
    toolsHint: "The advertised Tools, computed from the snapshot taken at deployment. To see the actual list, call tools/list on the endpoint.",
    renamedFrom: (original: string): string => `Renamed from ${original}`,
  },
  codeModeAudit:
    "A script is recorded in this Gateway's audit and metrics as one execute_tool_script call. The Tool calls inside it are recorded by each backend, under this Gateway rather than the person who called.",
  consumerNotice: {
    closed: (names: string): string =>
      `Once this Gateway is deployed again, calls from the Gateways that use it as a backend (${names}) are all refused, because its Authz policy (Cedar) holds a forbid statement.`,
  },
  publish: {
    modeHint: "entry: publishes only the URL / proxy: wraps it in an MCPRemoteProxy.",
    nameHint: "Leave Name empty to use the Gateway's name. In proxy mode, the item gets its own policy and auditing. Publishing again with the same name changes nothing.",
    empty: "No published items yet.",
    deleteBlocked: "This Gateway is published to MCP Hub, so you cannot delete it yet. Unpublish the items below first.",
    title: "Publish to hub",
    mode: "Mode",
    consumerHint:
      "When another Gateway uses this one as a backend, that Gateway enforces this one's Tool rules as well. A Gateway whose Authz policy (Cedar) holds a forbid statement cannot be used as a backend.",
    labels: {
      modeSelect: "publish mode",
    },
  },
};
