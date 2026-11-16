/**
 * Gateway 가 광고할 도구 계산과 Degraded 배지의 말. 도구 계산은 상세 화면과 Agent 캔버스의 인스펙터가, 배지는 목록과
 * 상세가 같이 쓴다.
 *
 * 페이지 모듈에 두면 캔버스가 Gateway 상세 페이지를 끌어와야 한다. 화면에 걸리지 않는 순수
 * 계산이라 lib 에 둔다.
 */
import type { Gateway, GatewayStatus } from "@/api/types";
import type { Messages } from "@/i18n";
import { ownValue } from "@/lib/utils";

/**
 * Degraded 배지의 말 (P49 6절). 사전이 아는 코드면 화면 언어로 고르고, 코드가 없거나(코드를 싣기 전의 서버) 사전이 모르는
 * 코드(UI 보다 새 서버)면 서버가 적은 한국어 badge 그대로다. 배지가 없으면 null 이다.
 */
export function degradedBadgeText(t: Messages, status: Pick<GatewayStatus, "badge" | "badge_code">): string | null {
  return (status.badge_code ? ownValue(t.gateways.degradedBadge, status.badge_code) : undefined) ?? status.badge ?? null;
}

export interface AdvertisedTool {
  /** vMCP 가 광고하는 이름. prefix 모드면 `<workload>_<tool>`, 아니면 tool_overrides 로 바뀐 이름이거나 원래 이름이다. */
  name: string;
  /** 백엔드의 원래 도구 이름. */
  original: string;
}

export interface AdvertisedBackend {
  workload: string;
  hub_item_id: string;
  hub_item_name: string;
  tools: AdvertisedTool[];
}

/**
 * 배포 시점 스냅샷(`deployed_backends` 의 tools ∩ tool_names, tool_overrides)에서 vMCP 가 광고할 도구를 계산한다.
 * 플랫폼이 게시 항목의 tool_names 를 만드는 규칙(services/gateways.advertised_tools)과 같다. 충돌 해소가 prefix 면
 * vMCP 가 모든 이름 앞에 워크로드를 붙이고 오버라이드는 쓰지 않는다. 실제 목록은 status.url 로 tools/list 를 부르면 나온다.
 *
 * 정의 쪽 `backends` 가 아니라 배포 쪽 `deployed_backends` 를 본다 (D42). 정의는 고른 허브 항목일 뿐이고
 * 워크로드 이름과 도구 이름은 배포하면서 해석되므로, 정의만 고쳐 둔 `draft` 에는 광고할 도구가 아직 없다.
 */
export function advertisedTools(gw: Pick<Gateway, "deployed_backends" | "deployed_conflict_resolution" | "tool_overrides">): AdvertisedBackend[] {
  const prefix = gw.deployed_conflict_resolution === "prefix";
  return gw.deployed_backends.map((b) => {
    let names = b.tool_names.filter((t) => !b.tools.length || b.tools.includes(t));
    // tool_names 를 모르면 필터를 그대로 광고 후보로 둔다
    if (!names.length && b.tools.length) names = b.tools;
    const overrides = gw.tool_overrides[b.workload] ?? {};
    return {
      workload: b.workload,
      hub_item_id: b.hub_item_id,
      hub_item_name: b.hub_item_name,
      tools: names.map((t) => ({ name: prefix ? `${b.workload}_${t}` : (overrides[t] ?? t), original: t })),
    };
  });
}

/**
 * Gateway 가 이 광고 이름의 Tool 호출을 기다리는 시간(초, P50 7절). 광고 이름은 `advertisedTools` 가 푼 것이라 prefix
 * 모드의 `<workload>_<tool>` 과 tool_overrides 로 바뀐 이름을 그대로 맞댄다. 그 이름을 광고하는 백엔드의 배포 스냅샷
 * 타임아웃이고, 둘 이상이면 긴 쪽이다. 어느 백엔드도 그 이름을 광고하지 않으면, Tool 목록을 모르는 백엔드가 있을 때만
 * 그 백엔드들 가운데 가장 긴 타임아웃이고(거기서 올 수 있다), 아니면 null 이다(지운 Tool 이름 따위).
 */
export function gatewayToolTimeout(
  gw: Pick<Gateway, "deployed_backends" | "deployed_conflict_resolution" | "tool_overrides">,
  tool: string,
): number | null {
  const seconds = new Map(gw.deployed_backends.map((b) => [b.workload, b.timeout_seconds]));
  const advertised = advertisedTools(gw);
  const serving = advertised.filter((b) => b.tools.some((t) => t.name === tool));
  const pool = serving.length ? serving : advertised.filter((b) => !b.tools.length);
  return pool.length ? Math.max(...pool.map((b) => seconds.get(b.workload) ?? 0)) : null;
}
