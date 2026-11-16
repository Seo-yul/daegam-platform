import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeAll, beforeEach, describe, expect, it } from "vitest";

import { chatBase } from "@/api/client";
import type { AssistantTranscriptItem } from "@/api/types";
import { TestApp } from "@/App";
import { applyChatEvent, ChatItemView, CitationScope, type ChatItem } from "@/components/chat";
import { dictionary } from "@/i18n";
import { itemsFromTranscript } from "@/lib/assistant";
import { citationTable, citedNumbers, EXCERPT_MAX_CHARS } from "@/lib/citations";
import { chatFeatures, RUNTIME_CITATIONS } from "@/lib/runtimeFeatures";

import { focusLikeBrowser } from "./browserFocus";
import { stubDownloads } from "./downloads";
import { agentSessionState, agentState, agentWithRuntimeFeatures, apiError, holdRequest, http, HttpResponse, lastExact, server } from "./msw";
import { renderWithProviders } from "./render";
import { CITATIONS_FIXTURE } from "./runtimeFixtures";

const ko = dictionary("ko");

/**
 * 답의 인용 번호 (P36 7절). 런타임 표본(agent-runtime/schema/fixtures/chat/citations.json)의 검색 턴을 되살려 번호 표, 번호
 * 칩, 출처 줄, 작은 창의 원문 열기·다운로드·가림 안내를 본다. 마크다운 본문은 지연 청크라 처음에 한 번 실어 둔다.
 */
const ITEMS = itemsFromTranscript(CITATIONS_FIXTURE.transcript);
const ANSWER_AT = ITEMS.findIndex((it) => it.kind === "assistant");
const SOURCE_PATH = "/agents/ag-support/sessions/s1/citations/1/source";

/** 상태의 runtime 기록과 도는 리비전의 가림 사실로 채팅 화면이 아는 런타임의 사실을 만든다. */
function features({ canDownload = true, masked = false }: { canDownload?: boolean; masked?: boolean }) {
  const runtime = { image: "daegam-agent-runtime:0.2.0", features: canDownload ? [RUNTIME_CITATIONS] : [] };
  return chatFeatures({ runtime, chat: null, sources_masked: masked });
}

type TranscriptOptions = { canDownload?: boolean; masked?: boolean };

function Transcript({ items, options = {} }: { items: ChatItem[]; options?: TranscriptOptions }) {
  return (
    <CitationScope items={items} chat={chatBase("ag-support")} sessionId="s1" features={features(options)}>
      {items.map((it, i) => (
        <ChatItemView key={i} index={i} item={it} />
      ))}
    </CitationScope>
  );
}

function renderTranscript(items: ChatItem[] = ITEMS, options: TranscriptOptions = {}) {
  return renderWithProviders(<Transcript items={items} options={options} />);
}

function answer(): HTMLElement {
  return screen.getByTestId(`chat-message-${ANSWER_AT}`);
}

beforeAll(async () => {
  const { unmount } = renderTranscript();
  await within(answer()).findByTestId("citation-chip-1");
  unmount();
});

beforeEach(() => {
  agentState.items = [agentWithRuntimeFeatures(RUNTIME_CITATIONS)];
});

