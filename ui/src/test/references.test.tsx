/**
 * "참조" 카드와 리소스 사이 링크 (D43 셋째·넷째).
 *
 * 화면이 "이걸 바꾸거나 지우면 무엇이 깨지나" 에 답할 수 있어야 한다.
 * 그래서 이 파일은 셋을 본다. 카드가 종류별로 묶어 링크와 리비전을 말하는가, 다른 리소스를
 * 가리키는 자리가 링크인가, 그리고 사람이 보는 글에 날 id 가 남아 있지 않은가.
 */
import { render, screen, waitFor, within } from "@testing-library/react";

import { TestApp } from "@/App";
import { dictionary } from "@/i18n";
import { dataServiceLabel, sourceLabel } from "@/components/canvas/inspector/types";
import { LOADING_LABEL } from "@/lib/names";
import { referenceLabel, referencePath } from "@/lib/references";
import { clusterEdge, clusterHome, dsPostgres, hubItemFixture, hubState, mcpServerFixture, mcpServerState, reference, referencesState, server, http, HttpResponse } from "./msw";

const ko = dictionary("ko");

beforeEach(() => {
  hubState.items = [hubItemFixture()];
});

describe("참조 카드", () => {
  it("종류별로 묶어 링크와 상태, 어느 쪽이 붙드는지를 보인다", async () => {
    referencesState["mcp-servers/ms-calc"] = [
      reference("hub-item", "hub-calc", { display_name: "calc", state: "created" }),
      reference("gateway", "gw-support", { display_name: "지원 Gateway", state: "deployed", revision: "both", via: "hub-item" }),
      reference("agent", "ag-support", { display_name: "상담 에이전트", state: "deployed", revision: "deployed" }),
    ];
    render(<TestApp initialPath="/mcp-servers/ms-calc" />);
    const card = await screen.findByTestId("references-card");
    await within(card).findByTestId("references-card-list");

    expect(within(card).getByText("Hub item")).toBeInTheDocument();
    expect(within(card).getByText("Gateway")).toBeInTheDocument();
    expect(within(card).getByText("Agent")).toBeInTheDocument();
    expect(within(card).getByTestId("references-card-link-gw-support")).toHaveAttribute("href", "/gateways/gw-support");
    expect(within(card).getByTestId("references-card-link-ag-support")).toHaveAttribute("href", "/agents/ag-support");
    expect(within(card).getByTestId("references-card-link-hub-calc")).toHaveAttribute("href", "/hub/hub-calc");
    expect(within(card).getByTestId("references-card-state-gw-support")).toHaveTextContent("deployed");
    // 간접 참조는 무엇을 거쳤는지, 리비전은 정의인지 배포본인지 말한다
    expect(within(card).getByTestId("references-card-revision-gw-support")).toHaveTextContent("허브 항목을 거쳐 정의와 배포본이 참조");
    expect(within(card).getByTestId("references-card-revision-ag-support")).toHaveTextContent("배포본이 참조");
    expect(within(card).getByTestId("references-card-revision-hub-calc")).toHaveTextContent("정의가 참조");
  });

  it("비어 있으면 그렇게 말한다", async () => {
    render(<TestApp initialPath="/mcp-servers/ms-calc" />);
    const card = await screen.findByTestId("references-card");
    await waitFor(() => expect(within(card).getByTestId("state-empty")).toHaveTextContent("참조가 없습니다"));
  });

  it("조회에 실패하면 없는 것이 아니라 오류라고 말한다", async () => {
    server.use(http.get("/mcp-servers/:id/references", () => HttpResponse.json({ detail: { code: "forbidden", detail: "no" } }, { status: 403 })));
    render(<TestApp initialPath="/mcp-servers/ms-calc" />);
    const card = await screen.findByTestId("references-card");
    await waitFor(() => expect(within(card).getByTestId("state-error")).toBeInTheDocument());
    expect(within(card).queryByText("참조가 없습니다")).not.toBeInTheDocument();
  });

  it("읽기 토큰은 갈 곳이 없으므로 개수만 말한다", async () => {
    referencesState["backend-apis/bp-orders"] = [reference("token", "tok-1", { name: "ci", display_name: "ci" }), reference("token", "tok-2")];
    render(<TestApp initialPath="/backend-apis/bp-orders" />);
    const card = await screen.findByTestId("references-card");
    expect(within(card).getByTestId("references-card-token")).toHaveTextContent("2개");
    expect(within(card).queryByTestId("references-card-link-tok-1")).not.toBeInTheDocument();
    expect(card).not.toHaveTextContent("tok-1");
  });

  it("Cluster 상세는 이 클러스터에 배포된 것을 보인다", async () => {
    referencesState["clusters/cl-edge"] = [reference("mcp-server", "ms-calc", { display_name: "calc", state: "deployed", revision: "deployed" })];
    render(<TestApp initialPath="/clusters/cl-edge" />);
    const card = await screen.findByTestId("cluster-references-card");
    expect(card).toHaveTextContent("이 클러스터에 배포된 것");
    expect(within(card).getByTestId("cluster-references-card-link-ms-calc")).toHaveAttribute("href", "/mcp-servers/ms-calc");
  });
});

