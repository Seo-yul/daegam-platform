/**
 * 이탈 확인의 언어 (P49 F3c). 훅은 사전을 붙들지 않으므로 그린 뒤에 언어를 바꿔도 묻는 순간의 언어로 묻는다.
 */
import { act, fireEvent, screen } from "@testing-library/react";
import { vi } from "vitest";

import { setLanguage } from "@/lib/language";
import { useUnsavedGuard } from "@/lib/unsavedGuard";

import { renderWithProviders } from "./render";

describe("unsaved guard in English", () => {
  beforeEach(() => {
    setLanguage("en");
  });

  it("asks before leaving in the language of the moment it asks", () => {
    setLanguage("ko");
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
    function Guarded() {
      const confirmLeave = useUnsavedGuard(true);
      return (
        <button type="button" onClick={() => confirmLeave()}>
          Cancel
        </button>
      );
    }
    renderWithProviders(<Guarded />);
    // 훅은 사전을 붙들지 않는다. 그린 뒤에 언어를 바꿔도 묻는 순간의 언어로 묻는다
    act(() => setLanguage("en"));
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(confirm).toHaveBeenCalledWith("You have unsaved changes. Leave this page and discard them?");
    confirm.mockRestore();
  });
});