describe("번호 표", () => {
  it("전사의 도구 호출 카드가 든 출처로 표를 만들고 발췌는 같은 호출 결과의 cite 본문이다", () => {
    const table = citationTable(ITEMS);
    expect([...table.keys()]).toEqual([1, 2]);
    expect(table.get(1)).toMatchObject({ title: "환불규정.md", kb: { name: "faq" }, chunk: 3, url: null });
    expect(table.get(1)?.excerpt).toBe("환불은 결제일로부터 7일 안에 신청할 수 있습니다. 영수증은 마이페이지에서 다시 받습니다.");
    expect(table.get(2)?.url).toBe("https://help.example.com/shipping");
  });

  it("발췌는 300자에서 자르고, 결과 본문이 검색 모양이 아니면 발췌 없이 둔다", () => {
    const card = ITEMS.find((it): it is Extract<ChatItem, { kind: "tool_call" }> => it.kind === "tool_call")!;
    const long = { ...card, result: { ...card.result!, content: JSON.stringify({ results: [{ cite: 1, content: "가".repeat(400) }] }) } };
    expect(citationTable([long]).get(1)?.excerpt).toBe(`${"가".repeat(EXCERPT_MAX_CHARS)}…`);
    const plain = { ...card, result: { ...card.result!, content: "막았습니다" } };
    expect(citationTable([plain]).get(2)?.excerpt).toBeNull();
  });

  it("citations 이벤트는 같은 호출의 카드에 붙고 카드가 없으면 버린다", () => {
    const { events } = CITATIONS_FIXTURE;
    let items: ChatItem[] = [{ kind: "user", text: "환불 기한" }];
    items = applyChatEvent(items, { event: "tool_call", data: events.tool_call });
    items = applyChatEvent(items, { event: "tool_result", data: events.tool_result });
    items = applyChatEvent(items, { event: "citations", data: events.citations });
    expect(items[1]).toMatchObject({ kind: "tool_call", citations: events.citations.items });
    const before = applyChatEvent(items, { event: "citations", data: { call_id: "nope", items: [] } });
    expect(before).toBe(items);
  });

  it("출처 줄의 번호는 처음 나온 차례이고 코드 안의 번호와 표에 없는 번호가 든 묶음은 빠진다", () => {
    const text = "앞 [2] 뒤 [1][2]. `[3]` 은 코드다.\n\n```\n[3]\n```\n\n[1, 9] 와 [3, 1]";
    expect(citedNumbers(text, new Set([1, 2, 3]))).toEqual([2, 1, 3]);
    expect(citedNumbers("[1, 9]", new Set([1]))).toEqual([]);
  });
});

describe("번호 칩과 출처 줄", () => {
  it("답의 [n] 은 번호 칩이 되고 코드 안의 번호는 글자로 남는다. 출처 줄은 쓴 번호를 문서 이름과 모은다", async () => {
    renderTranscript();
    const bubble = answer();
    expect(await within(bubble).findByTestId("citation-chip-1")).toHaveTextContent("1");
    expect(within(bubble).getByTestId("citation-chip-2")).toBeInTheDocument();
    expect(within(bubble).queryByTestId("citation-chip-9")).toBeNull();
    expect(within(bubble).getByText("[9]", { selector: "code" })).toBeInTheDocument();
    const sources = screen.getByTestId(`chat-message-${ANSWER_AT}-sources`);
    expect(within(sources).getAllByRole("button").map((b) => b.textContent)).toEqual(["1환불규정.md", "2배송 안내"]);
  });

  it("출처가 없는 대화의 [n] 은 글자 그대로이고 출처 줄이 없다", async () => {
    const items: ChatItem[] = [{ kind: "assistant", text: "**참고** [1]", streaming: false }];
    renderTranscript(items);
    expect(await screen.findByText("참고", { selector: "strong" })).toBeInTheDocument();
    expect(screen.getByTestId("chat-message-0")).toHaveTextContent("참고 [1]");
    expect(screen.queryByTestId("citation-chip-1")).toBeNull();
    expect(screen.queryByTestId("chat-message-0-sources")).toBeNull();
  });
});

