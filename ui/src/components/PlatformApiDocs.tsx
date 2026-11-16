/**
 * 플랫폼 자신의 API 문서 (P59, D71). token 모드(oidc·local)에서 /api/docs 는 토큰을 요구하는데 새 탭은 토큰이 없어
 * 401 이다. 그래서 화면 안의 팝업이 /api/openapi.json 을 그 사람의 토큰으로 받아 Backend API 문서와 같은 뷰어로 그린다.
 * header 모드에서도 같다. Swagger UI·ReDoc 은 header 모드 개발용으로 그대로 둔다.
 *
 * 문서는 플랫폼 빌드마다 같으므로 `GET /version` 의 api_build(로컬 프로세스는 api_version)를 키에 넣고, 그 빌드의
 * 문서는 탭이 살아 있는 동안 버리지 않는다(gcTime Infinity). 플랫폼이 새로 배포되면 키가 바뀌어 새 문서를 받는다.
 */
import { useQuery } from "@tanstack/react-query";
import { FileCode } from "lucide-react";
import { useState } from "react";

import { getPlatformOpenApi } from "@/api/resources";
import { ApiDocsBody } from "@/components/ApiDocsDialog";
import { Badge } from "@/components/ui/badge";
import { Modal } from "@/components/ui/modal";
import { MENU_ROW } from "@/components/ui/menu-row";
import { useT } from "@/i18n";
import { useVersionInfo } from "@/lib/versionInfo";

/** 플랫폼 문서의 쿼리 키. 빌드(api_build, 없으면 api_version)마다 따로 캐시한다. */
export function platformOpenApiKey(build: string | null) {
  return ["platform-openapi", build] as const;
}

export function PlatformApiDocsDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const t = useT();
  const version = useVersionInfo();
  const build = version.data?.api_build ?? version.data?.api_version ?? null;
  const doc = useQuery({
    queryKey: platformOpenApiKey(build),
    queryFn: getPlatformOpenApi,
    // 판을 받는 동안에는 받지 않는다. 옛 빌드의 키에 새 문서가 들어가지 않게 판 조회가 끝난 뒤(실패해도) 받는다
    enabled: open && !version.isFetching,
    staleTime: Infinity,
    gcTime: Infinity,
  });
  return (
    <Modal
      open={open}
      size="lg"
      testId="platform-api-docs-dialog"
      closeTestId="platform-api-docs-close"
      title={t.shared.apiDocsDialog.title(t.shell.names.product)}
      titleExtra={version.data ? <Badge variant="muted">{version.data.api_version}</Badge> : null}
      onClose={onClose}
    >
      <ApiDocsBody query={doc} toolNames={false} />
    </Modal>
  );
}

/** 계정 메뉴의 API 문서 줄 (P81 5절). 대화상자는 단추가 품는다. 계정 메뉴가 닫혀도 단추를 남기는 까닭이다. */
export function PlatformApiDocsButton() {
  const t = useT();
  const [open, setOpen] = useState(false);
  return (
    <>
      <button type="button" data-testid="platform-api-docs" className={MENU_ROW} onClick={() => setOpen(true)}>
        <FileCode size={16} aria-hidden />
        {t.shell.apiDocs}
      </button>
      {open ? <PlatformApiDocsDialog open onClose={() => setOpen(false)} /> : null}
    </>
  );
}
