/**
 * 에이전트 그래프(lib/agentGraph.ts) 테스트는 주제마다 파일 하나다. 여기는 서버와 같은 규칙·code 의 검증이고,
 * 직렬화와 메인 모델은 agent-graph-serialization, 노드별 규칙(장기 기억·지식·노드 data 문장)은 agent-graph-nodes,
 * 연결과 자동 배치는 agent-graph-canvas 에 있다.
 */
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { dictionary } from "@/i18n";
import {
  DEFAULT_SERVICE,
  GLM_EFFORT_MODELS,
  REDIS_CHECKPOINTER_DETAIL,
  checkNodeData,
  defaultNodeData,
  fromTemplate,
  gatewayNeedsAgentOauth,
  issueText,
  serializeGraph,
  validateGraph,
  type Graph,
  type ModelData,
  type ValidationContext,
} from "@/lib/agentGraph";

import { codes, textOf } from "./graph";
import { agentTemplateSupport, filledTemplateGraph, mockValidationContext } from "./msw";

/** 화면이 그리는 검사 이슈의 한국어 표 (사전 canvas.issues). */
const ISSUES = dictionary("ko").canvas.issues;
const ISSUE_TEXT_CODES: readonly string[] = Object.keys(ISSUES.text);
const REDIS_CHECKPOINTER_HINT = ISSUES.redisCheckpointerHint;