describe("리소스 사이 링크", () => {
  it("MCP Server 상세의 클러스터는 링크다", async () => {
    render(<TestApp initialPath="/mcp-servers/ms-calc" />);
    await screen.findByTestId("mcp-server-detail-page");
    await waitFor(() => expect(screen.getByTestId("cluster-link")).toHaveAttribute("href", `/clusters/${clusterHome.id}`));
    expect(screen.getByTestId("cluster-link")).toHaveTextContent(clusterHome.display_name ?? clusterHome.name);
  });

  it("Gateway 상세의 백엔드 줄은 허브 항목 옆에 MCP Server 로 바로 가는 링크를 둔다", async () => {
    render(<TestApp initialPath="/gateways/gw-support" />);
    await screen.findByTestId("gateway-detail-page");
    const link = await screen.findByTestId("backend-mcp-calc");
    expect(link).toHaveAttribute("href", `/mcp-servers/${hubItemFixture().source.instance_id}`);
  });

  it("Agent 상세의 bindings 는 Database 이름이고 링크다", async () => {
    render(<TestApp initialPath="/agents/ag-support" />);
    await screen.findByTestId("agent-detail-page");
    const binding = await screen.findByTestId("binding-DAEGAM_DB_0");
    expect(binding).toHaveAttribute("href", "/data-services/ds-pg");
    expect(binding).toHaveTextContent("Orders DB");
    expect(screen.getByTestId("status-bindings")).not.toHaveTextContent("ds-pg");
  });

  it("캔버스 인스펙터도 고른 것을 이름으로 부르고 못 찾으면 삭제된 항목이다", () => {
    const options = { providers: [], gateways: [], mcpServers: [mcpServerFixture()], dataServices: [dsPostgres], knowledgeBases: [], skills: [], ready: true };
    expect(sourceLabel(ko, { kind: "mcp_instance", id: "ms-calc" }, options)).toBe("calc");
    expect(sourceLabel(ko, { kind: "mcp_instance", id: "ms-gone" }, options)).toBe(ko.ui.names.deleted);
    expect(sourceLabel(ko, { kind: "mcp_instance", id: "" }, options)).toBe("미선택");
    expect(dataServiceLabel(ko, "ds-pg", options)).toBe("Orders DB");
    expect(dataServiceLabel(ko, "ds-gone", options)).toBe(ko.ui.names.deleted);
    expect(dataServiceLabel(ko, null, options)).toBe("Database 미선택");
  });

  it("목록을 아직 못 받았으면 지워졌다고 하지 않는다", () => {
    // 빈 목록과 "아직 안 온 목록" 은 같은 모양이다. 둘을 같은 말로 그리면 화면이 열리는 동안
    // 멀쩡한 참조가 지워진 것처럼 보인다 (캔버스 노드 제목이 실제로 그랬다)
    const loading = { providers: [], gateways: [], mcpServers: [], dataServices: [], knowledgeBases: [], skills: [], ready: false };
    expect(sourceLabel(ko, { kind: "mcp_instance", id: "ms-calc" }, loading)).toBe(LOADING_LABEL);
    expect(dataServiceLabel(ko, "ds-pg", loading)).toBe(LOADING_LABEL);
    // 아직 고르지 않은 것은 로딩과 상관없이 그대로다
    expect(sourceLabel(ko, { kind: "mcp_instance", id: "" }, loading)).toBe("미선택");
    expect(dataServiceLabel(ko, null, loading)).toBe("Database 미선택");
  });

  it("참조 종류마다 사전의 종류 이름과 상세 경로를 쓴다", () => {
    expect(referenceLabel(ko, "data-service")).toBe("Database");
    expect(referenceLabel(ko, "hub-item")).toBe("Hub item");
    expect(referenceLabel(ko, "token")).toBe("읽기 토큰");
    expect(referencePath("knowledge-base", "kb-1")).toBe("/knowledge-bases/kb-1");
    expect(referencePath("hub-item", "hub-1")).toBe("/hub/hub-1");
    // 토큰은 갈 곳이 없다
    expect(referencePath("token", "tok-1")).toBeNull();
  });

  it("지워진 클러스터는 id 대신 삭제된 항목이라고 말한다", async () => {
    mcpServerState.items = [mcpServerFixture({ cluster_id: "cl-gone" })];
    render(<TestApp initialPath="/mcp-servers/ms-calc" />);
    await screen.findByTestId("mcp-server-detail-page");
    await waitFor(() => expect(screen.getByTestId("cluster-link")).toHaveTextContent("(삭제된 항목)"));
    expect(screen.getByTestId("cluster-link")).not.toHaveTextContent("cl-gone");
    expect(clusterEdge.id).not.toBe("cl-gone");
  });
});
