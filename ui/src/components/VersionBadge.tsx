import { useQuery } from "@tanstack/react-query";

import { useT } from "@/i18n";
import { getServedUiBuild } from "@/lib/servedBuild";
import { useVersionInfo } from "@/lib/versionInfo";

export const UI_BUILD: string = typeof __UI_BUILD__ === "string" ? __UI_BUILD__ : "dev";
/** 이 번들을 낸 릴리스 태그. 릴리스로 구운 번들에만 있다. */
export const UI_RELEASE: string | null = typeof __UI_RELEASE__ === "string" ? __UI_RELEASE__ : null;

/**
 * API 빌드와 지금 실행 중인 UI 빌드를 보여 준다. API 와 UI 는 따로 배포되므로 두 값을 각자 읽는다(D52).
 * API 는 `GET /version` 의 `api_build`(없으면 `api_version`), 서빙 중인 UI 는 UI 컨테이너의 `/build.json` 이다.
 * 서빙 중인 UI 가 실행 중인 번들과 다르면 이 탭이 옛 번들이라는 뜻이라 옆에 적는다. 릴리스로 구운 이미지면 그 릴리스
 * 태그를 맨 위에 적는다. API 쪽 태그를 먼저 보고, 없으면 이 번들의 태그다.
 */
export function VersionBadge() {
  const text = useT().shell.versionBadge;
  const api = useVersionInfo();
  const served = useQuery({ queryKey: ["served-ui-build"], queryFn: getServedUiBuild, staleTime: 60_000 });
  const servedBuild = served.data ?? null;
  const stale = servedBuild !== null && servedBuild !== UI_BUILD;
  const release = api.data?.release ?? UI_RELEASE;
  return (
    <div className="text-xs text-muted" data-testid="version-badge">
      {release ? (
        <div>
          {text.release} <span data-testid="release">{release}</span>
        </div>
      ) : null}
      <div>
        {text.api}{" "}
        <span data-testid="api-version">
          {api.isLoading ? "…" : (api.data?.api_build ?? api.data?.api_version ?? "?")}
        </span>
      </div>
      <div>
        {text.ui} <span data-testid="ui-build">{UI_BUILD}</span>
        {stale ? (
          <span className="ml-1 text-warn-fg" title={text.staleTitle(servedBuild)} data-testid="ui-build-stale">
            {text.stale(servedBuild)}
          </span>
        ) : null}
      </div>
    </div>
  );
}
