import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { MONACO_DOMPURIFY, MONACO_SANITIZER, monacoDompurify } from "../../vite.config";

const UI = resolve(import.meta.dirname, "../..");

// 플러그인의 resolveId 를 Vite 없이 부른다. this.resolve 는 받은 이름을 그대로 돌려준다
async function resolveWith(source: string, importer: string | undefined) {
  const plugin = monacoDompurify();
  const hook = plugin.resolveId as (this: unknown, s: string, i: string | undefined) => Promise<unknown>;
  const ctx = { resolve: async (id: string) => ({ id: `resolved:${id}` }) };
  return hook.call(ctx, source, importer);
}

describe("Monaco 의 DOMPurify 복사본을 npm dompurify 로 바꿔 끼운다", () => {
  it("domSanitize.js 가 부르는 복사본만 npm 패키지로 돌린다", async () => {
    const importer = `${UI}/node_modules/${MONACO_SANITIZER}`;
    expect(await resolveWith(MONACO_DOMPURIFY, importer)).toEqual({ id: "resolved:dompurify" });
    expect(await resolveWith(MONACO_DOMPURIFY, `${UI}/src/other.ts`)).toBeNull();
    expect(await resolveWith("./other.js", importer)).toBeNull();
  });

  it("Monaco 가 아직 그 경로로 복사본을 부른다", () => {
    // Monaco 가 구조를 바꾸면 플러그인이 조용히 아무것도 안 하게 되므로 여기서 먼저 깨진다
    const source = readFileSync(resolve(UI, "node_modules", MONACO_SANITIZER), "utf8");
    expect(source).toContain(`from '${MONACO_DOMPURIFY}'`);
  });
});
