/**
 * 접근성의 회귀 시험.
 *
 * 눈으로 보면 멀쩡한 화면이 낭독기에는 다르게 읽히는 자리들이라, 고친 뒤에는 회귀로 고정해 둔다.
 * 대비는 색 토큰의 값이라 여기서 보지 않는다(브라우저 실측이고 index.css 주석이 값을 적어 둔다).
 */
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { TestApp } from "@/App";
import { NameFields } from "@/components/NameFields";
import { Input, Field, Select } from "@/components/ui/input";

import { consumeAssistantAutoFocus, setAssistantOpen } from "@/lib/assistant";

import { openSelect } from "./select";

describe("a11y", () => {
  it("Field 의 도움말이 aria-describedby 로 입력에 이어진다", () => {
    render(
      <Field label="Host" htmlFor="h" hint="소문자 DNS-1123 호스트명입니다.">
        <Input id="h" data-testid="h" />
      </Field>,
    );
    const input = screen.getByTestId("h");
    const describedBy = input.getAttribute("aria-describedby")!;
    expect(describedBy).toBe("h-hint");
    expect(document.getElementById(describedBy)).toHaveTextContent("소문자 DNS-1123 호스트명입니다.");
    expect(input).not.toHaveAttribute("aria-invalid");
  });

  it("Field 의 오류는 도움말을 대신하고 입력을 aria-invalid 로 만든다", () => {
    render(
      <Field label="Host" htmlFor="h" hint="도움말" error="이미 쓰는 호스트입니다.">
        <Input id="h" data-testid="h" />
      </Field>,
    );
    const input = screen.getByTestId("h");
    expect(input).toHaveAttribute("aria-invalid", "true");
    // 오류를 먼저 읽고 도움말이 뒤따른다
    expect(input.getAttribute("aria-describedby")).toBe("h-error h-hint");
    expect(screen.getByRole("alert")).toHaveTextContent("이미 쓰는 호스트입니다.");
  });

  it("Field 는 자식이 이미 가진 aria-describedby 를 지우지 않는다", () => {
    render(
      <Field label="Host" htmlFor="h" hint="도움말">
        <Input id="h" data-testid="h" aria-describedby="other" />
      </Field>,
    );
    expect(screen.getByTestId("h").getAttribute("aria-describedby")).toBe("other h-hint");
  });

  it("Field 의 도움말과 오류가 Select 트리거까지 닿는다", () => {
    render(
      <Field label="Cluster" htmlFor="c" hint="배포 네임스페이스는 생성 뒤 바꿀 수 없습니다." error="고를 수 있는 클러스터가 없습니다.">
        <Select id="c" data-testid="c" value="a" onChange={() => undefined}>
          <option value="a">a</option>
        </Select>
      </Field>,
    );
    // Select 가 나머지 prop 을 트리거 버튼에 펼치지 않으면 여기서 통째로 사라진다
    const trigger = screen.getByTestId("c");
    expect(trigger).toHaveAttribute("role", "combobox");
    expect(trigger.getAttribute("aria-describedby")).toBe("c-error c-hint");
    expect(trigger).toHaveAttribute("aria-invalid", "true");
    expect(document.getElementById("c-hint")).toHaveTextContent("배포 네임스페이스는 생성 뒤 바꿀 수 없습니다.");
  });

  it("NameFields 의 규칙과 오류도 입력에 이어진다", () => {
    render(<NameFields testId="t" value={{ displayName: "", name: "" }} onChange={() => undefined} errors={{ name: "이미 쓰는 이름입니다." }} />);
    const name = screen.getByTestId("t-name");
    expect(name).toHaveAttribute("aria-invalid", "true");
    expect(document.getElementById(name.getAttribute("aria-describedby")!)).toHaveTextContent("이미 쓰는 이름입니다.");
    const display = screen.getByTestId("t-display-name");
    expect(document.getElementById(display.getAttribute("aria-describedby")!)).not.toBeEmptyDOMElement();
  });

  it("첫 Tab 은 본문 건너뛰기 링크에 닿고 그 링크가 main 을 가리킨다", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/hub" />);
    await screen.findByTestId("sidebar");
    await user.tab();
    const skip = screen.getByTestId("skip-to-main");
    expect(skip).toHaveFocus();
    expect(skip).toHaveAttribute("href", "#main");
    // 목적지가 실제로 있고 포커스를 받을 수 있어야 링크가 일한다
    const main = document.getElementById("main")!;
    expect(main.tagName).toBe("MAIN");
    expect(main).toHaveAttribute("tabindex", "-1");
  });

  it("페이지를 열 때 서랍이 포커스를 뺏지 않는다. 직접 열면 입력으로 간다", async () => {
    const user = userEvent.setup();
    // 저장된 상태로 패널이 열린 채 페이지가 선다
    setAssistantOpen(true);
    consumeAssistantAutoFocus();
    render(<TestApp initialPath="/hub" />);
    await screen.findByTestId("assistant-panel");
    await waitFor(() => expect(screen.getByTestId("assistant-input")).not.toHaveFocus());
    // 첫 Tab 은 그래서 건너뛰기 링크에 닿는다
    await user.tab();
    expect(screen.getByTestId("skip-to-main")).toHaveFocus();

    // 토글로 직접 열면 바로 쓸 수 있게 입력으로 간다
    await user.click(screen.getByTestId("assistant-toggle"));
    await user.click(screen.getByTestId("assistant-toggle"));
    await waitFor(() => expect(screen.getByTestId("assistant-input")).toHaveFocus());
  });

  it("목록의 결과 수가 live region 이라 검색 결과가 읽힌다", async () => {
    render(<TestApp initialPath="/backend-apis" />);
    const info = await screen.findByTestId("page-info");
    expect(info).toHaveAttribute("aria-live", "polite");
    // 숫자만 읽히면 무슨 수인지 모른다. 낱말이 함께 있다
    expect(info).toHaveTextContent(/개 중 .*번째까지 보입니다/);
  });

  it("대화상자 안에서 연 셀렉트 목록은 그 대화상자가 aria-owns 로 갖는다", async () => {
    const user = userEvent.setup();
    function Harness() {
      return (
        <div role="dialog" aria-modal="true" data-testid="dlg">
          <Select aria-label="클러스터" data-testid="c" value="a" onChange={() => undefined}>
            <option value="a">a</option>
            <option value="b">b</option>
          </Select>
        </div>
      );
    }
    render(<Harness />);
    const dialog = screen.getByTestId("dlg");
    expect(dialog).not.toHaveAttribute("aria-owns");
    const listbox = await openSelect(user, "c");
    // 목록은 body 로 portal 되므로 DOM 으로는 대화상자 밖이다. 관계는 aria-owns 가 잇는다
    expect(dialog.contains(listbox)).toBe(false);
    expect(dialog.getAttribute("aria-owns")).toBe(listbox.id);
    // 닫으면 되돌린다
    await user.keyboard("{Escape}");
    await waitFor(() => expect(dialog).not.toHaveAttribute("aria-owns"));
  });

  it("저장하지 않은 입력을 두고 링크로 나가면 확인을 지난다", async () => {
    const user = userEvent.setup();
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
    render(<TestApp initialPath="/gateways/new" />);
    await screen.findByTestId("gateway-form-page");
    // 아직 아무것도 안 고쳤으면 묻지 않고 나간다
    await user.click(screen.getByTestId("nav-mcp-servers"));
    await screen.findByTestId("mcp-servers-page");
    expect(confirm).not.toHaveBeenCalled();

    await user.click(screen.getByTestId("nav-gateways"));
    await user.click(await screen.findByTestId("gateway-new"));
    await screen.findByTestId("gateway-form-page");
    await user.type(screen.getByTestId("gp-name"), "smoke");
    await user.click(screen.getByTestId("nav-mcp-servers"));
    expect(confirm).toHaveBeenCalledTimes(1);
    // 아니라고 하면 그 자리에 남는다
    expect(screen.getByTestId("gateway-form-page")).toBeInTheDocument();

    confirm.mockReturnValue(true);
    await user.click(screen.getByTestId("nav-mcp-servers"));
    expect(confirm).toHaveBeenCalledTimes(2);
    await screen.findByTestId("mcp-servers-page");
  });

  it("상세의 상태 배지가 바뀌면 낭독기가 읽는다", async () => {
    render(<TestApp initialPath="/agents/ag-support" />);
    await screen.findByTestId("agent-detail-page");
    const phase = await screen.findByTestId("status-phase");
    const live = phase.closest('[aria-live="polite"]')!;
    expect(live).toHaveAttribute("role", "status");
    expect(live).toHaveAttribute("aria-atomic", "true");
    expect(within(live as HTMLElement).getByText(/^상태: /)).toBeInTheDocument();
  });
});
