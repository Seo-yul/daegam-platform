import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { TestApp, makeQueryClient } from "@/App";
import { ASSISTANT_MAX_TOOL_CALLS } from "@/api/types";
import { dictionary } from "@/i18n";
import { HttpResponse, apiError, assistantConfigFixture, assistantState, http, lastRequest, meDeveloper, platformSettings, record, server } from "./msw";
import { openSelect, selectValue } from "./select";

/** 연결 확인 결과 배지의 한국어 문구. */
const CHECK_RESULT = dictionary("ko").admin.platformSettings.provider.checkResult;

describe("platform settings (/admin/settings)", () => {
  it("loads the form and PATCHes only the changed keys", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/admin/settings" />);
    await waitFor(() => expect(screen.getByTestId("ps-model")).toHaveValue("claude-opus-5"));
    expect(screen.getByTestId("ps-provider-name-0")).toHaveValue("openai-main");
    expect(screen.getByTestId("ps-spec-generator")).toHaveValue("openai-main");

    // 아무것도 안 바꾸면 요청이 없다
    await user.click(screen.getByTestId("save-button"));
    // 저장하지 않은 것을 "저장했습니다" 와 같은 초록 알림으로 띄우지 않는다
    const notice = await screen.findByTestId("ps-notice");
    expect(notice).toHaveTextContent("바뀐 것이 없어 저장하지 않았습니다.");
    expect(notice).toHaveClass("bg-info-soft");
    expect(lastRequest("PATCH", "/settings")).toBeUndefined();

    const model = screen.getByTestId("ps-model");
    await user.clear(model);
    await user.type(model, "claude-sonnet-5");
    await user.click(screen.getByTestId("ps-provider-add"));
    await user.type(screen.getByTestId("ps-provider-name-1"), "anthropic-main");
    await selectValue(user, "ps-provider-kind-1", "anthropic");
    await user.type(screen.getByTestId("ps-provider-secret-1"), "anthropic-key");
    await selectValue(user, "ps-spec-generator", "anthropic-main");
    await user.click(screen.getByTestId("save-button"));

    await waitFor(() => expect(lastRequest("PATCH", "/settings")).toBeDefined());
    expect(lastRequest("PATCH", "/settings")?.body).toEqual({
      default_model: "claude-sonnet-5",
      // embedding_model 은 D32 의 칸이다. 비우면 null(kind 기본) 이고 anthropic 은 언제나 null 이다
      llm_providers: [
        { name: "openai-main", kind: "openai", model: null, api_key_secret: "openai-key", base_url: null, embedding_model: null, routing: null, stream_usage: true },
        { name: "anthropic-main", kind: "anthropic", model: null, api_key_secret: "anthropic-key", base_url: null, embedding_model: null, routing: null, stream_usage: true },
      ],
      // defaults 는 통째로 간다 (D31 의 assistant 가 더해졌다). 비서 절은 건드리지 않았으니 assistant 키는 없다
      defaults: { spec_generator: "anthropic-main", assistant: null },
    });
    expect(await screen.findByTestId("ps-notice")).toHaveTextContent("저장했습니다");
  });

  it("saves the Steward section and defaults.assistant, and the header toggle appears right after (D31)", async () => {
    const user = userEvent.setup();
    // 비서가 꺼진 서버. 저장이 켜면 GET /assistant/config 도 enabled 를 돌려준다
    assistantState.config = { ...assistantConfigFixture, enabled: false, provider: null, providers: [] };
    server.use(
      http.get("/settings", () =>
        HttpResponse.json({
          ...platformSettings,
          assistant: { enabled: false, confirm_deploy: true, effort: "low", max_tool_calls_per_turn: 24, turn_timeout_seconds: 300 },
          defaults: { spec_generator: "openai-main", assistant: null },
        }),
      ),
      http.patch("/settings", async ({ request }) => {
        const r = await record(request);
        assistantState.config = { ...assistantConfigFixture, enabled: true, provider: "openai-main" };
        return HttpResponse.json({ ...platformSettings, ...(r.body as object), updated_by: "header-admin" });
      }),
    );
    render(<TestApp initialPath="/admin/settings" />);
    await waitFor(() => expect(screen.getByTestId("ps-model")).toHaveValue("claude-opus-5"));
    expect(screen.queryByTestId("assistant-toggle")).not.toBeInTheDocument();
    const section = screen.getByTestId("ps-assistant");
    expect(within(section).getByTestId("ps-assistant-enabled")).not.toBeChecked();
    // 꺼져 있으면 세부 설정이 잠긴다
    expect(within(section).getByTestId("ps-assistant-effort")).toBeDisabled();
    expect(within(section).getByTestId("ps-assistant-max-tool-calls")).toHaveValue(24);
    expect(within(section).getByTestId("ps-assistant-turn-timeout")).toHaveValue(300);

    await user.click(within(section).getByTestId("ps-assistant-enabled"));
    await selectValue(user, "ps-assistant-effort", "medium");
    const max = within(section).getByTestId("ps-assistant-max-tool-calls");
    await user.clear(max);
    await user.type(max, "12");
    await user.click(within(section).getByTestId("ps-assistant-confirm-deploy"));
    await selectValue(user, "ps-assistant-provider", "openai-main");
    await user.click(screen.getByTestId("save-button"));
    await waitFor(() => expect(lastRequest("PATCH", "/settings")).toBeDefined());
    expect(lastRequest("PATCH", "/settings")?.body).toEqual({
      assistant: { enabled: true, confirm_deploy: false, effort: "medium", max_tool_calls_per_turn: 12, turn_timeout_seconds: 300 },
      defaults: { spec_generator: "openai-main", assistant: "openai-main" },
    });
    expect(await screen.findByTestId("ps-notice")).toHaveTextContent("저장했습니다");
    // 저장이 config 쿼리를 무효화해 헤더의 청지기 토글이 바로 나타난다
    expect(await screen.findByTestId("assistant-toggle")).toBeInTheDocument();
  });

  it("shows the assistant fields the server sends, the max tool calls hint from the code constant, and sends nothing for them", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/admin/settings" />);
    await waitFor(() => expect(screen.getByTestId("ps-model")).toHaveValue("claude-opus-5"));
    expect(screen.getByTestId("ps-assistant-enabled")).not.toBeChecked();
    expect(screen.getByTestId("ps-assistant-provider")).toHaveValue("");
    // 도우미 문구의 최대 호출 수는 코드 상수에서 온다
    const hint = screen.getByTestId("ps-assistant-max-tool-calls").parentElement!.querySelector("p")!;
    expect(hint).toHaveTextContent(`최대 ${ASSISTANT_MAX_TOOL_CALLS} 이며`);
    expect(hint).not.toHaveTextContent("{ASSISTANT_MAX_TOOL_CALLS}");
    const ns = screen.getByTestId("ps-ns");
    await user.clear(ns);
    await user.type(ns, "team-{{tenant}");
    await user.click(screen.getByTestId("save-button"));
    await waitFor(() => expect(lastRequest("PATCH", "/settings")).toBeDefined());
    expect(lastRequest("PATCH", "/settings")?.body).toEqual({ namespace_template: "team-{tenant}" });
  });

  it("sends oidc and default_cluster_id when configured, and shows API errors", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/admin/settings" />);
    await waitFor(() => expect(screen.getByTestId("ps-model")).toHaveValue("claude-opus-5"));
    await selectValue(user, "ps-cluster", "cl-edge");
    await user.click(screen.getByTestId("ps-oidc-enabled"));
    await user.type(screen.getByTestId("ps-oidc-issuer"), "https://okta.example.com/oauth2/default");
    await user.type(screen.getByTestId("ps-oidc-client"), "0oa123");
    await user.click(screen.getByTestId("save-button"));
    await waitFor(() => expect(lastRequest("PATCH", "/settings")).toBeDefined());
    expect(lastRequest("PATCH", "/settings")?.body).toEqual({
      default_cluster_id: "cl-edge",
      oidc: { issuer: "https://okta.example.com/oauth2/default", client_id: "0oa123", audience: null },
    });

    server.use(http.patch("/settings", () => apiError(422, "invalid_value", "namespace_template must contain {tenant}")));
    const ns = screen.getByTestId("ps-ns");
    await user.clear(ns);
    await user.type(ns, "fixed");
    await user.click(screen.getByTestId("save-button"));
    expect(await screen.findByTestId("save-error")).toHaveTextContent("invalid_value");
  });

  it("explains that a provider needs a key Secret when the list is empty", async () => {
    server.use(http.get("/settings", () => HttpResponse.json({ ...platformSettings, llm_providers: [], defaults: { spec_generator: null } })));
    render(<TestApp initialPath="/admin/settings" />);
    expect(await screen.findByTestId("ps-providers-empty")).toHaveTextContent(
      "등록된 provider 가 없습니다. provider 를 추가하고 API 키를 넣어야 LLM 기능을 쓸 수 있습니다.",
    );
    expect(screen.getByTestId("ps-provider-secret-hint")).toHaveTextContent("넣은 키는 플랫폼이 플랫폼 네임스페이스의 Secret 에 넣고 화면에는 다시 보이지 않습니다.");
  });

  it("puts an API key into a platform-made Secret without ever sending it in the settings PATCH (D56)", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/admin/settings" />);
    await waitFor(() => expect(screen.getByTestId("ps-provider-name-0")).toHaveValue("openai-main"));
    await user.click(screen.getByTestId("ps-provider-add"));
    await user.type(screen.getByTestId("ps-provider-name-1"), "openai-mini");
    // 키가 없으면 없다고 말한다
    expect(screen.getByTestId("ps-provider-key-state-1")).toHaveTextContent("키가 없습니다.");
    const key = screen.getByTestId("ps-provider-key-1");
    expect(key).toHaveAttribute("type", "password");
    await user.type(key, "sk-new-key-123");
    expect(screen.getByTestId("ps-provider-key-state-1")).toHaveTextContent("저장하면 Secret 에 넣습니다.");
    await user.click(screen.getByTestId("save-button"));

    // 설정 PATCH 가 먼저 provider 를 만들고, 키는 따로 간다
    await waitFor(() => expect(lastRequest("PUT", "/settings/llm-providers/openai-mini/api-key")).toBeDefined());
    const patch = lastRequest("PATCH", "/settings")?.body as { llm_providers: Record<string, unknown>[] };
    expect(patch.llm_providers[1]).toEqual({ name: "openai-mini", kind: "openai", model: null, api_key_secret: null, base_url: null, embedding_model: null, routing: null, stream_usage: true });
    expect(JSON.stringify(patch)).not.toContain("sk-new-key-123");
    expect(lastRequest("PUT", "/settings/llm-providers/openai-mini/api-key")?.body).toEqual({ api_key: "sk-new-key-123" });
    expect(await screen.findByTestId("ps-notice")).toHaveTextContent("openai-mini 의 키를 Secret 에 넣었습니다.");
    // 넣은 키가 통하는지 바로 확인한다
    await waitFor(() => expect(lastRequest("POST", "/settings/llm-providers/openai-mini/check")).toBeDefined());
  });

  it("only a new key, with no other change, is still saved", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/admin/settings" />);
    await waitFor(() => expect(screen.getByTestId("ps-provider-name-0")).toHaveValue("openai-main"));
    await user.type(screen.getByTestId("ps-provider-key-0"), "sk-rotated");
    await user.click(screen.getByTestId("save-button"));
    await waitFor(() => expect(lastRequest("PUT", "/settings/llm-providers/openai-main/api-key")).toBeDefined());
    expect(lastRequest("PATCH", "/settings")).toBeUndefined();
  });

  it("checks a saved provider and shows one badge per result code", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/admin/settings" />);
    await waitFor(() => expect(screen.getByTestId("ps-provider-name-0")).toHaveValue("openai-main"));
    // 운영자가 만든 Secret 을 쓰는 provider 는 그 이름을 보이고, 키 칸은 바꿀 때만 쓴다
    expect(screen.getByTestId("ps-provider-key-state-0")).toHaveTextContent("운영자가 만든 Secret openai-key");
    expect(screen.getByTestId("ps-provider-key-0")).toHaveAttribute("placeholder", "바꿀 때만 입력");

    await user.click(screen.getByTestId("ps-provider-check-0"));
    expect(await screen.findByTestId("ps-provider-check-result-0")).toHaveTextContent(CHECK_RESULT.ok);
    expect(lastRequest("POST", "/settings/llm-providers/openai-main/check")).toBeDefined();

    // 서버 코드마다 합니다체 문구 하나. provider 원문 메시지는 오지 않는다
    for (const code of ["no_secret", "no_api_key", "auth", "connection", "quota_exhausted", "provider_error"] as const) {
      server.use(http.post("/settings/llm-providers/:name/check", () => HttpResponse.json({ ok: false, code })));
      await user.click(screen.getByTestId("ps-provider-check-0"));
      await waitFor(() => expect(screen.getByTestId("ps-provider-check-result-0")).toHaveTextContent(CHECK_RESULT[code]));
    }
    // 요청 자체가 실패하면(home 클러스터 없음 등) 오류 코드를 보인다
    server.use(http.post("/settings/llm-providers/:name/check", () => apiError(422, "no_secret_store", "home cluster is not available")));
    await user.click(screen.getByTestId("ps-provider-check-0"));
    await waitFor(() => expect(screen.getByTestId("ps-provider-check-result-0")).toHaveTextContent("no_secret_store"));

    // 아직 저장하지 않은 행은 서버가 모르므로 확인할 수 없다
    await user.click(screen.getByTestId("ps-provider-add"));
    expect(screen.getByTestId("ps-provider-check-1")).toBeDisabled();
    await user.type(screen.getByTestId("ps-provider-name-1"), "openai-main");
    expect(screen.getByTestId("ps-provider-check-1")).toBeEnabled();
  });

  it("has an Embedding model column per provider: openai sends embedding_model, anthropic is disabled with a hint (D32)", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/admin/settings" />);
    await waitFor(() => expect(screen.getByTestId("ps-provider-name-0")).toHaveValue("openai-main"));
    const emb = screen.getByTestId("ps-provider-embedding-model-0");
    expect(emb).toBeEnabled();
    // 비우면 kind 기본을 placeholder 로 보인다
    expect(emb).toHaveAttribute("placeholder", "text-embedding-3-small");
    await user.type(emb, "text-embedding-3-large");

    await user.click(screen.getByTestId("ps-provider-add"));
    await user.type(screen.getByTestId("ps-provider-name-1"), "anthropic-main");
    await user.type(screen.getByTestId("ps-provider-embedding-model-1"), "oops");
    // anthropic 으로 바꾸면 칸이 잠기고 적혀 있던 값도 비운다 (서버가 anthropic 의 embedding_model 을 거부한다)
    await selectValue(user, "ps-provider-kind-1", "anthropic");
    const emb1 = screen.getByTestId("ps-provider-embedding-model-1");
    expect(emb1).toBeDisabled();
    expect(emb1).toHaveAttribute("title", "임베딩 API 가 없습니다");
    expect(emb1).toHaveValue("");
    await user.type(screen.getByTestId("ps-provider-secret-1"), "anthropic-key");
    await user.click(screen.getByTestId("save-button"));
    await waitFor(() => expect(lastRequest("PATCH", "/settings")).toBeDefined());
    expect((lastRequest("PATCH", "/settings")?.body as { llm_providers: unknown[] }).llm_providers).toEqual([
      { name: "openai-main", kind: "openai", model: null, api_key_secret: "openai-key", base_url: null, embedding_model: "text-embedding-3-large", routing: null, stream_usage: true },
      { name: "anthropic-main", kind: "anthropic", model: null, api_key_secret: "anthropic-key", base_url: null, embedding_model: null, routing: null, stream_usage: true },
    ]);
  });

  it("offers OpenRouter and Z.AI as kinds: default model and address show as placeholders, a typed address is sent, and embedding stays off", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/admin/settings" />);
    await waitFor(() => expect(screen.getByTestId("ps-provider-name-0")).toHaveValue("openai-main"));
    await user.click(screen.getByTestId("ps-provider-add"));
    await user.type(screen.getByTestId("ps-provider-name-1"), "kimi");
    await selectValue(user, "ps-provider-kind-1", "openrouter");
    // 칸을 비우면 서버가 OpenRouter 주소와 Kimi K3 를 쓴다
    expect(screen.getByTestId("ps-provider-model-1")).toHaveAttribute("placeholder", "moonshotai/kimi-k3");
    expect(screen.getByTestId("ps-provider-url-1")).toHaveAttribute("placeholder", "https://openrouter.ai/api/v1");
    const emb = screen.getByTestId("ps-provider-embedding-model-1");
    expect(emb).toBeDisabled();
    expect(emb).toHaveAttribute("title", "OpenRouter provider 는 Knowledge Base 임베딩에 쓰지 않습니다");
    await user.type(screen.getByTestId("ps-provider-secret-1"), "kimi-key");
    // Z.AI 도 칸을 비우면 종량제 주소와 glm-5.3 이고, 주소를 적으면 그 주소가 나간다
    await user.click(screen.getByTestId("ps-provider-add"));
    await user.type(screen.getByTestId("ps-provider-name-2"), "glm");
    await selectValue(user, "ps-provider-kind-2", "zai");
    expect(screen.getByTestId("ps-provider-model-2")).toHaveAttribute("placeholder", "glm-5.3");
    expect(screen.getByTestId("ps-provider-url-2")).toHaveAttribute("placeholder", "https://api.z.ai/api/paas/v4");
    expect(screen.getByTestId("ps-provider-embedding-model-2")).toHaveAttribute("title", "Z.AI provider 는 Knowledge Base 임베딩에 쓰지 않습니다");
    await user.type(screen.getByTestId("ps-provider-url-2"), "https://open.bigmodel.cn/api/paas/v4");
    await user.type(screen.getByTestId("ps-provider-secret-2"), "glm-key");
    await user.click(screen.getByTestId("save-button"));
    await waitFor(() => expect(lastRequest("PATCH", "/settings")).toBeDefined());
    const sent = (lastRequest("PATCH", "/settings")?.body as { llm_providers: unknown[] }).llm_providers;
    expect(sent).toContainEqual({
      name: "kimi",
      kind: "openrouter",
      model: null,
      api_key_secret: "kimi-key",
      base_url: null,
      embedding_model: null,
      routing: null,
      stream_usage: true,
    });
    expect(sent).toContainEqual({
      name: "glm",
      kind: "zai",
      model: null,
      api_key_secret: "glm-key",
      base_url: "https://open.bigmodel.cn/api/paas/v4",
      embedding_model: null,
      routing: null,
      stream_usage: true,
    });
  });

  it("edits OpenRouter provider preferences, and saving another field keeps the stored ones (T19)", async () => {
    const user = userEvent.setup();
    const routed = { name: "kimi", kind: "openrouter" as const, model: null, api_key_secret: "kimi-key", base_url: null, routing: { order: ["moonshotai"], allow_fallbacks: true, require_parameters: true, quantizations: [] } };
    server.use(http.get("/settings", () => HttpResponse.json({ ...platformSettings, llm_providers: [...platformSettings.llm_providers, routed] })));
    render(<TestApp initialPath="/admin/settings" />);
    await waitFor(() => expect(screen.getByTestId("ps-provider-name-1")).toHaveValue("kimi"));
    // openrouter 줄에만 업체 선호 칸이 있고, 접힌 제목이 지금 값을 한 줄로 보인다
    expect(screen.queryByTestId("ps-provider-routing-0")).not.toBeInTheDocument();
    expect(screen.getByTestId("ps-provider-routing-summary-1")).toHaveTextContent("moonshotai · 요청 인자를 모두 지원하는 업체만");
    // 다른 칸만 바꿔 저장해도 업체 선호는 그대로 실린다
    await user.type(screen.getByTestId("ps-provider-model-1"), "moonshotai/kimi-k3");
    await user.click(screen.getByTestId("save-button"));
    await waitFor(() => expect(lastRequest("PATCH", "/settings")).toBeDefined());
    const sent = (body: unknown) => (body as { llm_providers: { name: string; routing: unknown }[] }).llm_providers.find((p) => p.name === "kimi")?.routing;
    expect(sent(lastRequest("PATCH", "/settings")?.body)).toEqual(routed.routing);
    // 업체를 더하고 넘어가지 않게 하면 그 값이 나간다. 모두 기본으로 돌리면 null 이다
    await user.click(screen.getByTestId("ps-provider-routing-summary-1"));
    await user.type(screen.getByTestId("ps-provider-routing-order-1"), ", deepinfra");
    await user.click(screen.getByTestId("ps-provider-routing-no-fallback-1"));
    await user.type(screen.getByTestId("ps-provider-routing-quant-1"), "fp8 bf16");
    await user.click(screen.getByTestId("save-button"));
    await waitFor(() => expect(sent(lastRequest("PATCH", "/settings")?.body)).toEqual({ order: ["moonshotai", "deepinfra"], allow_fallbacks: false, require_parameters: true, quantizations: ["fp8", "bf16"] }));
    await user.clear(screen.getByTestId("ps-provider-routing-order-1"));
    await user.clear(screen.getByTestId("ps-provider-routing-quant-1"));
    await user.click(screen.getByTestId("ps-provider-routing-no-fallback-1"));
    await user.click(screen.getByTestId("ps-provider-routing-require-1"));
    await user.click(screen.getByTestId("save-button"));
    await waitFor(() => expect(sent(lastRequest("PATCH", "/settings")?.body)).toBeNull());
  });

  it("sends a non platform-admin who reached the URL directly back to the first screen", async () => {
    server.use(http.get("/me", () => HttpResponse.json(meDeveloper)));
    render(<TestApp initialPath="/admin/settings" />);
    // 바꿀 수 없는 화면을 읽기 전용으로 열어 두면 다 고쳐 놓고 Save 를 눌러서야 403 을 만난다 (D43)
    expect(await screen.findByTestId("home-chat-page")).toBeInTheDocument();
    expect(screen.queryByTestId("save-button")).not.toBeInTheDocument();
  });
});

