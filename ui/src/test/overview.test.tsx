/**
 * 개요 카드 다섯 (D44 셋째, P24 F3). 카드마다 혼자 세워 보고 도메인 셋을 붙인 모양은 각 상세
 * 테스트가 본다. 셋이 같은 모양인 배포하지 않은 개요는 resource-states.test.tsx 의 표가 본다.
 */
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import type { InstanceCondition } from "@/api/types";
import { AddressCard } from "@/components/overview/AddressCard";
import { ConditionsDetails, conditionsSummary } from "@/components/overview/ConditionsDetails";
import { DefinitionSummaryCard } from "@/components/overview/DefinitionSummaryCard";
import { ReadinessCard, readySince } from "@/components/overview/ReadinessCard";
import { RecentCard, recentEvents } from "@/components/overview/RecentCard";
import { dictionary } from "@/i18n";
import { formatAgo, formatDuration } from "@/lib/utils";

import { stubClipboard } from "./clipboard";

const ko = dictionary("ko");

const cond = (over: Partial<InstanceCondition> = {}): InstanceCondition => ({
  type: "Ready",
  status: "True",
  reason: null,
  message: null,
  last_transition: null,
  ...over,
});

describe("formatDuration", () => {
  it.each([
    // 분 아래는 뭉친다
    [0, "1분 미만"],
    [59, "1분 미만"],
    [90, "1분"],
    // 위로 갈수록 단위를 둘까지만 적는다
    [3600, "1시간"],
    [3660, "1시간 1분"],
    [90000, "1일 1시간"],
    [172800, "2일"],
  ])("%i초 -> %s", (seconds, text) => {
    expect(formatDuration(ko, seconds)).toBe(text);
  });
});

describe("formatAgo", () => {
  const now = Date.parse("2026-10-08T12:00:00Z");
  const before = (seconds: number) => new Date(now - seconds * 1000).toISOString();

  it.each([
    [30, "방금"],
    [5 * 60, "5분 전"],
    [3 * 3600 + 59 * 60, "3시간 전"],
    // 카드의 흐린 줄은 단위를 하나만 적는다
    [20 * 86400 + 15 * 3600, "20일 전"],
  ])("%i초 전 -> %s", (seconds, text) => {
    expect(formatAgo(ko, before(seconds), now)).toBe(text);
  });

  it("한 달이 넘으면 화면 언어의 날짜로, 값이 없거나 틀리면 null 로 답한다", () => {
    const old = before(40 * 86400);
    expect(formatAgo(ko, old, now)).toBe(new Date(old).toLocaleDateString("ko-KR"));
    expect(formatAgo(dictionary("en"), old, now)).toBe(new Date(old).toLocaleDateString("en-US"));
    expect(formatAgo(ko, null, now)).toBeNull();
    expect(formatAgo(ko, "not-a-date", now)).toBeNull();
  });
});

describe("AddressCard", () => {
  it("안 주소를 복사하고 인증 방식을 한 줄로 적는다", async () => {
    const user = userEvent.setup();
    const writeText = stubClipboard();
    render(
      <AddressCard
        url="http://mcp-calc.daegam-default.svc:8080/mcp"
        externalUrl="https://calc.example.com/mcp"
        auth="서비스 토큰이 있어야 부를 수 있습니다."
      />,
    );
    const card = screen.getByTestId("address-card");
    expect(within(card).getByTestId("status-url")).toHaveTextContent("mcp-calc.daegam-default.svc");
    await user.click(within(card).getByTestId("copy-url"));
    await waitFor(() => expect(writeText).toHaveBeenCalledWith("http://mcp-calc.daegam-default.svc:8080/mcp"));
    expect(within(card).getByTestId("status-external-url")).toHaveTextContent("https://calc.example.com/mcp");
    expect(within(card).getByTestId("address-auth")).toHaveTextContent("서비스 토큰");
    // 노출돼 있으면 켜러 가라고 하지 않는다
    expect(within(card).queryByTestId("address-expose")).not.toBeInTheDocument();
  });

  it("노출이 꺼져 있으면 노출 안 함과 배포 탭으로 가는 길을 보인다", async () => {
    const onExpose = vi.fn();
    const user = userEvent.setup();
    render(<AddressCard url={null} externalUrl={null} onExpose={onExpose} />);
    const card = screen.getByTestId("address-card");
    expect(within(card).getByTestId("status-external-url")).toHaveTextContent("외부 노출 안 함");
    expect(within(card).queryByTestId("copy-external-url")).not.toBeInTheDocument();
    await user.click(within(card).getByTestId("address-expose"));
    expect(onExpose).toHaveBeenCalled();
    // 주소가 없으면 복사할 것도 없다
    expect(within(card).queryByTestId("copy-url")).not.toBeInTheDocument();
  });
});

