import { act, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";

import { Select, type SelectChangeEvent } from "@/components/ui/input";
import { openSelect, selectValue } from "./select";

/** 호출부와 같은 모양의 제어 컴포넌트. onChange 는 e.target.value 로 읽는다. */
function Harness({
  initial = "",
  onChange,
  disabled,
  children,
}: {
  initial?: string;
  onChange?: (e: SelectChangeEvent) => void;
  disabled?: boolean;
  children?: React.ReactNode;
}) {
  const [value, setValue] = useState(initial);
  return (
    <div>
      <label htmlFor="fruit">Fruit</label>
      <Select
        id="fruit"
        data-testid="fruit"
        value={value}
        disabled={disabled}
        onChange={(e) => {
          setValue(e.target.value);
          onChange?.(e);
        }}
      >
        {children ?? (
          <>
            <option value="">(select)</option>
            <option value="apple">Apple</option>
            <option value="banana">Banana</option>
            <option value="cherry" disabled>
              Cherry
            </option>
            <option value="date">Date</option>
          </>
        )}
      </Select>
      <button type="button" data-testid="other">
        other
      </button>
    </div>
  );
}

describe("Select (custom dropdown)", () => {
  it("renders the selected label as a combobox trigger and exposes the value", () => {
    render(<Harness initial="banana" />);
    const trigger = screen.getByTestId("fruit");
    expect(trigger).toHaveAttribute("role", "combobox");
    expect(trigger).toHaveAttribute("aria-haspopup", "listbox");
    expect(trigger).toHaveAttribute("aria-expanded", "false");
    expect(trigger).toHaveTextContent("Banana");
    expect(trigger).toHaveValue("banana");
    expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
    // <label htmlFor> 가 트리거에 붙는다
    expect(screen.getByLabelText("Fruit")).toBe(trigger);
  });

  it("opens below the trigger on click, marks the selected option, and closes on outside click", async () => {
    const user = userEvent.setup();
    render(<Harness initial="banana" />);
    const trigger = screen.getByTestId("fruit");
    await user.click(trigger);
    const listbox = screen.getByTestId("fruit-listbox");
    expect(trigger).toHaveAttribute("aria-expanded", "true");
    expect(trigger).toHaveAttribute("aria-controls", listbox.id);
    expect(listbox).toHaveAttribute("data-side", "bottom");
    // 패널은 body 직속(portal) 이다
    expect(listbox.parentElement).toBe(document.body);
    const options = within(listbox).getAllByRole("option");
    expect(options.map((o) => o.textContent)).toEqual(["(select)", "Apple", "Banana", "Cherry", "Date"]);
    expect(within(listbox).getByRole("option", { name: "Banana" })).toHaveAttribute("aria-selected", "true");
    expect(within(listbox).getByRole("option", { name: "Apple" })).toHaveAttribute("aria-selected", "false");
    expect(within(listbox).getByRole("option", { name: "Cherry" })).toHaveAttribute("aria-disabled", "true");

    await user.click(document.body);
    expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
    expect(trigger).toHaveAttribute("aria-expanded", "false");
  });

  it("selects an option by click, fires onChange with e.target.value, and returns focus to the trigger", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<Harness onChange={onChange} />);
    await selectValue(user, "fruit", "apple");
    expect(onChange).toHaveBeenCalledTimes(1);
    expect(onChange.mock.calls[0][0].target.value).toBe("apple");
    expect(screen.getByTestId("fruit")).toHaveValue("apple");
    expect(screen.getByTestId("fruit")).toHaveTextContent("Apple");
    expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
    expect(screen.getByTestId("fruit")).toHaveFocus();

    // 같은 값을 다시 고르면 change 가 나지 않는다 (네이티브와 같다)
    await selectValue(user, "fruit", "apple");
    expect(onChange).toHaveBeenCalledTimes(1);
  });

  it("ignores clicks on a disabled option", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<Harness onChange={onChange} />);
    const listbox = await openSelect(user, "fruit");
    await user.click(within(listbox).getByRole("option", { name: "Cherry" }));
    expect(onChange).not.toHaveBeenCalled();
    expect(screen.getByRole("listbox")).toBeInTheDocument();
  });

  it("navigates with the keyboard: arrows open and move, Home/End jump, Enter selects, Escape closes", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<Harness initial="apple" onChange={onChange} />);
    const trigger = screen.getByTestId("fruit");
    trigger.focus();

    await user.keyboard("{ArrowDown}");
    const listbox = screen.getByRole("listbox");
    const activeOption = () => listbox.querySelector("[data-active]")?.textContent;
    expect(activeOption()).toBe("Apple"); // 열리면 현재 값이 활성
    expect(trigger).toHaveAttribute("aria-activedescendant", listbox.querySelector("[data-active]")?.id);

    await user.keyboard("{ArrowDown}");
    expect(activeOption()).toBe("Banana");
    await user.keyboard("{ArrowDown}"); // Cherry(disabled) 는 건너뛴다
    expect(activeOption()).toBe("Date");
    await user.keyboard("{ArrowDown}"); // 끝에서 멈춘다
    expect(activeOption()).toBe("Date");
    await user.keyboard("{Home}");
    expect(activeOption()).toBe("(select)");
    await user.keyboard("{End}");
    expect(activeOption()).toBe("Date");
    await user.keyboard("{ArrowUp}");
    expect(activeOption()).toBe("Banana");

    await user.keyboard("{Enter}");
    expect(onChange).toHaveBeenCalledTimes(1);
    expect(onChange.mock.calls[0][0].target.value).toBe("banana");
    expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
    expect(trigger).toHaveFocus();

    // Escape 는 값을 바꾸지 않고 닫는다
    await user.keyboard("{ArrowUp}");
    expect(screen.getByRole("listbox")).toBeInTheDocument();
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
    expect(onChange).toHaveBeenCalledTimes(1);
    expect(trigger).toHaveFocus();
  });

  it("selects with Space and does not reopen on the key release", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<Harness onChange={onChange} />);
    const trigger = screen.getByTestId("fruit");
    trigger.focus();
    await user.keyboard("[Space]"); // 닫힌 상태의 Space 는 button click 으로 연다
    expect(screen.getByRole("listbox")).toBeInTheDocument();
    await user.keyboard("{ArrowDown}");
    await user.keyboard("[Space]");
    expect(onChange.mock.calls[0][0].target.value).toBe("apple");
    expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
  });

  it("jumps to the first option starting with a typed character while open", async () => {
    const user = userEvent.setup();
    render(<Harness />);
    const trigger = screen.getByTestId("fruit");
    trigger.focus();
    await user.keyboard("{ArrowDown}");
    const listbox = screen.getByRole("listbox");
    // 접두어를 묶는 리셋 시간(500ms)은 Date.now() 로 잰다. Date 만 가짜로 두므로 userEvent 는 진짜 타이머로 돈다
    vi.useFakeTimers({ toFake: ["Date"] });
    try {
      await user.keyboard("d");
      expect(listbox.querySelector("[data-active]")?.textContent).toBe("Date");
      // 연속 입력은 접두어("db") 로 묶이므로 리셋 시간을 넘긴 뒤 새 글자를 친다
      vi.advanceTimersByTime(600);
      await user.keyboard("b");
      expect(listbox.querySelector("[data-active]")?.textContent).toBe("Banana");
    } finally {
      vi.useRealTimers();
    }
    await user.keyboard("{Enter}");
    expect(trigger).toHaveValue("banana");
  });

  it("closes on Tab and lets focus move on", async () => {
    const user = userEvent.setup();
    render(<Harness />);
    const trigger = screen.getByTestId("fruit");
    await user.click(trigger);
    expect(screen.getByRole("listbox")).toBeInTheDocument();
    await user.tab();
    expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
    expect(screen.getByTestId("other")).toHaveFocus();
  });

  it("stays open on scroll and resize, and follows the trigger to its new place", async () => {
    const user = userEvent.setup();
    render(<Harness />);
    const trigger = screen.getByTestId("fruit");
    await user.click(trigger);
    const listbox = screen.getByTestId("fruit-listbox");
    // jsdom 의 rect 는 전부 0 이라 트리거 아래(GAP 4px)가 곧 0+4 다
    expect(listbox.style.top).toBe("4px");

    // 스크롤로 트리거가 내려갔다고 하고 프레임을 한 번 돌린다
    const original = HTMLElement.prototype.getBoundingClientRect;
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (this: HTMLElement) {
      if (this === trigger) return { top: 64, bottom: 100, left: 20, right: 220, width: 200, height: 36, x: 20, y: 64, toJSON: () => ({}) } as DOMRect;
      return original.call(this);
    });
    await act(async () => {
      document.dispatchEvent(new Event("scroll"));
      await new Promise((r) => requestAnimationFrame(() => r(null)));
    });
    // 닫히지 않고 트리거 바로 아래(GAP 4px)로 다시 자리 잡는다
    expect(screen.getByRole("listbox")).toBeInTheDocument();
    expect(listbox.style.top).toBe("104px");
    expect(listbox.style.left).toBe("20px");

    await act(async () => {
      window.dispatchEvent(new Event("resize"));
      await new Promise((r) => requestAnimationFrame(() => r(null)));
    });
    expect(screen.getByRole("listbox")).toBeInTheDocument();
    expect(listbox.style.top).toBe("104px");
  });

  it("does nothing while disabled", async () => {
    const user = userEvent.setup();
    render(<Harness initial="apple" disabled />);
    const trigger = screen.getByTestId("fruit");
    expect(trigger).toBeDisabled();
    await user.click(trigger);
    expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
  });

  it("opens upward when there is no room below the trigger", async () => {
    const user = userEvent.setup();
    render(<Harness />);
    const trigger = screen.getByTestId("fruit");
    const original = HTMLElement.prototype.getBoundingClientRect;
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (this: HTMLElement) {
      if (this === trigger) return { top: 700, bottom: 736, left: 20, right: 220, width: 200, height: 36, x: 20, y: 700, toJSON: () => ({}) } as DOMRect;
      if (this.getAttribute("role") === "listbox") return { top: 0, bottom: 200, left: 0, right: 200, width: 200, height: 200, x: 0, y: 0, toJSON: () => ({}) } as DOMRect;
      return original.call(this);
    });
    await user.click(trigger);
    const listbox = screen.getByTestId("fruit-listbox");
    expect(listbox).toHaveAttribute("data-side", "top");
    expect(listbox.style.bottom).toBe(`${window.innerHeight - 700 + 4}px`);
    expect(listbox.style.minWidth).toBe("200px");
    expect(listbox.style.left).toBe("20px");
  });

  it("reads value, label and disabled from <option> children, including mixed text nodes", async () => {
    const user = userEvent.setup();
    render(
      <Harness>
        {[1, 2].map((n) => (
          <option key={n} value={String(n)}>
            r{n} ({n * 10} tools)
          </option>
        ))}
        <option>label-as-value</option>
      </Harness>,
    );
    const listbox = await openSelect(user, "fruit");
    const options = within(listbox).getAllByRole("option");
    expect(options.map((o) => o.textContent)).toEqual(["r1 (10 tools)", "r2 (20 tools)", "label-as-value"]);
    expect(options.map((o) => o.getAttribute("data-value"))).toEqual(["1", "2", "label-as-value"]);
    await user.click(options[2]);
    expect(screen.getByTestId("fruit")).toHaveValue("label-as-value");
  });

  it("blocks form submit while required and empty, then submits once a value is chosen", async () => {
    const user = userEvent.setup();
    const onSubmit = vi.fn();
    function Form() {
      const [value, setValue] = useState("");
      return (
        <form
          onSubmit={(e) => {
            e.preventDefault();
            onSubmit(value);
          }}
        >
          <Select data-testid="req" required value={value} onChange={(e) => setValue(e.target.value)}>
            <option value="">(select)</option>
            <option value="a">A</option>
          </Select>
          <button type="submit" data-testid="submit">
            go
          </button>
        </form>
      );
    }
    render(<Form />);
    await user.click(screen.getByTestId("submit"));
    expect(onSubmit).not.toHaveBeenCalled();
    expect(screen.getByTestId("req")).toHaveAttribute("aria-invalid", "true");
    expect(screen.getByTestId("req")).toHaveFocus();
    // 숨은 native input 은 클릭 대상이 아니다
    const hidden = document.querySelector<HTMLInputElement>('input[required][aria-hidden="true"]');
    expect(hidden).not.toBeNull();
    expect(hidden!.className).toContain("pointer-events-none");
    expect(hidden!.tabIndex).toBe(-1);

    await selectValue(user, "req", "a");
    expect(screen.getByTestId("req")).not.toHaveAttribute("aria-invalid");
    await user.click(screen.getByTestId("submit"));
    expect(onSubmit).toHaveBeenCalledWith("a");
  });
});
