/**
 * 화면이 리소스의 상태를 말하는 방식 (D43). 화면마다 모양이 같아 표 한 줄이 화면 하나다.
 *
 * - 목록을 못 읽은 것을 "없습니다" 로 말하지 않는다. 이유와 다시 시도를 주고, 다시 읽어 정말 비었을 때만 없다고 말하며
 *   다음에 할 일을 가리킨다.
 * - 없는 상세를 열면 목록으로 돌아가는 길을 준다. Agent·MCP Server·Gateway 는 410 이면 언제 지워졌는지 먼저 말한다.
 * - 배포하지 않은 Agent·MCP Server·Gateway 의 개요는 그렇다고 말하고 배포 탭으로 보낸다. 배포해야 볼 수 있는 탭은 닫혀 있다.
 * - 런타임이 알리지 않은 기능의 단추는 서지 않는다(D61). 기능이 있을 때의 동작은 기능마다의 테스트가 본다.
 */
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent, { type UserEvent } from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import type { Agent } from "@/api/types";
import { TestApp } from "@/App";
import { particle } from "@/lib/particle";
import { RUNTIME_ATTACHMENTS, RUNTIME_CANCEL, RUNTIME_MEMORY_FILES, RUNTIME_REGENERATE, RUNTIME_SESSION_ADMIN, RUNTIME_SESSION_FILES } from "@/lib/runtimeFeatures";

import { firstTurn, openAgentChat, openHomeSession, openHomeSessionList } from "./chat";
import { HttpResponse, NOW, agentDraftFixture, agentState, agentWithLongTerm, agentWithRuntimeFeatures, apiError, draftFixture, gatewayDraftFixture, gatewayState, http, mcpServerState, server } from "./msw";

interface ListFailure {
  /** 테스트 이름에 쓰는 화면 이름. */
  screen: string;
  /** 목록 API 경로. */
  list: string;
  /** 화면 경로. 목록 API 와 같으면 주지 않는다. */
  path?: string;
  /** 목록 API 의 실패 응답. */
  fail: () => Response;
  /** 오류 상자가 말할 이유. 네트워크 오류처럼 정한 문장이 없으면 주지 않는다. */
  says?: string;
  /** 빈 목록일 때의 문장. 실패했을 때는 어디에도 없어야 한다. */
  empty: string;
  /** 빈 목록이 가리키는 다음 행동. */
  next?: { testId: string; href: string };
  /** 못 읽은 동안 잠겨야 하는 쓰기 단추. */
  locked?: string[];
}

const networkError = () => HttpResponse.error();
const forbidden = () => apiError(403, "forbidden", "scope required");
const unavailable = () => apiError(503, "unavailable", "upstream is down");

const LIST_FAILURES: ListFailure[] = [
  { screen: "Agent", list: "/agents", fail: networkError, empty: "Agent 가 없습니다", next: { testId: "goto-template", href: "/agents/new?template=support-agent" } },
  { screen: "MCP Server", list: "/mcp-servers", fail: networkError, empty: "MCP Server 가 없습니다", next: { testId: "goto-backend-apis", href: "/backend-apis" } },
  { screen: "Gateway", list: "/gateways", fail: networkError, empty: "Gateway 가 없습니다", next: { testId: "goto-hub", href: "/hub" } },
  {
    screen: "Backend API",
    list: "/backend-apis",
    fail: () => apiError(502, "cluster_unreachable", "Max retries exceeded"),
    says: "클러스터에 연결하지 못했습니다",
    empty: "Backend API 가 없습니다. 스펙을 업로드하면 그 Tool 로 MCP Server 를 만들 수 있습니다",
    next: { testId: "empty-backend-api-new", href: "/backend-apis/new" },
  },
  { screen: "MCP Hub", list: "/hub", fail: unavailable, empty: "허브 항목이 없습니다", next: { testId: "goto-instances", href: "/mcp-servers" } },
  { screen: "Database", list: "/data-services", fail: unavailable, empty: "Database 가 없습니다" },
  { screen: "클러스터", list: "/clusters", fail: forbidden, says: "권한이 없습니다", empty: "등록된 클러스터가 없습니다" },
  { screen: "감사 로그", list: "/audit", fail: forbidden, says: "권한이 없습니다", empty: "조건에 맞는 기록이 없습니다" },
  // 못 읽은 표를 전체 교체로 밀어 넣으면 남의 매핑이 사라진다
  { screen: "그룹 매핑", list: "/group-mappings", path: "/admin/group-mappings", fail: forbidden, says: "권한이 없습니다", empty: "등록된 그룹 매핑이 없습니다", locked: ["gm-add", "save-button"] },
];

