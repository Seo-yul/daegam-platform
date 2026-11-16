/**
 * MCP 토큰 발급자 카드 (P53b 6.9절): Platform Settings 에서 oidc 모드의 데이터 플레인 발급자, 전환의 진행, 붙잡힌 기동,
 * IdP 토큰 수명 경고와 전환이 바꾸거나 열어 두는 목록을 보인다. 다른 모드에서는 data_plane_issuer 가 null 이라 카드가 없다.
 */
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { TestApp } from "@/App";
import type { DataPlaneIssuer } from "@/api/types";
import { switching } from "@/components/settings/IssuerSwitchCard";
import { dictionary } from "@/i18n";

import { HttpResponse, apiError, authConfigState, countExact, dataPlaneAuth, http, server } from "./msw";
import { oidcStub } from "./stubs/oidc-client-ts";

const KO = dictionary("ko");
const TEXT = KO.admin.platformSettings.issuer;

const doc = (name: string, tenant = "acme") => ({ id: `id-${name}`, name, tenant, cluster_id: "home", kind: "gateway" as const });

const idpIssuer: DataPlaneIssuer = {
  setting: "idp",
  trusted: "idp",
  error: null,
  pending: 0,
  clusters: [],
  idp_token_life: { largest_seconds: 300, limit_seconds: 4440, warn: false },
  jwks_unreachable_clusters: [],
  gateways_idp_names: [doc("okta-groups")],
  gateways_permit: [],
  exposed_agents_foreign_groups: [{ ...doc("helper"), kind: "agent", groups: ["daegam-other"] }],
  exposed_agents_open: [],
  exposed_agents_one_issuer: [],
  tenants_agent_client_ids: ["acme"],
  agents_gateway_sources: [],
  gateways_published_refused: [doc("composer")],
  tenants_foreign_mcp_group: [],
};

/** oidc 모드로 띄우고 issuer 를 data_plane_issuer 로 낸다. 로그인한 사용자를 심어야 /me 를 불러 플랫폼 관리자임을 안다. */
function serve(issuer: DataPlaneIssuer | null) {
  authConfigState.mode = "oidc";
  oidcStub.user = { access_token: "tok-oidc" };
  server.use(http.get("/settings/data-plane-auth", () => HttpResponse.json({ ...dataPlaneAuth, data_plane_issuer: issuer })));
}

const PATH = "/api/settings/data-plane-auth";

