import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { TestApp } from "@/App";
import { ApiError } from "@/api/client";
import { EMPTY_AGENT_EXPOSURE, agentExposureFormOf, parseCorsOrigins, toAgentExposureInput } from "@/components/AgentExposureSection";
import { exposureFieldErrors } from "@/lib/exposure";
import { stubClipboard } from "./clipboard";
import { signInLocal } from "./localSignIn";
import { agentDraftFixture, agentState, apiError, authConfigState, exposedAgentFixture, http, HttpResponse, lastRequest, resetAgentState, server, TAKEN_HOST, tenantSettings } from "./msw";
import { oidcStub } from "./stubs/oidc-client-ts";

/**
 * Agent 외부 노출 (D27, D42). 배포 대화상자의 노출 섹션(oidc 전용 잠금·cors_origins·allowed_groups·422 매핑),
 * 상세의 external_url·외부 호출 카드, 재배포 대화상자의 exposure 본문.
 *
 * 배포가 문서에 거는 동작이 되면서 노출 섹션도 폼 페이지가 아니라 대화상자 안에 산다.
 */

/**
 * oidc 모드로 바꾼다. 템플릿의 Gateway 도구 소스가 oidc 모드에서 요구하는 agent_oauth 도 채워 그래프 검증이 통과하게 한다.
 * 로그인한 사용자도 같이 심는다. oidc 모드의 initAuth 는 UserManager 에 사용자가 있을 때만 /me 를 부르므로,
 * 안 심으면 me 가 null 로 남고 배포 권한 검사가 거짓이 되어 배포·재배포 버튼이 사라진다.
 */
function enterOidcMode() {
  authConfigState.mode = "oidc";
  oidcStub.user = { access_token: "tok-oidc" };
  server.use(
    http.get("/tenants/:tenant/settings", ({ params }) =>
      HttpResponse.json({
        ...tenantSettings,
        tenant: params.tenant,
        agent_oauth: { client_id: "agent-bot", secret_name: "agent-bot-oauth", token_url: "https://okta.example.com/oauth2/default/v1/token", scope: "mcp" },
        agent_client_ids: ["agent-bot"],
      }),
    ),
  );
}

/** 미배포 Agent 의 배포 대화상자를 연다. 문서 이름은 support 로 둔다 (host 기본값이 거기서 온다). */
async function openAgentDeploy(user: ReturnType<typeof userEvent.setup>) {
  agentState.items = [agentDraftFixture({ id: "ag-draft", name: "support", display_name: "상담 에이전트" })];
  render(<TestApp initialPath="/agents/ag-draft?tab=deployment" />);
  await user.click(await screen.findByTestId("deployment-deploy"));
  return screen.findByTestId("deploy-dialog");
}

describe("agent exposure helpers", () => {
  it("parses one origin per line, lower-cases, dedupes and reports invalid lines", () => {
    expect(parseCorsOrigins("https://App.Example.com\n\n https://app.example.com \nhttp://localhost:5173")).toEqual({
      origins: ["https://app.example.com", "http://localhost:5173"],
      invalid: [],
    });
    expect(parseCorsOrigins("https://app.example.com/path\nftp://x\napp.example.com").invalid).toEqual(["https://app.example.com/path", "ftp://x", "app.example.com"]);
  });

  it("builds the request body and the form from a snapshot", () => {
    expect(toAgentExposureInput(EMPTY_AGENT_EXPOSURE)).toEqual({ enabled: false });
    expect(toAgentExposureInput({ ...EMPTY_AGENT_EXPOSURE, enabled: true, host: "a.example.com", tlsMode: "none", corsOrigins: "HTTPS://app.example.com", allowedGroups: ["g1"] })).toEqual({
      enabled: true,
      host: "a.example.com",
      ingress_class: null,
      tls: { mode: "none", secret_name: null, issuer: null },
      cors_origins: ["https://app.example.com"],
      allowed_groups: ["g1"],
    });
    const form = agentExposureFormOf(exposedAgentFixture().exposure, "daegam-default");
    expect(form).toMatchObject({ namespace: "daegam-default", enabled: true, host: "support.default.apps.local", tlsMode: "secret", secretName: "calc-tls", corsOrigins: "https://app.example.com", allowedGroups: [] });
    expect(exposureFieldErrors(new ApiError(422, { detail: { code: "invalid_cors_origin", detail: "bad" } }))).toEqual({ cors: "bad" });
  });
});

