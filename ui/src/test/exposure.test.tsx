import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { TestApp } from "@/App";
import { exposureFieldErrors, exposureHostBlank, exposureInvalid, isDns1123Host, previewUrl, suggestHost, toExposureInput, EMPTY_EXPOSURE } from "@/lib/exposure";
import { ApiError } from "@/api/client";
import { stubClipboard } from "./clipboard";
import {
  apiError,
  draftFixture,
  exposedGatewayFixture,
  exposedMcpServerFixture,
  exposureOptionsFixture,
  gatewayDraftFixture,
  gatewayState,
  http,
  HttpResponse,
  lastRequest,
  mcpServerFixture,
  mcpServerState,
  server,
  TAKEN_HOST,
} from "./msw";
import { selectValue } from "./select";

/**
 * 외부 노출 (docs/guide/exposure.md 2·6절, resource-model 11절). 배포 대화상자와 상세의 노출 카드. 클러스터의 노출 기본값(ingress 카드)은 clusters.test 에 있다.
 *
 * 배포가 문서에 거는 동작이 되면서(D42) 노출 섹션도 폼 페이지가 아니라 대화상자 안에 산다. 호스트 기본값은
 * 이제 사용자가 치는 이름이 아니라 문서의 이름에서 온다. 이름은 만들 때 이미 정해졌기 때문이다.
 */

describe("exposure helpers", () => {
  it("suggests the host from the template and validates DNS-1123", () => {
    expect(suggestHost("{name}.default.apps.local", "Calc 2")).toBe("calc-2.default.apps.local");
    expect(suggestHost(null, "calc")).toBe("");
    expect(suggestHost("{name}.default.apps.local", "")).toBe("");
    expect(isDns1123Host("calc-1.default.apps.local")).toBe(true);
    // 대문자는 서버가 소문자로 정규화하므로 통과한다
    expect(isDns1123Host("Calc.example.com")).toBe(true);
    expect(isDns1123Host("-bad.example.com")).toBe(false);
    expect(isDns1123Host("a_b.example.com")).toBe(false);
    expect(isDns1123Host("")).toBe(false);
  });

  it("derives the scheme from the TLS mode and builds the request body", () => {
    expect(previewUrl({ host: "x.example.com", tlsMode: "none" })).toBe("http://x.example.com/mcp");
    expect(previewUrl({ host: "x.example.com", tlsMode: "secret" })).toBe("https://x.example.com/mcp");
    expect(previewUrl({ host: "", tlsMode: "cert-manager" })).toBe("https://<host>/mcp");
    expect(toExposureInput(EMPTY_EXPOSURE)).toEqual({ enabled: false });
    expect(toExposureInput({ ...EMPTY_EXPOSURE, enabled: true, host: "x.example.com", ingressClass: "", tlsMode: "secret", secretName: "x-tls", issuer: "le" })).toEqual({
      enabled: true,
      host: "x.example.com",
      ingress_class: null,
      tls: { mode: "secret", secret_name: "x-tls", issuer: null },
    });
  });

  it("maps every 422 code to its field", () => {
    const at = (code: string) => Object.keys(exposureFieldErrors(new ApiError(422, { detail: { code, detail: "x" } })))[0];
    expect(at("invalid_host")).toBe("host");
    expect(at("host_conflict")).toBe("host");
    expect(at("host_reserved")).toBe("host");
    expect(at("ingress_disabled")).toBe("enabled");
    expect(at("unauthenticated_exposure")).toBe("enabled");
    expect(at("unknown_ingress_class")).toBe("class");
    expect(at("ingress_class_not_allowed")).toBe("class");
    expect(at("tls_secret_invalid")).toBe("tls-secret");
    expect(at("cert_manager_unavailable")).toBe("tls-mode");
    expect(at("unknown_issuer")).toBe("issuer");
    expect(at("namespace_not_allowed")).toBe("namespace");
    expect(exposureFieldErrors(new ApiError(409, { detail: { code: "state_conflict", detail: "x" } }))).toEqual({});
  });
});

