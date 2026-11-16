/**
 * 한국어 사전. 원본이라 이 객체의 모양이 `Messages` 다(../types). 영역마다 모듈 하나이고, 키는 `영역.화면.역할` 이다.
 * 값은 문자열이거나, 문장에 값이 들어가면 매개변수와 반환 타입(string 또는 ReactNode)을 적은 함수다.
 */
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

export const ko = {
  locale: "ko-KR",
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
