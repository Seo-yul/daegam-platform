import type { Messages } from "../types";
import { plural } from "./plural";

const polling = (seconds: number): string => `This page checks the status every ${plural(seconds, "second", "seconds")}, so you do not need to reload it.`;

/** 종류 이름이 아니라 보통 명사라 문장 가운데서는 소문자로 적는다(7절). */
const HUB_ITEM = "Hub item";

const slow = (what: string): string =>
  `This is taking longer than expected. ${what}. Check the image name and the cluster's free capacity. For the exact reason, see the pod events in the cluster (kubectl describe pod).`;

/** 리소스 종류, 단계(phase), 상태 배지, 삭제 규칙. */
export const resources: Messages["resources"] = {
  kind: {
    "backend-api": "Backend API",
    "mcp-server": "MCP Server",
    gateway: "Gateway",
    agent: "Agent",
    "data-service": "Database",
    "knowledge-base": "Knowledge Base",
    cluster: "Cluster",
    hub: HUB_ITEM,
    skill: "Skill",
  },
  state: {
    draft: "Not deployed",
    deploying: "Deploying",
    deployed: "Deployed",
    failed: "Deployment failed",
    undeploying: "Undeploying",
    deleting: "Deleting",
  },
  // 배지가 이미 phase 를 영어로 적으므로 같은 낱말을 되풀이하지 않고 뜻을 적는다. Failed 만은 상태 배지(state.failed)와
  // 같은 말이 되지 않게 phase 이름을 그대로 둔다
  phase: {
    label: {
      Pending: "Starting",
      Rolling: "Updating",
      Ready: "Healthy",
      Degraded: "Partly unavailable",
      Failed: "Failed",
      Missing: "Not in the cluster",
      Deleted: "Removed",
      Sleeping: "Sleeping",
      Waking: "Waking",
    },
    progress: {
      Pending: (pollSeconds: number): string => `Deploying. Pods usually start within 30 seconds. ${polling(pollSeconds)}`,
      Rolling: (pollSeconds: number): string =>
        `Replacing the pods. This usually takes less than a minute, and connections may drop briefly. ${polling(pollSeconds)}`,
      Waking: (pollSeconds: number): string => `Starting the pod that was stopped while unused. Waiting requests are answered once it is up. ${polling(pollSeconds)}`,
    },
    slow: {
      Pending: (minutes: number): string => slow(`The pod has not started in over ${plural(minutes, "minute", "minutes")}`),
      Rolling: (minutes: number): string => slow(`The new pods have not become ready in over ${plural(minutes, "minute", "minutes")}`),
      Waking: (minutes: number): string => slow(`The pod has not started again for over ${plural(minutes, "minute", "minutes")}`),
    },
    failure: {
      Failed: "The deployment failed. Find the cause in the error and conditions below, fix the image name or resource requests, and deploy again.",
      Missing: "This resource was not found in the cluster. Check the cluster connection and deploy again.",
    },
  },
  delete: {
    // 제목에서는 종류 이름이 동사 뒤에 오므로 hub item 만 소문자가 된다(Delete hub item)
    title: (kind: string, action: string): string => `${action} ${kind === HUB_ITEM ? HUB_ITEM.toLowerCase() : kind}`,
    description: {
      "backend-api": "Its version history and issued tokens are deleted with it. You cannot delete it while an MCP Server uses this Backend API.",
      "mcp-server":
        "Its revision history is deleted with it. If it is deployed, its running pods and cluster resources are removed, along with any item it published to MCP Hub.",
      gateway:
        "Its revision history is deleted with it. If it is deployed, the Gateway and its group and Entry cluster resources are removed, and any item it published to MCP Hub is unpublished first.",
      agent:
        "Its revision history is deleted with it. If it is deployed, its pods, Deployment and Service, issued tokens and copied provider Secret are removed, along with its active sessions and pending approvals.",
      "data-service": "Its saved connection details and Secret are deleted with it. You cannot delete it while an MCP Server uses this Database.",
      "knowledge-base":
        "Its documents and vector table are deleted with it. You cannot delete it while an Agent uses this Knowledge Base or while documents are being indexed.",
      cluster: "Its registered connection details (Secret) are deleted with it. You cannot delete it while an MCP Server is deployed to this Cluster.",
      hub: "You cannot delete this item while a Gateway uses it. You need the manage permission.",
      skill: "All its revisions are deleted with it. You cannot delete it while any Agent has this Skill attached, including Agents in other tenants.",
    },
    force: "Revoke tokens and delete",
    continuing: "This will be deleted once the operation in progress finishes.",
    rowLabel: (name: string): string => `Delete ${name}`,
  },
};