/** 미배포 MCP Server 의 배포 대화상자를 연다. */
async function openMcpDeploy(user: ReturnType<typeof userEvent.setup>) {
  render(<TestApp initialPath="/mcp-servers/ms-draft?tab=deployment" />);
  await user.click(await screen.findByTestId("deployment-deploy"));
  return screen.findByTestId("deploy-dialog");
}

describe("exposure in the MCP Server deploy dialog", () => {
  beforeEach(() => {
    mcpServerState.items = [draftFixture()];
  });

  it("loads options for the tenant default cluster, suggests the host from the document name and previews http then https", async () => {
    const user = userEvent.setup();
    const dialog = await openMcpDeploy(user);
    // 클러스터를 고르지 않아도 home 으로 옵션을 미리 받는다
    await waitFor(() => expect(lastRequest("GET", "/clusters/cl-home/exposure-options")).toBeDefined());
    await waitFor(() => expect(screen.getByTestId("exp-namespace")).toHaveValue("daegam-default"));
    expect(screen.queryByTestId("exp-preview")).not.toBeInTheDocument();

    await user.click(screen.getByTestId("exp-enabled"));
    expect(screen.getByTestId("exp-warning")).toHaveTextContent("인증 없이 외부에 노출됩니다");
    expect(screen.getByTestId("exp-class")).toHaveValue("traefik");
    // 이름은 문서의 것이다 (ms-draft 의 name 은 draft)
    await waitFor(() => expect(screen.getByTestId("exp-host")).toHaveValue("draft.default.apps.local"));
    expect(screen.getByTestId("exp-preview")).toHaveTextContent("http://draft.default.apps.local/mcp");

    await selectValue(user, "exp-tls-mode", "secret");
    expect(screen.getByTestId("exp-tls-secret")).toHaveValue("calc-tls");
    expect(screen.getByTestId("exp-preview")).toHaveTextContent("https://draft.default.apps.local/mcp");
    await selectValue(user, "exp-tls-mode", "cert-manager");
    expect(screen.getByTestId("exp-issuer")).toBeInTheDocument();

    // host 를 손대면 즉시 DNS-1123 검증
    await user.clear(screen.getByTestId("exp-host"));
    await user.type(screen.getByTestId("exp-host"), "Bad_Host");
    expect(screen.getByTestId("exp-host-invalid")).toBeInTheDocument();
    await user.clear(screen.getByTestId("exp-host"));
    await user.type(screen.getByTestId("exp-host"), "calc.example.com");
    expect(screen.queryByTestId("exp-host-invalid")).not.toBeInTheDocument();

    await selectValue(user, "exp-issuer", "letsencrypt");
    await user.click(within(dialog).getByTestId("deploy-submit"));
    await waitFor(() =>
      expect(lastRequest("POST", "/mcp-servers/ms-draft/deploy")?.body).toEqual({
        cluster_id: null,
        namespace: null,
        exposure: { enabled: true, host: "calc.example.com", ingress_class: "traefik", tls: { mode: "cert-manager", secret_name: null, issuer: "letsencrypt" } },
      }),
    );
    // 배포 응답의 무인증 경고와 external_url
    expect(await screen.findByTestId("status-external-url")).toHaveTextContent("https://calc.example.com/mcp");
    expect(screen.getByTestId("exp-badge-unauth")).toBeInTheDocument();
  });

  it("locks TLS to edge on a cluster whose front terminates TLS, previews https and shows the front as the publisher (D54)", async () => {
    server.use(
      http.get("/clusters/:id/exposure-options", () =>
        HttpResponse.json(exposureOptionsFixture({ defaults: { host: "{name}.default.apps.local", ingress_class: "traefik", tls_mode: "edge", issuer: null } })),
      ),
    );
    const user = userEvent.setup();
    const dialog = await openMcpDeploy(user);
    await waitFor(() => expect(screen.getByTestId("exp-namespace")).toHaveValue("daegam-default"));
    await user.click(screen.getByTestId("exp-enabled"));
    await waitFor(() => expect(screen.getByTestId("exp-tls-mode")).toHaveValue("edge"));
    expect(screen.getByTestId("exp-tls-mode")).toBeDisabled();
    await waitFor(() => expect(screen.getByTestId("exp-preview")).toHaveTextContent("https://draft.default.apps.local/mcp"));
    await user.click(within(dialog).getByTestId("deploy-submit"));
    await waitFor(() =>
      expect(lastRequest("POST", "/mcp-servers/ms-draft/deploy")?.body).toMatchObject({
        exposure: { enabled: true, tls: { mode: "edge", secret_name: null, issuer: null } },
      }),
    );
    // LoadBalancer 가 없어 주소 판정 대신 "앞단이 공개" 이고, 인증서는 앞단의 것이라 TLS Secret 배지가 없다
    expect(await screen.findByTestId("exp-badge-address")).toHaveTextContent("앞단이 공개");
    expect(screen.queryByTestId("exp-badge-tls-secret")).not.toBeInTheDocument();
  });

  it("re-queries options when the namespace changes and disables secret mode when the namespace has none", async () => {
    const user = userEvent.setup();
    const dialog = await openMcpDeploy(user);
    await waitFor(() => expect(screen.getByTestId("exp-namespace")).toHaveValue("daegam-default"));
    await selectValue(user, "exp-namespace", "daegam-default-edge");
    await waitFor(() => expect(lastRequest("GET", "/clusters/cl-home/exposure-options?namespace=daegam-default-edge")).toBeDefined());
    await user.click(screen.getByTestId("exp-enabled"));
    expect(await screen.findByTestId("exp-no-secrets")).toHaveTextContent("Secret 이 없습니다");
    const listbox = await (async () => {
      await user.click(screen.getByTestId("exp-tls-mode"));
      return document.getElementById(screen.getByTestId("exp-tls-mode").getAttribute("aria-controls") as string) as HTMLElement;
    })();
    expect(within(listbox).getByRole("option", { name: /TLS Secret/ })).toHaveAttribute("aria-disabled", "true");
    await user.keyboard("{Escape}");
    await user.click(within(dialog).getByTestId("deploy-submit"));
    await waitFor(() => expect(lastRequest("POST", "/mcp-servers/ms-draft/deploy")?.body).toMatchObject({ namespace: "daegam-default-edge", exposure: { enabled: true, host: "draft.default.apps.local" } }));
  });

  it("disables the toggle with a reason on a cluster that forbids exposure", async () => {
    const user = userEvent.setup();
    await openMcpDeploy(user);
    await selectValue(user, "deploy-cluster", "cl-locked");
    expect(await screen.findByTestId("exp-disabled-reason")).toHaveTextContent("ingress.enabled 가 false");
    expect(screen.getByTestId("exp-enabled")).toBeDisabled();
  });

  it("disables the toggle in header mode when the cluster does not allow unauthenticated exposure", async () => {
    server.use(http.get("/clusters/:id/exposure-options", () => HttpResponse.json(exposureOptionsFixture({ allow_unauthenticated: false }))));
    const user = userEvent.setup();
    await openMcpDeploy(user);
    expect(await screen.findByTestId("exp-disabled-reason")).toHaveTextContent("allow_unauthenticated 가 false");
    expect(screen.getByTestId("exp-enabled")).toBeDisabled();
  });

  it("shows the missing verb on 403 and leaves the toggle usable", async () => {
    const user = userEvent.setup();
    await openMcpDeploy(user);
    await selectValue(user, "deploy-cluster", "cl-edge");
    expect(await screen.findByTestId("exp-forbidden")).toHaveTextContent("secrets:list@daegam-default");
    expect(screen.getByTestId("exp-enabled")).not.toBeDisabled();
    await user.click(screen.getByTestId("exp-enabled"));
    expect(screen.getAllByTestId("exp-no-secrets")[0]).toHaveTextContent("Secret 목록을 볼 수 없어");
    // 기본값이 없으니 host 는 비어 있고 IngressClass 는 클러스터 기본
    expect(screen.getByTestId("exp-host")).toHaveValue("");
    expect(screen.getByTestId("exp-class")).toHaveValue("");
  });

  it("maps every 422 from the deploy onto its field", async () => {
    const user = userEvent.setup();
    mcpServerState.items = [draftFixture({ id: "ms-taken", name: "taken" })];
    render(<TestApp initialPath="/mcp-servers/ms-taken?tab=deployment" />);
    await user.click(await screen.findByTestId("deployment-deploy"));
    const dialog = await screen.findByTestId("deploy-dialog");
    await waitFor(() => expect(screen.getByTestId("exp-namespace")).toHaveValue("daegam-default"));
    await user.click(screen.getByTestId("exp-enabled"));
    await waitFor(() => expect(screen.getByTestId("exp-host")).toHaveValue(TAKEN_HOST));
    await user.click(within(dialog).getByTestId("deploy-submit"));
    expect(await screen.findByTestId("exp-host-error")).toHaveTextContent("already exposed");
    // 필드 오류로 흡수됐으니 공통 오류는 겹쳐 보이지 않는다
    expect(screen.queryByTestId("deploy-error")).not.toBeInTheDocument();

    server.use(http.post("/mcp-servers/:id/deploy", () => apiError(422, "namespace_not_allowed", "namespace daegam-default is not allowed for tenant default")));
    await user.click(within(dialog).getByTestId("deploy-submit"));
    expect(await screen.findByTestId("exp-namespace-error")).toHaveTextContent("not allowed");

    server.use(http.post("/mcp-servers/:id/deploy", () => apiError(422, "unauthenticated_exposure", "cluster home does not allow unauthenticated exposure")));
    await user.click(within(dialog).getByTestId("deploy-submit"));
    expect(await screen.findByTestId("exp-enabled-error")).toHaveTextContent("unauthenticated");

    server.use(http.post("/mcp-servers/:id/deploy", () => apiError(422, "tls_secret_invalid", "secret calc-tls is not kubernetes.io/tls")));
    await selectValue(user, "exp-tls-mode", "secret");
    await user.click(within(dialog).getByTestId("deploy-submit"));
    expect(await screen.findByTestId("exp-tls-secret-error")).toHaveTextContent("calc-tls");
  });
});

