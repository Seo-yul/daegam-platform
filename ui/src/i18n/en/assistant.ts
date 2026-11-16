import type { Messages } from "../types";
import { plural } from "./plural";

/** 청지기 패널. 비서는 이름 Steward 로 부르고, 대화는 conversation 이다. */
export const assistant: Messages["assistant"] = {
  follow: "Follow",
  followTitle: "Follows Steward to the screens it opens.",
  closeTitle: "Closes the panel.",
  deleteSession: "Delete conversation",
  loading: "Loading the conversation…",
  running: "In progress",
  runningElsewhere: (seconds: number): string => `A turn started elsewhere is running. Checking every ${plural(seconds, "second", "seconds")}.`,
  empty:
    'What should we build? For example, say "Create an MCP Server from the petstore spec," and Steward handles everything from the Backend API to deployment. It asks for approval before it deploys, deletes or changes settings.',
  pendingPlaceholder: "Approve or reject in the card above. Typing here cancels the pending request.",
  placeholder: (hint: string): string => `How can I help? ${hint}`,
  input: "Message to Steward",
};
