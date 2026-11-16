import type { ReactNode } from "react";

import type { Messages } from "../types";
import { plural } from "./plural";

const tools = (count: number): string => plural(count, "tool", "tools");

/** MCP Server 목록·상세·폼과 오버라이드 제안. 다시 배포하는 일은 deploy again 으로 적는다. */
export const mcpServers: Messages["mcpServers"] = {
  backends: "Backends",
  revision: "Revision",
  bindings: "Bindings",
  list: {
    create: "Create MCP Server",
    published: "Published to hub",
    columns: { name: "Name", state: "Status", cluster: "Cluster", hub: "Hub" },
    publish: "Publish",
    noMatch: (query: string): string => `No MCP Servers match "${query}".`,
    noDeployed: `No MCP Servers are deployed. Turn off "Deployed only" to see the ones not deployed yet.`,
    empty: (tenant: string, link: ReactNode): ReactNode => (
      <>
        Tenant {tenant} has no MCP Servers. {link}, then select its Tools to create one.
      </>
    ),
    emptyLink: "Register a Backend API first",
    title: "MCP Servers",
    search: "Search MCP servers…",
    url: "URL",
  },
  detail: {
    back: "Back to MCP Servers",
    deployTitle: "Deploy MCP Server",
    deployBody: (revision: number): string =>
      `Creates the cluster resources from definition r${revision}. The definition stays after deployment, so you can still change it.`,
    redeployBehind: (deployed: number, revision: number): string =>
      `r${deployed} is running now. Deploying again moves it to the latest definition r${revision}, which can change the image, resources and bindings. Connections drop briefly while the new pods start.`,
    redeployBody: (revision: number): string => `Recreates the pods. The definition stays at r${revision}. Connections drop briefly while the new pods start.`,
    undeployBody: "Removes the pods and cluster resources. The definition and revision history stay, so you can deploy again.",
    undeployPublished: "This MCP Server is published to the hub. If you undeploy it, Gateways that use the hub item lose this backend.",
    clonePlaceholders: { display: "Orders MCP copy", name: "orders-mcp-copy" },
    manifestLink: "Check the cluster resources in the Advanced tab",
    saved: (revision: number): string =>
      `Definition r${revision} is saved. Once you deploy, the cluster resources are created, and the pod status and addresses appear here.`,
    innerHint: "Callers need a service token issued by the platform.",
    authExposed: "Call the in-cluster address with a service token, and the external address with the authentication the front proxy checks.",
    authInternal: "Only callers with a service token can call it. It is not open outside the cluster.",
    allTools: (total: number | null): string => `All ${total ?? "?"}`,
    allSpecTools: "(all Tools in the spec)",
    revisionColumns: { backendVersion: "Backend version", image: "Image", created: "Created" },
    deployHint: (revision: number): string => `Choose the cluster, namespace and external exposure to deploy definition r${revision}.`,
    deploymentHint: "Where it was deployed and the values resolved at that time.",
    labels: {
      image: "image",
      backends: "backends",
      tools: "tools",
      runtime: "runtime",
      bindings: "bindings",
      preview: "MCP Server preview",
      cluster: "cluster",
      namespace: "namespace",
      lockedPaths: (count: number): string => `locked paths (${count})`,
      revisions: "Revisions",
      effective: "effective",
    },
  },
  form: {
    clashes: (count: number): string =>
      `With the overrides applied, Tool names collide within this MCP Server (${count}). Enter a different override name for the colliding Tools.`,
    clashUse: (index: number, tool: string): string => `${tool} of Backend API #${index}`,
    clashMore: (count: number): string => `${count} more`,
    timeout: "Timeout (s)",
    timeoutHint: "The longest wait for a call to this Backend API. Default 30 seconds, at most 300.",
    versionHint: "The Backend API version this MCP Server stays on. It does not follow new versions of the backend.",
    noBackends: "No Backend APIs are registered. Upload a spec first to create a Backend API.",
    dropped: (count: number, names: string): string =>
      `Removed ${plural(count, "Tool", "Tools")} that the new version lacks (${names}) from the selection. The other selections and overrides stay.`,
    allExposed: (count: number): string => `(No Tools selected, so all ${count} are exposed)`,
    selectFound: "Select results",
    selectAll: "Select all",
    clearFound: "Clear results",
    clearAll: "Clear all",
    hiddenPicked: (count: number): string => `${plural(count, "more selection is", "more selections are")} hidden by the search.`,
    noMatch: "No matching Tools.",
    pickVersion: "Select a spec version first.",
    overrideName: "Override name (exposed as the MCP Tool name)",
    switchTitle: "Switch to another Backend API",
    switchBody: (tools: number, overrides: number): string =>
      `You lose ${plural(tools, "selected Tool", "selected Tools")} and ${plural(overrides, "override", "overrides")}. Tools differ by backend, so they cannot carry over.`,
    switchConfirm: "Switch",
    backendsRequired: "Select a Backend API and a spec version for each backend.",
    revisionNotice: (revision: number): string =>
      `Saving creates revision r${revision}. The deployed instance keeps using the previous revision until you deploy again.`,
    placeholders: { display: "Order lookup Tools", name: "orders-tools" },
    addBackend: "Add backend",
    uniqueNote: "After overrides, Tool names must be unique within this MCP Server. You cannot save while they collide.",
    imageHint: "Leave empty to use the cluster or platform default image.",
    clashTitle: "Tool names collide. Change the override names using the list above.",
    versionsPending: "Loading the backend versions. You can save in a moment.",
    specVersion: "Spec version",
    versionOption: (version: number, kind: string, count: number): string => `v${version} (${kind}, ${tools(count)})`,
    tools: "Tools",
    searchTools: "search tools",
    overrideDescription: "override description",
    title: "New MCP Server",
    source: "Source",
    runtime: "Runtime",
    image: "Image",
    replicas: "Replicas",
    bindings: "Bindings",
  },
  suggest: {
    title: "Suggest overrides",
    presetHint: {
      enrich_descriptions: "Fills in descriptions so that Agents can select the Tools. Renames a Tool only when its original name is unclear.",
      korean_descriptions: "Writes descriptions in Korean. Tool names stay as they are, because Agents call Tools by name.",
    },
    subtitle: "An LLM suggests Tool names and descriptions. Only the ones you accept go into the new revision.",
    instructionPlaceholder: "e.g. Start names with a verb.",
    notice: "A suggestion cannot be stopped once it starts. Open this address again to pick up where you left off, even after reloading or leaving.",
    deployNotice:
      "Applying is not deploying. The running instance keeps the revision it is pinned to, and these names go out with the next deployment. If names change, the hub listing changes with them.",
    unaccepted: "Items you do not accept stay as they are.",
    apply: (revision: number): string => `Accept and create r${revision}`,
    empty: "Select presets on the left and run them to see suggestions here.",
    llmJob: "LLM job",
  },
};