describe("agent graph validation (same rules and codes as the server)", () => {
  const ctx: ValidationContext = mockValidationContext();

  it("passes the filled template with no errors", () => {
    const issues = validateGraph(filledTemplateGraph(), { ...DEFAULT_SERVICE, budget: { session_tokens: 200000, daily_tokens: null } }, ctx);
    expect(issues.filter((i) => i.level === "error")).toEqual([]);
  });

  it("marks the template's empty provider (on the model node), source and data service on their nodes", () => {
    const { nodes, edges } = fromTemplate(agentTemplateSupport);
    const issues = validateGraph(serializeGraph(nodes, edges).graph, DEFAULT_SERVICE, ctx);
    expect(issues.map((i) => [i.code, i.node_id])).toEqual([
      ["unknown_provider", "model-main"],
      ["unknown_source", "tools-calculator"],
      ["unknown_data_service", "memory"],
    ]);
  });

  it("model_count: every agent needs exactly one model edge, and a model node may be shared", () => {
    const g = filledTemplateGraph();
    const sub = { ...defaultNodeData("agent", "sub"), name: "helper", description: "helps" };
    g.nodes.push({ id: "agent-sub", type: "agent", data: sub });
    g.edges.push({ id: "e-del", source: "agent-main", target: "agent-sub", kind: "delegate" });
    const missing = validateGraph(g, DEFAULT_SERVICE, ctx);
    expect(missing.filter((i) => i.code === "model_count").map((i) => i.node_id)).toEqual(["agent-sub"]);
    // 붙은 자리로 문장 갈래가 갈린다. 노드에만 붙으면 "모델이 없다", 간선에도 붙으면 "하나만 붙일 수 있다" 다
    expect(textOf(missing.find((i) => i.code === "model_count"))).toContain("모델 노드가 없습니다");

    // 공유: 서브가 메인의 모델을 같이 쓰면 통과
    g.edges.push({ id: "e-sub-model", source: "agent-sub", target: "model-main", kind: "model" });
    const shared = validateGraph(g, DEFAULT_SERVICE, ctx);
    expect(codes(shared)).not.toContain("model_count");
    expect(codes(shared)).not.toContain("unattached_node");

    // 두 번째 모델 간선은 그 간선에 표식, 붙지 않은 모델 노드는 경고
    g.nodes.push({ id: "model-2", type: "model", data: { ...defaultNodeData("model"), provider: "openai-main" } });
    g.edges.push({ id: "e-second", source: "agent-main", target: "model-2", kind: "model" });
    g.nodes.push({ id: "model-3", type: "model", data: { ...defaultNodeData("model"), provider: "openai-main" } });
    const extra = validateGraph(g, DEFAULT_SERVICE, ctx);
    expect(extra.find((i) => i.code === "model_count")?.edge_id).toBe("e-second");
    expect(textOf(extra.find((i) => i.code === "model_count"))).toContain("하나만 붙일 수 있습니다");
    expect(extra.find((i) => i.code === "unattached_node")).toMatchObject({ node_id: "model-3", level: "warning" });
  });

  it("entry_count, main_agent_count, memory_count, memory_required", () => {
    const issues = validateGraph({ nodes: [], edges: [] }, DEFAULT_SERVICE, ctx);
    expect(codes(issues)).toEqual(["entry_count", "main_agent_count"]);

    const g = filledTemplateGraph();
    g.nodes.push({ id: "entry-2", type: "entry", data: defaultNodeData("entry") });
    g.nodes.push({ id: "memory-2", type: "memory", data: { checkpointer: { data_service_id: "ds-pg" } } });
    const more = validateGraph(g, DEFAULT_SERVICE, ctx);
    expect(more.find((i) => i.code === "entry_count")?.node_id).toBe("entry-2");
    expect(more.find((i) => i.code === "memory_count")?.node_id).toBe("memory-2");
    expect(more.find((i) => i.code === "unattached_node")?.node_id).toBe("memory-2");

    // memory 없이 승인·rate_limit·일당 예산이 있으면 memory_required
    const noMemory: Graph = { nodes: g.nodes.filter((n) => n.type !== "memory" && n.id !== "entry-2"), edges: g.edges.filter((e) => e.kind !== "memory") };
    const req = validateGraph(noMemory, { ...DEFAULT_SERVICE, budget: { session_tokens: null, daily_tokens: 1000 } }, ctx);
    const mr = req.find((i) => i.code === "memory_required");
    expect(textOf(mr)).toContain("entry.rate_limit");
    expect(textOf(mr)).toContain("service.budget.daily_tokens");
    expect(textOf(mr)).toContain("tools 'tools-calculator' approval");
    // 이유 이름은 서버가 세는 것과 같고 문장만 한국어다. D33 뒤로 체크포인터는 postgres 만이 아니다
    expect(textOf(mr)).toContain("체크포인터와 공유 카운터가 필요한 곳: ");
  });

  it("edge rules: invalid_edge_kind, dangling_edge, delegation_cycle, unreachable_agent", () => {
    const g = filledTemplateGraph();
    const sub = { ...defaultNodeData("agent", "sub"), name: "helper", description: "helps" };
    g.nodes.push({ id: "agent-sub", type: "agent", data: sub });
    g.nodes.push({ id: "agent-sub2", type: "agent", data: { ...sub, name: "helper2" } });
    g.nodes.push({ id: "agent-sub3", type: "agent", data: { ...sub, name: "helper3" } });
    g.edges.push({ id: "e-bad", source: "tools-calculator", target: "memory", kind: "memory" });
    g.edges.push({ id: "e-bad-model", source: "model-main", target: "agent-main", kind: "model" });
    g.edges.push({ id: "e-gone", source: "agent-main", target: "nope", kind: "tools" });
    g.edges.push({ id: "e-del", source: "agent-main", target: "agent-sub", kind: "delegate" });
    g.edges.push({ id: "e-del2", source: "agent-sub", target: "agent-sub2", kind: "delegate" });
    g.edges.push({ id: "e-self", source: "agent-main", target: "agent-main", kind: "delegate" });
    const issues = validateGraph(g, DEFAULT_SERVICE, ctx);
    expect(issues.find((i) => i.code === "invalid_edge_kind" && i.edge_id === "e-bad")).toBeTruthy();
    expect(textOf(issues.find((i) => i.code === "invalid_edge_kind" && i.edge_id === "e-bad-model"))).toContain("모델 노드에서 에이전트 노드로 가는 모델 간선입니다");
    expect(issues.find((i) => i.code === "dangling_edge")?.edge_id).toBe("e-gone");
    expect(issues.find((i) => i.code === "delegation_cycle")?.edge_id).toBe("e-self");
    // 서브는 위임 못 한다 (서버와 같이 그 간선도 도달성에는 센다)
    expect(textOf(issues.find((i) => i.code === "invalid_edge_kind" && i.edge_id === "e-del2"))).toContain("서브 에이전트는 위임하지 못합니다");
    expect(issues.filter((i) => i.code === "unreachable_agent").map((i) => i.node_id)).toEqual(["agent-sub3"]);
    // 역할마다 다음에 할 일이 다르다. 서브는 위임으로, 메인은 입구로 잇는다
    expect(textOf(issues.find((i) => i.code === "unreachable_agent"))).toContain("메인 에이전트의 위임 포트에서");
  });

  it("provider rules on the model node: provider_no_key, unsupported_param warning, duplicate_agent_name", () => {
    const g = filledTemplateGraph();
    const withProviders: ValidationContext = {
      ...ctx,
      providers: [
        ...ctx.providers,
        { name: "anthropic-nokey", kind: "anthropic", model: "claude-opus-5", api_key_secret: null, base_url: null, stream_usage: true },
        { name: "anthropic-ok", kind: "anthropic", model: "claude-opus-5", api_key_secret: "ak", base_url: null, stream_usage: true },
        { name: "stub", kind: "stub", model: null, api_key_secret: null, base_url: null, stream_usage: true },
      ],
    };
    const model = g.nodes.find((n) => n.id === "model-main")!.data as ModelData;
    model.provider = "anthropic-ok";
    model.temperature = 0.3;
    model.fallback = { provider: "anthropic-nokey", model: null };
    g.nodes.push({ id: "agent-dup", type: "agent", data: { ...defaultNodeData("agent", "sub"), name: "Agent", description: "dup" } });
    g.nodes.push({ id: "model-stub", type: "model", data: { ...defaultNodeData("model"), provider: "stub" } });
    g.edges.push({ id: "e-dup", source: "agent-main", target: "agent-dup", kind: "delegate" });
    g.edges.push({ id: "e-dup-model", source: "agent-dup", target: "model-stub", kind: "model" });
    const issues = validateGraph(g, DEFAULT_SERVICE, withProviders);
    expect(issues.find((i) => i.code === "unsupported_param")).toMatchObject({ level: "warning", node_id: "model-main" });
    expect(issues.find((i) => i.code === "provider_no_key")).toMatchObject({ node_id: "model-main" });
    expect(textOf(issues.find((i) => i.code === "provider_no_key"))).toContain("키가 없는 폴백 provider: anthropic-nokey");
    expect(issues.find((i) => i.code === "duplicate_agent_name")?.node_id).toBe("agent-dup");
    // stub 은 키가 없어도 provider_no_key 가 아니다
    expect(issues.filter((i) => i.code === "provider_no_key")).toHaveLength(1);
    expect(issues.filter((i) => i.node_id === "model-stub")).toEqual([]);
  });

  it("agent_oauth 는 oidc 모드에서만 요구한다. local 은 플랫폼이 낸 토큰으로 Gateway 를 부른다 (D41)", () => {
    expect(gatewayNeedsAgentOauth("oidc")).toBe(true);
    expect(gatewayNeedsAgentOauth("local")).toBe(false);
    expect(gatewayNeedsAgentOauth("header")).toBe(false);
    expect(gatewayNeedsAgentOauth(undefined)).toBe(false);
  });

  it("source rules in oidc mode: agent_oauth_missing and agent_client_id_not_allowed", () => {
    const g = filledTemplateGraph();
    const oidc: ValidationContext = { ...ctx, oidcMode: true, agentOauth: null, agentClientIds: [] };
    expect(codes(validateGraph(g, DEFAULT_SERVICE, oidc))).toContain("agent_oauth_missing");
    const cid: ValidationContext = { ...oidc, agentOauth: { client_id: "daegam-agent" }, agentClientIds: ["other"] };
    expect(codes(validateGraph(g, DEFAULT_SERVICE, cid))).toContain("agent_client_id_not_allowed");
    const ok: ValidationContext = { ...cid, agentClientIds: ["daegam-agent"] };
    expect(codes(validateGraph(g, DEFAULT_SERVICE, ok))).not.toContain("agent_client_id_not_allowed");
    // header 모드는 조건이 없다
    expect(codes(validateGraph(g, DEFAULT_SERVICE, ctx))).not.toContain("agent_oauth_missing");
  });

  it("memory node takes postgres or a redis whose verify left checkpointer ready (D33)", () => {
    const g = filledTemplateGraph();
    const memory = g.nodes.find((n) => n.id === "memory")!.data as { checkpointer: { data_service_id: string } };
    memory.checkpointer.data_service_id = "ds-redis";
    // 픽스처의 redis 는 capabilities.checkpointer 가 ready 라 통과한다
    expect(validateGraph(g, DEFAULT_SERVICE, ctx).filter((i) => i.level === "error")).toEqual([]);

    // verify 가 modules_missing·unknown 을 남겼거나 아직 verify 전(capability 없음)이면 서버와 같은 코드의 오류이고,
    // 화면 문장은 세 갈래를 갈라 무엇을 하면 되는지까지 적는다
    const withRedis = (capabilities?: Record<string, string>): ValidationContext => ({ ...ctx, dataServices: ctx.dataServices.map((d) => (d.id === "ds-redis" ? { ...d, capabilities } : d)) });
    const missing = validateGraph(g, DEFAULT_SERVICE, withRedis({ checkpointer: "modules_missing" })).find((i) => i.code === "redis_modules_missing");
    expect(missing?.level).toBe("error");
    expect(missing?.node_id).toBe("memory");
    expect(textOf(missing)).toContain(REDIS_CHECKPOINTER_HINT.modules_missing);
    expect(textOf(missing)).toContain("(Database: cache)");
    const unknown = validateGraph(g, DEFAULT_SERVICE, withRedis({ checkpointer: "unknown" })).find((i) => i.code === "redis_modules_missing");
    expect(textOf(unknown)).toContain(REDIS_CHECKPOINTER_HINT.unknown);
    for (const capabilities of [undefined, {}, { pgvector: "installed" }] as (Record<string, string> | undefined)[]) {
      const none = validateGraph(g, DEFAULT_SERVICE, withRedis(capabilities)).find((i) => i.code === "redis_modules_missing");
      expect(textOf(none), JSON.stringify(capabilities)).toContain(REDIS_CHECKPOINTER_HINT.none);
    }
    // db 가 0 이 아니면 RediSearch 가 인덱스를 못 만들어 파드가 안 뜬다 (F5). modules 검사와 독립이다
    const withDb = (db: number | null): ValidationContext => ({ ...ctx, dataServices: ctx.dataServices.map((d) => (d.id === "ds-redis" ? { ...d, db } : d)) });
    const dbIssue = validateGraph(g, DEFAULT_SERVICE, withDb(3)).find((i) => i.code === "redis_db_not_zero");
    expect(dbIssue?.level).toBe("error");
    expect(dbIssue?.node_id).toBe("memory");
    expect(textOf(dbIssue)).toContain("(Database: cache, db: 3)");
    for (const db of [0, null] as (number | null)[]) {
      expect(codes(validateGraph(g, DEFAULT_SERVICE, withDb(db))), String(db)).not.toContain("redis_db_not_zero");
    }

    // postgres 는 capability·db 와 무관하게 통과한다
    memory.checkpointer.data_service_id = "ds-pg";
    expect(codes(validateGraph(g, DEFAULT_SERVICE, ctx))).not.toContain("redis_modules_missing");
    expect(codes(validateGraph(g, DEFAULT_SERVICE, { ...ctx, dataServices: ctx.dataServices.map((d) => (d.id === "ds-pg" ? { ...d, db: 5 } : d)) }))).not.toContain("redis_db_not_zero");

    // postgres·redis 가 아닌 종류와 빈 자리는 무엇을 골랐고 무엇을 고르면 되는지를 말한다
    const other = validateGraph(g, DEFAULT_SERVICE, { ...ctx, dataServices: [{ id: "ds-pg", name: "orders-db", kind: "mysql" }] }).find((i) => i.code === "invalid_data_service_kind");
    expect(textOf(other)).toBe("이 Database 는 체크포인터로 쓸 수 없습니다. Postgres 나 Redis 를 선택해 주세요. (선택한 Database: orders-db, 종류: mysql)");
    memory.checkpointer.data_service_id = "";
    expect(textOf(validateGraph(g, DEFAULT_SERVICE, ctx).find((i) => i.code === "unknown_data_service"))).toBe("체크포인터로 쓸 Database 를 정하지 못했습니다. 오른쪽 패널에서 Postgres 나 Redis Database 를 선택해 주세요.");
  });

  /*
   * 화면 문장은 한국어이고 서버는 같은 규칙에 영어 문장을 낸다. 그래서 둘이 같은지 보는 자리는 "문장" 이 아니라
   * "code" 다. 아래 둘이 그것을 지킨다. 하나는 UI 의 한국어 표가 서버가 내는 code 를 다 덮는지, 다른 하나는
   * 서버가 준 이슈가 그 표를 거쳐 한국어로 나오는지다.
   */
  it("한국어 문장 표가 서버가 내는 code 를 다 덮는다 (문장은 다르고 code 는 같다)", () => {
    const src = readFileSync(resolve(process.cwd(), "../platform/daegam/services/agent_graph.py"), "utf8");
    // Skill 규칙은 서버도 따로 둔 모듈이다(D64). `Finding(상수, ...)` 의 상수를 그 파일의 값으로 푼다
    const skills = readFileSync(resolve(process.cwd(), "../platform/daegam/services/agent_skills.py"), "utf8");
    const constants = new Map([...skills.matchAll(/^([A-Z_]+) = "([a-z_]+)"$/gm)].map((m) => [m[1], m[2]]));
    const skillCodes = [...skills.matchAll(/Finding\(\s*([A-Z_]+),/g)].map((m) => constants.get(m[1]) ?? m[1]);
    // 저장과 배포 앞의 경고 하나는 tool_pins.py 가 낸다(`builtin_name_issues`). 상수의 값을 그 파일에서 읽는다
    const pins = readFileSync(resolve(process.cwd(), "../platform/daegam/services/tool_pins.py"), "utf8");
    const pinCodes = [...pins.matchAll(/^BUILTIN_TOOL_NAME = "([a-z_]+)"$/gm)].map((m) => m[1]);
    expect(pinCodes).toEqual(["builtin_tool_name"]);
    // code mode 스크립트의 선택과 중복 오류는 agent_code_mode.py 가 낸다(P56 6.5절). 상수의 값을 그 파일에서 읽는다
    const codeMode = readFileSync(resolve(process.cwd(), "../platform/daegam/services/agent_code_mode.py"), "utf8");
    const codeModeCodes = [...codeMode.matchAll(/^CODE_MODE_[A-Z_]+ = "([a-z_]+)"$/gm)].map((m) => m[1]);
    expect(codeModeCodes.sort()).toEqual(["code_mode_duplicate_script", "code_mode_partial_selection"]);
    const serverCodes = [...new Set([...[...src.matchAll(/\.(?:error|warn)\(\s*"([a-z_]+)"/g)].map((m) => m[1]), ...skillCodes, ...pinCodes, ...codeModeCodes])];
    expect(serverCodes.length).toBeGreaterThan(20);
    expect(skillCodes).toContain("skill_behind");
    // 서버가 내는데 표에 없는 code 는 없어야 한다. 있으면 그 이슈가 영어로 화면에 나온다
    expect(serverCodes.filter((c) => !ISSUE_TEXT_CODES.includes(c)).sort()).toEqual([]);
    // 거꾸로 표에만 있는 것은 P17 F1(플랫폼 compile·검증)이 아직 내지 않는 셋뿐이다. 화면이 먼저 막고 있는
    // 자리이고 code 이름은 플랫폼과 맞춰 두었다. 서버가 내기 시작하면 이 목록에서 지운다
    expect(ISSUE_TEXT_CODES.filter((c) => !serverCodes.includes(c)).sort()).toEqual(["long_term_filesystem_disabled", "long_term_needs_memory", "redis_db_not_zero"].filter((c) => !serverCodes.includes(c)));
  });

  it("캔버스가 max 를 여는 GLM 이름이 런타임의 GLM_EFFORTS 와 같다", () => {
    const src = readFileSync(resolve(process.cwd(), "../agent-runtime/runtime/models.py"), "utf8");
    const table = /^GLM_EFFORTS = \{([^}]*)\}/m.exec(src)?.[1] ?? "";
    const names = [...table.matchAll(/"(glm-[0-9.]+)":/g)].map((m) => m[1]);
    expect(names.length).toBeGreaterThan(0);
    expect([...GLM_EFFORT_MODELS].sort()).toEqual(names.sort());
  });

  it("builtin_tool_name 은 서버가 준 data 로 겹친 Tool 과 에이전트를 문장에 끼운다", () => {
    const issue = {
      level: "warning" as const,
      node_id: "tools-1",
      edge_id: null,
      code: "builtin_tool_name",
      message: "server text",
      data: { clashes: [{ agent: "상담원", names: ["task", "ls"] }, { agent: "helper", names: ["ls"] }] },
    };
    expect(issueText(dictionary("ko"), issue)).toContain("겹치는 Tool: task (상담원), ls (상담원), ls (helper)");
    expect(issueText(dictionary("en"), issue)).toContain("Clashing Tools: task (상담원), ls (상담원), ls (helper)");
    // 꼴이 맞지 않는 data 는 건너뛴다
    expect(issueText(dictionary("en"), { ...issue, data: { clashes: [null, { agent: 1 }, "x"] } })).toMatch(/Clashing Tools: $/);
    expect(issueText(dictionary("en"), { ...issue, data: undefined })).toMatch(/Clashing Tools: $/);
  });

  it("issueText: 서버가 준 영어 이슈를 code 로 한국어로 바꾸고, 모르는 code 는 서버 문장을 그대로 낸다", () => {
    const at = { level: "error" as const, node_id: null, edge_id: null };
    const ko = dictionary("ko");
    // 서버 문장(영어) → 표의 한국어
    expect(issueText(ko, { ...at, code: "entry_count", message: "the graph needs exactly one entry node" })).toBe("Chat 트리거 노드가 하나 있어야 합니다. 팔레트에서 Chat 트리거를 추가해 주세요.");
    // 같은 code 라도 표식이 붙은 자리로 갈래가 갈린다
    expect(issueText(ko, { ...at, code: "entry_count", message: "only one entry node is allowed", node_id: "entry-2" })).toContain("하나만 둘 수 있습니다");
    // 표에 없는 code (서버가 규칙을 더한 때) 는 서버 문장 그대로다. 객체의 기본 속성 이름도 표에 없는 code 다
    expect(issueText(ko, { ...at, code: "server_only_rule", message: "서버만 아는 규칙" })).toBe("서버만 아는 규칙");
    expect(issueText(ko, { ...at, code: "constructor", message: "server text" })).toBe("server text");
    // 캔버스가 낸 이슈는 문장 없이 code 와 상세의 인자만 든다. 문장은 그릴 때 표의 문장 뒤에 상세를 붙여 만든다
    const own = validateGraph({ nodes: [{ id: "memory", type: "memory", data: { checkpointer: { data_service_id: "nope" } } }], edges: [] }, DEFAULT_SERVICE, ctx).find((i) => i.code === "unknown_data_service")!;
    expect(own).toEqual({ ...at, code: "unknown_data_service", node_id: "memory", detail: { key: "databaseMissing", args: ["nope"] } });
    expect(issueText(ko, own)).toBe("체크포인터로 쓸 Database 를 정하지 못했습니다. 오른쪽 패널에서 Postgres 나 Redis Database 를 선택해 주세요. (이 테넌트에서 찾지 못한 Database: nope)");
  });

  it("redis_modules_missing details are the server's REDIS_CHECKPOINTER_DETAIL, letter for letter", () => {
    // 정본은 platform/daegam/services/agent_graph.py 다. 셋이 다 그 파일에 글자대로 있어야 한다
    const src = readFileSync(resolve(process.cwd(), "../platform/daegam/services/agent_graph.py"), "utf8");
    const block = src.slice(src.indexOf("REDIS_CHECKPOINTER_DETAIL: dict"), src.indexOf("def _check_memory"));
    for (const detail of Object.values(REDIS_CHECKPOINTER_DETAIL)) expect(block, detail).toContain(`"${detail}"`);
    expect(block.match(/^\s+(None|"[a-z_]+"): "/gm)).toHaveLength(Object.keys(REDIS_CHECKPOINTER_DETAIL).length);
  });

  it("invalid_node_data mirrors the server field checks", () => {
    expect(checkNodeData("entry", { session_ttl_seconds: 0 })).toBe("session_ttl_seconds: Input should be greater than or equal to 1");
    expect(checkNodeData("agent", { role: "sub", name: "", description: "" })).toBe("name: String should have at least 1 character");
    expect(checkNodeData("agent", { ...defaultNodeData("agent"), context: { compaction: { trigger: { tokens: 1, fraction: 0.5 }, keep: { fraction: 0.1 } } } })).toContain("exactly one of tokens, messages, fraction");
    // 모델 필드가 에이전트에 남아 있으면 서버(extra=forbid)와 같이 거부한다
    expect(checkNodeData("agent", { ...defaultNodeData("agent"), provider: "openai-main" })).toBe("provider: Extra inputs are not permitted");
    expect(checkNodeData("model", { ...defaultNodeData("model"), name: "" })).toBe("name: String should have at least 1 character");
    expect(checkNodeData("model", { ...defaultNodeData("model"), max_output_tokens: 0 })).toBe("max_output_tokens: Input should be greater than or equal to 1");
    expect(checkNodeData("model", { ...defaultNodeData("model"), reasoning: { effort: "ultra" } })).toContain("reasoning.effort");
    expect(checkNodeData("model", { ...defaultNodeData("model"), temperature: -1 })).toContain("temperature");
    expect(checkNodeData("model", { ...defaultNodeData("model"), fallback: { provider: "" } })).toBe("fallback.provider: String should have at least 1 character");
    expect(checkNodeData("model", defaultNodeData("model"))).toBeNull();
    expect(checkNodeData("tools", { source: { kind: "gateway_instance", id: "" }, allow_tools: ["Add", "Add"] })).toBe("data: Value error, allow_tools contains duplicates");
    expect(checkNodeData("tools", { source: { kind: "gateway_instance", id: "" }, approval: { Divide: [] } })).toContain("at least one decision");
    expect(checkNodeData("guardrail", { position: "input", rules: [] })).toBe("rules: List should have at least 1 item after validation, not 0");
    expect(checkNodeData("guardrail", { position: "input", rules: [{ kind: "deny_pattern", value: "(" }] })).toContain("invalid regex");
    expect(checkNodeData("guardrail", { position: "input", rules: [{ kind: "pii_mask", value: { types: ["regex"] } }] })).toContain("regex is required");
    expect(checkNodeData("guardrail", { position: "input", rules: [{ kind: "max_length", value: 10 }] })).toBeNull();
    // 서브에이전트는 설명이 필수
    const g: Graph = { nodes: [{ id: "s", type: "agent", data: { ...defaultNodeData("agent", "sub"), description: " " } }], edges: [] };
    expect(textOf(validateGraph(g, DEFAULT_SERVICE, ctx).find((i) => i.code === "invalid_node_data"))).toContain("서브 에이전트에는 설명이 필요합니다");
  });
});
