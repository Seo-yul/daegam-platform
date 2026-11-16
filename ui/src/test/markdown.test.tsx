import { readFileSync, readdirSync } from "node:fs";
import { join, resolve } from "node:path";

import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { Markdown, MarkdownFallback, STREAM_PARSE_INTERVAL_MS } from "@/components/markdown/Markdown";
import { classifyLink } from "@/lib/markdownLinks";

import { stubClipboard } from "./clipboard";
import { renderWithProviders } from "./render";
import { fakeClock } from "./timers";

/**
 * 채팅 마크다운 (D61). 모델이 쓴 글은 믿을 수 없으므로 원문 HTML·이미지·주소가 어떻게 그려지는지를 여기서 못 박는다.
 * 본문은 지연 청크라 파일 처음에 한 번 실어 두고, 그 뒤의 렌더는 청크를 기다리지 않는다.
 */
function renderMarkdown(text: string) {
  return renderWithProviders(<Markdown text={text} testId="m" />);
}

beforeAll(async () => {
  const { unmount } = renderMarkdown("**준비**");
  await screen.findByText("준비", { selector: "strong" });
  unmount();
});

describe("마크다운 그리기", () => {
  it("GFM 표를 가로 스크롤 틀 안의 표로 그린다", () => {
    renderMarkdown("| 이름 | 값 |\n|---|---:|\n| 갑 | 1 |\n| 을 | 2 |");
    const table = screen.getByRole("table");
    expect(table.parentElement).toHaveClass("overflow-x-auto");
    expect(within(table).getAllByRole("columnheader").map((th) => th.textContent)).toEqual(["이름", "값"]);
    expect(within(table).getAllByRole("cell").map((td) => td.textContent)).toEqual(["갑", "1", "을", "2"]);
  });

  it("취소선은 물결표 둘만 만든다. 범위를 적는 물결표 하나는 글자 그대로다", () => {
    renderMarkdown("10~20분, 월~금 중에 ~~취소~~ 한다");
    const p = screen.getByText(/10~20분/);
    expect(p.textContent).toBe("10~20분, 월~금 중에 취소 한다");
    expect(within(p).getByText("취소").tagName).toBe("DEL");
    expect(p.querySelectorAll("del")).toHaveLength(1);
  });

  it("코드 블록에 언어 이름을 달고 복사는 마지막 줄바꿈 없는 코드다", async () => {
    const user = userEvent.setup();
    const writeText = stubClipboard();
    renderMarkdown("설명\n\n```yaml\nname: calc\nport: 80\n```");
    const code = screen.getByTestId("m-code");
    expect(code.tagName).toBe("PRE");
    expect(code).toHaveAttribute("data-lang", "yaml");
    expect(code.parentElement).toHaveTextContent("yaml");
    await user.click(screen.getByTestId("m-code-copy"));
    await waitFor(() => expect(writeText).toHaveBeenCalledWith("name: calc\nport: 80"));
  });

  it("닫히지 않은 코드 펜스는 끝까지 코드다", () => {
    renderMarkdown("앞 글\n\n```py\nprint(1)\nprint(2)");
    expect(screen.getByTestId("m-code")).toHaveTextContent("print(1) print(2)");
    expect(screen.getByTestId("m-code")).toHaveAttribute("data-lang", "py");
  });

  it("원문 HTML 은 요소가 되지 않고 글자로 보인다", () => {
    const { container } = renderMarkdown('<script>alert(1)</script>\n\n<b>굵게</b> <img src="https://evil.example/x.png">');
    expect(container.querySelector("script, b, img")).toBeNull();
    expect(container).toHaveTextContent("<script>alert(1)</script>");
    expect(container).toHaveTextContent("<b>굵게</b>");
  });

  it("할 일 칸은 읽기 전용이다", () => {
    renderMarkdown("- [x] 끝난 일\n- [ ] 남은 일");
    const boxes = screen.getAllByRole("checkbox");
    expect(boxes.map((b) => (b as HTMLInputElement).checked)).toEqual([true, false]);
    for (const box of boxes) expect(box).toBeDisabled();
  });
});

describe("마크다운 링크", () => {
  it("바깥 주소는 새 탭 링크이고 호스트를 title 에 보인다", () => {
    renderMarkdown("[문서](https://docs.example.com/guide?a=1) 와 [메일](mailto:ops@example.com)");
    const doc = screen.getByRole("link", { name: "문서" });
    expect(doc).toHaveAttribute("href", "https://docs.example.com/guide?a=1");
    expect(doc).toHaveAttribute("target", "_blank");
    expect(doc).toHaveAttribute("rel", "noopener noreferrer nofollow");
    expect(doc).toHaveAttribute("title", "docs.example.com");
    expect(screen.getByRole("link", { name: "메일" })).toHaveAttribute("title", "ops@example.com");
  });

  it("앱 안 경로는 조회 문자열과 조각을 떼고 같은 탭 링크다. 같은 origin 의 절대 주소도 그렇다", () => {
    renderMarkdown(`[세션](/agents/a1?tab=sessions#top) 과 [목록](${window.location.origin}/agents?q=x)`);
    const session = screen.getByRole("link", { name: "세션" });
    expect(session).toHaveAttribute("href", "/agents/a1");
    expect(session).not.toHaveAttribute("target");
    expect(screen.getByRole("link", { name: "목록" })).toHaveAttribute("href", "/agents");
  });

  it("위험하거나 앱 화면이 아닌 주소는 링크를 빼고 글자만 남긴다", () => {
    const cases = [
      "[스크립트](javascript:alert(1))",
      "[데이터](data:text/html,hi)",
      "[API](/api/agents)",
      "[동의](/oauth/consent?request=abc)",
      "[비밀번호](/account/password)",
      "[로그인](/login)",
      "[상대](docs/readme.md)",
      "[프로토콜 생략](//evil.example/x)",
      `[같은 origin 동의](${window.location.origin}/oauth/consent?request=abc)`,
    ];
    const { container } = renderMarkdown(cases.join("\n\n"));
    expect(screen.queryAllByRole("link")).toEqual([]);
    for (const name of ["스크립트", "데이터", "API", "동의", "비밀번호", "로그인", "상대", "프로토콜 생략", "같은 origin 동의"]) {
      expect(container).toHaveTextContent(name);
    }
  });

  it("규칙은 대소문자·퍼센트 부호·역슬래시로 비켜 갈 수 없다", () => {
    const origin = "https://daegam.test";
    expect(classifyLink("/OAuth/consent", origin)).toEqual({ kind: "text" });
    expect(classifyLink("/%6Fauth/consent", origin)).toEqual({ kind: "text" });
    expect(classifyLink("/agents/../api/agents", origin)).toEqual({ kind: "text" });
    expect(classifyLink("/\\evil.example/x", origin)).toEqual({ kind: "text" });
    expect(classifyLink(" java\tscript:alert(1)", origin)).toEqual({ kind: "text" });
    expect(classifyLink("https://daegam.test/agents/a1?x=1", origin)).toEqual({ kind: "app", to: "/agents/a1" });
  });
});

