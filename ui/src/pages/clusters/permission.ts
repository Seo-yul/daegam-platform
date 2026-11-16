/**
 * 클러스터 화면의 쓰기 권한.
 *
 * `cluster:read` 는 viewer 부터 갖는 scope 라 사이드바의 Clusters 와 목록이 누구에게나 열린다.
 * 그런데 쓰기는 platform-admin 만 할 수 있어서(platform/daegam/auth/rbac.py), 권한을 안 보고 그리면
 * 이름과 kubeconfig 를 다 채워 Register 를 누른 뒤에야 403 을 받는다. 세 화면이 같은 판단을 쓰므로
 * 여기 한 곳에 둔다. 읽기 전용 안내와 버튼 title 은 사전의 clusters.readOnly 다.
 */
import { hasScope, isAuthReady, useAuthState } from "@/lib/auth";
import { useTenant } from "@/lib/tenant";

export interface ClusterWrite {
  /** GET /me 가 끝나 권한을 판단할 수 있는가. 그 전에는 canWrite 를 믿으면 안 된다. */
  ready: boolean;
  canWrite: boolean;
}

export function useClusterWrite(): ClusterWrite {
  const auth = useAuthState();
  const tenant = useTenant();
  return {
    ready: isAuthReady(auth),
    canWrite: hasScope(auth.me, tenant, "cluster:write"),
  };
}

/**
 * 읽기 전용으로 잠가야 하는가.
 *
 * 아직 못 읽었을 때는 잠그지 않는다. 기동 직후 한 프레임 동안 모든 사람에게 "권한이 없습니다" 가
 * 번쩍이는 것이 권한 있는 사람에게 더 나쁘기 때문이다.
 */
export function useClusterReadOnly(): boolean {
  const { ready, canWrite } = useClusterWrite();
  return ready && !canWrite;
}
