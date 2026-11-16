import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { DaegamMark } from "@/components/icons/brand";

describe("brand icons", () => {
  it("대감 마크는 인주 인장에 글자를 새기고, 40px 부터 안쪽 테를 두른다", () => {
    const { container } = render(
      <>
        <DaegamMark size={28} className="shrink-0" />
        <DaegamMark size={52} />
      </>,
    );
    const [small, large] = Array.from(container.querySelectorAll("svg"));
    expect(small.getAttribute("width")).toBe("28");
    // 인주와 글자는 마크만 쓰는 토큰이다. 다크에서는 인주가 조금 밝아진다
    expect(small.querySelector("rect")?.getAttribute("fill")).toBe("var(--ui-seal)");
    expect(small.querySelector("path")?.getAttribute("fill")).toBe("var(--ui-seal-ink)");
    expect(small.querySelector("[data-seal-frame]")).toBeNull();
    expect(large.querySelector("[data-seal-frame]")).not.toBeNull();
  });
});
