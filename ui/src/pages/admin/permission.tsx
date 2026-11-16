/**
 * /admin 화면의 권한.
 *
 * 사이드바만 이 링크를 감추던 때에는 `/admin/settings` 를 주소창에 넣거나 Tenant Settings 의 링크를
 * 누르면 누구나 들어왔다. `settings:read` 는 viewer 도 갖는 scope 라 GET 이 성공하고, 모든 칸이
 * 편집되는 채로 Save 도 살아 있다가 누르면 403 이었다. 플랫폼 설정은 `PlatformAdminOnly` 가 막고,
 * 나머지 둘은 읽기 전용 안내(사전의 `admin.readOnly`)로 그 사실을 먼저 말한다.
 */
import type { ReactElement } from "react";
import { Navigate } from "react-router";

import { isAuthReady, useAuthState } from "@/lib/auth";

export interface AdminAccess {
  /** GET /me 가 끝나 권한을 판단할 수 있는가. */
  ready: boolean;
  isPlatformAdmin: boolean;
  /** 판단이 끝났고 관리자가 아닌 상태. 이때만 화면을 잠근다. */
  readOnly: boolean;
}

export function useAdminAccess(): AdminAccess {
  const auth = useAuthState();
  const ready = isAuthReady(auth);
  const isPlatformAdmin = auth.me?.is_platform_admin ?? false;
  return { ready, isPlatformAdmin, readOnly: ready && !isPlatformAdmin };
}

/**
 * 플랫폼 설정 화면의 문. 관리자가 아니면 첫 화면으로 보낸다 (D43).
 *
 * 읽기 전용으로 열어 두던 자리인데, 바꿀 수 없는 화면을 열어 두면 사람은 "여기서 바꾸면 되겠다" 고
 * 읽고 Save 를 눌러서야 403 을 만난다. 권한을 아직 모를 때는 그대로 두고 기다린다.
 */
export function PlatformAdminOnly({ children }: { children: ReactElement }): ReactElement {
  const { ready, isPlatformAdmin } = useAdminAccess();
  if (ready && !isPlatformAdmin) return <Navigate to="/" replace />;
  return children;
}