describe("작은 창", () => {
  let saved: string[] = [];

  beforeEach(() => {
    // 창이 열리자마자 포커스를 받는 것을 숨은 요소에 포커스를 주지 않는 브라우저 규칙으로 본다
    focusLikeBrowser();
    saved = stubDownloads();
  });

  it("올린 문서는 이름·출처·조각과 발췌를 보이고 원문을 다운로드한다. Escape 는 닫고 칩으로 돌아간다", async () => {
    const user = userEvent.setup();
    renderTranscript();
    const chip = await within(answer()).findByTestId("citation-chip-1");
    await user.click(chip);
    const popover = screen.getByTestId("citation-popover");
    expect(popover).toHaveAccessibleName("출처 1");
    expect(popover).toHaveTextContent("환불규정.md");
    expect(popover).toHaveTextContent("faq");
    expect(popover).toHaveTextContent("4번째 조각 · 관련도 0.83");
    expect(popover).toHaveTextContent("영수증은 마이페이지에서 다시 받습니다.");
    expect(within(popover).queryByTestId("citation-open")).toBeNull();

    await user.click(within(popover).getByTestId("citation-download"));
    await waitFor(() => expect(saved).toEqual(["환불규정.md"]));
    expect(lastExact("GET", SOURCE_PATH)).toBeDefined();

    await user.keyboard("{Escape}");
    expect(screen.queryByTestId("citation-popover")).toBeNull();
    expect(chip).toHaveFocus();

    // 키보드로 열면 창이 포커스를 받아 Escape 가 바로 닫는다
    await user.keyboard("{Enter}");
    expect(screen.getByTestId("citation-popover")).toHaveFocus();
    await user.keyboard("{Escape}");
    expect(screen.queryByTestId("citation-popover")).toBeNull();
    expect(chip).toHaveFocus();
  });

  it("뒤 답이 새 출처를 더해 표가 바뀌어도 앞 답의 열린 작은 창은 그대로다", async () => {
    const user = userEvent.setup();
    const { rerender } = renderTranscript();
    await user.click(await within(answer()).findByTestId("citation-chip-1"));
    expect(screen.getByTestId("citation-popover")).toBeInTheDocument();

    const card = ITEMS.find((it): it is Extract<ChatItem, { kind: "tool_call" }> => it.kind === "tool_call")!;
    const next: ChatItem = { ...card, call: { ...card.call, id: "call_kb_2" }, result: card.result && { ...card.result, id: "call_kb_2" }, citations: [{ ...card.citations![0], n: 3, title: "교환규정.md" }] };
    rerender(<Transcript items={[...ITEMS, next, { kind: "assistant", text: "교환은 7일 안에 합니다 [3].", streaming: false }]} />);
    expect(await screen.findByTestId("citation-chip-3")).toBeInTheDocument();
    // 칩이 다시 마운트되면 열린 창이 닫힌다
    expect(screen.getByTestId("citation-popover")).toHaveTextContent("환불규정.md");
  });

  it("URL 문서는 새 탭으로 원문을 열고 다운로드 단추가 없다", async () => {
    const user = userEvent.setup();
    renderTranscript();
    await user.click(await screen.findByTestId("citation-source-2"));
    const popover = screen.getByTestId("citation-popover");
    expect(popover).toHaveTextContent("help.example.com");
    const open = within(popover).getByTestId("citation-open");
    expect(open).toHaveAttribute("href", "https://help.example.com/shipping");
    expect(open).toHaveAttribute("target", "_blank");
    expect(open.getAttribute("rel")).toContain("noopener");
    expect(within(popover).queryByTestId("citation-download")).toBeNull();
  });

  it("도는 리비전에 가림 규칙이 있으면 다운로드 단추 대신 원문을 열 수 없다고 안내한다", async () => {
    const user = userEvent.setup();
    renderTranscript(ITEMS, { masked: true });
    await user.click(await within(answer()).findByTestId("citation-chip-1"));
    const popover = screen.getByTestId("citation-popover");
    expect(within(popover).queryByTestId("citation-download")).toBeNull();
    expect(within(popover).getByTestId("citation-masked")).toHaveTextContent("이 Agent 는 가림 규칙이 있어 원문을 열 수 없습니다.");
    // URL 문서는 바깥 주소라 가림과 상관없이 연다
    await user.keyboard("{Escape}");
    await user.click(await screen.findByTestId("citation-source-2"));
    expect(within(screen.getByTestId("citation-popover")).getByTestId("citation-open")).toBeInTheDocument();
  });

  it("받는 동안의 다운로드 단추는 disabled 가 아니라 aria-disabled 라 초점을 지키고 Escape 가 창을 닫는다", async () => {
    const source = holdRequest("get", "/agents/:id/sessions/:sid/citations/:n/source");
    const user = userEvent.setup();
    renderTranscript();
    const chip = await within(answer()).findByTestId("citation-chip-1");
    await user.click(chip);
    const download = screen.getByTestId("citation-download");
    await user.click(download);
    // disabled 를 걸면 누른 단추가 초점을 잃어 창의 Escape 가 멈춘다
    expect(download).toHaveAttribute("aria-disabled", "true");
    expect(download).toBeEnabled();
    expect(download).toHaveFocus();
    await user.click(download);
    await user.keyboard("{Escape}");
    expect(screen.queryByTestId("citation-popover")).toBeNull();
    expect(chip).toHaveFocus();
    source.release();
    await waitFor(() => expect(saved).toEqual(["환불규정.md"]));
  });

  it("상태를 읽기 전에 누른 다운로드가 가림으로 거절되면 같은 안내를 보인다", async () => {
    server.use(http.get("/agents/:id/sessions/:sid/citations/:n/source", () => apiError(403, "source_masked", "the agent masks tool results or answers; sources stay closed")));
    const user = userEvent.setup();
    renderTranscript();
    await user.click(await within(answer()).findByTestId("citation-chip-1"));
    await user.click(screen.getByTestId("citation-download"));
    expect(await screen.findByTestId("citation-download-error")).toHaveTextContent(ko.errors.codes.source_masked);
    expect(ko.errors.codes.source_masked).toContain("이 Agent 는 가림 규칙이 있어 원문을 열 수 없습니다.");
    expect(saved).toEqual([]);
  });

  it("런타임이 citations 를 알리지 않았으면 다운로드 단추가 없다", async () => {
    const user = userEvent.setup();
    renderTranscript(ITEMS, { canDownload: false });
    await user.click(await within(answer()).findByTestId("citation-chip-1"));
    expect(within(screen.getByTestId("citation-popover")).queryByTestId("citation-download")).toBeNull();
  });
});

