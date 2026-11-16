import { beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "@/api/client";
import { REVOKE_AFTER_MS, downloadFile, filenameFromDisposition, saveBlob } from "@/lib/download";

import { STUB_OBJECT_URL, stubDownloads } from "./downloads";
import { DOWNLOAD_HEADER_CASES as CASES, apiError, downloadHeaders, http, HttpResponse, server } from "./msw";
import { fakeClock } from "./timers";

describe("filenameFromDisposition", () => {
  it.each(CASES.map((c) => [c.filename, c] as const))("표본의 헤더에서 원래 이름 %s 를 읽는다", (_name, c) => {
    expect(filenameFromDisposition(c.headers["content-disposition"])).toBe(c.filename);
  });

  it.each([
    // 퍼센트 인코딩이 깨졌거나 UTF-8 이름이 없으면 ASCII 대체 이름이다
    [`attachment; filename="a_b.txt"; filename*=UTF-8''%E0%A4%A`, "a_b.txt"],
    ["attachment; filename=plain.txt", "plain.txt"],
    // 헤더나 이름이 없으면 null 이라 부르는 쪽 이름을 쓴다
    [null, null],
    ["attachment", null],
    ['attachment; filename=""', null],
  ])("filenameFromDisposition(%j) -> %j", (header, filename) => {
    expect(filenameFromDisposition(header)).toBe(filename);
  });
});

describe("목의 내려받기 헤더", () => {
  it.each(CASES.map((c) => [c.filename, c] as const))("표본의 경로에서 %s 의 헤더 다섯을 그대로 만든다", (_name, c) => {
    expect(downloadHeaders(c.path, c.media_type)).toEqual(c.headers);
  });
});

describe("내려받기", () => {
  let saved: string[] = [];
  beforeEach(() => {
    saved = stubDownloads();
  });

  it("saveBlob 은 링크를 눌러 내려받고 잠시 뒤 object URL 을 해제한다", () => {
    fakeClock();
    saveBlob(new Blob(["x"]), "보고서.md");
    expect(saved).toEqual(["보고서.md"]);
    const link = vi.mocked(HTMLAnchorElement.prototype.click).mock.contexts[0] as HTMLAnchorElement;
    expect(link.href).toBe(STUB_OBJECT_URL);
    expect(document.querySelector("a[download]")).toBeNull();
    expect(URL.revokeObjectURL).not.toHaveBeenCalled();
    vi.advanceTimersByTime(REVOKE_AFTER_MS);
    expect(URL.revokeObjectURL).toHaveBeenCalledWith(STUB_OBJECT_URL);
  });

  it("downloadFile 은 API 요청과 같은 헤더를 실어 받고 응답 헤더의 이름으로 저장한다", async () => {
    const c = CASES[0];
    let tenant: string | null = null;
    server.use(
      http.get("/agents/a1/sessions/s1/files/reports/x.md", ({ request }) => {
        tenant = request.headers.get("X-Daegam-Tenant");
        return new HttpResponse("본문", { headers: c.headers });
      }),
    );
    await downloadFile("/agents/a1/sessions/s1/files/reports/x.md", "x.md");
    expect(tenant).not.toBeNull();
    expect(saved).toEqual([c.filename]);
    const blob = vi.mocked(URL.createObjectURL).mock.calls[0][0] as Blob;
    expect(await blob.text()).toBe("본문");
  });

  it("downloadFile 은 헤더에 이름이 없으면 부르는 쪽 이름이고 오류는 ApiError 다", async () => {
    server.use(
      http.get("/files/plain", () => new HttpResponse("x")),
      http.get("/files/closed", () => apiError(410, "session_closed", "session is closed")),
    );
    await downloadFile("/files/plain", "기본.txt");
    expect(saved.at(-1)).toBe("기본.txt");
    const failure = await downloadFile("/files/closed", "x").catch((e: unknown) => e);
    expect(failure).toBeInstanceOf(ApiError);
    expect((failure as ApiError).status).toBe(410);
    expect((failure as ApiError).code).toBe("session_closed");
    expect(saved).toHaveLength(1);
  });
});
