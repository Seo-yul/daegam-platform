import { useQuery } from "@tanstack/react-query";

import { listUserDirectory } from "@/api/resources";
import { useAuthState } from "@/lib/auth";

/**
 * 주체(sub)를 사람이 읽는 이름으로 바꾼다. local 인증 모드(D40)에서는 sub 가 사용자 id(UUID)라 화면에 그대로
 * 두면 "by 31063fe2-…" 처럼 보인다. 그때만 사용자 디렉터리(`/users/directory`)를 한 번 받아 아이디로 바꾼다.
 * header·oidc 모드의 sub 는 이미 사람이 읽는 값이라 그대로 둔다. 디렉터리에 없는 sub 도 그대로 돌려준다.
 */
export function useUserLabel(): (sub: string | null | undefined, fallback?: string) => string {
  const auth = useAuthState();
  const localMode = auth.mode === "local";
  const me = auth.me;
  // 디렉터리는 어느 테넌트든 members:write 가 있어야 준다. 없는 사람(개발자)이 부르면 403 만 받으므로 부르지 않는다
  const canList = Boolean(me && (me.is_platform_admin || Object.values(me.scopes_by_tenant ?? {}).some((s) => s.includes("members:write"))));
  const directory = useQuery({ queryKey: ["users", "directory"], queryFn: listUserDirectory, enabled: localMode && canList });
  return (sub, fallback) => {
    if (!sub) return "";
    const u = directory.data?.find((x) => x.id === sub);
    if (u) return u.username;
    // 디렉터리를 못 보는 사람도 자기 행은 알아본다. /me 의 이름이다
    if (localMode && me && sub === me.sub && me.name) return me.name;
    // local 모드에서 풀지 못한 sub 는 UUID 다. fallback 을 주면 그것을 돌려준다(목록 카드는 빈 값으로 이름을 뺀다)
    return localMode && fallback !== undefined ? fallback : sub;
  };
}