/**
 * 플랫폼 설정은 화면 여럿이 같이 읽는다(캔버스 모델 노드·Deploy·Knowledge Base 생성 폼·Refine).
 * 그 화면들이 저마다 다른 쿼리 키로 캐시하면 저장이 한쪽만 무효화해서 나머지가 옛 목록을 계속 보인다.
 * lib/queryKeys.ts 의 PLATFORM_SETTINGS_KEY 하나로 모은 것을 이 테스트가 지킨다.
 */
describe("platform settings cache (lib/queryKeys.ts)", () => {
  it("shows a provider saved in Platform Settings on the Knowledge Base form that shares the cache", async () => {
    const user = userEvent.setup();
    // GET 과 PATCH 가 같은 상태를 본다. 저장한 뒤의 GET 은 새 provider 를 돌려준다
    let current = { ...platformSettings };
    server.use(
      http.get("/settings", () => HttpResponse.json(current)),
      http.patch("/settings", async ({ request }) => {
        const r = await record(request);
        current = { ...current, ...(r.body as object) };
        return HttpResponse.json({ ...current, updated_by: "header-admin" });
      }),
    );
    const client = makeQueryClient();

    // 1) 생성 폼이 먼저 provider 목록을 캐시에 담는다
    const form = render(<TestApp initialPath="/knowledge-bases/new" client={client} />);
    const before = await openSelect(user, await screen.findByTestId("kb-provider"));
    expect(within(before).queryByText(/openai-embed/)).not.toBeInTheDocument();
    form.unmount();

    // 2) Platform Settings 에서 provider 를 하나 더해 저장한다
    const admin = render(<TestApp initialPath="/admin/settings" client={client} />);
    await waitFor(() => expect(screen.getByTestId("ps-provider-name-0")).toHaveValue("openai-main"));
    await user.click(screen.getByTestId("ps-provider-add"));
    await user.type(screen.getByTestId("ps-provider-name-1"), "openai-embed");
    await selectValue(user, "ps-provider-kind-1", "openai");
    await user.type(screen.getByTestId("ps-provider-secret-1"), "openai-embed-key");
    await user.click(screen.getByTestId("save-button"));
    expect(await screen.findByTestId("ps-notice")).toHaveTextContent("저장했습니다");
    admin.unmount();

    // 3) 같은 캐시로 폼을 다시 열면 새 provider 가 보인다. 키가 갈려 있으면 옛 목록이 남는다
    render(<TestApp initialPath="/knowledge-bases/new" client={client} />);
    const after = await openSelect(user, await screen.findByTestId("kb-provider"));
    await waitFor(() => expect(within(after).getByText(/openai-embed/)).toBeInTheDocument());
  });
  it("OTLP 카드는 따로 저장하고 내보내기의 상태를 보인다 (P37 15절)", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/admin/settings" />);
    const card = await screen.findByTestId("ps-otlp");
    expect(await within(card).findByTestId("ps-otlp-status")).toHaveTextContent("실패 2회 · 버린 실행 1건 · 대기 중인 실행 0건");
    await user.click(within(card).getByTestId("ps-otlp-enabled"));
    await user.type(within(card).getByTestId("ps-otlp-endpoint"), "https://otel.example.com/v1/traces");
    await user.type(within(card).getByTestId("ps-otlp-headers"), "daegam-otlp-headers");
    await user.click(within(card).getByTestId("ps-otlp-save"));
    await waitFor(() => expect(lastRequest("PUT", "/settings/observability/otlp")).toBeDefined());
    expect(lastRequest("PUT", "/settings/observability/otlp")?.body).toEqual({
      enabled: true,
      endpoint: "https://otel.example.com/v1/traces",
      headers_secret: "daegam-otlp-headers",
      sample_ratio: 1,
      always_errors: true,
      capture_content: false,
    });
    // 설정 PATCH 에는 실리지 않는다
    expect(lastRequest("PATCH", "/settings")).toBeUndefined();
  });
});
