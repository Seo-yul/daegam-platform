/**
 * 캔버스 아래의 테스트 채팅 서랍. 배포돼 Ready 이면 지금 보고 있는 Agent 와 바로 대화한다.
 *
 * 정의와 배포가 한 리소스가 되면서(D42) 고를 인스턴스가 없어졌다. 이 캔버스가 곧 그 Agent 의 정의이고
 * 대화 상대도 그 Agent 하나다. 그래서 고르개가 사라지고, 대신 지금 도는 것이 편집 중인 리비전과 같은지를
 * 배지로 말한다. 다른 리비전이 돌고 있는데 그것을 모르고 대화하면 방금 고친 것이 왜 안 먹는지 알 수 없다.
 * 쓰지 않아 잠든 Agent 도 Ready 처럼 대화한다. 보낸 턴이 깨우고 파드가 뜨기를 기다린다 (P44 8절).
 *
 * `agent:deploy` 가 있는 사람은 저장한 리비전이 배포된 것이 아니거나 Agent 가 draft 이면 "r{n} 미리 보기" 로 저장한 최신 리비전을
 * 자기만의 미리 보기로 띄운다(P44b 11절). 미리 보기가 열려 있으면 서랍은 배포된 Agent 대신 그것과 대화하고, 파드가 뜨는 동안의
 * 진행, 장기 기억이 없고 끝나면 대화가 사라진다는 것, 실제 Tool 을 부른다는 것을 말한다. 그 뒤에 저장한 리비전으로 옮기면 파드가 다시
 * 떠 대화가 사라지므로 옮기기 전에 확인을 받는다. 끝내기 단추가 미리 보기를 끝내고, 쓰지 않아서나 두 시간이 지나서, 다른 사람이
 * 끝냈거나 배포 권한을 잃었거나 플랫폼이 미리 보기를 꺼서 끝났으면 그 까닭을 말한다. 뜨지 못한 미리 보기는 진행 대신 시작하지
 * 못했다고 말한다. 채팅이 끝난 미리 보기의 404 를 받으면 곧바로 다시 읽어 그 까닭을 보인다. 미리 보기의 런타임 사실은
 * 응답에 없으므로 패널은 런타임 기능 없이 연다(취소는 떨어져 도는 턴의 머리를 따른다).
 */
import { useEffect, useState } from "react";

import type { Agent } from "@/api/types";
import { isBehind } from "@/components/BehindBadge";
import { ChatPanel } from "@/components/ChatPanel";
import { ErrorMessage } from "@/components/ErrorMessage";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Alert } from "@/components/ui/card";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { useT } from "@/i18n";
import { previewable, previewEndedText, previewFailed, previewGone, previewReady } from "@/lib/agentPreview";
import { acceptsTurns } from "@/lib/agentSleep";
import { hasScope, useAuthState } from "@/lib/auth";
import { describeErrorLine } from "@/lib/errorText";
import { chatFeatures, useChatFeatures } from "@/lib/runtimeFeatures";
import { useTenant } from "@/lib/tenant";
import { cn } from "@/lib/utils";

import { useMyPreview } from "./usePreviews";

/** 아직 대화할 수 없을 때, 무엇을 하면 쓸 수 있는가. 저장하지 않은 변경, 배포 전, Ready 전이다. */
export type TestChatWait = "dirty" | "draft" | "notReady";

/** 미리 보기 패널의 런타임 기능. 미리 보기의 응답은 런타임 사실을 싣지 않는다. */
const PREVIEW_FEATURES = chatFeatures(null);

export interface TestChatDrawerProps {
  isNew: boolean;
  /** 지금 보고 있는 Agent. 저장 전에는 없다. */
  agent: Agent | undefined;
  /** 저장하지 않은 변경이 있으면 배포 안내 대신 먼저 저장하라고 말한다. */
  dirty: boolean;
}

