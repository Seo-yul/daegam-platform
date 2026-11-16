/**
 * 배포 상태 폴링 주기와 집계 (D42). 리소스 넷의 목록·상세가 같이 쓴다.
 *
 * 화면에 걸리지 않는 순수 함수라 lib 에 둔다. 페이지 모듈에 두면 이것 하나 때문에 그 페이지와
 * 딸린 대화상자 사슬이 다른 페이지 번들로 끌려 들어온다.
 */
import type { ResourceState } from "@/api/types";
import type { Messages } from "@/i18n";

/** 목록·상세의 상태 폴링 주기(ms). MCP Server·Gateway·Agent 가 같은 값을 쓴다. */
export const DEPLOY_POLL_MS = 5000;

/**
 * "배포된 것만" 토글이 남기는 것.
 *
 * `draft` 만 빼면 올리다 실패한 것과 내리는 중인 것까지 "배포된 것" 으로 세게 된다. 클러스터에
 * 올라가 있거나 올라가는 중인 둘만 센다.
 */
export function isDeployedOrDeploying(state: ResourceState | undefined): boolean {
  return state === "deploying" || state === "deployed";
}

/** 이 문서가 폴링할 값어치가 있는가. 멈춰 있는 `draft` 를 5초마다 다시 부르는 것은 낭비다. */
export function shouldPoll(items: { state?: ResourceState }[]): number | false {
  return items.some((i) => i.state && i.state !== "draft") ? DEPLOY_POLL_MS : false;
}

export interface StateCounts {
  total: number;
  draft: number;
  ready: number;
  progressing: number;
  failed: number;
}

/** 한 문서가 어느 갈래인가. 배지의 variant 와 같은 낱말이라 색을 정하는 자리가 그대로 쓴다. */
export type StateTone = "ok" | "warn" | "error" | "muted";

/**
 * CR phase 의 갈래. 요약 줄·카드의 상태(`stateTone`)와 행의 점·배지(`phaseVariant`)가 이 표 하나를 본다.
 *
 * 둘이 따로 적으면 같은 phase 가 카드에서는 주황인데 점은 회색인 일이 생긴다(Degraded 가 그랬다). 표에 없는
 * phase 를 어떻게 칠할지만 쓰는 쪽이 정한다.
 */
export const PHASE_TONE: Record<string, StateTone> = {
  Ready: "ok",
  // 쓰지 않아 0 으로 줄인 Agent 는 멀쩡히 배포된 것이다. 다음 대화가 깨운다 (P44 10절)
  Sleeping: "ok",
  Waking: "warn",
  Pending: "warn",
  Rolling: "warn",
  Degraded: "warn",
  Terminating: "warn",
  Failed: "error",
  Missing: "error",
};

/**
 * 문서 상태를 먼저 보고 `deployed` 일 때만 CR 의 phase 로 갈라본다.
 *
 * `deployed` 는 플랫폼이 CR 을 올려놨다는 뜻일 뿐이라 파드가 아직 안 떴거나 죽어 있을 수 있다.
 * 그래서 ok 는 문서가 `deployed` 이면서 phase 도 Ready 인 것뿐이다.
 *
 * 집계(`countByState`)와 카드의 상태 자리가 이 함수 하나를 본다. 둘이 따로 갈라보면 같은 문서가
 * 요약 줄에서는 Failed 인데 카드는 정상으로 조용한 일이 생긴다.
 */
export function stateTone(item: { state?: ResourceState; status?: { phase?: string | null } | null }): StateTone {
  switch (item.state) {
    case "deploying":
    case "undeploying":
    case "deleting":
      return "warn";
    case "failed":
      return "error";
    case "deployed":
      return PHASE_TONE[item.status?.phase ?? ""] ?? "warn";
    default:
      return "muted";
  }
}

/** 목록 집계. 갈래는 `stateTone` 이 정하고 여기서는 세기만 한다. */
export function countByState(items: { state?: ResourceState; status?: { phase?: string | null } | null }[]): StateCounts {
  const out: StateCounts = { total: items.length, draft: 0, ready: 0, progressing: 0, failed: 0 };
  for (const i of items) {
    // 상태를 모르는 항목은 어느 갈래에도 넣지 않는다. muted 는 `draft` 만의 자리다
    if (!i.state) continue;
    switch (stateTone(i)) {
      case "ok":
        out.ready += 1;
        break;
      case "warn":
        out.progressing += 1;
        break;
      case "error":
        out.failed += 1;
        break;
      case "muted":
        out.draft += 1;
        break;
    }
  }
  return out;
}

/**
 * 제목 옆 한 줄 요약 (D44 넷째). "12개 · Failed 1" 처럼 개수와 한마디를 붙인다.
 *
 * 나쁜 쪽을 먼저 말한다. 열둘 중 하나가 죽어 있으면 그 하나가 제목 옆에 서야지, 나머지 열하나가
 * 멀쩡하다는 말이 먼저 나오면 화면을 닫고 만다. 자세한 수는 아래 점 요약 줄이 나눠 놓는다.
 */
export function countSummary(t: Messages, c: StateCounts): string {
  const text = t.ui.deployment.summary;
  const head = text.count(c.total);
  const fact = summaryFact(text, c);
  return fact ? `${head} · ${fact}` : head;
}

/** 개수 뒤의 한마디. 빈 목록에는 붙이지 않는다. */
function summaryFact(text: Messages["ui"]["deployment"]["summary"], c: StateCounts): string | null {
  if (c.total === 0) return null;
  if (c.failed) return text.failed(c.failed);
  if (c.progressing) return text.progressing(c.progressing);
  if (c.ready === c.total) return text.allReady;
  if (c.draft === c.total) return text.allDraft;
  return text.mixed(c.ready, c.draft);
}
