import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";

import { ApiError } from "@/api/client";
import { NameFields, isNameFieldError, nameFieldErrors, type NameFieldsProps, type NameValue } from "@/components/NameFields";

/** 폼처럼 값을 들고 있는 래퍼. 제출 본문 검사는 페이지 테스트가 맡고, 여기서는 두 칸의 상호작용만 본다. */
function Harness(props: Omit<NameFieldsProps, "value" | "onChange"> & { initial?: NameValue }) {
  const [value, setValue] = useState<NameValue>(props.initial ?? { displayName: "", name: "" });
  return (
    <>
      <NameFields {...props} value={value} onChange={setValue} />
      <output data-testid="value">{JSON.stringify(value)}</output>
    </>
  );
}

describe("NameFields (D24)", () => {
  it("fills Name with the slug of Display name while the user has not touched Name", async () => {
    const user = userEvent.setup();
    render(<Harness testId="t" />);
    await user.type(screen.getByTestId("t-display-name"), "계산기 SOAP");
    expect(screen.getByTestId("t-name")).toHaveValue("soap");
    await user.clear(screen.getByTestId("t-display-name"));
    await user.type(screen.getByTestId("t-display-name"), "Orders v2");
    expect(screen.getByTestId("t-name")).toHaveValue("orders-v2");
  });

  it("stops following once Name is edited by hand, and follows again when Name is cleared", async () => {
    const user = userEvent.setup();
    render(<Harness testId="t" />);
    await user.type(screen.getByTestId("t-display-name"), "Calc");
    expect(screen.getByTestId("t-name")).toHaveValue("calc");
    await user.clear(screen.getByTestId("t-name"));
    await user.type(screen.getByTestId("t-name"), "mine");
    await user.type(screen.getByTestId("t-display-name"), " Two");
    expect(screen.getByTestId("t-name")).toHaveValue("mine");
    expect(screen.getByTestId("t-display-name")).toHaveValue("Calc Two");
    // Name 을 비우면 다시 따라간다
    await user.clear(screen.getByTestId("t-name"));
    await user.type(screen.getByTestId("t-display-name"), "!");
    expect(screen.getByTestId("t-name")).toHaveValue("calc-two");
  });

  it("validates Name on blur, not on every keystroke, with ~해 주세요 guidance", async () => {
    const user = userEvent.setup();
    render(<Harness testId="t" />);
    expect(screen.queryByTestId("t-name-error")).not.toBeInTheDocument();
    expect(screen.getByTestId("t-name-fields")).toHaveTextContent("소문자 영문, 숫자, 하이픈(-)만 쓸 수 있고");
    await user.type(screen.getByTestId("t-name"), "My_Calc");
    // 치는 동안은 아직 말하지 않는다
    expect(screen.queryByTestId("t-name-error")).not.toBeInTheDocument();
    await user.tab();
    expect(screen.getByTestId("t-name-error")).toHaveTextContent("하이픈은 양끝이나 연속으로 올 수 없습니다. 32자 이내입니다.");
    expect(screen.getByTestId("t-name")).toHaveAttribute("aria-invalid", "true");
    // 한 번 들른 칸은 고치는 동안 바로 따라온다
    await user.clear(screen.getByTestId("t-name"));
    await user.type(screen.getByTestId("t-name"), "my-calc");
    expect(screen.queryByTestId("t-name-error")).not.toBeInTheDocument();
    await user.clear(screen.getByTestId("t-name"));
    await user.type(screen.getByTestId("t-name"), "a".repeat(33));
    expect(screen.getByTestId("t-name-error")).toHaveTextContent("32자 이내로 입력해 주세요");
  });

  it("validates both fields on submit even when neither was visited", async () => {
    const user = userEvent.setup();
    render(
      <form onSubmit={(e) => e.preventDefault()}>
        <Harness testId="t" initial={{ displayName: "   ", name: "My_Calc" }} />
        <button type="submit" data-testid="submit">
          go
        </button>
      </form>,
    );
    expect(screen.queryByTestId("t-name-error")).not.toBeInTheDocument();
    await user.click(screen.getByTestId("submit"));
    expect(screen.getByTestId("t-name-error")).toHaveTextContent("하이픈은 양끝이나 연속으로 올 수 없습니다");
    expect(screen.getByTestId("t-display-name-error")).toHaveTextContent("공백만으로 채울 수 없습니다");
  });

  it("validates Display name length and whitespace-only input", async () => {
    const user = userEvent.setup();
    render(<Harness testId="t" />);
    await user.type(screen.getByTestId("t-display-name"), "   ");
    await user.tab();
    expect(screen.getByTestId("t-display-name-error")).toHaveTextContent("공백만으로 채울 수 없습니다");
    await user.clear(screen.getByTestId("t-display-name"));
    await user.type(screen.getByTestId("t-display-name"), "x".repeat(81));
    expect(screen.getByTestId("t-display-name-error")).toHaveTextContent("80자 이내");
  });

  it("previews the cluster resource name with the prefix", async () => {
    const user = userEvent.setup();
    render(<Harness testId="mi" preview={{ prefix: "mcp" }} />);
    expect(screen.getByTestId("mi-name-preview")).toHaveTextContent("클러스터 리소스 이름: mcp-…");
    await user.type(screen.getByTestId("mi-display-name"), "계산기 SOAP");
    expect(screen.getByTestId("mi-name-preview")).toHaveTextContent("클러스터 리소스 이름: mcp-soap");
    expect(screen.getByTestId("mi-name-preview").querySelector("code")).toHaveClass("font-mono");
  });

  it("readOnlyName keeps Name fixed while Display name stays editable", async () => {
    const user = userEvent.setup();
    render(<Harness testId="mp" readOnlyName initial={{ displayName: "calc", name: "calc" }} />);
    const name = screen.getByTestId("mp-name");
    expect(name).toHaveAttribute("readonly");
    expect(name).toHaveClass("font-mono");
    expect(screen.getByTestId("mp-name-fields")).toHaveTextContent("이름은 생성 뒤 바꿀 수 없습니다");
    await user.clear(screen.getByTestId("mp-display-name"));
    await user.type(screen.getByTestId("mp-display-name"), "계산기");
    expect(name).toHaveValue("calc");
    expect(screen.getByTestId("value")).toHaveTextContent('{"displayName":"계산기","name":"calc"}');
  });

  it("shows server 422 codes under the matching field", () => {
    const invalid = new ApiError(422, { detail: { code: "invalid_name", detail: "name must be 1-32 lowercase letters" } });
    expect(nameFieldErrors(invalid)).toEqual({ name: "name must be 1-32 lowercase letters" });
    expect(isNameFieldError(invalid)).toBe(true);
    const immutable = new ApiError(422, { detail: { code: "name_immutable", detail: "name cannot be changed" } });
    expect(nameFieldErrors(immutable)).toEqual({ name: "name cannot be changed" });
    const display = new ApiError(422, { detail: { code: "invalid_display_name", detail: "display_name must be 1-80 characters" } });
    expect(nameFieldErrors(display)).toEqual({ display_name: "display_name must be 1-80 characters" });
    const other = new ApiError(409, { detail: { code: "name_conflict", detail: "exists" } });
    expect(nameFieldErrors(other)).toEqual({});
    expect(isNameFieldError(other)).toBe(false);

    render(<Harness testId="t" initial={{ displayName: "x", name: "ok" }} errors={{ name: "server says no", display_name: "too long" }} />);
    expect(screen.getByTestId("t-name-error")).toHaveTextContent("server says no");
    expect(screen.getByTestId("t-display-name-error")).toHaveTextContent("too long");
  });
});

describe("NameFields placeholders", () => {
  it("shows the REST API example by default and a resource-specific pair when given", async () => {
    const { NameFields } = await import("@/components/NameFields");
    const { render, screen } = await import("@testing-library/react");
    const { unmount } = render(<NameFields testId="t" value={{ displayName: "", name: "" }} onChange={() => {}} />);
    expect(screen.getByTestId("t-display-name")).toHaveAttribute("placeholder", "주문 API");
    expect(screen.getByTestId("t-name")).toHaveAttribute("placeholder", "orders-api");
    unmount();
    render(<NameFields testId="u" value={{ displayName: "", name: "" }} onChange={() => {}} placeholders={{ display: "주문 DB", name: "orders-db" }} />);
    expect(screen.getByTestId("u-display-name")).toHaveAttribute("placeholder", "주문 DB");
    expect(screen.getByTestId("u-name")).toHaveAttribute("placeholder", "orders-db");
  });
});
