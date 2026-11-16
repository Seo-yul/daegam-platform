/**
 * 지금 서빙 중인 UI 빌드. UI 컨테이너(nginx)가 내는 `/build.json` 의 `ui_build` 다(D52).
 *
 * vite 빌드 플러그인이 남기는 정적 파일이라 API 가 아니고 `/api` 밖에 있어 API 클라이언트를 쓰지 않는다. 배포 직후의
 * 새 값을 보려고 캐시를 건너뛴다. 파일이 없거나(vite 개발 서버는 index.html 을 준다) JSON 이 아니면 null 이다.
 * 배지가 표시를 안 하면 될 뿐 오류로 알릴 일이 아니다.
 */
export async function getServedUiBuild(): Promise<string | null> {
  // 브라우저에서는 상대 경로와 같다. 테스트(jsdom + Node fetch)는 절대 URL 이 필요하다(api/client.ts 의 API_BASE 와 같은 까닭)
  const origin = typeof window !== "undefined" ? window.location.origin : "";
  try {
    const res = await globalThis.fetch(`${origin}/build.json`, {
      cache: "no-store",
      headers: { Accept: "application/json" },
    });
    if (!res.ok) return null;
    const data: unknown = await res.json();
    if (data !== null && typeof data === "object" && "ui_build" in data) {
      const build = (data as { ui_build: unknown }).ui_build;
      return typeof build === "string" ? build : null;
    }
    return null;
  } catch {
    return null;
  }
}
