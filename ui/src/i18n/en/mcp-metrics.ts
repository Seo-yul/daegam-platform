import type { Messages } from "../types";
import { plural } from "./plural";

export const mcpMetrics: Messages["mcpMetrics"] = {
  window: { "24h": "Last 24 hours", "7d": "Last 7 days", "30d": "Last 30 days" },
  calls: "Tool calls",
  errors: "Errors",
  outcome: {
    success: "Success",
    application_error: "Error answer",
    denied: "Denied",
    failure: "Bad request",
    error: "Server error",
    unknown: "Unknown",
  },
  duration: "Latency p50 / p95 / max (ms)",
  approx: "p50 and p95 are estimated from time ranges.",
  otherCalls: (reads: number, prompts: number): string => `${plural(reads, "resource read", "resource reads")} · ${plural(prompts, "prompt get", "prompt gets")}`,
  rolledUpAt: "Last rolled up",
  chartLabel: "Tool calls by outcome",
  chartEmpty: "No calls to chart.",
  byTool: "Calls by Tool",
  byBackend: "Calls by backend",
  otherTools: "Other tools",
  removedBackend: "Backend not in the current deployment",
  noBackend: "Calls without a backend",
  notes: {
    toolErrors: "Calls a Tool answered with an error result (isError), and errors inside streamed answers, count as success. Tool failures of an Agent's calls are in that Agent's Metrics tab.",
    collector: "Call records are read from pod logs. Records not read before the log rotates are missing, and counts appear a few minutes late.",
    hops: "A call through Gateways counts once on each Gateway on its way and once on the backend MCP Server. Each duration is measured at its own hop.",
  },
  auditOff: "This cluster's ToolHive version does not record Gateway calls, so there are no metrics.",
  collectorOff: "Call record collection is off, so there are no metrics.",
  auditPending: "This Gateway is waiting to be applied again so that it records calls. Calls before that are not recorded.",
  countingFrom: (time: string): string => `Calls before ${time} are still being counted.`,
};