describe("세션 탭", () => {
  /** provider 가 대화마다 0부터 센 호출 id 다(Kimi 계열의 `functions.<이름>:<n>`). 다른 세션에서 같은 값이 온다. */
  const SHARED_CALL_ID = "functions.search_faq:0";

  /** 표본의 검색 턴을 그 호출 id 로 바꾸고 1번 출처의 문서 이름을 `title` 로 바꾼 전사다. */
  function citedTranscript(title: string): AssistantTranscriptItem[] {
    return CITATIONS_FIXTURE.transcript.map((it) => {
      if (it.kind !== "tool_call") return it;
      const card = it as unknown as Extract<ChatItem, { kind: "tool_call" }>;
      return {
        ...card,
        call: { ...card.call, id: SHARED_CALL_ID },
        result: card.result && { ...card.result, id: SHARED_CALL_ID },
        citations: card.citations?.map((c) => (c.n === 1 ? { ...c, title } : c)),
      };
    });
  }

  it("같은 호출 id 를 쓰는 세션을 오가도 출처는 지금 고른 세션의 것이다", async () => {
    const titles: Record<string, string> = { "s-1": "환불규정.md", "s-3": "교환규정.md" };
    server.use(
      http.get("/agents/:id/sessions/:sid", ({ params }) => {
        const sid = String(params.sid);
        const session = agentSessionState.items.find((s) => s.session_id === sid);
        return HttpResponse.json({ session, messages: [], pending: false, transcript: { items: citedTranscript(titles[sid]), limit: 500 } });
      }),
    );
    const user = userEvent.setup();
    render(<TestApp initialPath="/agents/ag-support?tab=sessions" />);
    const sources = () => within(screen.getByTestId("session-detail")).getByTestId(`chat-message-${ANSWER_AT}-sources`);

    // s-1 을 연 뒤 s-3 을 열고, 캐시에 남은 s-1 로 돌아온다. 돌아올 때는 상세가 내려가지 않고 전사만 바뀐다
    for (const sid of ["s-1", "s-3", "s-1"]) {
      await user.click(await screen.findByTestId(`session-row-${sid}`));
      await waitFor(() => expect(sources()).toHaveTextContent(`1${titles[sid]}`));
    }
    await user.click(within(screen.getByTestId("session-detail")).getByTestId("citation-chip-1"));
    expect(screen.getByTestId("citation-popover")).toHaveTextContent(titles["s-1"]);
  });
});