/** 목록 API 를 처음에는 실패시키고, 돌려준 함수를 부른 뒤로는 빈 목록으로 답한다. */
function failThenEmpty(list: string, fail: () => Response): () => void {
  let failing = true;
  server.use(http.get(list, () => (failing ? fail() : HttpResponse.json([]))));
  return () => {
    failing = false;
  };
}

describe("목록을 못 읽은 화면", () => {
  it.each(LIST_FAILURES.map((row) => [row.screen, row] as const))("%s 목록을 못 읽으면 '없습니다' 대신 이유와 다시 시도를 주고, 다시 읽어 비었을 때만 없다고 말한다", async (_screen, row) => {
    const user = userEvent.setup();
    const recover = failThenEmpty(row.list, row.fail);
    render(<TestApp initialPath={row.path ?? row.list} />);
    const error = await screen.findByTestId("state-error");
    if (row.says) expect(error).toHaveTextContent(row.says);
    // 못 불러온 것을 "없습니다" 로 말하면 이미 만들어 둔 사람이 같은 것을 또 만든다
    expect(document.body).not.toHaveTextContent(row.empty);
    expect(screen.queryByTestId("state-empty")).not.toBeInTheDocument();
    if (row.next) expect(screen.queryByTestId(row.next.testId)).not.toBeInTheDocument();
    for (const testId of row.locked ?? []) expect(screen.getByTestId(testId)).toBeDisabled();

    recover();
    await user.click(within(error).getByTestId("state-retry"));
    const empty = await screen.findByTestId("state-empty");
    expect(empty).toHaveTextContent(row.empty);
    if (row.next) expect(within(empty).getByTestId(row.next.testId)).toHaveAttribute("href", row.next.href);
    expect(screen.queryByTestId("state-error")).not.toBeInTheDocument();
  });
});

interface Missing {
  /** 못 찾은 것의 이름. 화면에 쓰는 리소스 이름 그대로다(NotFoundPage 의 resource). */
  resource: string;
  /** 서버가 404·410 의 detail 에 적는 영어 이름이다(플랫폼과 msw `docStore` 의 noun). */
  noun: string;
  /** 목록 경로. 상세는 그 아래 `/:id` 이고 돌아가는 길도 여기다. */
  list: string;
  /** 410 이면 언제 지워졌는지 말하는가(MissingResourceScreen). */
  saysWhen?: boolean;
  /** 이름 뒤에 붙는 주격 조사. 이름의 끝소리를 따른다. */
  ga: "이" | "가";
}

const MISSING: Missing[] = [
  { resource: "Agent", noun: "agent", list: "/agents", saysWhen: true, ga: "가" },
  { resource: "MCP Server", noun: "mcp server", list: "/mcp-servers", saysWhen: true, ga: "가" },
  { resource: "Gateway", noun: "gateway", list: "/gateways", saysWhen: true, ga: "가" },
  { resource: "Backend API", noun: "backend api", list: "/backend-apis", ga: "가" },
  { resource: "허브 항목", noun: "hub item", list: "/hub", ga: "이" },
  { resource: "Database", noun: "data service", list: "/data-services", ga: "가" },
  { resource: "Knowledge Base", noun: "knowledge base", list: "/knowledge-bases", ga: "가" },
  { resource: "Cluster", noun: "cluster", list: "/clusters", ga: "가" },
];

/** 목에 없는 문서 id 다. 응답은 테스트가 정한다. */
const GONE_ID = "gone";

function answerDetail(row: Missing, response: () => Response) {
  server.use(http.get(`${row.list}/:id`, response));
  render(<TestApp initialPath={`${row.list}/${GONE_ID}`} />);
}

describe("없는 상세", () => {
  it.each(MISSING.map((row) => [row.resource, row] as const))("없는 %s 상세를 열면 무엇을 못 찾았는지 말하고 목록으로 돌아가는 길을 준다", async (_resource, row) => {
    answerDetail(row, () => apiError(404, "not_found", `${row.noun} ${GONE_ID} not found`));
    const page = await screen.findByTestId("not-found-page");
    expect(page).toHaveTextContent(`요청한 ${row.resource}${row.ga} 없습니다.`);
    expect(within(page).getByTestId("not-found-back")).toHaveAttribute("href", row.list);
    expect(screen.queryByTestId("not-found-gone")).not.toBeInTheDocument();
  });

  it.each(MISSING.filter((row) => row.saysWhen).map((row) => [row.resource, row] as const))("%s 상세가 410 이면 언제 지워졌는지 말한다", async (_resource, row) => {
    answerDetail(row, () => apiError(410, "gone", `${row.noun} ${GONE_ID} was deleted`, { deleted_at: NOW }));
    expect(await screen.findByTestId("not-found-gone")).toHaveTextContent("삭제됐습니다");
    expect(screen.getByTestId("not-found-page")).toBeInTheDocument();
  });
});

