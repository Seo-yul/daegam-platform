import type { Messages } from "../types";
import { plural } from "./plural";

/** 감사 기록 (/audit 의 Platform API 와 MCP proxy 두 탭). */
export const audit: Messages["audit"] = {
  apply: "Apply",
  clear: "Clear",
  truncated: (limit: number): string =>
    `Showing only the latest ${plural(limit, "entry", "entries")}. To see older entries, change Since and Until, or raise Limit.`,
  more: (next: number): string => `Show up to ${plural(next, "entry", "entries")}`,
  openSession: "Opens this session in the Steward panel.",
  assistantOnly: "Only Steward's actions",
  assistantOnlyTitle: "Shows only rows whose Via starts with assistant/.",
  columns: {
    time: "Time",
    subject: "Subject",
    via: "Via",
    tenant: "Tenant",
    action: "Action",
    resource: "Resource",
    request: "Request",
    result: "Result",
    requestId: "Request ID",
    type: "Type",
    component: "Component",
    backend: "Backend",
    auditId: "Audit ID",
  },
  empty: "No entries match the filters. Widen the period or clear the filters.",
  mcpEmpty: "No calls match the filters. Widen the period or clear the filters.",
  tabs: {
    api: "Platform API",
    mcp: "MCP proxy",
  },
  page: {
    title: "Audit log",
    duration: (ms: number): string => `${ms}ms`,
  },
};
