/**
 * Agent 캔버스 (D42). 캔버스가 곧 Agent 의 정의다. `/agents/new` 에서는 화면 하나로 서고, 저장된 뒤에는 상세의 정의
 * 탭 안에 들어간다.
 *
 * 캔버스는 jsdom 폴리필(setup.ts) 위에서 그려지고 노드는 data-testid `node-<id>` 로 찾는다.
 */
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { TestApp } from "@/App";
import { dictionary } from "@/i18n";
import { LOADING_LABEL } from "@/lib/names";
import { HttpResponse, agentState, agentTemplateSupport, agentWithSkill, apiError, dsPostgres, dsRedis, gatewayDraftFixture, http, lastRequest, platformSettings, server } from "./msw";
import { openSelect, selectValue } from "./select";

async function selectNode(id: string) {
  fireEvent.click(screen.getByTestId(`node-${id}`));
  await waitFor(() => expect(screen.getByTestId("inspector")).toHaveAttribute("data-selected-node", id));
}

/** 저장된 Agent 의 캔버스는 상세의 정의 탭 안이다. */
const DEFINITION = "/agents/ag-support?tab=definition";

describe("agents (canvas is the definition)", () => {
  it("an empty canvas says what is required and starts from a template", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/agents/new" />);
    const empty = await screen.findByTestId("canvas-empty");
    expect(empty).toHaveTextContent("Chat 트리거·에이전트·모델 노드가 있어야 저장할 수 있습니다.");
    // 아직 아무것도 그리지 않은 캔버스에 빨간 오류 개수를 앞세우지 않는다
    expect(screen.queryByTestId("validation-badge")).not.toBeInTheDocument();
    expect(screen.getByTestId("canvas-validation-idle")).toBeInTheDocument();
    expect(screen.getByTestId("canvas-autolayout")).toHaveAccessibleName("자동 배치. 캔버스에 노드가 없습니다. 팔레트에서 먼저 노드를 놓아 주세요.");
    await user.click(await screen.findByTestId("canvas-empty-template-support-agent"));
    expect(await screen.findByTestId("node-agent-main")).toBeInTheDocument();
    expect(screen.getByTestId("validation-badge")).toBeInTheDocument();
  });

  it("creating saves a draft: the template's empty slots block the save until they are filled, a delete key inside the inspector or palette deletes no node, and the model node carries provider, effort limits and the agents that share it (D28)", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/agents/new?template=support-agent" />);
    expect(await screen.findByTestId("node-agent-main")).toBeInTheDocument();
    const badge = screen.getByTestId("validation-badge");
    await waitFor(() => expect(badge).toHaveAttribute("data-level", "error"));
    expect(badge).toHaveTextContent("오류 3");
    expect(screen.getByTestId("canvas-save")).toBeDisabled();
    await waitFor(() => expect(screen.getByTestId("canvas-save-reason")).toHaveTextContent("오류 3건을 고쳐야 저장할 수 있습니다."));

    await user.clear(screen.getByTestId("ap-name"));
    await user.type(screen.getByTestId("ap-name"), "smoke");

    expect(await screen.findByTestId("node-model-main")).toBeInTheDocument();
    await selectNode("model-main");
    // 인스펙터나 팔레트 안에서 누른 지우기 키는 캔버스 노드를 지우지 않는다
    const nodes = () => document.querySelectorAll(".react-flow__node").length;
    const before = nodes();
    expect(before).toBeGreaterThan(1);
    for (const key of ["Backspace", "Delete"]) {
      fireEvent.keyDown(screen.getByTestId("model-provider"), { key, bubbles: true });
      fireEvent.keyDown(screen.getByTestId("palette"), { key, bubbles: true });
    }
    await waitFor(() => expect(screen.getByTestId("inspector")).toHaveAttribute("data-selected-node", "model-main"));
    expect(nodes()).toBe(before);
    expect(screen.getByTestId("inspector").className).toContain("nokey");
    expect(screen.getByTestId("palette").className).toContain("nokey");

    // 모델 노드는 provider·effort 한도와 그 모델을 같이 쓰는 에이전트를 싣는다 (D28)
    expect(screen.getByTestId("model-temperature-note")).toBeInTheDocument();
    await selectValue(user, "model-provider", "openai-main");
    expect(await screen.findByTestId("model-temperature")).toBeInTheDocument();
    const effortList = await openSelect(user, "model-effort");
    expect(within(effortList).getByRole("option", { name: "max" })).toHaveAttribute("aria-disabled", "true");
    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.getByTestId("node-provider-model-main")).toHaveTextContent("openai-main (openai)"));
    expect(screen.getByTestId("attached-agent-main")).toHaveTextContent("Agent (main)");

    await selectNode("tools-calculator");
    // 도구 소스는 배포된 것만 고를 수 있다 (D42)
    await selectValue(user, "tools-source", "gw-support");
    await selectNode("memory");
    await selectValue(user, "memory-data-service", "ds-pg");

    await waitFor(() => expect(badge).toHaveAttribute("data-level", "ok"));
    await user.click(screen.getByTestId("canvas-save"));

    // 저장하면 상세로 옮겨 가고 그 문서는 아직 안 올라간 상태다
    expect(await screen.findByTestId("agent-detail-page")).toBeInTheDocument();
    const body = lastRequest("POST", "/agents")?.body as { name: string; graph: { nodes: { id: string; data: Record<string, unknown> }[]; edges: unknown[] }; layout: Record<string, unknown> };
    expect(body.name).toBe("smoke");
    expect(body.graph.nodes.map((n) => n.id)).toEqual(["entry", "agent-main", "model-main", "tools-calculator", "memory", "guardrail-input"]);
    expect(body.graph.edges).toEqual(agentTemplateSupport.graph.edges);
  });

  it("에이전트 카드가 붙인 Skill 수를 보이고, 뒤처진 핀은 그 리비전을 더 읽어 skill_behind 경고로 말한다 (D64)", async () => {
    agentState.items = [agentWithSkill({ id: "sk-report", revision: 1 })];
    render(<TestApp initialPath={DEFINITION} />);
    const chip = await screen.findByTestId("node-skills-agent-main");
    expect(chip).toHaveTextContent("Skills 1");
    await waitFor(() => expect(chip).toHaveAttribute("title", "보고서 문체 · r1"));
    await selectNode("agent-main");
    // 목록에는 최신 r2 만 있다. r1 의 메타는 리비전 목록에서 오므로 unknown_skill 로 잘못 막지 않는다
    await waitFor(() => expect(screen.getByTestId("inspector-issues")).toHaveTextContent("skill_behind"));
    expect(screen.getByTestId("inspector-issues")).not.toHaveTextContent("unknown_skill");
    expect(lastRequest("GET", "/skills/sk-report/revisions")).toBeDefined();
  });

  it("a tools source that is not deployed is an error with its own sentence", async () => {
    // 정의만 있는 Gateway 는 부를 주소가 없다. 서버가 저장을 거부하기 전에 캔버스가 먼저 말한다
    server.use(http.get("/gateways", () => HttpResponse.json([gatewayDraftFixture({ id: "gw-support", name: "support" })])));
    render(<TestApp initialPath={DEFINITION} />);
    expect(await screen.findByTestId("node-tools-calculator")).toBeInTheDocument();
    await selectNode("tools-calculator");
    await waitFor(() => expect(screen.getByTestId("inspector-issues")).toHaveTextContent("source_not_deployed"));
    expect(screen.getByTestId("inspector-issues")).toHaveTextContent("먼저 배포하거나");
  });

  it("editing makes a new revision and the detail says the running one is behind", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath={DEFINITION} />);
    expect(await screen.findByTestId("node-agent-main")).toBeInTheDocument();
    await waitFor(() => expect(screen.getByTestId("validation-badge")).toHaveAttribute("data-level", "ok"));
    expect(screen.getByTestId("canvas-undo")).toBeDisabled();

    // 팔레트로 추가하면 미저장 표시가 뜨고 undo 가 살아난다
    await user.click(screen.getByTestId("palette-guardrail"));
    expect(await screen.findByTestId("node-guardrail")).toBeInTheDocument();
    expect(screen.getByTestId("canvas-dirty")).toBeInTheDocument();
    await user.click(screen.getByTestId("canvas-undo"));
    await waitFor(() => expect(screen.queryByTestId("node-guardrail")).not.toBeInTheDocument());
    await user.click(screen.getByTestId("canvas-redo"));
    expect(await screen.findByTestId("node-guardrail")).toBeInTheDocument();
    await selectNode("guardrail");
    await user.click(screen.getByTestId("inspector-delete"));
    await waitFor(() => expect(screen.queryByTestId("node-guardrail")).not.toBeInTheDocument());

    await selectNode("entry");
    await user.clear(screen.getByTestId("entry-max-turns"));
    await user.type(screen.getByTestId("entry-max-turns"), "60");
    await user.click(screen.getByTestId("canvas-validate"));
    await waitFor(() => expect(screen.getByTestId("validation-badge")).toHaveTextContent("서버"));

    await user.click(screen.getByTestId("canvas-save"));
    expect(await screen.findByTestId("canvas-notice")).toHaveTextContent("리비전 r3");
    expect(screen.getByTestId("canvas-notice")).toHaveTextContent("재배포해야");
    const put = lastRequest("PUT", "/agents/ag-support")?.body as { name: string; graph: { nodes: { data: { max_turns?: number } }[] } };
    expect(put.name).toBe("support");
    expect(put.graph.nodes[0].data.max_turns).toBe(60);
    // 정의 탭은 캔버스만 남기므로 머리의 배너가 없다. 탭을 옮기면 돌아온다
    expect(screen.queryByTestId("revision-behind")).not.toBeInTheDocument();
    await user.click(screen.getByTestId("tab-overview"));
    // 정의만 올라갔으므로 돌고 있는 것은 아직 r2 다
    expect(await screen.findByTestId("revision-behind")).toHaveTextContent("재배포해야");
  });

  it("a server-only rule rejects the save and marks the node it names", async () => {
    server.use(
      http.put("/agents/:id", () =>
        apiError(422, "invalid_graph", "graph has 1 error(s)", {
          issues: [{ code: "server_only_rule", message: "서버만 아는 규칙", level: "error", node_id: "agent-main", edge_id: null }],
        }),
      ),
    );
    const user = userEvent.setup();
    render(<TestApp initialPath={DEFINITION} />);
    expect(await screen.findByTestId("node-agent-main")).toBeInTheDocument();
    await selectNode("entry");
    await user.type(screen.getByTestId("entry-greeting"), "!");
    await user.click(screen.getByTestId("canvas-save"));

    const badge = await screen.findByTestId("validation-badge");
    await waitFor(() => expect(badge).toHaveAttribute("data-level", "error"));
    await user.click(badge);
    expect(await screen.findByTestId("validation-issue-0")).toHaveTextContent("server_only_rule");
    expect(screen.getByTestId("node-mark-agent-main")).toBeInTheDocument();
  });

  it("the memory inspector shows the checkpointer chip of the selected Redis (D33)", async () => {
    server.use(http.get("/data-services", () => HttpResponse.json([dsPostgres, { ...dsRedis, capabilities: { checkpointer: "modules_missing" } }])));
    const user = userEvent.setup();
    render(<TestApp initialPath={DEFINITION} />);
    expect(await screen.findByTestId("node-memory")).toBeInTheDocument();
    await selectNode("memory");
    await selectValue(user, "memory-data-service", "ds-redis");
    expect(await screen.findByTestId("memory-checkpointer")).toHaveTextContent("RediSearch·RedisJSON 이 없습니다");
  });

  it("long term memory needs the main agent's filesystem tool, and an empty retention means it is never dropped (D34)", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath={DEFINITION} />);
    expect(await screen.findByTestId("node-memory")).toBeInTheDocument();
    await selectNode("memory");
    expect(screen.getByTestId("memory-long-term-vs-knowledge")).toHaveTextContent("사용자마다 따로 쌓이며 에이전트가 스스로 작성합니다");
    expect(screen.getByTestId("memory-long-term-enabled")).toBeEnabled();

    await selectNode("agent-main");
    await user.click(screen.getByTestId("inspector-tabs-tools"));
    await user.click(screen.getByTestId("agent-builtin-filesystem"));
    await selectNode("memory");
    expect(screen.getByTestId("memory-long-term-enabled")).toBeDisabled();
    expect(screen.getByTestId("memory-long-term-blocked")).toHaveTextContent("내장 파일 Tool 노출");

    await selectNode("agent-main");
    await user.click(screen.getByTestId("inspector-tabs-tools"));
    await user.click(screen.getByTestId("agent-builtin-filesystem"));
    await selectNode("memory");
    await user.click(screen.getByTestId("memory-long-term-enabled"));
    expect(await screen.findByTestId("memory-retention-days")).toHaveValue(null);
    expect(screen.getByTestId("memory-retention-note")).toHaveTextContent("기억을 지우지 않습니다");

    const savedMemory = () => (lastRequest("PUT", "/agents/ag-support")?.body as { graph: { nodes: { id: string; data: Record<string, unknown> }[] } }).graph.nodes.find((n) => n.id === "memory")!.data;
    await user.type(screen.getByTestId("memory-retention-days"), "30");
    await waitFor(() => expect(screen.getByTestId("validation-badge")).toHaveAttribute("data-level", "ok"));
    await user.click(screen.getByTestId("canvas-save"));
    await waitFor(() => expect(savedMemory().long_term).toEqual({ retention_days: 30 }));

    // 다시 끄면 키가 사라져 장기 기억을 몰랐을 때와 같은 본문이 된다
    await user.click(screen.getByTestId("memory-long-term-enabled"));
    await waitFor(() => expect(screen.queryByTestId("memory-retention-days")).not.toBeInTheDocument());
    await user.click(screen.getByTestId("canvas-save"));
    await waitFor(() => expect(Object.keys(savedMemory())).toEqual(["checkpointer"]));
  });

  it("a knowledge node attaches to the selected agent and carries its own tool name (D32)", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath={DEFINITION} />);
    expect(await screen.findByTestId("node-agent-main")).toBeInTheDocument();
    await waitFor(() => expect(screen.getByTestId("validation-badge")).toHaveAttribute("data-level", "ok"));
    await selectNode("agent-main");
    await user.click(screen.getByTestId("palette-knowledge"));
    expect(await screen.findByTestId("node-knowledge")).toBeInTheDocument();
    await selectNode("knowledge");
    expect(screen.getByTestId("inspector-issues")).toHaveTextContent("unknown_knowledge_base");

    await selectValue(user, "knowledge-base", "kb-faq");
    await waitFor(() => expect(screen.getByTestId("node-title-knowledge")).toHaveTextContent("제품 FAQ"));
    const toolName = screen.getByTestId("knowledge-tool-name");
    await user.clear(toolName);
    await user.type(toolName, "task");
    await waitFor(() => expect(screen.getByTestId("inspector-issues")).toHaveTextContent("reserved_tool_name"));
    await user.clear(toolName);
    await user.type(toolName, "search_faq");
    await waitFor(() => expect(screen.getByTestId("inspector-issues")).not.toHaveTextContent("reserved_tool_name"));

    await user.click(screen.getByTestId("canvas-save"));
    await waitFor(() => expect(lastRequest("PUT", "/agents/ag-support")).toBeDefined());
    const put = lastRequest("PUT", "/agents/ag-support")?.body as { graph: { nodes: { id: string; data: Record<string, unknown> }[]; edges: { id: string; source: string; target: string; kind: string }[] } };
    expect(put.graph.nodes.find((n) => n.id === "knowledge")?.data).toMatchObject({ knowledge_base_id: "kb-faq", tool_name: "search_faq" });
    expect(put.graph.edges).toContainEqual({ id: "e-agent-main-knowledge", source: "agent-main", target: "knowledge", kind: "knowledge" });
  });

  it("a reference list that failed to load does not raise false errors or lock the save", async () => {
    server.use(http.get("/settings", () => apiError(503, "upstream_unavailable", "platform settings are unavailable")));
    const user = userEvent.setup();
    render(<TestApp initialPath={DEFINITION} />);
    expect(await screen.findByTestId("node-agent-main")).toBeInTheDocument();
    expect(await screen.findByTestId("canvas-context-error")).toHaveTextContent("읽지 못해");
    await waitFor(() => expect(screen.getByTestId("validation-badge")).toHaveAttribute("data-level", "ok"));
    await user.click(screen.getByTestId("palette-guardrail"));
    await waitFor(() => expect(screen.getByTestId("canvas-save")).toBeEnabled());
  });

  it("one failed lookup still shows the agent and says which lookup failed and why", async () => {
    server.use(http.get("/gateways", () => apiError(403, "forbidden", "gateway:read is required")));
    const app = render(<TestApp initialPath={DEFINITION} />);
    expect(await screen.findByTestId("node-agent-main")).toBeInTheDocument();
    // 에이전트는 참조 조회를 기다리지 않는다. 정의 탭의 캔버스가 그 이름을 보이는 자리는 이름 칸이다
    expect(screen.getByTestId("ap-display-name")).toHaveValue("상담 에이전트");
    // 실패한 조회만 이름으로 말하고 까닭은 공용 오류 문장으로 붙인다
    const notice = await screen.findByTestId("canvas-context-error");
    expect(notice).toHaveTextContent("참조 목록(Gateway)을 읽지 못해");
    expect(notice).not.toHaveTextContent("Knowledge Base");
    expect(notice).toHaveTextContent("권한이 없습니다");
    expect(within(notice).getByTestId("error-code")).toHaveTextContent("forbidden");
    expect(screen.queryByTestId("canvas-context-loading")).not.toBeInTheDocument();
    // 못 읽은 목록을 가리키는 노드는 모른다고 하고, 읽은 목록을 가리키는 노드는 제 이름을 보인다
    await waitFor(() => expect(screen.getByTestId("node-title-memory")).toHaveTextContent("Orders DB"));
    expect(screen.getByTestId("node-title-tools-calculator")).toHaveTextContent(LOADING_LABEL);

    // 참조 목록이 아닌 Tenant Settings 가 실패하면 그것을 말한다. 멀쩡히 읽은 목록을 탓하지 않는다
    app.unmount();
    server.resetHandlers();
    server.use(http.get("/tenants/:tenant/settings", () => apiError(500, "internal_error", "tenant store is down")));
    render(<TestApp initialPath={DEFINITION} />);
    const tenantNotice = await screen.findByTestId("canvas-context-error");
    expect(tenantNotice).toHaveTextContent("참조 목록(Tenant Settings)을 읽지 못해");
    expect(tenantNotice).not.toHaveTextContent("Gateway");
  });

  it("leaving with unsaved work asks first, and loading a revision asks before it overwrites", async () => {
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
    const user = userEvent.setup();
    render(<TestApp initialPath={DEFINITION} />);
    expect(await screen.findByTestId("node-agent-main")).toBeInTheDocument();
    await user.click(screen.getByTestId("palette-guardrail"));
    expect(await screen.findByTestId("canvas-dirty")).toBeInTheDocument();
    await user.click(screen.getByTestId("nav-mcp-hub"));
    expect(confirm).toHaveBeenCalledWith(expect.stringContaining("저장하지 않은 변경이 있습니다"));
    expect(screen.getByTestId("agent-canvas-page")).toBeInTheDocument();

    await selectValue(user, "canvas-revision", "1");
    expect(confirm).toHaveBeenCalledWith(expect.stringContaining("r1을 불러오면"));
    expect(screen.getByTestId("canvas-revision")).toHaveAttribute("value", "2");
  });

  it("an OpenRouter provider follows the openai rules: temperature shown, max off, its default model as placeholder", async () => {
    const user = userEvent.setup();
    const kimi = { name: "kimi", kind: "openrouter" as const, model: null, api_key_secret: "kimi-key", base_url: null };
    server.use(http.get("/settings", () => HttpResponse.json({ ...platformSettings, llm_providers: [...platformSettings.llm_providers, kimi] })));
    render(<TestApp initialPath="/agents/new?template=support-agent" />);
    expect(await screen.findByTestId("node-model-main")).toBeInTheDocument();
    await selectNode("model-main");
    await selectValue(user, "model-provider", "kimi");
    expect(await screen.findByTestId("model-temperature")).toBeInTheDocument();
    expect(screen.getByTestId("model-model")).toHaveAttribute("placeholder", "moonshotai/kimi-k3");
    const effortList = await openSelect(user, "model-effort");
    expect(within(effortList).getByRole("option", { name: "max" })).toHaveAttribute("aria-disabled", "true");
  });

  it("a Z.AI provider shows the GLM effort hint and takes max for the GLM models the runtime maps", async () => {
    const user = userEvent.setup();
    const glm = { name: "glm", kind: "zai" as const, model: null, api_key_secret: "glm-key", base_url: null };
    server.use(http.get("/settings", () => HttpResponse.json({ ...platformSettings, llm_providers: [...platformSettings.llm_providers, glm] })));
    render(<TestApp initialPath="/agents/new?template=support-agent" />);
    expect(await screen.findByTestId("node-model-main")).toBeInTheDocument();
    await selectNode("model-main");
    await selectValue(user, "model-provider", "glm");
    expect(await screen.findByText(dictionary("ko").canvas.model.effortHintGlm)).toBeInTheDocument();
    // 기본 모델 glm-5.3 은 max 를 받는다
    let effortList = await openSelect(user, "model-effort");
    expect(within(effortList).getByRole("option", { name: "max" })).not.toHaveAttribute("aria-disabled", "true");
    await user.keyboard("{Escape}");
    // 런타임이 모르는 GLM 이름은 openai 규칙 그대로 max 가 막힌다
    await user.type(screen.getByTestId("model-model"), "z-ai/glm-5.3");
    effortList = await openSelect(user, "model-effort");
    expect(within(effortList).getByRole("option", { name: "max" })).toHaveAttribute("aria-disabled", "true");
  });
});