export function TestChatDrawer({ isNew, agent, dirty }: TestChatDrawerProps) {
  const t = useT();
  const text = t.agents.testChat;
  const tenant = useTenant();
  const { me } = useAuthState();
  // 미리 보기를 띄울 수 있는 사람인가. 없으면 서랍은 지금까지의 말과 대화 그대로다
  const canPreview = hasScope(me, tenant, "agent:deploy");
  const [open, setOpen] = useState(false);
  const [confirmingMove, setConfirmingMove] = useState(false);
  const ready = agent?.state === "deployed" && acceptsTurns(agent.status?.phase);
  const behind = agent && isBehind(agent);
  const features = useChatFeatures(agent?.status);
  const previewing = Boolean(agent && canPreview && !isNew && previewable(agent));
  const { preview, ended, loaded, error, refetch, start, end } = useMyPreview(previewing && agent ? agent.id : null);
  // 끝나는 중인 미리 보기는 없는 것처럼 다룬다. 다시 읽기가 사라지는 것을 본다
  const mine = preview && preview.state !== "deleting" ? preview : null;
  const mineKey = mine ? `${mine.id}:${mine.revision}` : null;
  // 한 번 대화를 연 미리 보기는 같은 파드인 동안 패널을 둔다. 상태 읽기 한 번이 phase 를 놓쳐도 보이던 대화가 사라지지 않게 한다
  const [shownKey, setShownKey] = useState<string | null>(null);
  const mineReady = mine !== null && previewReady(mine);
  useEffect(() => {
    if (mineReady && mineKey) setShownKey(mineKey);
  }, [mineKey, mineReady]);
  const talking = mineKey !== null && (mineReady || shownKey === mineKey);
  const failed = mine !== null && !talking && previewFailed(mine);
  // 저장한 최신 리비전을 띄우거나 그리로 옮기는 단추. 내 미리 보기를 읽은 뒤에만 선다. 그 전에 누르면 열린 미리 보기를 묻지 않고
  // 옮길 수 있다. 저장하지 않은 변경이 있으면 먼저 저장해야 한다
  const offerStart = previewing && loaded && agent !== undefined && !dirty && preview?.state !== "deleting" && mine?.revision !== agent.revision;
  const endedText = previewing && !mine ? previewEndedText(t, ended) : null;
  const chatting = talking || (!mine && ready);
  const busy = start.isPending || end.isPending;

  /** 미리 보기를 띄우거나 옮긴다. 서랍을 열어 진행과 거절을 보인다. */
  const launch = () => {
    setOpen(true);
    start.mutate();
  };
  // 열린 미리 보기를 옮기면 지금 대화가 사라지므로 먼저 묻는다
  const begin = () => (mine ? setConfirmingMove(true) : launch());

  return (
    <div className={cn("surface flex shrink-0 flex-col rounded-card transition-[height]", open && talking ? "h-96" : open && chatting ? "h-80" : "min-h-11")} data-testid="test-chat-drawer" data-open={open ? "true" : "false"}>
      <div className="flex min-h-11 shrink-0 flex-wrap items-center gap-x-3 gap-y-1 px-4 py-2">
        <button type="button" className="text-sm font-semibold text-heading" data-testid="test-chat-toggle" aria-expanded={open} onClick={() => setOpen((v) => !v)}>
          {text.title} {open ? "▾" : "▸"}
        </button>
        {isNew ? (
          <span className="text-xs text-muted">{text.unsaved(canPreview)}</span>
        ) : mine ? (
          <span className="min-w-0 flex-1 text-xs text-muted" data-testid="preview-status">
            {talking ? text.previewTalking(mine.revision) : failed ? text.previewFailed : text.previewStarting}
          </span>
        ) : ready && agent ? (
          <>
            <span className="text-xs text-muted">{text.talkingTo(agent.deployed_revision)}</span>
            {behind ? (
              <Badge variant="warn" data-testid="test-chat-behind">
                {text.behind(agent.revision)}
              </Badge>
            ) : null}
          </>
        ) : (
          <span className="text-xs text-muted" data-testid="test-chat-empty">
            {text.waiting(dirty ? "dirty" : agent?.state === "draft" ? "draft" : "notReady", canPreview)}
          </span>
        )}
        {offerStart || mine ? (
          <span className="ml-auto flex shrink-0 items-center gap-2">
            {offerStart && agent ? (
              <Button size="sm" variant="secondary" data-testid="preview-start" disabled={busy} onClick={begin}>
                {text.previewStart(agent.revision)}
              </Button>
            ) : null}
            {mine ? (
              <Button size="sm" variant="ghost" data-testid="preview-end" disabled={busy} onClick={() => end.mutate()}>
                {text.previewEnd}
              </Button>
            ) : null}
          </span>
        ) : null}
      </div>
      {/* 읽기와 시작과 끝의 거절은 서랍을 닫아 두어도 보인다 */}
      {error || start.error || end.error ? (
        <div className="mx-4 mb-2">
          <ErrorMessage error={start.error ?? end.error ?? error} testId="preview-error" />
        </div>
      ) : null}
      {open ? (
        <>
          {endedText ? (
            <Alert variant="info" className="mx-4 mb-2" data-testid="preview-ended">
              {endedText}
            </Alert>
          ) : null}
          {mine ? (
            <Alert variant="warn" className="mx-4 mb-2 text-xs" data-testid="preview-tools">
              {text.previewTools}
            </Alert>
          ) : null}
          {mine && failed && mine.last_error ? (
            <Alert variant="error" className="mx-4 mb-2" data-testid="preview-failed">
              {describeErrorLine(t, mine.last_error).text}
            </Alert>
          ) : null}
        </>
      ) : null}
      {open && talking && agent ? (
        <div className="min-h-0 flex-1 border-t border-line-strong">
          {/* 옮기면 파드가 다시 떠 대화가 사라지므로 리비전마다 새 패널이다. 인사말은 첫 턴의 greeting 이벤트가 낸다 */}
          <ChatPanel
            key={mineKey}
            instanceId={agent.id}
            preview
            ephemeral
            compact
            features={PREVIEW_FEATURES}
            onError={(e) => {
              // 다음 다시 읽기를 기다리지 않고 끝난 까닭을 보인다
              if (previewGone(e)) refetch();
            }}
          />
        </div>
      ) : open && !mine && ready && agent ? (
        <div className="min-h-0 flex-1 border-t border-line-strong">
          {/* 인사말은 편집 중인 정의가 아니라 배포된 리비전의 것이다. 대화 상대가 그 리비전이기 때문이다 (D65) */}
          <ChatPanel instanceId={agent.id} greeting={agent.status?.greeting} ephemeral compact features={features} />
        </div>
      ) : null}
      {agent ? (
        <ConfirmDialog
          open={confirmingMove}
          title={text.previewStart(agent.revision)}
          description={text.previewMove}
          confirmLabel={text.previewStart(agent.revision)}
          onConfirm={() => {
            setConfirmingMove(false);
            launch();
          }}
          onCancel={() => setConfirmingMove(false)}
        />
      ) : null}
    </div>
  );
}