describe("agent deploy dialog exposure", () => {
  it("header mode: the toggle is locked with the oidc-only notice regardless of the cluster allow_unauthenticated", async () => {
    const user = userEvent.setup();
    await openAgentDeploy(user);
    const section = await screen.findByTestId("ai-exposure-section");
    expect(within(section).getByTestId("ai-exposure-oidc-only")).toHaveTextContent("외부 노출은 oidc 모드에서만 켤 수 있습니다.");
    // home 클러스터는 allow_unauthenticated 가 true 지만 Agent 는 그 분기를 타지 않는다
    await waitFor(() => expect(lastRequest("GET", "/clusters/cl-home/exposure-options")).toBeDefined());
    await waitFor(() => expect(screen.getByTestId("exp-namespace")).toHaveValue("daegam-default"));
    expect(screen.getByTestId("exp-enabled")).toBeDisabled();
    expect(screen.queryByTestId("exp-disabled-reason")).not.toBeInTheDocument();
    expect(screen.queryByTestId("exp-warning")).not.toBeInTheDocument();
  });

  it("oidc mode: enables exposure with cors origins and allowed groups, validates origins on the client and sends them in the body", async () => {
    enterOidcMode();
    const user = userEvent.setup();
    const dialog = await openAgentDeploy(user);
    await waitFor(() => expect(screen.getByTestId("exp-namespace")).toHaveValue("daegam-default"));
    expect(screen.queryByTestId("ai-exposure-oidc-only")).not.toBeInTheDocument();
    await waitFor(() => expect(screen.getByTestId("exp-enabled")).toBeEnabled());
    await user.click(screen.getByTestId("exp-enabled"));
    // 무인증 경고는 Agent 에 없다. host 는 문서 이름에서 오고 미리보기는 경로가 없다
    expect(screen.queryByTestId("exp-warning")).not.toBeInTheDocument();
    await waitFor(() => expect(screen.getByTestId("exp-host")).toHaveValue("support.default.apps.local"));
    expect(screen.getByTestId("exp-preview")).toHaveTextContent("http://support.default.apps.local");
    expect(screen.getByTestId("exp-preview")).not.toHaveTextContent("/mcp");

    // 그룹이 비어 있으면 경고 배지, 칩을 더하면 사라진다
    expect(screen.getByTestId("ai-allowed-groups-empty")).toHaveTextContent("인증된 모든 사용자가 이 Agent 를 부를 수 있습니다.");
    await user.type(screen.getByTestId("ai-allowed-groups"), "daegam-default-dev{Enter}");
    expect(screen.getByTestId("ai-allowed-groups-chip-daegam-default-dev")).toBeInTheDocument();
    expect(screen.queryByTestId("ai-allowed-groups-empty")).not.toBeInTheDocument();

    // origin 형식이 틀리면 즉시 표시하고 배포를 잠근다. 고치면 풀린다
    await user.type(screen.getByTestId("ai-cors-origins"), "https://app.example.com/path");
    expect(screen.getByTestId("ai-cors-origins-invalid")).toHaveTextContent("https://app.example.com/path");
    expect(within(dialog).getByTestId("deploy-submit")).toBeDisabled();
    await user.clear(screen.getByTestId("ai-cors-origins"));
    await user.type(screen.getByTestId("ai-cors-origins"), "HTTPS://App.Example.com{Enter}http://localhost:5173");
    expect(screen.queryByTestId("ai-cors-origins-invalid")).not.toBeInTheDocument();
    expect(within(dialog).getByTestId("deploy-submit")).toBeEnabled();

    await user.click(within(dialog).getByTestId("deploy-submit"));
    await waitFor(() =>
      expect(lastRequest("POST", "/agents/ag-draft/deploy")?.body).toMatchObject({
        namespace: null,
        exposure: {
          enabled: true,
          host: "support.default.apps.local",
          ingress_class: "traefik",
          tls: { mode: "none", secret_name: null, issuer: null },
          cors_origins: ["https://app.example.com", "http://localhost:5173"],
          allowed_groups: ["daegam-default-dev"],
        },
      }),
    );
    // 응답의 external_url 은 경로가 없고 상세에 복사 버튼과 함께 보인다
    expect(await screen.findByTestId("external-url")).toHaveTextContent("http://support.default.apps.local");
    expect(screen.getByTestId("external-url-copy")).toBeInTheDocument();
    expect(screen.getByTestId("exp-badge-address")).toHaveTextContent("주소 대기");
  });

  it("maps codes to fields: invalid_cors_origin, unauthenticated_exposure and host_conflict", async () => {
    enterOidcMode();
    const user = userEvent.setup();
    const dialog = await openAgentDeploy(user);
    await waitFor(() => expect(screen.getByTestId("exp-enabled")).toBeEnabled());
    await user.click(screen.getByTestId("exp-enabled"));
    await waitFor(() => expect(screen.getByTestId("exp-host")).toHaveValue("support.default.apps.local"));

    server.use(http.post("/agents/:id/deploy", () => apiError(422, "invalid_cors_origin", "invalid CORS origin 'https://x/'")));
    await user.click(within(dialog).getByTestId("deploy-submit"));
    expect(await screen.findByTestId("ai-cors-origins-error")).toHaveTextContent("invalid CORS origin");
    expect(screen.queryByTestId("deploy-error")).not.toBeInTheDocument();

    server.use(http.post("/agents/:id/deploy", () => apiError(422, "unauthenticated_exposure", "Agent exposure requires oidc mode")));
    await user.click(within(dialog).getByTestId("deploy-submit"));
    expect(await screen.findByTestId("exp-enabled-error")).toHaveTextContent("Agent exposure requires oidc mode");

    // 목은 TAKEN_HOST 를 이미 쓰인 host 로 보고 409 host_conflict 를 낸다 (deploy 핸들러만 원래대로 되돌린다)
    server.resetHandlers();
    enterOidcMode();
    await user.clear(screen.getByTestId("exp-host"));
    await user.type(screen.getByTestId("exp-host"), TAKEN_HOST);
    await user.click(within(dialog).getByTestId("deploy-submit"));
    expect(await screen.findByTestId("exp-host-error")).toHaveTextContent("already exposed");
  });

  it("a k8s name that is already taken lands on its own alert", async () => {
    const user = userEvent.setup();
    agentState.items = [agentDraftFixture({ id: "ag-draft", name: "taken" })];
    render(<TestApp initialPath="/agents/ag-draft?tab=deployment" />);
    await user.click(await screen.findByTestId("deployment-deploy"));
    const dialog = await screen.findByTestId("deploy-dialog");
    await user.click(within(dialog).getByTestId("deploy-submit"));
    expect(await screen.findByTestId("name-conflict")).toHaveTextContent("같은 이름이 이미 있습니다");
  });
});