describe("ReadinessCard", () => {
  const NOW = Date.parse("2026-09-16T12:00:00Z");

  it("Ready 조건의 전이 시각이 있으면 가동 시간을 보인다", () => {
    render(
      <ReadinessCard
        phase="Ready"
        ready={2}
        wanted={2}
        conditions={[cond({ last_transition: "2026-09-16T09:30:00Z" })]}
        observedAt="2026-09-16T11:59:55Z"
        deployedRevision={3}
        revision={3}
        now={NOW}
      />,
    );
    expect(screen.getByTestId("status-phase")).toHaveTextContent("Ready");
    expect(screen.getByTestId("status-ready-replicas")).toHaveTextContent("2 / 2");
    expect(screen.getByTestId("readiness-uptime")).toHaveTextContent("2시간 30분");
    expect(screen.getByTestId("readiness-observed")).toHaveTextContent("5초마다 확인");
    // 같으면 덧붙이지 않는다. 리비전 숫자만 선다
    expect(screen.getByTestId("status-revision")).toHaveTextContent("r3");
    expect(screen.getByTestId("status-revision")).not.toHaveTextContent("정의");
  });

  it("전이 시각이 없거나 Ready 가 아니면 그 줄을 뺀다", () => {
    render(
      <ReadinessCard
        phase="Pending"
        conditions={[cond({ status: "False", last_transition: "2026-09-16T09:30:00Z" })]}
        observedAt="2026-09-16T11:59:55Z"
        deployedRevision={2}
        revision={5}
        message="파드를 기다리는 중"
        now={NOW}
      />,
    );
    expect(screen.queryByTestId("readiness-uptime")).not.toBeInTheDocument();
    // Gateway 처럼 준비된 파드 수가 없으면 그 줄도 없다
    expect(screen.queryByTestId("status-ready-replicas")).not.toBeInTheDocument();
    // 값은 돌고 있는 리비전이고 저장된 정의는 말로 붙는다
    expect(screen.getByTestId("status-revision")).toHaveTextContent("r2");
    expect(screen.getByTestId("status-revision")).toHaveTextContent("저장된 정의는 r5입니다");
    expect(screen.getByTestId("status-message")).toHaveTextContent("파드를 기다리는 중");
  });

  it.each([
    ["True 인 Ready", [cond({ last_transition: "2026-09-16T09:30:00Z" })], "2026-09-16T09:30:00Z"],
    ["False 인 Ready", [cond({ status: "False", last_transition: "x" })], null],
    ["Ready 가 아닌 조건", [cond({ type: "Progressing", last_transition: "x" })], null],
    ["빈 조건", [], null],
    ["조건 없음", undefined, null],
  ])("readySince 는 True 인 Ready 조건만 본다: %s", (_case, conditions, since) => {
    expect(readySince(conditions)).toBe(since);
  });
});

