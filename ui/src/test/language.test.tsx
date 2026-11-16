/**
 * 화면 언어 저장소와 전환 단추 (P49 1절). 첫 언어는 브라우저 언어와 상관없이 한국어이고, 고른 언어는 브라우저에 남으며,
 * 바꾸면 `useT()` 를 부른 컴포넌트가 다시 그려진다.
 */
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { LanguageSwitch } from "@/components/LanguageSwitch";
import { useT } from "@/i18n";
import { getLanguage, setLanguage } from "@/lib/language";
import { STORAGE_KEYS, readItem, writeItem } from "@/lib/storage";

function Locale() {
  return <span data-testid="locale">{useT().locale}</span>;
}

describe("화면 언어", () => {
  it("고른 적이 없으면 브라우저 언어가 영어여도 한국어다", () => {
    expect(navigator.language).toMatch(/^en/);
    expect(getLanguage()).toBe("ko");
    expect(document.documentElement.lang).toBe("ko");
  });

  // setup.ts 가 테스트마다 resetLanguage 로 저장된 선택과 읽어 둔 값을 지운다
  it("저장된 선택을 따른다", () => {
    writeItem(STORAGE_KEYS.language, "en");
    expect(getLanguage()).toBe("en");
    expect(document.documentElement.lang).toBe("en");
  });

  it("모르는 저장값은 버리고 한국어로 연다", () => {
    writeItem(STORAGE_KEYS.language, "ja");
    expect(getLanguage()).toBe("ko");
  });

  it("바꾸면 저장하고 <html lang> 과 사전을 쓰는 컴포넌트가 따라온다", async () => {
    render(<Locale />);
    expect(screen.getByTestId("locale")).toHaveTextContent("ko-KR");
    setLanguage("en");
    expect(await screen.findByText("en-US")).toBeInTheDocument();
    expect(readItem(STORAGE_KEYS.language)).toBe("en");
    expect(document.documentElement.lang).toBe("en");
  });

  it("전환 단추는 언어마다 자기 이름을 적고 누른 언어로 바꾼다", async () => {
    render(<LanguageSwitch />);
    expect(screen.getByTestId("language-option-ko")).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByTestId("language-option-en")).toHaveTextContent("English");
    await userEvent.click(screen.getByTestId("language-option-en"));
    expect(getLanguage()).toBe("en");
    expect(screen.getByTestId("language-option-en")).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByTestId("language-option-ko")).toHaveTextContent("한국어");
  });
});