describe("exposure in the Gateway deploy dialog", () => {
  beforeEach(() => {
    gatewayState.items = [gatewayDraftFixture()];
  });

  it("sends namespace and exposure with the gateway deploy body and lands on the detail with external_url", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/gateways/gw-draft?tab=deployment" />);
    await user.click(await screen.findByTestId("deployment-deploy"));
    const dialog = await screen.findByTestId("deploy-dialog");
    await selectValue(user, "deploy-cluster", "cl-home");
    await waitFor(() => expect(screen.getByTestId("exp-namespace")).toHaveValue("daegam-default"));
    await user.click(screen.getByTestId("exp-enabled"));
    await waitFor(() => expect(screen.getByTestId("exp-host")).toHaveValue("draft.default.apps.local"));
    await user.clear(screen.getByTestId("exp-host"));
    await user.type(screen.getByTestId("exp-host"), "gw-bot-2.e2e.local");
    await selectValue(user, "exp-class", "traefik");
    expect(screen.getByTestId("exp-preview")).toHaveTextContent("http://gw-bot-2.e2e.local/mcp");
    await user.click(within(dialog).getByTestId("deploy-submit"));
    await waitFor(() =>
      expect(lastRequest("POST", "/gateways/gw-draft/deploy")?.body).toEqual({
        cluster_id: "cl-home",
        namespace: null,
        exposure: { enabled: true, host: "gw-bot-2.e2e.local", ingress_class: "traefik", tls: { mode: "none", secret_name: null, issuer: null } },
      }),
    );
    expect(await screen.findByTestId("status-external-url")).toHaveTextContent("http://gw-bot-2.e2e.local/mcp");
    expect(screen.getByTestId("copy-external-url")).toBeInTheDocument();
    expect(screen.getByTestId("exp-badge-address")).toHaveTextContent("주소 대기");
  });

  it("maps 409 host_conflict under the host field on the gateway deploy", async () => {
    const user = userEvent.setup();
    gatewayState.items = [gatewayDraftFixture({ id: "gw-taken", name: "taken" })];
    render(<TestApp initialPath="/gateways/gw-taken?tab=deployment" />);
    await user.click(await screen.findByTestId("deployment-deploy"));
    const dialog = await screen.findByTestId("deploy-dialog");
    await waitFor(() => expect(screen.getByTestId("exp-namespace")).toHaveValue("daegam-default"));
    await user.click(screen.getByTestId("exp-enabled"));
    await user.clear(screen.getByTestId("exp-host"));
    await user.type(screen.getByTestId("exp-host"), TAKEN_HOST);
    await user.click(within(dialog).getByTestId("deploy-submit"));
    expect(await screen.findByTestId("exp-host-error")).toHaveTextContent("already exposed");
  });
});