describe("RecentCard", () => {
  it("새것이 위이고 시각을 못 읽는 줄은 서지 않는다", () => {
    const events = recentEvents({
      createdAt: "2026-09-01T00:00:00Z",
      revisions: [
        { revision: 1, created_at: "2026-09-01T00:00:00Z" },
        { revision: 2, created_at: "2026-09-05T00:00:00Z" },
      ],
      deployedRevision: 2,
      since: "2026-09-06T00:00:00Z",
      observedAt: "2026-09-16T00:00:00Z",
    });
    expect(events).toEqual([
      { at: "2026-09-16T00:00:00Z", kind: "observed" },
      { at: "2026-09-06T00:00:00Z", kind: "deployed", revision: 2 },
      { at: "2026-09-05T00:00:00Z", kind: "saved", revision: 2 },
      { at: "2026-09-01T00:00:00Z", kind: "created" },
      { at: "2026-09-01T00:00:00Z", kind: "saved", revision: 1 },
    ]);
    render(<RecentCard events={events} />);
    // 줄은 갈래만 들고 낱말은 그릴 때 사전에서 만든다
    const items = within(screen.getByTestId("recent-card-list")).getAllByRole("listitem");
    expect(items).toHaveLength(5);
    ["상태 확인", "r2 배포", "정의 r2 저장", "생성", "정의 r1 저장"].forEach((word, i) => expect(items[i]).toHaveTextContent(word));
  });

  it("리비전 이력이 없으면 정의를 수정 한 줄로 대신한다", () => {
    const events = recentEvents({ lastSpecChange: "2026-09-10T00:00:00Z", since: "not-a-date", limit: 2 });
    expect(events).toEqual([{ at: "2026-09-10T00:00:00Z", kind: "edited" }]);
  });

  it("줄이 하나도 없으면 그렇게 말한다", () => {
    render(<RecentCard events={[]} />);
    expect(screen.getByTestId("recent-card")).toHaveTextContent("아직 기록이 없습니다");
  });
});

describe("DefinitionSummaryCard", () => {
  it("리비전 배지와 넘겨받은 줄을 그린다", () => {
    render(
      <DefinitionSummaryCard
        revision={4}
        rows={[
          { label: "백엔드", value: "calc v2", testId: "sum-backend" },
          { label: "Tool", value: "1 / 3", testId: "sum-tools" },
        ]}
      />,
    );
    const card = screen.getByTestId("definition-summary");
    expect(card).toHaveTextContent("r4");
    expect(within(card).getByTestId("sum-backend")).toHaveTextContent("calc v2");
    expect(within(card).getByTestId("sum-tools")).toHaveTextContent("1 / 3");
  });
});

describe("ConditionsDetails", () => {
  it("전부 정상이면 접힌 채 요약 한 줄이다", () => {
    render(<ConditionsDetails conditions={[cond(), cond({ type: "Progressing", status: "True" })]} />);
    const details = screen.getByTestId("conditions-details");
    expect(details).toHaveTextContent("클러스터 상태 자세히 · conditions 2개 전부 정상");
    expect(details).not.toHaveAttribute("open");
  });

  it("문제인 condition 이 있으면 몇 개인지 말하고 펼친 채로 연다", () => {
    render(
      <ConditionsDetails
        conditions={[cond({ status: "False", reason: "BackendUnavailable", last_transition: "2026-09-16T09:30:00Z" }), cond({ type: "Valid", status: "True" })]}
      />,
    );
    const details = screen.getByTestId("conditions-details");
    expect(details).toHaveTextContent("conditions 주의 1개");
    expect(details).toHaveAttribute("open");
    expect(within(screen.getByTestId("conditions-table")).getByTestId("condition-Ready")).toHaveTextContent("BackendUnavailable");
  });

  it("조건이 없으면 접힌 상세 자체를 두지 않는다", () => {
    render(<ConditionsDetails conditions={[]} />);
    expect(screen.queryByTestId("conditions-details")).not.toBeInTheDocument();
    expect(conditionsSummary(ko, [])).toBe("conditions 0개 전부 정상");
  });
});
