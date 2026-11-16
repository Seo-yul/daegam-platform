/**
 * 화면 언어 바꾸기 (P49 1절·9절). 언어를 바꿔도 아무것도 다시 마운트되지 않는다. 살아 있는 Agent 캔버스와 저장하지 않은
 * 입력이 있는 폼에서 영어로 바꾸면 껍데기의 글자는 바뀌고, 입력과 캔버스 상태(노드, 고른 노드)는 그대로 남는다.
 * 캔버스의 노드 카드는 xyflow 가 memo 로 감싸 부모가 다시 그려져도 따라 그려지지 않으므로, 카드마다 언어를 읽어 글자가
 * 바뀌는지 따로 본다.
 */
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { TestApp } from "@/App";
import { setLanguage } from "@/lib/language";

import { selectValue } from "./select";

/** 캔버스에 그려진 노드의 id. xyflow 가 노드마다 data-id 를 단다. */
function nodeIds(): string[] {
  return [...document.querySelectorAll(".react-flow__node")].map((node) => node.getAttribute("data-id") ?? "");
}

/** 노드 카드와 범례의 글자. 템플릿의 빈 provider·Database 자리와 그 검증 표식, 간선 범례다. */
function cardText(): string[] {
  return [
    screen.getByTestId("node-title-entry").textContent ?? "",
    screen.getByTestId("node-provider-model-main").textContent ?? "",
    screen.getByTestId("node-title-memory").textContent ?? "",
    screen.getByTestId("node-mark-memory").getAttribute("aria-label") ?? "",
    screen.getByTestId("canvas-legend").textContent ?? "",
  ];
}

const KOREAN_CARDS = ["Chat 트리거", "provider 미선택", "Database 미선택", "오류 1건", "입구모델Tool서브 에이전트메모리가드레일지식⇧끌어 영역 선택⌘여러 개 선택Delete삭제"];

describe("language switch", () => {
  it("keeps a live canvas: its nodes, the typed name and the selected node stay while the shell changes language", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/agents/new?template=support-agent" />);
    expect(await screen.findByTestId("node-agent-main")).toBeInTheDocument();
    // 아무 노드도 고르지 않은 인스펙터가 Agent 의 이름 칸이다
    const name = await screen.findByTestId("ap-name");
    await user.clear(name);
    await user.type(name, "smoke");
    const nodes = nodeIds();
    expect(nodes.length).toBeGreaterThan(1);
    // 캔버스에서는 사이드바가 아이콘 레일로 접혀 항목 이름이 aria-label 에 있다
    expect(screen.getByTestId("nav-home-chat")).toHaveAttribute("aria-label", "새 대화");
    expect(screen.getByTestId("sidebar-toggle")).toHaveAttribute("aria-label", "사이드바 펼치기");
    expect(cardText()).toEqual(KOREAN_CARDS);

    act(() => setLanguage("en"));

    expect(screen.getByTestId("nav-home-chat")).toHaveAttribute("aria-label", "New conversation");
    expect(screen.getByTestId("sidebar-toggle")).toHaveAttribute("aria-label", "Expand sidebar");
    expect(screen.getByTestId("ap-name")).toHaveValue("smoke");
    expect(nodeIds()).toEqual(nodes);
    // 노드 카드도 영어다. 카드는 xyflow 의 memo 안이라 언어를 스스로 읽어야 바뀐다
    expect(cardText()).toEqual(["Chat trigger", "No provider selected", "No Database selected", "1 error", "EntryModelToolSubagentMemoryGuardrailKnowledge⇧Drag to select an area⌘Select severalDeleteDelete"]);

    // 고른 노드도 캔버스 상태다. 한국어로 되돌려도 그대로 남는다
    fireEvent.click(screen.getByTestId("node-model-main"));
    await waitFor(() => expect(screen.getByTestId("inspector")).toHaveAttribute("data-selected-node", "model-main"));

    act(() => setLanguage("ko"));

    expect(screen.getByTestId("nav-home-chat")).toHaveAttribute("aria-label", "새 대화");
    expect(screen.getByTestId("inspector")).toHaveAttribute("data-selected-node", "model-main");
    expect(nodeIds()).toEqual(nodes);
    expect(cardText()).toEqual(KOREAN_CARDS);
  });

  it("keeps the unsaved input of a form while the sidebar turns English", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/mcp-servers/new" />);
    await user.type(await screen.findByTestId("mp-name"), "calc2");
    await selectValue(user, "mp-backend", "bp-orders");
    await selectValue(user, "mp-version", "2");
    await user.click(await screen.findByTestId("tool-CreateOrder"));
    expect(screen.getByTestId("nav-home-chat")).toHaveTextContent("새 대화");
    expect(screen.getByTestId("skip-to-main")).toHaveTextContent("본문으로 건너뛰기");

    act(() => setLanguage("en"));

    expect(screen.getByTestId("nav-home-chat")).toHaveTextContent("New conversation");
    expect(screen.getByTestId("skip-to-main")).toHaveTextContent("Skip to main content");
    expect(screen.getByTestId("sidebar-toggle")).toHaveAttribute("aria-label", "Collapse sidebar");
    expect(screen.getByTestId("mp-name")).toHaveValue("calc2");
    expect(screen.getByTestId("tool-CreateOrder")).toBeChecked();
  });
});