interface Undeployed {
  /** 테스트 이름에 쓰는 리소스 이름. */
  resource: string;
  /** 초안 상세 경로. */
  path: string;
  /** 목에 초안을 세운다. */
  seed: () => void;
  /** 배포해야 열리는 탭. */
  closedTabs: string[];
}

const UNDEPLOYED: Undeployed[] = [
  {
    resource: "Agent",
    path: "/agents/ag-draft",
    seed: () => {
      agentState.items = [agentDraftFixture()];
    },
    closedTabs: ["chat", "sessions", "approvals", "metrics", "events", "advanced"],
  },
  {
    resource: "MCP Server",
    path: "/mcp-servers/ms-draft",
    seed: () => {
      mcpServerState.items = [draftFixture()];
    },
    closedTabs: ["advanced"],
  },
  {
    resource: "Gateway",
    path: "/gateways/gw-draft",
    seed: () => {
      gatewayState.items = [gatewayDraftFixture()];
    },
    closedTabs: ["advanced"],
  },
];

describe("배포하지 않은 상세", () => {
  it.each(UNDEPLOYED.map((row) => [row.resource, row] as const))("배포하지 않은 %s 의 개요는 그렇다고 말하고 배포 탭으로 보내며, 배포해야 볼 수 있는 탭은 닫혀 있다", async (_resource, row) => {
    const user = userEvent.setup();
    row.seed();
    render(<TestApp initialPath={row.path} />);
    const overview = await screen.findByTestId("overview-empty");
    expect(overview).toHaveTextContent("아직 배포하지 않았습니다");
    for (const tab of row.closedTabs) expect(screen.getByTestId(`tab-${tab}`)).toBeDisabled();
    await user.click(within(overview).getByTestId("overview-deploy"));
    expect(await screen.findByTestId("deployment-empty")).toHaveTextContent("아직 배포하지 않았습니다");
  });
});

interface Gate {
  /** 런타임이 알리지 않은 기능. */
  feature: string;
  /** 그때 화면이 어떤지. 테스트 이름의 뒷부분이다. */
  outcome: string;
  /** 그 기능만 빠진 Agent. 주지 않으면 cancel 만 알린 런타임이다. */
  agent?: () => Agent;
  /** 단추가 설 자리를 열고 화면이 다 설 때까지 기다린다. */
  open: (user: UserEvent) => Promise<unknown>;
  /** 없어야 하는 단추. */
  hidden: string[];
}

const GATES: Gate[] = [
  {
    feature: RUNTIME_REGENERATE,
    outcome: "다시 생성과 메시지 수정 단추가 없다",
    open: async (user) => {
      await firstTurn(user, "출력 차단");
      // 턴이 끝나 입력칸이 다시 열린 뒤에 본다. 도는 동안에는 기능이 있어도 단추가 없다
      await waitFor(() => expect(screen.getByTestId("chat-send")).toHaveTextContent("보내기"));
    },
    hidden: ["chat-regenerate", "chat-message-1-edit"],
  },
  { feature: RUNTIME_ATTACHMENTS, outcome: "입력칸에 클립 단추가 없다", open: () => openAgentChat(), hidden: ["chat-attach"] },
  {
    feature: RUNTIME_SESSION_ADMIN,
    outcome: "홈 챗 대화 목록에 검색칸과 행 메뉴가 없다",
    open: openHomeSessionList,
    hidden: ["home-sessions-search", "home-session-menu-s-1", "home-session-delete-s-1"],
  },
  {
    feature: RUNTIME_SESSION_ADMIN,
    outcome: "Agent 세션 탭에 검색칸이 없다",
    open: async () => {
      render(<TestApp initialPath="/agents/ag-support?tab=sessions" />);
      await screen.findByTestId("session-row-s-1");
    },
    hidden: ["sessions-search"],
  },
  {
    feature: RUNTIME_SESSION_FILES,
    outcome: "대화 위에 파일 단추가 없다",
    open: async (user) => {
      await openHomeSession(user);
      await screen.findByTestId("chat-message-0");
    },
    hidden: ["session-files-open"],
  },
  { feature: RUNTIME_MEMORY_FILES, outcome: "장기 기억을 켜도 기억 단추가 없다", agent: () => agentWithLongTerm(RUNTIME_CANCEL), open: openHomeSessionList, hidden: ["memory-drawer-open"] },
];

const gateName = ({ feature, outcome }: Gate) => `런타임이 ${feature} ${particle(feature, "을/를")} 알리지 않으면 ${outcome}`;

describe("런타임이 알리지 않은 기능", () => {
  it.each(GATES.map((row) => [gateName(row), row] as const))("%s", async (_name, row) => {
    agentState.items = [row.agent?.() ?? agentWithRuntimeFeatures(RUNTIME_CANCEL)];
    await row.open(userEvent.setup());
    for (const testId of row.hidden) expect(screen.queryByTestId(testId)).not.toBeInTheDocument();
  });
});
