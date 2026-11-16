import type { Messages } from "../types";

import { plural } from "./plural";

export const runs: Messages["runs"] = {
  schedules: {
    title: "Schedules",
    empty: "No schedules yet. Add one to run this Agent at set times.",
    add: "Add schedule",
    cron: "Repeat rule (cron)",
    cronHint: "A five-field cron expression. For example, every day at 9:00 is 0 9 * * *.",
    timeZone: "Time zone",
    message: "Message to send",
    nextRun: "Next run",
  },
  pause: "Pause",
  resume: "Resume",
  edit: "Edit",
  deleteBody: "Deleting stops its open runs and cannot be undone.",
  state: { active: "Active", paused: "Paused" },
  triggers: {
    title: "Webhook triggers",
    empty: "No triggers yet. Add one to let another system run this Agent.",
    add: "Add trigger",
    url: "Call URL",
    template: "Message template",
    templateHint: "The received body goes where {{payload}} is, one of its values where {{payload./path}} is.",
    secretOnce: "This signing secret is shown only now. Store it in the calling system.",
    rotate: "Rotate secret",
    secretLabel: "Signing secret",
  },
  history: {
    title: "Run history",
    empty: "No runs yet.",
    runNow: "Run now",
    runNowPlaceholder: "Write the message to send to the Agent.",
    columns: { source: "Source", started: "Started", duration: "Duration", tokens: "Tokens", actsAs: "Runs as", reason: "Reason" },
    openConversation: "Open conversation",
    actsAs: {
      person: (name: string): string => `As ${name}`,
      agent: "As the Agent",
    },
  },
  source: { webhook: "Webhook", manual: "Run now", agent: "Agent call" },
  status: {
    starting: "Starting",
    refused: "Refused at the deadline",
    skipped: (count: number): string => `Skipped ${plural(count, "time", "times")}`,
  },
  reason: {
    previousRunning: "The previous run had not finished.",
    agentRunLimit: "This Agent reached its run limit.",
    missed: "The run time passed while the platform was down.",
    runLost: "The end of the run could not be confirmed.",
    actsAsLostAccess: "The user it runs as can no longer use this Agent.",
    actsAsUnconfirmed: (days: number): string => `The user it runs as has not signed in to Daegam for ${plural(days, "day", "days")}.`,
    agentChanged: "Someone else redeployed the Agent.",
    prerequisites: "A setting that runs need is missing.",
    inputTooLong: "The message is longer than this Agent's input limit.",
    rateOrSessions: "This Agent reached its session or request limit.",
    dailyBudget: "This Agent used up its daily token budget.",
    guardrail: "An input guardrail blocked the message.",
    unavailable: "The Agent did not answer or was not deployed.",
    aborted: "The run could not finish because its pod stopped.",
    accessLost: "The user it runs as lost access during the run.",
  },
  pausedReason: {
    actsAsLostAccess: "Stopped because the user it runs as can no longer use this Agent. Resuming runs it as whoever resumes it.",
    actsAsUnconfirmed: (days: number): string =>
      `Paused because the user it runs as has not signed in to Daegam for ${plural(days, "day", "days")}. Start it again after that user signs in.`,
    agentChanged: "Stopped because someone else redeployed the Agent. Check the change, then resume.",
    agentUndeployed: "Stopped because the Agent was undeployed.",
    expired: "Stopped because it expired. Resuming sets a new expiry.",
    prerequisites:
      "Stopped because a setting runs need (memory node, session budget, approval rules on writing Tools, message length) no longer holds. Fix it, then resume.",
  },
};