describe("MCP token issuer card (P53b 6.9절)", () => {
  it("has no card and no request outside oidc mode", async () => {
    render(<TestApp initialPath="/admin/settings" />);
    // 웹 Tool 카드가 같은 응답을 읽으므로, 그 요청이 끝난 뒤에도 카드가 없는지 본다
    await waitFor(() => expect(countExact("GET", PATH)).toBeGreaterThan(0));
    await screen.findByTestId("ps-web-tools");
    expect(screen.queryByTestId("ps-issuer")).not.toBeInTheDocument();
  });

  it("polls only while a switch to platform tokens has items left", () => {
    expect(switching({ ...idpIssuer, trusted: "platform", pending: 2 })).toBe(true);
    expect(switching({ ...idpIssuer, trusted: "platform", pending: 0 })).toBe(false);
    expect(switching({ ...idpIssuer, setting: "platform", pending: 2 })).toBe(false);
    expect(switching(null)).toBe(false);
  });

  it("keeps the card and shows the error when the status fails to load", async () => {
    serve(idpIssuer);
    server.use(http.get("/settings/data-plane-auth", () => apiError(500, "internal", "boom")));
    render(<TestApp initialPath="/admin/settings" />);
    const card = await screen.findByTestId("ps-issuer");
    expect(await within(card).findByTestId("ps-issuer-load-error")).toBeInTheDocument();
    expect(within(card).queryByTestId("ps-issuer-trusted")).not.toBeInTheDocument();
  });

  it("under idp shows the lists to read before switching and no progress", async () => {
    serve(idpIssuer);
    render(<TestApp initialPath="/admin/settings" />);
    const card = await screen.findByTestId("ps-issuer");
    expect(await within(card).findByTestId("ps-issuer-trusted")).toHaveTextContent(TEXT.trusted.idp);
    expect(within(card).queryByTestId("ps-issuer-progress")).not.toBeInTheDocument();
    expect(within(card).queryByTestId("ps-issuer-clusters")).not.toBeInTheDocument();
    expect(within(card).getByTestId("ps-issuer-token-life")).toHaveTextContent("300초 · 기준 4440초 미만");
    expect(within(card).queryByTestId("ps-issuer-token-life-warn")).not.toBeInTheDocument();
    // 빈 목록은 빠지고, 문서는 테넌트와 이름, 노출한 Agent 는 그룹까지 보인다
    const idpNames = within(card).getByTestId("ps-issuer-list-gateways_idp_names");
    expect(idpNames).toHaveTextContent(`${TEXT.lists.gateways_idp_names.label} (1)`);
    await userEvent.click(within(idpNames).getByText(`${TEXT.lists.gateways_idp_names.label} (1)`));
    expect(idpNames).toHaveAttribute("open");
    expect(idpNames).toHaveTextContent("acme / okta-groups");
    expect(within(card).getByTestId("ps-issuer-list-exposed_agents_foreign_groups")).toHaveTextContent("acme / helper: daegam-other");
    expect(within(card).getByTestId("ps-issuer-list-tenants_agent_client_ids")).toHaveTextContent("acme");
    expect(within(card).queryByTestId("ps-issuer-list-gateways_permit")).not.toBeInTheDocument();
    expect(within(card).getByTestId("ps-issuer-list-gateways_published_refused")).toHaveTextContent("acme / composer");
  });

  it("names the tenants that held the platform on idp", async () => {
    serve({ ...idpIssuer, setting: "platform", error: "foreign_mcp_group", tenants_foreign_mcp_group: [{ tenant: "acme", mcp_group: "daegam-beta" }] });
    render(<TestApp initialPath="/admin/settings" />);
    const card = await screen.findByTestId("ps-issuer");
    expect(await within(card).findByTestId("ps-issuer-trusted")).toHaveTextContent(TEXT.trusted.idp);
    expect(within(card).getByTestId("ps-issuer-held")).toHaveTextContent(TEXT.held);
    expect(within(card).getByTestId("ps-issuer-list-tenants_foreign_mcp_group")).toHaveTextContent("acme: daegam-beta");
  });

  it("under platform shows each cluster's progress, what still waits and the warnings", async () => {
    serve({
      ...idpIssuer,
      setting: "platform",
      trusted: "platform",
      pending: 3,
      idp_token_life: { largest_seconds: 4500, limit_seconds: 4440, warn: true },
      jwks_unreachable_clusters: ["edge"],
      gateways_idp_names: [],
      exposed_agents_foreign_groups: [],
      tenants_agent_client_ids: [],
      gateways_published_refused: [],
      clusters: [
        {
          cluster_id: "home",
          probed_at: "2026-11-14T01:00:00Z",
          error: null,
          platform_namespaces: ["daegam-acme"],
          idp_namespaces: [],
          pending_documents: [{ ...doc("sales"), blocked: true }, { ...doc("mirror"), kind: "hub_item", blocked: false }],
          waiting_verifiers: [{ kind: "VirtualMCPServer", name: "sales", namespace: "daegam-acme", tenant: "acme", since: "2026-11-14T00:59:00Z", refused: true, last_at: "2026-11-14T01:00:00Z" }],
          verified_verifiers: [{ kind: "VirtualMCPServer", name: "ops", namespace: "daegam-acme", at: "2026-11-14T01:00:00Z" }],
        },
        { cluster_id: "edge", probed_at: null, error: "connect timeout", platform_namespaces: [], idp_namespaces: [], pending_documents: [], waiting_verifiers: [], verified_verifiers: [] },
      ],
    });
    render(<TestApp initialPath="/admin/settings" />);
    const card = await screen.findByTestId("ps-issuer");
    expect(await within(card).findByTestId("ps-issuer-trusted")).toHaveTextContent(TEXT.trusted.platform);
    expect(within(card).getByTestId("ps-issuer-progress")).toHaveTextContent(TEXT.pending(3));
    expect(within(card).getByTestId("ps-issuer-cluster-home")).toHaveTextContent("home");
    expect(within(card).getByTestId("ps-issuer-cluster-edge")).toHaveTextContent(TEXT.notProbed);
    expect(within(card).getByTestId("ps-issuer-cluster-edge")).toHaveTextContent(`${TEXT.probeFailed}: connect timeout`);
    // 렌더 대기와 토큰 대기 목록은 접혀 있고 제목에 수를 붙인다. 종류 이름은 resources.kind 의 것이다
    expect(within(card).getByTestId("ps-issuer-pending-documents")).not.toHaveAttribute("open");
    expect(within(card).getByTestId("ps-issuer-pending-documents")).toHaveTextContent(`${TEXT.pendingDocumentsTitle} (2)`);
    expect(within(card).getByTestId("ps-issuer-pending-documents")).toHaveTextContent(`home: Gateway acme / sales (${TEXT.blocked})`);
    expect(within(card).getByTestId("ps-issuer-pending-documents")).toHaveTextContent(`home: ${KO.resources.kind.hub} acme / mirror`);
    expect(within(card).getByTestId("ps-issuer-waiting-verifiers")).toHaveTextContent(`home: VirtualMCPServer daegam-acme/sales (${TEXT.refused})`);
    // 조사는 클러스터 이름의 끝소리를 따른다
    expect(within(card).getByTestId("ps-issuer-jwks")).toHaveTextContent("원격 클러스터 edge는 플랫폼 토큰을");
    expect(within(card).getByTestId("ps-issuer-token-life-warn")).toHaveTextContent(TEXT.tokenLifeWarn);
    expect(within(card).getByTestId("ps-issuer-lists-empty")).toHaveTextContent(TEXT.listsEmpty);
  });

  it("says the switch is done when nothing is pending", async () => {
    serve({ ...idpIssuer, setting: "platform", trusted: "platform", clusters: [] });
    render(<TestApp initialPath="/admin/settings" />);
    expect(await screen.findByTestId("ps-issuer-progress")).toHaveTextContent(TEXT.done);
  });
});
