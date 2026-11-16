import type { Messages } from "../types";
import { plural } from "./plural";

/** 기본 UI 부품과 공용 lib (F3c). Ready·Progressing·Failed 는 phase 이름이라 한국어 화면처럼 그대로 둔다. */
export const ui: Messages["ui"] = {
  cardGrid: {
    edited: (ago: string): string => `Edited ${ago}`,
    noDescription: "No description",
    menu: (name: string): string => `Actions for ${name}`,
    menuTrigger: (name: string): string => `More actions for ${name}`,
  },
  // 복사 단추의 문장 가운데 들어가므로 소문자 명사다(Copy code)
  codeSnippet: { copyLabel: "code" },
  files: {
    tooLarge: (name: string, size: string, limit: string): string => `"${name}" is ${size}. The limit is ${limit}, so it was not uploaded.`,
  },
  input: { required: "(required)" },
  modal: { close: "Close" },
  table: { actions: "Actions" },
  placeholder: {
    pending: "Coming soon.",
    notFound: "Page not found.",
  },
  deployment: {
    started: {
      deploy: "Deploying.",
      redeploy: "Deploying again.",
      undeploy: "Undeploying.",
    },
    summary: {
      count: (total: number): string => plural(total, "item", "items"),
      failed: (count: number): string => `Failed ${count}`,
      progressing: (count: number): string => `Progressing ${count}`,
      allReady: "all Ready",
      allDraft: "none deployed",
      mixed: (ready: number, draft: number): string => `Ready ${ready} · Not deployed ${draft}`,
    },
  },
  names: {
    nameRule: "Use only lowercase letters, digits and hyphens (-), up to 32 characters. A hyphen cannot come first, last or twice in a row.",
    displayNameRule: "The name shown on screen, up to 80 characters in any script. Leave it empty to use the Name.",
    nameRequired: "Enter a name.",
    nameTooLong: (length: number): string => `Enter 32 characters or fewer. You entered ${length}.`,
    displayNameBlank: "A display name cannot be only spaces.",
    displayNameTooLong: (length: number): string => `Enter a display name of 80 characters or fewer. You entered ${length}.`,
    deleted: "(deleted)",
  },
  pointer: { schemaRefs: (count: number): string => `Used by ${plural(count, "operation", "operations")}` },
  providers: {
    routing: {
      none: "No provider preferences",
      only: (order: string): string => `Only ${order}`,
      noFallback: "No fallback to other providers",
      requireParameters: "Only providers that support all parameters",
    },
  },
  references: {
    revision: {
      current: "Referenced by the definition",
      deployed: "Referenced by the deployed revision",
      both: "Referenced by the definition and the deployed revision",
    },
    viaHubItem: (revision: string): string => `${revision} through a hub item`,
    pinnedCurrent: (revision: number): string => `Definition r${revision}`,
    pinnedDeployed: (revision: number): string => `Deployed r${revision}`,
  },
  tabs: {
    standard: {
      overview: "Overview",
      definition: "Definition",
      deployment: "Deployment",
      metrics: "Metrics",
      advanced: "Advanced",
    },
  },
  unsavedGuard: { confirm: "You have unsaved changes. Leave this page and discard them?" },
  select: {
    noOptions: "(no options)",
  },
};
