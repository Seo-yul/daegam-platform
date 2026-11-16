import type { Messages } from "../types";
import { common } from "./common";
import { errors } from "./errors";
import { warnings } from "./warnings";
import { resources } from "./resources";
import { shared } from "./shared";
import { detail } from "./detail";
import { ui } from "./ui";
import { shell } from "./shell";
import { account } from "./account";
import { hub } from "./hub";
import { mcpServers } from "./mcp-servers";
import { backendApis } from "./backend-apis";
import { gateways } from "./gateways";
import { mcpMetrics } from "./mcp-metrics";
import { dataServices } from "./data-services";
import { knowledgeBases } from "./knowledge-bases";
import { clusters } from "./clusters";
import { agents } from "./agents";
import { canvas } from "./canvas";
import { chat } from "./chat";
import { assistant } from "./assistant";
import { skills } from "./skills";
import { admin } from "./admin";
import { tenant } from "./tenant";
import { audit } from "./audit";
import { auth } from "./auth";
import { usage } from "./usage";
import { runs } from "./runs";
import { toolRules } from "./tool-rules";

/** 영어 사전. 영역 모듈마다 `Messages["영역"]` 으로 타입을 적어 키가 빠지거나 남으면 tsc 가 실패한다. */
export const en: Messages = {
  locale: "en-US",
  common,
  errors,
  warnings,
  resources,
  shared,
  detail,
  ui,
  shell,
  account,
  hub,
  mcpServers,
  backendApis,
  gateways,
  mcpMetrics,
  dataServices,
  knowledgeBases,
  clusters,
  agents,
  canvas,
  chat,
  assistant,
  skills,
  admin,
  tenant,
  audit,
  auth,
  usage,
  runs,
  toolRules,
};
