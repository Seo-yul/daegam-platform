/**
 * Agent 미리 보기의 화면 규칙 (P44b 11절, D107). 테스트 채팅 서랍과 개요의 열린 미리 보기 카드가 같이 쓴다.
 *
 * `agent:deploy` 가 있는 사람은 저장한 리비전이 배포된 것이 아니거나 Agent 가 draft 일 때 저장한 최신 리비전을 자기만의 미리 보기로
 * 띄운다. 배포된 리비전이 저장한 것이면 서랍은 지금처럼 배포된 Agent 와 대화한다. 미리 보기는 파드 하나에 상태를 두고 장기 기억이
 * 없으며, 끝나면 대화가 사라진다. 끝난 까닭 가운데 서랍이 말하는 것은 쓰지 않아 끝남, 처음 시작 두 시간 뒤, 다른 사람이 끝냄,
 * 배포 권한을 잃음, 플랫폼이 미리 보기를 끔이다.
 */
import { PREVIEW_NOT_FOUND } from "@/api/agentPreview";
import { ApiError } from "@/api/client";
import type { Agent, AgentPreview, PreviewEnded } from "@/api/types";
import type { Messages } from "@/i18n";
import { acceptsTurns } from "@/lib/agentSleep";

/**
 * 미리 보기의 두 길이(분, 시간). 플랫폼 `services/agent_preview.py` 의 PREVIEW_IDLE 과 PREVIEW_MAX_AGE 이고 설정이 아니다. 끝난 뒤의
 * 답(404 의 `ended`)에는 길이가 없으므로 끝난 까닭의 문장이 이 값으로 말한다.
 */
export const PREVIEW_LENGTHS = { idleMinutes: 30, maxHours: 2 } as const;

/** 서랍이 내 미리 보기를 다시 읽는 간격(ms). 파드가 뜨거나 끝나는 동안은 짧게 읽는다. */
export const PREVIEW_PENDING_POLL_MS = 2_000;
/** 파드가 뜬 뒤나 실패한 뒤에 다시 읽는 간격(ms). 다른 사람이 끝냈거나 쓰지 않아 끝났으면 서랍이 그 까닭을 말한다. */
export const PREVIEW_OPEN_POLL_MS = 15_000;
/** 개요가 열린 미리 보기 목록을 다시 읽는 간격(ms). */
export const PREVIEWS_POLL_MS = 30_000;

/** 미리 보기를 띄울 수 있는 Agent 인가. 저장한 리비전이 배포된 것이 아니거나 배포한 적이 없다. */
export function previewable(agent: Pick<Agent, "state" | "revision" | "deployed_revision">): boolean {
  return agent.state === "draft" || agent.deployed_revision !== agent.revision;
}

/** 대화할 수 있는 미리 보기인가. 파드가 Ready 다. 끝나는 중(`deleting`)이면 아니다. */
export function previewReady(preview: AgentPreview): boolean {
  return preview.state !== "deleting" && acceptsTurns(preview.phase);
}

/** 뜨지 못한 미리 보기인가. 배포가 실패했거나(`failed`) Deployment 가 실패로 읽힌다(이미지를 받지 못함, 거듭 죽음). */
export function previewFailed(preview: AgentPreview): boolean {
  return preview.state === "failed" || preview.phase === "Failed";
}

/** 내 미리 보기를 다시 읽는 간격. 없으면 읽지 않고, 실패한 것은 끝나기를 기다리므로 길게 읽는다. */
export function previewPollMs(preview: AgentPreview | null | undefined): number | false {
  if (!preview) return false;
  return previewReady(preview) || previewFailed(preview) ? PREVIEW_OPEN_POLL_MS : PREVIEW_PENDING_POLL_MS;
}

/** 미리 보기가 이미 끝났다는 답인가(404 `preview_not_found`). 채팅이 이것을 받으면 서랍이 미리 보기를 다시 읽는다. */
export function previewGone(e: unknown): boolean {
  return e instanceof ApiError && e.status === 404 && e.code === PREVIEW_NOT_FOUND;
}

/** 끝난 까닭의 문장. 서랍이 말하는 까닭이 아니면(내가 끝냈다, 원본이 지워졌다) null 이다. */
export function previewEndedText(t: Messages, ended: PreviewEnded | null): string | null {
  const text = t.agents.testChat;
  switch (ended?.reason) {
    case "idle":
      return text.previewEndedIdle(PREVIEW_LENGTHS.idleMinutes);
    case "max_age":
      return text.previewEndedMax(PREVIEW_LENGTHS.maxHours);
    case "ended_by_other":
      return text.previewEndedByOther;
    case "access_lost":
      return text.previewEndedAccess;
    case "disabled":
      return text.previewEndedDisabled;
    default:
      return null;
  }
}
