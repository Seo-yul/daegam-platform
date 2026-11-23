/**
 * Hub 항목 종류 → 배지 변형. 허브 목록과 Gateway 폼이 같이 쓴다.
 *
 * 페이지 모듈에 두면 폼이 허브 목록 페이지를 끌어와야 한다 (D29 와 같은 취지로 계층을 지킨다).
 */
import type { BadgeProps } from "@/components/ui/badge";
import type { HubItem } from "@/api/types";
import type { Messages } from "@/i18n";
import { resourceLabel, type DeletableResource } from "@/lib/deletion";
import { DEPLOY_POLL_MS } from "@/lib/deployment";
import { ownValue } from "@/lib/utils";

/**
 * 원본이 우리 Mongo 안에 있어 `tool_names` 를 계산할 수 있는 종류인가 (D49).
 *
 * 이 한 줄이 화면에서 세 가지를 가른다. 목록에서 지울 수 있는가(파생은 원본 상세에서 해제한다),
 * 다른 테넌트에 열 수 있는가(파생은 422 cross_tenant_unsupported), 도구 새로고침이 있는가(파생은
 * 422). 같은 갈래를 이름 셋으로 적고 있다가 종류가 하나 늘면 한 곳만 고치게 된다.
 */
export function isDerivedHubItem(kind: HubItem["kind"]): boolean {
  return DERIVED_SOURCE[kind] !== undefined;
}

/**
 * 파생 종류 → 도구 이름이 오는 원본 리소스. 파생 종류의 정의도 이 표 하나다 (docs/guide/hub-gateway.md 9절).
 * 키는 서버의 `store/hub.DERIVED_KINDS` 를 그대로 옮긴 것이고 platform/tests/test_hub_ui_parity.py 가 맞춰 본다.
 */
export const DERIVED_SOURCE: Partial<Record<HubItem["kind"], DeletableResource>> = { internal: "mcp-server", published: "gateway" };

/** 파생 항목의 원본 리소스 이름. internal 은 MCP Server, published 는 Gateway 다. 파생이 아니면 null 이다. */
export function derivedHubSource(t: Messages, kind: HubItem["kind"]): string | null {
  const source = DERIVED_SOURCE[kind];
  return source ? resourceLabel(t, source) : null;
}

/** 외부에서 등록한 항목만 목록에서 지울 수 있다. internal 은 MCP Server 상세에서, published 는 Gateway 상세에서 해제한다. */
export function isExternalHubItem(kind: HubItem["kind"]): boolean {
  return !isDerivedHubItem(kind) && !isPlatformHubItem(kind);
}

/** 플랫폼 웹 Tool 항목인가(P39b 13절). 지우기·새로고침이 없고 붙여 넣을 MCP 주소도 없다. */
export function isPlatformHubItem(kind: HubItem["kind"]): boolean {
  return kind === "platform";
}

/** 배지에 보일 종류 이름. 플랫폼 항목만 화면 언어의 이름이고 나머지는 종류 그대로다. */
export function hubKindLabel(t: Messages, kind: HubItem["kind"]): string {
  return isPlatformHubItem(kind) ? t.hub.detail.platform : kind;
}

/** ToolHive code mode 의 가상 Tool 이름. 게시된 Gateway 가 code mode 로 돌면 그 목록에 있다. */
export const CODE_MODE_TOOL = "execute_tool_script";

/** proxy 모드로 게시한 항목인가. 새로 게시할 수는 없고 전에 게시한 항목에만 있다. */
export function isProxyPublished(item: HubItem): boolean {
  return item.kind === "published" && item.k8s.kind === "MCPRemoteProxy";
}

/**
 * Gateway 폼이 백엔드마다 보이는 Tool 선택지. 토큰 인증에서 게시된 Gateway 의 execute_tool_script 는 고를 수 없다. 스크립트 안의
 * 호출이 이 Gateway 의 Tool 규칙을 지나지 않기 때문이다 (P57 6.2절, 422 published_script_tool).
 */
export function selectableTools(item: HubItem, tokenAuth: boolean): string[] {
  return tokenAuth && item.kind === "published" ? item.tool_names.filter((name) => name !== CODE_MODE_TOOL) : item.tool_names;
}

/**
 * 상세 화면이 항목을 다시 물어볼 주기. 클러스터 리소스가 있는데 아직 Ready 가 아니면 묻는다.
 *
 * 외부 MCP 를 등록하면 상세가 Pending 으로 열리고 프록시는 몇십 초 뒤에 뜬다. 다시 묻지 않으면
 * 새로고침하기 전까지 Pending 에 멈춘 것처럼 보인다. entry 는 클러스터 리소스가 없어 물을 것이 없다.
 * 목록은 항목마다 클러스터를 한 번씩 밟으므로 여기에 걸지 않는다.
 */
export function hubPollInterval(item: HubItem | undefined): number | false {
  return item && item.k8s.kind && !item.status?.ready ? DEPLOY_POLL_MS : false;
}

export function hubKindVariant(kind: HubItem["kind"]): BadgeProps["variant"] {
  switch (kind) {
    case "internal":
      return "info";
    case "published":
    case "platform":
      return "ok";
    case "image":
      return "warn";
    default:
      return "default";
  }
}

/**
 * 플랫폼이 붙이는 항목 phase 의 말 (P55 5절, `hub.status`). 상태 배지가 label 을, 상세가 text 를 쓴다. 클러스터 리소스의 phase 면
 * undefined 라 그대로 보인다.
 */
export function hubStatusNote(t: Messages, phase: string | null | undefined): { label: string; text: string } | undefined {
  return phase ? ownValue(t.hub.status, phase) : undefined;
}