describe("exposure on the detail pages", () => {
  beforeEach(() => {
    mcpServerState.items = [exposedMcpServerFixture()];
    gatewayState.items = [exposedGatewayFixture()];
  });

  it("the MCP Server overview shows the external url with a copy button, the badges and the Korean warning", async () => {
    const user = userEvent.setup();
    const writeText = stubClipboard();
    render(<TestApp initialPath="/mcp-servers/ms-calc" />);
    await screen.findByTestId("mcp-server-detail-page");
    expect(screen.getByTestId("status-external-url")).toHaveTextContent("https://calc.default.apps.local/mcp");
    await user.click(screen.getByTestId("copy-external-url"));
    await waitFor(() => expect(writeText).toHaveBeenCalledWith("https://calc.default.apps.local/mcp"));
    const badges = screen.getByTestId("exposure-badges");
    expect(within(badges).getByTestId("exp-badge-front")).toHaveTextContent("앞단 정상");
    expect(within(badges).getByTestId("exp-badge-address")).toHaveTextContent("주소 할당됨");
    expect(within(badges).getByTestId("exp-badge-tls-secret")).toHaveTextContent("TLS Secret 있음");
    expect(within(badges).queryByTestId("exp-badge-cert")).not.toBeInTheDocument();
    expect(within(badges).getByTestId("exp-badge-unauth")).toHaveTextContent("무인증 노출");
    // 경고는 서버 코드·영어 원문이 아니라 한국어 한 문장이다 (lib/warningText.ts)
    expect(screen.getByTestId("status-warning")).toHaveTextContent("인증 없이 외부에 열립니다");
    expect(screen.getByTestId("status-warning")).not.toHaveTextContent("unauthenticated_exposure");
  });

  it("says not exposed and shows no badges when exposure is off", async () => {
    mcpServerState.items = [mcpServerFixture()];
    render(<TestApp initialPath="/mcp-servers/ms-calc" />);
    await screen.findByTestId("mcp-server-detail-page");
    expect(screen.getByTestId("status-external-url")).toHaveTextContent("외부 노출 안 함");
    expect(screen.queryByTestId("copy-external-url")).not.toBeInTheDocument();
    expect(screen.queryByTestId("exposure-badges")).not.toBeInTheDocument();
  });

  it("the deployment tab edits the exposure and PATCHes only {exposure}, and maps a 422 from the PATCH under the field", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/mcp-servers/ms-calc?tab=deployment" />);
    const card = await screen.findByTestId("exposure-card");
    expect(within(card).getByTestId("exp-namespace")).toBeDisabled();
    expect(within(card).getByTestId("exp-enabled")).toBeChecked();
    await waitFor(() => expect(within(card).getByTestId("exp-host")).toHaveValue("calc.default.apps.local"));
    // 바꾼 게 없으면 저장할 수 없다
    expect(within(card).getByTestId("exposure-save")).toBeDisabled();
    await selectValue(user, within(card).getByTestId("exp-tls-mode"), "none");
    expect(within(card).getByTestId("exp-preview")).toHaveTextContent("http://calc.default.apps.local/mcp");
    await user.click(within(card).getByTestId("exposure-save"));
    expect(await screen.findByTestId("exposure-saved")).toHaveTextContent("재배포");
    expect(lastRequest("PATCH", "/mcp-servers/ms-calc")?.body).toEqual({
      exposure: { enabled: true, host: "calc.default.apps.local", ingress_class: "traefik", tls: { mode: "none", secret_name: null, issuer: null } },
    });

    // 위 저장은 TLS 만 바꿨다. host 는 그대로라 이어서 이미 쓰인 host 를 넣으면 422 가 칸 아래에 선다
    await waitFor(() => expect(within(card).getByTestId("exp-host")).toHaveValue("calc.default.apps.local"));
    await user.clear(within(card).getByTestId("exp-host"));
    await user.type(within(card).getByTestId("exp-host"), TAKEN_HOST);
    await user.click(within(card).getByTestId("exposure-save"));
    expect(await within(card).findByTestId("exp-host-error")).toHaveTextContent("already exposed");
    expect(within(card).queryByTestId("exposure-error")).not.toBeInTheDocument();
  });

  it("the Gateway shows the external url and its deployment tab turns exposure off with PATCH", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/gateways/gw-support?tab=deployment" />);
    await screen.findByTestId("gateway-detail-page");
    const card = await screen.findByTestId("exposure-card");
    await user.click(within(card).getByTestId("exp-enabled"));
    await user.click(within(card).getByTestId("exposure-save"));
    await screen.findByTestId("exposure-saved");
    expect(lastRequest("PATCH", "/gateways/gw-support")?.body).toEqual({ exposure: { enabled: false } });
  });

  it("the Gateway overview shows the address badge and no front badge", async () => {
    render(<TestApp initialPath="/gateways/gw-support" />);
    await screen.findByTestId("gateway-detail-page");
    expect(screen.getByTestId("status-external-url")).toHaveTextContent("http://gw-support.e2e.local/mcp");
    const badges = screen.getByTestId("exposure-badges");
    // Gateway 에는 앞단 phase 가 없다 (vMCP phase 가 곧 앞단)
    expect(within(badges).queryByTestId("exp-badge-front")).not.toBeInTheDocument();
    expect(within(badges).getByTestId("exp-badge-address")).toHaveTextContent("주소 할당됨");
    expect(within(badges).getByTestId("exp-badge-unauth")).toBeInTheDocument();
  });
});