describe("agent detail exposure", () => {
  beforeEach(() => {
    agentState.items = [exposedAgentFixture()];
  });

  it("shows external_url with copy, exposure badges, the endpoint table, the curl example and the allowed-groups warning", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/agents/ag-support" />);
    expect(await screen.findByTestId("status-phase")).toHaveTextContent("Ready");
    expect(screen.getByTestId("external-url")).toHaveTextContent("https://support.default.apps.local");
    expect(screen.getByTestId("exp-badge-address")).toHaveTextContent("주소 할당됨");
    expect(screen.getByTestId("exp-badge-tls-secret")).toHaveTextContent("TLS Secret 있음");
    expect(screen.queryByTestId("exp-badge-front")).not.toBeInTheDocument();
    expect(screen.queryByTestId("exp-badge-unauth")).not.toBeInTheDocument();
    const writeText = stubClipboard();
    await user.click(screen.getByTestId("external-url-copy"));
    await waitFor(() => expect(writeText).toHaveBeenCalledWith("https://support.default.apps.local"));

    const card = screen.getByTestId("external-call-card");
    const table = within(card).getByTestId("external-endpoints");
    for (const path of ["/chat", "/sessions", "/sessions/{sid}", "/sessions/{sid}/pending", "/sessions/{sid}/decisions"]) {
      expect(table).toHaveTextContent(path);
    }
    expect(within(table).getByTestId("external-endpoint-POST-chat")).toHaveTextContent("POST");
    // 세션 이름 바꾸기·지우기·제목 검색은 런타임이 session_admin 을 알릴 때만 있는 경로다(P36)
    expect(within(table).queryByTestId("external-endpoint-PATCH-sessions-sid")).not.toBeInTheDocument();
    expect(within(table).queryByTestId("external-endpoint-GET-sessions-q")).not.toBeInTheDocument();
    // 다시 생성도 런타임이 regenerate 를 알릴 때만 있는 경로다(P36)
    expect(within(table).queryByTestId("external-endpoint-POST-sessions-sid-regenerate")).not.toBeInTheDocument();
    const curl = within(card).getByTestId("external-curl");
    expect(curl).toHaveTextContent("curl -N https://support.default.apps.local/chat");
    // 토큰은 자리표시가 아니라 연결 카드와 같은 env 이름이다. 발급 링크는 local 에만 있다
    expect(curl).toHaveTextContent("Authorization: Bearer $DAEGAM_PAT");
    expect(within(card).queryByTestId("external-pat-link")).not.toBeInTheDocument();
    expect(within(card).getByTestId("external-eventsource-note")).toHaveTextContent("브라우저에서는 EventSource 를 쓸 수 없고 fetch 로 스트림을 읽습니다.");
    expect(within(card).getByTestId("external-allowed-groups-empty")).toHaveTextContent("인증된 모든 사용자가 이 Agent 를 부를 수 있습니다.");
    expect(within(card).getByTestId("external-cors-origins")).toHaveTextContent("https://app.example.com");
  });

  it("local mode: an empty allowed_groups reads as tenant members only, in the card and the redeploy dialog", async () => {
    // local 은 플랫폼이 빈 목록을 그 테넌트의 MCP 그룹으로 채워 내린다 (P32 2절). 경고가 아니라 안내다
    authConfigState.mode = "local";
    const user = userEvent.setup();
    render(<TestApp initialPath="/agents/ag-support" />);
    await signInLocal(user);
    const card = await screen.findByTestId("external-call-card");
    expect(within(card).getByTestId("external-allowed-groups-empty")).toHaveTextContent("이 테넌트의 멤버만 부를 수 있습니다.");
    expect(card).not.toHaveTextContent("인증된 모든 사용자");
    // local 은 curl 예시 아래에서 개인 토큰을 받으러 가는 길을 준다 (P32 10절)
    expect(within(card).getByTestId("external-pat-note")).toHaveTextContent("DAEGAM_PAT 에 넣을 개인 MCP 토큰은 내 MCP 토큰에서 발급해 주세요.");
    expect(within(card).getByTestId("external-pat-link")).toHaveAttribute("href", "/account/tokens");
    await user.click(screen.getByTestId("redeploy-button"));
    const section = within(await screen.findByTestId("redeploy-dialog")).getByTestId("redeploy-exposure");
    expect(within(section).getByTestId("ai-allowed-groups-empty")).toHaveTextContent("이 테넌트의 멤버만 부를 수 있습니다.");
    expect(section).toHaveTextContent("비우면 이 테넌트의 멤버만 부를 수 있습니다.");
  });

  it("lists allowed groups instead of the warning and hides the card when the agent is not exposed", async () => {
    agentState.items = [exposedAgentFixture({ exposure: { ...exposedAgentFixture().exposure, allowed_groups: ["daegam-default-dev", "ops"] } })];
    const { unmount } = render(<TestApp initialPath="/agents/ag-support" />);
    expect(await screen.findByTestId("external-allowed-groups")).toHaveTextContent("daegam-default-dev");
    expect(screen.getByTestId("external-allowed-groups")).toHaveTextContent("ops");
    expect(screen.queryByTestId("external-allowed-groups-empty")).not.toBeInTheDocument();
    unmount();

    resetAgentState();
    render(<TestApp initialPath="/agents/ag-support" />);
    expect(await screen.findByTestId("status-phase")).toBeInTheDocument();
    expect(screen.queryByTestId("external-call-card")).not.toBeInTheDocument();
    expect(screen.getByTestId("external-url")).toHaveTextContent("외부 노출 안 함");
    expect(screen.queryByTestId("external-url-copy")).not.toBeInTheDocument();
  });

  it("the redeploy dialog edits the exposure and posts {exposure}; the detail shows the new snapshot; a 422 from the server lands on the field and keeps the dialog open", async () => {
    enterOidcMode();
    const user = userEvent.setup();
    render(<TestApp initialPath="/agents/ag-support" />);
    expect(await screen.findByTestId("status-phase")).toBeInTheDocument();
    await user.click(screen.getByTestId("redeploy-button"));
    const dialog = await screen.findByTestId("redeploy-dialog");
    const section = within(dialog).getByTestId("redeploy-exposure");
    // 저장된 스냅샷으로 채워진다. edit 모드라 네임스페이스는 잠긴다
    expect(within(section).getByTestId("exp-enabled")).toBeChecked();
    expect(within(section).getByTestId("exp-namespace")).toBeDisabled();
    expect(within(section).getByTestId("exp-host")).toHaveValue("support.default.apps.local");
    expect(within(section).getByTestId("ai-cors-origins")).toHaveValue("https://app.example.com");
    expect(within(dialog).getByText("노출 설정은 그대로 둡니다.")).toBeInTheDocument();

    await user.type(within(section).getByTestId("ai-allowed-groups"), "ops{Enter}");
    expect(within(dialog).getByText("노출 설정이 바뀌어 Ingress 를 다시 만듭니다.")).toBeInTheDocument();
    await user.click(within(dialog).getByTestId("redeploy-submit"));
    await waitFor(() => expect(screen.queryByTestId("redeploy-dialog")).not.toBeInTheDocument());
    expect(lastRequest("POST", "/agents/ag-support/redeploy")?.body).toEqual({
      exposure: {
        enabled: true,
        host: "support.default.apps.local",
        ingress_class: "traefik",
        tls: { mode: "secret", secret_name: "calc-tls", issuer: null },
        cors_origins: ["https://app.example.com"],
        allowed_groups: ["ops"],
      },
    });
    await waitFor(() => expect(screen.getByTestId("external-allowed-groups")).toHaveTextContent("ops"));
    expect(screen.getByTestId("redeploy-warning")).toHaveTextContent("승인을 기다리던 세션이 있습니다");

    // 대화상자를 다시 열어 이미 쓰인 host 로 보내면 서버의 422 가 칸 아래에 서고 대화상자는 닫히지 않는다.
    // host 검사는 바로 앞 재배포가 바꾼 allowed_groups 와 상관이 없다
    await user.click(screen.getByTestId("redeploy-button"));
    const again = await screen.findByTestId("redeploy-dialog");
    await user.clear(within(again).getByTestId("exp-host"));
    await user.type(within(again).getByTestId("exp-host"), TAKEN_HOST);
    await user.click(within(again).getByTestId("redeploy-submit"));
    expect(await within(again).findByTestId("exp-host-error")).toHaveTextContent("already exposed");
    expect(screen.getByTestId("redeploy-dialog")).toBeInTheDocument();
  });
});
