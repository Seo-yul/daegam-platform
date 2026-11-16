/**
 * API 층의 캐스트 (P73 2절). `ui/src/api` 의 함수는 생성 타입을 캐스트 없이 돌려주어 컴파일러가 라우트의 응답과 선언한
 * 타입을 맞춰 본다. `as unknown as` 는 그 검사를 끄므로 새로 쓰면 이 검사가 실패한다.
 */
import { readFileSync, readdirSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

const CAST = "as unknown as";

/** 파일의 캐스트마다 그것을 감싼 최상위 함수 이름. 함수 밖이면 "(top level)" 이다. */
function castSites(source: string): string[] {
  const sites: string[] = [];
  let at = source.indexOf(CAST);
  while (at >= 0) {
    const before = source.slice(0, at);
    const fn = [...before.matchAll(/^export (?:async )?function (\w+)/gm)].pop()?.[1];
    sites.push(fn ?? "(top level)");
    at = source.indexOf(CAST, at + CAST.length);
  }
  return sites;
}

describe("API 층의 캐스트", () => {
  it("as unknown as 를 쓰지 않는다", () => {
    const dir = join(import.meta.dirname, "..", "api");
    const found = readdirSync(dir)
      .filter((name) => name.endsWith(".ts") && !name.endsWith(".d.ts"))
      .flatMap((name) => castSites(readFileSync(join(dir, name), "utf8")).map((fn) => `${name} ${fn}`));
    expect(found).toEqual([]);
  });

  it("함수 이름을 찾는다", () => {
    expect(castSites(`export async function a() {}\nexport async function b() {\n  return x ${CAST} Y;\n}`)).toEqual(["b"]);
  });
});