describe("exposureInvalid", () => {
  it("칸 아래 빨간 줄과 같은 판정이라, 그 상태에서는 저장이 눌리지 않는다", () => {
    // 규칙이 ExposureSection 안에만 있던 동안 Gateway·MCP Server 복제는 이것을 못 보고 눌렸다.
    // 화면에는 "규칙에 맞지 않습니다" 가 떠 있는데 서버가 422 로 돌려주는 모양이었다
    const base = { ...EMPTY_EXPOSURE, enabled: true };
    expect(exposureInvalid({ ...base, host: "calc.example.com" })).toBe(false);
    expect(exposureInvalid({ ...base, host: "대문자.Example.com" })).toBe(true);
    expect(exposureInvalid({ ...base, host: "-bad.example.com" })).toBe(true);
    // 비어 있는 것은 아직 안 적은 것이지 틀린 것이 아니다
    expect(exposureInvalid({ ...base, host: "   " })).toBe(false);
    // 꺼 두었으면 무엇이 적혀 있든 상관없다
    expect(exposureInvalid({ ...base, enabled: false, host: "!!!" })).toBe(false);
  });

  it("비운 호스트는 exposureHostBlank 가 따로 본다", () => {
    expect(exposureHostBlank({ ...EMPTY_EXPOSURE, enabled: true, host: "   " })).toBe(true);
    expect(exposureHostBlank({ ...EMPTY_EXPOSURE, enabled: true, host: "calc.example.com" })).toBe(false);
    expect(exposureHostBlank({ ...EMPTY_EXPOSURE, enabled: false, host: "" })).toBe(false);
  });
});
