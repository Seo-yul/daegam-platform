/**
 * 배포 뒤 열린 탭이 지워진 지연 청크를 부르면 한 번 새로 불러 새 index.html 을 받는다(D52).
 *
 * UI 는 플랫폼과 따로 굴러가는 컨테이너라 배포가 잦고, 롤링 중에는 index.html 과 에셋이 다른 파드에서 올 수도 있다.
 * Vite 는 지연 import 가 실패하면 `vite:preloadError` 를 낸다. 10초 안에 다시 오면(새로 불러도 청크가 없다)
 * 되풀이하지 않고 원래 오류를 화면의 오류 경계가 받게 둔다.
 *
 * `preventDefault()` 는 부르지 않는다. Vite 의 도우미는 그렇게 막은 오류를 삼키고 모듈 자리에 undefined 를 돌려줘,
 * 새로고침을 건너뛰거나 사용자가 입력 보호 경고(beforeunload)에서 취소하면 `React.lazy` 가 원인과 무관한 오류를 낸다.
 * 세션 저장소가 막힌 환경이면 되풀이를 막을 길이 없으므로 새로 부르지 않는다.
 */
const KEY = "daegam:preload-reload-at";
const WINDOW_MS = 10_000;

type Target = Pick<Window, "addEventListener" | "sessionStorage"> & { location: Pick<Location, "reload"> };

export function reloadOnceOnPreloadError(win: Target, now: () => number = Date.now): void {
  win.addEventListener("vite:preloadError", () => {
    try {
      const last = Number(win.sessionStorage.getItem(KEY) ?? 0);
      if (now() - last < WINDOW_MS) return;
      win.sessionStorage.setItem(KEY, String(now()));
    } catch {
      return;
    }
    win.location.reload();
  });
}
