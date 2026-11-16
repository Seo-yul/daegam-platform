/**
 * 문서 상태 칩 (D42). 리소스 넷이 정의와 배포를 함께 가지므로 문서마다 상태가 하나 붙는다.
 *
 * CR 의 phase(Ready·Pending·Rolling·Failed·Missing)와는 다른 축이다. 문서 상태는 플랫폼이 무엇을
 * 하는 중인지 말하고, phase 는 클러스터가 실제로 어떤 꼴인지 말한다. `deployed` 인데 phase 가
 * Failed 일 수 있고 그 둘을 나란히 보여야 무슨 일인지 읽힌다. 그래서 칩도 둘을 따로 둔다.
 */
import type { ResourceState } from "@/api/types";
import { Badge, type BadgeProps } from "@/components/ui/badge";
import { useT } from "@/i18n";

/** 상태마다 칩 색. 글자는 사전(resources.state)에 있다. */
const VARIANTS: Record<ResourceState, BadgeProps["variant"]> = {
  draft: "muted",
  deploying: "info",
  deployed: "ok",
  failed: "error",
  undeploying: "warn",
  deleting: "warn",
};

/** 진행 중인 상태인가. 폴링을 계속할지, 버튼을 잠글지 판단하는 자리가 같은 답을 쓰게 한다. */
export function isBusyState(state: ResourceState): boolean {
  return state === "deploying" || state === "undeploying" || state === "deleting";
}

/** 클러스터에 실체가 있는 상태인가. 배포 해제·재배포·매니페스트 탭이 이때만 뜻이 있다. */
export function isLiveState(state: ResourceState): boolean {
  return state === "deployed" || state === "failed" || state === "undeploying";
}

/**
 * 머리의 주 동작이 재배포인 상태인가 (D44 둘째).
 *
 * `isLiveState` 와 다른 것은 `undeploying` 이다. 해제가 도는 중에는 머리에 주 동작을 두지 않는다.
 */
export function isRedeployableState(state: ResourceState): boolean {
  return state === "deployed" || state === "failed";
}

/** 서버가 모르는 상태를 주면 그 값을 기본 색으로 그대로 보인다. */
export function StateBadge({ state, testId = "state-badge" }: { state: ResourceState; testId?: string }) {
  const t = useT();
  return (
    <Badge variant={VARIANTS[state] ?? "default"} data-testid={testId} data-state={state} title={state}>
      {t.resources.state[state] ?? state}
    </Badge>
  );
}
