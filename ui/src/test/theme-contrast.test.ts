import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, it } from "vitest";

/*
 * 테마 대비 (P81 7절). index.css 의 두 테마 블록을 읽어 글자 토큰이 놓이는 면 위에서 4.5:1(AA),
 * 입력칸 경계와 포커스 링이 면 위에서 3:1(SC 1.4.11)을 넘는지 잰다. :root 가 다크이고 라이트 블록은
 * 그 위를 덮으므로 라이트는 두 블록을 합친 값이다.
 */
const CSS = readFileSync(resolve(__dirname, "../index.css"), "utf8");

function block(selector: string): Record<string, string> {
  const start = CSS.indexOf(`${selector} {`);
  if (start < 0) throw new Error(`no block ${selector}`);
  const end = CSS.indexOf("\n}", start);
  const out: Record<string, string> = {};
  for (const m of CSS.slice(start, end).matchAll(/^\s*--ui-([a-z0-9-]+):\s*([^;]+);/gm)) out[m[1]] = m[2].trim();
  return out;
}

const DARK = block(":root");
const LIGHT = { ...DARK, ...block(':root[data-theme="light"]') };

type RGBA = [number, number, number, number];

function parse(value: string): RGBA {
  const hex = /^#([0-9a-f]{6})$/i.exec(value);
  if (hex) {
    const n = parseInt(hex[1], 16);
    return [(n >> 16) & 255, (n >> 8) & 255, n & 255, 1];
  }
  const rgba = /^rgba\(\s*([\d.]+),\s*([\d.]+),\s*([\d.]+),\s*([\d.]+)\s*\)$/.exec(value);
  if (rgba) return [Number(rgba[1]), Number(rgba[2]), Number(rgba[3]), Number(rgba[4])];
  throw new Error(`not a color: ${value}`);
}

/** 반투명 색을 아래 색 위에 얹는다. */
function over(top: RGBA, below: RGBA): RGBA {
  const a = top[3];
  return [top[0] * a + below[0] * (1 - a), top[1] * a + below[1] * (1 - a), top[2] * a + below[2] * (1 - a), 1];
}

function luminance([r, g, b]: RGBA): number {
  const lin = (c: number) => {
    const s = c / 255;
    return s <= 0.04045 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
  };
  return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b);
}

function ratio(fg: RGBA, bg: RGBA): number {
  const [hi, lo] = [luminance(fg), luminance(bg)].sort((x, y) => y - x);
  return (hi + 0.05) / (lo + 0.05);
}

/** `fg` 를 `bg` 위에서 잰다. `tint` 를 주면 면에 그 옅은 색을 먼저 깐다(배지·옅은 면). */
function contrast(theme: Record<string, string>, fg: string, bg: string, tint?: string): number {
  let base = parse(theme[bg]);
  if (tint) base = over(parse(theme[tint]), base);
  const top = parse(theme[fg]);
  return ratio(top[3] < 1 ? over(top, base) : top, base);
}

const SURFACES = ["app-base", "surface", "surface-strong", "sunken"];
const TEXT = ["heading", "fg", "fg-soft", "muted", "faint", "label", "dim", "link"];

describe.each([
  ["light", LIGHT],
  ["dark", DARK],
])("%s 테마 대비", (_name, theme) => {
  it.each(TEXT.flatMap((fg) => SURFACES.map((bg) => [fg, bg])))("글자 %s 는 면 %s 위에서 4.5:1 을 넘는다", (fg, bg) => {
    expect(contrast(theme, fg, bg)).toBeGreaterThanOrEqual(4.5);
  });

  // 배지·알림·탭 개수 방울은 옅은 면을 얹은 글자다. 본문 면에도 바탕(한지) 위에도 바로 놓인다
  const TINTED = [
    ["ok-fg", "ok-soft"],
    ["warn-fg", "warn-soft"],
    ["err-fg", "err-soft"],
    ["info-fg", "info-soft"],
    ["link", "accent-soft"],
  ];
  it.each(TINTED.flatMap(([fg, tint]) => ["surface", "app-base"].map((bg) => [fg, tint, bg])))(
    "옅은 면의 글자 %s 는 %s 를 얹은 %s 위에서 4.5:1 을 넘는다",
    (fg, tint, bg) => {
      expect(contrast(theme, fg, bg, tint)).toBeGreaterThanOrEqual(4.5);
    },
  );

  // 상태 글자는 옅은 면 없이도 쓰인다. 배포 해제(danger 버튼), 타일의 경고 수, 복사 단추의 성공·실패 표시다
  it.each(["ok-fg", "warn-fg", "err-fg", "info-fg"].flatMap((fg) => SURFACES.map((bg) => [fg, bg])))(
    "상태 글자 %s 는 면 %s 위에서 4.5:1 을 넘는다",
    (fg, bg) => {
      expect(contrast(theme, fg, bg)).toBeGreaterThanOrEqual(4.5);
    },
  );

  it.each([
    ["on-primary", "primary"],
    ["on-primary", "err"],
  ])("채운 버튼의 글자 %s 는 %s 위에서 4.5:1 을 넘는다", (fg, bg) => {
    expect(contrast(theme, fg, bg)).toBeGreaterThanOrEqual(4.5);
  });

  // 인장 글자는 로고라 글자 대비 규칙 밖이지만, 그림으로서 3:1(SC 1.4.11)은 넘게 둔다
  it("마크의 인장 글자 seal-ink 는 인주 seal 위에서 3:1 을 넘는다", () => {
    expect(contrast(theme, "seal-ink", "seal")).toBeGreaterThanOrEqual(3);
  });

  it.each([
    ["field-line", "surface"],
    ["field-line", "surface-strong"],
    ["focus-ring", "surface"],
    ["focus-ring", "surface-strong"],
    ["focus-ring", "app-base"],
  ])("컨트롤 경계 %s 는 면 %s 위에서 3:1 을 넘는다", (fg, bg) => {
    // focus-ring 은 포커스 링이자 입력칸이 포커스를 받을 때의 1px 경계다(field.ts `fieldClass`의 border-focus-ring).
    // 둘레의 옅은 링(focus-ring/35)은 덧붙인 것이라 재지 않는다
    expect(contrast(theme, fg, bg)).toBeGreaterThanOrEqual(3);
  });
});

describe("두 테마의 토큰", () => {
  it("라이트 블록이 덮는 토큰은 모두 다크에도 있다", () => {
    const light = block(':root[data-theme="light"]');
    for (const name of Object.keys(light)) expect(DARK, name).toHaveProperty(name);
  });
});