describe("마크다운 이미지", () => {
  it("원격 이미지는 그리지 않고 '이미지: 설명' 링크 글자다", () => {
    const { container } = renderMarkdown("![매출 도표](https://img.example.com/chart.png)");
    expect(container.querySelector("img")).toBeNull();
    const link = screen.getByRole("link", { name: "이미지: 매출 도표" });
    expect(link).toHaveAttribute("href", "https://img.example.com/chart.png");
    expect(link).toHaveAttribute("target", "_blank");
  });

  it("data: 이미지는 링크도 없이 글자만 남는다", () => {
    const { container } = renderMarkdown("![점](data:image/png;base64,AAAA)");
    expect(container.querySelector("img")).toBeNull();
    expect(screen.queryByRole("link")).toBeNull();
    expect(container).toHaveTextContent("이미지: 점");
  });

  it("링크 안의 이미지는 링크를 또 만들지 않는다", () => {
    renderMarkdown("[![로고](https://img.example.com/logo.png)](https://example.com)");
    const links = screen.getAllByRole("link");
    expect(links).toHaveLength(1);
    expect(links[0]).toHaveTextContent("이미지: 로고");
    expect(links[0]).toHaveAttribute("href", "https://example.com/");
  });
});

describe("스트리밍 중과 대체 화면", () => {
  it(`스트리밍 중에는 ${STREAM_PARSE_INTERVAL_MS}ms 에 한 번만 새 글을 그리고 스트리밍이 끝나면 바로 그린다`, () => {
    fakeClock();
    const view = (text: string, streaming: boolean) => <Markdown text={text} streaming={streaming} />;
    const { rerender, container } = renderWithProviders(view("하나", true));
    act(() => vi.advanceTimersByTime(STREAM_PARSE_INTERVAL_MS));
    rerender(view("하나 둘", true));
    expect(container).toHaveTextContent("하나 둘");
    rerender(view("하나 둘 셋", true));
    expect(container).not.toHaveTextContent("셋");
    act(() => vi.advanceTimersByTime(STREAM_PARSE_INTERVAL_MS));
    expect(container).toHaveTextContent("하나 둘 셋");
    rerender(view("하나 둘 셋 넷", false));
    expect(container).toHaveTextContent("하나 둘 셋 넷");
  });

  it("청크가 오기 전에는 줄바꿈과 코드 펜스만 다루는 대체 화면이다", () => {
    const { container } = render(<MarkdownFallback text={"**굵게**\n둘째 줄\n```sh\nls -al"} codeTestId="f-code" />);
    expect(container.querySelector("strong")).toBeNull();
    expect(container.querySelector("p")).toHaveTextContent("**굵게** 둘째 줄");
    expect(screen.getByTestId("f-code")).toHaveTextContent("ls -al");
    expect(screen.getByTestId("f-code")).toHaveAttribute("data-lang", "sh");
    expect(screen.getByTestId("f-code-copy")).toBeInTheDocument();
  });
});

describe("소스 검사", () => {
  const SRC = resolve(process.cwd(), "src");
  /** 원문 HTML 을 요소로 만드는 두 길. 들이면 모델이 쓴 HTML 이 화면에서 돈다. */
  const FORBIDDEN = [["dangerously", "SetInnerHTML"].join(""), ["rehype", "raw"].join("-")];

  function sourceFiles(dir: string): string[] {
    return readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
      const path = join(dir, entry.name);
      if (entry.isDirectory()) return entry.name === "test" ? [] : sourceFiles(path);
      return /\.(ts|tsx)$/.test(entry.name) ? [path] : [];
    });
  }

  it("UI 소스와 의존성에 원문 HTML 을 그리는 길이 없다", () => {
    const hits = sourceFiles(SRC).filter((path) => FORBIDDEN.some((word) => readFileSync(path, "utf8").includes(word)));
    expect(hits).toEqual([]);
    const pkg = JSON.parse(readFileSync(resolve(process.cwd(), "package.json"), "utf8")) as Record<string, Record<string, string> | undefined>;
    const deps = { ...pkg.dependencies, ...pkg.devDependencies };
    expect(Object.keys(deps).filter((name) => FORBIDDEN.includes(name))).toEqual([]);
  });
});
