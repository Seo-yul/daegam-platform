import type { ReactNode } from "react";

import type { Messages } from "../types";

/** 테넌트 설정 (/tenant/settings). */
export const tenant: Messages["tenant"] = {
  settings: {
    scopeHint: (tenant: ReactNode, link: ReactNode): ReactNode => (
      <>
        These settings apply only to tenant {tenant}. Empty fields use the defaults in {link}.
      </>
    ),
    readOnly: "You are not an admin of this tenant, so you can only view these settings.",
    hints: {
      cluster: "If empty, the default cluster in Platform Settings is used.",
      namespace: "If empty, the namespace template of the cluster or the platform is used.",
      namespaceLocked: "Only platform admins can change this, because it moves where the tenant deploys.",
      labels: "Labels for the tenant namespace, as a JSON object.",
      quota: "spec.hard of the ResourceQuota for the tenant namespace, as a JSON object. If empty, no ResourceQuota is created.",
      bindings: "Databases used when the definition of an MCP Server or Agent has no bindings.",
    },
    title: (tenant: string): string => `Tenant Settings: ${tenant}`,
    platformSettings: "Platform Settings",
    clustersTitle: "Clusters",
    updated: "updated",
    defaultCluster: "Default cluster",
    platformDefault: "(platform default)",
    restrictClusters: "Restrict allowed clusters",
    namespaceTitle: "Namespace and quota",
    namespaceName: "Namespace name",
    namespaceLabels: "Namespace labels (JSON)",
    quota: "ResourceQuota spec.hard (JSON)",
    bindingsTitle: "Default bindings",
  },
  members: {
    add: "Add member manually",
    save: "Save members",
    columns: { subject: "Subject", role: "Role", source: "Source" },
    pickUser: "(Select a user)",
    empty: "No members.",
    formError: {
      subRequired: "Each member needs a subject. Enter or select one, or remove the row.",
      duplicate: (sub: string): string => `${sub} is listed more than once. Remove the extra row.`,
    },
    title: "Members",
    synced: "synced from groups",
    manual: "manual",
  },
  serviceAccounts: {
    title: "Service accounts",
    help: "Accounts for programs that call MCP without a person, such as CI. Their tokens call this tenant's Gateways, exposed MCP Servers and exposed Agents. They are Daegam accounts, not Kubernetes service accounts.",
    new: "New service account",
    empty: "No service accounts.",
    roleHint: "The role this account's tokens carry. Tool rules judge the account by it, and it cannot be higher than your own role.",
    deleteBody: "Every token of this account is revoked, and clients that hold one are refused within 16 minutes. A deleted account cannot be restored.",
    off: "This deployment does not use service accounts. Every token of the remaining accounts was revoked, and no new token can be made. Ask a platform admin to use them again.",
    platformAdminOnly: "In this deployment only platform admins create service accounts and their tokens. Tokens made earlier keep working.",
  },
};
