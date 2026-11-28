/**
 * 채팅 전사 항목 하나를 그린다 (D26·D31). ChatPanel(Agent) 과 AssistantPanel(플랫폼 비서) 이 같이 쓰고, Agent 상세의 세션 탭은
 * 결정 콜백 없이 읽기 전용으로 쓴다.
 * data-testid 규약: 항목은 `chat-message-{i}`, 종류는 `data-role` 이다. 말풍선의 복사 단추는 `-copy`, 메시지 수정 단추는 `-edit`,
 * 답 안의 코드 블록은 `-code`, 그 복사 단추는 `-code-copy`, 답 아래 출처 줄은 `-sources` 를 붙인다(인용 칩은 Citations.tsx).
 * 턴 마감 줄의 엄지는 `-up`·`-down` 이고(FeedbackButtons) 트레이스 단추는 `-trace` 이며 줄에 `data-turn-id` 가 붙는다. 그 아래 접힌 구조화된 결과는
 * `-structured` 다. 승인(interrupt confirm) 은 ApprovalCard, 질문(interrupt question) 은 AskCard, 리소스는 ResourceCard 로 그린다.
 * 사용자 말과 함께 보낸 첨부의 칩은 `-attachments` 다(AttachmentChips). 성공한 `write_file`·`edit_file` 카드의 다운로드는 `-download`
 * 다(SessionFilesDrawer 의 `WrittenFileDownload`).
 */
import { Activity, Pencil } from "lucide-react";
import { createContext, useContext, useState, type ReactNode, type Ref } from "react";
import { Link } from "react-router";

import type { AgentDecision, AssistantPending } from "@/api/types";
import { ApprovalCard } from "@/components/ApprovalCard";
import { Paragraph } from "@/components/markdown/blocks";
import { Markdown } from "@/components/markdown/Markdown";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Alert } from "@/components/ui/card";
import { CodePre, CodeSnippet } from "@/components/ui/code-snippet";
import { CopyButton } from "@/components/ui/copy-button";
import { IconButton } from "@/components/ui/icon-button";
import { BUBBLE_ACTION_REVEAL } from "@/components/ui/reveal";
import { useT } from "@/i18n";
import type { AttachmentFacts } from "@/lib/attachments";
import type { ToolCallStatus } from "@/lib/chatStream";
import { describeCode } from "@/lib/errorText";
import { isAgentCallTool } from "@/lib/hub";
import { guardrailMessage } from "@/lib/serverLiterals";
import { writtenFile } from "@/lib/sessionFiles";
import { tokensOf } from "@/lib/usage";
import { cn } from "@/lib/utils";

import { AgentRunLink } from "./AgentRunLink";
import { AskCard } from "./AskCard";
import { AttachmentChips, sentChips } from "./AttachmentChips";
import { CitationSources, useCitationMarkdown } from "./Citations";
import { FeedbackButtons } from "./FeedbackButtons";
import { fallbackNotice, toolLimitNotice, type ChatItem } from "./items";
import { ResourceCard } from "./ResourceCard";
import { WrittenFileDownload } from "./SessionFilesDrawer";
import type { TurnFeedback } from "./useTurnFeedback";

/** 스트리밍 중 글 끝의 커서. 마크다운의 마지막 블록 뒤(::after)에 붙어 마지막 문단의 글 바로 뒤에 선다. */
const STREAM_CURSOR =
  "[&>:last-child]:after:ml-0.5 [&>:last-child]:after:inline-block [&>:last-child]:after:h-3.5 [&>:last-child]:after:w-1.5 [&>:last-child]:after:animate-pulse [&>:last-child]:after:rounded-sm [&>:last-child]:after:bg-kind-entry [&>:last-child]:after:align-middle [&>:last-child]:after:content-['']";

/**
 * 말풍선. 사용자 말은 쓴 그대로 평문이고 어시스턴트·서브에이전트 말과 인사말은 마크다운이다(D61). 포인터를 올리거나 초점이
 * 오면 원문(마크다운 그대로)을 복사하는 단추가 보이고, 포인터가 없는 화면에서는 늘 보인다. `onEdit` 을 주면 같은 자리에
 * 메시지 수정(연필) 단추가 선다. 마지막 사용자 말에만 준다(P36 5절). `editRef` 는 그 단추에 붙는다. 바깥에 인용 번호 표
 * (`CitationScope`)가 있으면 답의 `[n]` 이 번호 칩이 되고 아래에 출처 줄이 선다(P36 7절). 사용자 말과 함께 보낸 첨부는
 * 글 아래에 칩으로 남는다(P36 9절).
 */
export function Bubble({
  role,
  text,
  label,
  streaming,
  testId,
  onEdit,
  editRef,
  attachments,
}: {
  role: "user" | "assistant";
  text: string;
  label?: string;
  streaming?: boolean;
  testId?: string;
  onEdit?: () => void;
  editRef?: Ref<HTMLButtonElement>;
  attachments?: AttachmentFacts[];
}) {
  const t = useT();
  const words = t.chat.item;
  const user = role === "user";
  const citations = useCitationMarkdown();
  const actions = (
    <span className="flex shrink-0 items-center">
      {onEdit ? (
        <IconButton ref={editRef} label={words.editMessage} testId={testId ? `${testId}-edit` : undefined} onClick={onEdit} className={cn("size-6", BUBBLE_ACTION_REVEAL)}>
          <Pencil size={13} aria-hidden />
        </IconButton>
      ) : null}
      <CopyButton value={text} label={user ? words.copyMessage : words.copyAnswer} testId={testId ? `${testId}-copy` : undefined} className={BUBBLE_ACTION_REVEAL} />
    </span>
  );
  return (
    <div className={cn("group/bubble flex items-end gap-1", user ? "justify-end" : "justify-start")} data-testid={testId} data-role={role}>
      {user ? actions : null}
      {/* 사람의 말은 옅게 꺼진 면의 말풍선이고, 답은 상자 없이 본문으로 읽힌다 (P81 6절) */}
      <div
        className={cn(
          "flex min-w-0 flex-col gap-1.5 text-sm leading-relaxed",
          user ? "max-w-[80%] rounded-dialog rounded-br-[4px] border border-line bg-sunken px-[13px] py-[9px] text-heading" : "max-w-[calc(100%-3.5rem)] px-0.5 py-1 text-fg",
        )}
      >
        {label ? <span className="block text-[11px] font-semibold text-muted">{label}</span> : null}
        {user ? <Paragraph>{text}</Paragraph> : <Markdown text={text} streaming={streaming} testId={testId} className={streaming ? STREAM_CURSOR : undefined} citations={citations} />}
        {attachments?.length ? <AttachmentChips chips={sentChips(attachments)} testId={`${testId ?? "bubble"}-attachments`} /> : null}
        {user ? null : <CitationSources text={text} testId={testId ? `${testId}-sources` : undefined} />}
      </div>
      {user ? null : actions}
    </div>
  );
}

export interface ChatItemViewProps {
  index: number;
  item: ChatItem;
  /**
   * 승인 카드의 결정. 위치 순서의 `{type}` 목록이다 (id 대응은 부르는 쪽이 한다). 없으면 읽기 전용이라 승인 카드가
   * 비활성이다. 남의 세션도 보이는 세션 탭이 그렇다.
   */
  onDecide?: (decisions: AgentDecision[]) => void;
  /** 질문 카드의 답. 비서 패널은 이 답을 다음 턴의 메시지로 보낸다. 없으면 질문 카드에 입력이 없다. */
  onAnswer?: (text: string) => void;
  /** 턴이 도는 중이다. 카드를 잠근다. */
  busy?: boolean;
  /** 사용자 말풍선의 메시지 수정. 마지막 턴의 사용자 말에만 준다(P36 5절). */
  onEdit?: () => void;
  /** 메시지 수정 단추에 붙는다. 수정 칸이 닫히면 다시 선 단추가 포커스를 받는다. */
  editRef?: Ref<HTMLButtonElement>;
  /** 답변 피드백(P36 6절). 주면 `turn_id` 가 있는 턴 마감 줄에 엄지가 선다. 세션 소유자의 채팅만 준다. */
  feedback?: TurnFeedback;
  /**
   * 턴의 트레이스를 연다(P37 12절). 주면 턴 마감 줄과 오류 항목에 "트레이스" 단추가 선다. turn_id 가 있으면 그 턴의 실행 전부, 없으면
   * trace_id 의 실행 하나이고, 둘 다 없는 옛 런타임의 턴은 창이 "트레이스가 없다" 고 말한다. 비서 패널은 주지 않는다.
   */
  onOpenTrace?: (ids: { turnId?: string; traceId?: string }) => void;
}

/** 읽기 전용 승인 카드의 결정 자리. 카드가 비활성이라 불리지 않는다. */
function ignoreDecision(): void {}

export function ChatItemView({ index, item, onDecide, onAnswer, busy = false, onEdit, editRef, feedback, onOpenTrace }: ChatItemViewProps) {
  const t = useT();
  const text = t.chat.item;
  const testId = `chat-message-${index}`;
  switch (item.kind) {
    case "user":
      return <Bubble role="user" text={item.text} testId={testId} onEdit={onEdit} editRef={editRef} attachments={item.attachments} />;
    case "assistant":
      return <Bubble role="assistant" text={item.text} label={item.greeting ? t.shared.chatPanel.greeting : item.agent ? text.subAgent(item.agent) : undefined} streaming={item.streaming} testId={testId} />;
    case "tool_call":
      return <ToolCallView item={item} testId={testId} />;
    case "interrupt":
      if (item.pending.kind === "question") {
        return (
          <div data-testid={testId} data-role="interrupt" data-interrupt-kind="question">
            <AskCard question={item.pending.question ?? { text: firstArgText(item.pending) }} onAnswer={onAnswer} disabled={busy} resolved={item.resolved} />
          </div>
        );
      }
      return (
        <div data-testid={testId} data-role="interrupt" data-interrupt-kind="confirm">
          <ApprovalCard pending={item.pending} onSubmit={onDecide ?? ignoreDecision} disabled={busy || !onDecide} resolved={item.resolved} />
        </div>
      );
    case "resource":
      return (
        <div data-testid={testId} data-role="resource">
          <ResourceCard resource={item.resource} />
        </div>
      );
    case "done":
      return (
        <div className="flex flex-col gap-1.5" data-testid={testId} data-role="done" data-turn-id={item.turn_id}>
          <div className="flex items-center gap-2 text-[11px] text-muted">
            <Badge variant="muted">{text.turn(item.turn)}</Badge>
            <Badge variant="muted" data-testid={`${testId}-usage`}>
              {/* 캐시된 입력은 입력 안의 몫이고 단가가 낮다. 있으면 따로 적어 실제 비용을 가늠하게 한다 */}
              {text.usage(tokensOf(item.usage), item.usage.input_tokens, item.usage.cached_input_tokens ?? 0, item.usage.output_tokens)}
            </Badge>
            {item.tool_calls ? <Badge variant="muted">{text.toolCalls(item.tool_calls)}</Badge> : null}
            {feedback && item.turn_id ? <FeedbackButtons turnId={item.turn_id} feedback={feedback} testId={testId} /> : null}
            {onOpenTrace ? <TraceButton item={item} onOpen={onOpenTrace} testId={testId} /> : null}
          </div>
          {item.structured_response != null ? <StructuredResult value={item.structured_response} testId={`${testId}-structured`} /> : null}
        </div>
      );
    case "error":
      // 스트림이 실은 코드도 errorText 표를 지난다. 표에 없으면 서버 원문 그대로다 (D43 첫째)
      // 오류로 끝난 턴도 실행의 트레이스가 있다. 무엇이 실패했는지는 거기서 본다
      return (
        <Alert variant="error" data-testid={testId} data-role="error">
          <span className="flex flex-wrap items-center gap-2">
            {describeCode(t, item.code, item.message).text}
            {onOpenTrace && (item.turn_id || item.trace_id) ? <TraceButton item={item} onOpen={onOpenTrace} testId={testId} /> : null}
          </span>
        </Alert>
      );
    case "guardrail":
      return (
        <Alert variant={item.action === "block" ? "warn" : "info"} data-testid={testId} data-role="guardrail">
          {text.guardrail(guardrailMessage(t, item.message) || text.guardrailDefault, item.position, item.rule)}
        </Alert>
      );
    case "fallback":
      return (
        <Alert variant="info" data-testid={testId} data-role="fallback">
          {fallbackNotice(t, item)}
        </Alert>
      );
    case "tool_limit":
      return (
        <Alert variant="warn" data-testid={testId} data-role="tool-limit">
          {toolLimitNotice(t, item)}
        </Alert>
      );
    case "navigate":
      return (
        <p className="text-xs" data-testid={testId} data-role="navigate">
          <Link to={item.to} className="text-accent underline-offset-2 hover:underline" data-testid={`${testId}-link`}>
            {text.navigate(item.to)}
          </Link>
        </p>
      );
  }
}

/** 턴의 트레이스를 여는 단추. 턴 마감 줄과 오류 항목의 끝에 선다. */
function TraceButton({ item, onOpen, testId }: { item: { turn_id?: string; trace_id?: string }; onOpen: NonNullable<ChatItemViewProps["onOpenTrace"]>; testId: string }) {
  const t = useT();
  return (
    <Button size="sm" variant="ghost" className="ml-auto h-6 px-1.5 text-[11px]" data-testid={`${testId}-trace`} onClick={() => onOpen({ turnId: item.turn_id, traceId: item.trace_id })}>
      <Activity size={12} aria-hidden />
      {t.chat.item.trace}
    </Button>
  );
}

/**
 * 턴 마감 줄 아래의 구조화된 결과 (P36 11절). 응답 형식이 `json_schema` 인 에이전트의 결과라 본문 답과 겹치므로 접어 두고,
 * 펴면 들여 쓴 JSON 과 복사 단추를 보인다. 펼침은 `<details>` 가 맡아 키보드와 낭독기 몫을 브라우저가 한다.
 */
function StructuredResult({ value, testId }: { value: unknown; testId: string }) {
  const t = useT();
  const text = t.chat.item;
  return (
    <details className="text-[11px] text-muted" data-testid={testId}>
      <summary className="w-fit cursor-pointer rounded-item focus-visible:ring-2 focus-visible:ring-focus-ring focus-visible:outline-none">{text.structured}</summary>
      <div className="mt-1.5">
        <CodeSnippet tone="sunken" title={text.structuredTitle} lang="json" code={JSON.stringify(value, null, 2)} copyLabel={text.structuredCopy} testId={`${testId}-code`} copyTestId={`${testId}-copy`} />
      </div>
    </details>
  );
}

/** question 없이 온 ask_user 인터럽트의 질문 글. 도구 인자의 question 을 쓰고 없으면 빈 글이다. */
function firstArgText(pending: AssistantPending): string {
  const q = pending.action_requests[0]?.args?.question;
  return typeof q === "string" ? q : "";
}

/** 런타임이 바깥 내용 뒤의 승인(`tool_safety`, P43)을 알렸는가. 도구 카드의 외부 내용 배지가 안내 한 줄을 더한다. */
const ToolSafetyContext = createContext(false);

/** 외부 내용의 종류마다 배지의 사전 키다. 글자는 그 키, 안내는 `<키>Hint`, 시험 id 의 꼬리도 그 키다(웹은 옛 이름 그대로다). */
const OUTSIDE_BADGE_KEY = { web: "untrusted", outside: "outside" } as const;

/** 채팅 패널이 런타임의 기능으로 감싼다. 감싸지 않은 곳(비서, 세션 탭)은 안내를 더하지 않는다. */
export function ToolSafetyScope({ on, children }: { on: boolean; children: ReactNode }) {
  return <ToolSafetyContext.Provider value={on}>{children}</ToolSafetyContext.Provider>;
}

/**
 * 도구 호출 카드. 머리를 누르면 인자와 결과가 펼쳐진다. 성공한 `write_file`·`edit_file` 은 그 대화의 파일을 남기므로 머리 아래에
 * 다운로드가 선다(P36 10절). 받을 세션은 바깥의 `SessionFilesScope` 가 주고 없으면 단추도 없다.
 */
export function ToolCallView({ item, testId }: { item: Extract<ChatItem, { kind: "tool_call" }>; testId: string }) {
  const t = useT();
  const text = t.chat.tool;
  const [open, setOpen] = useState(false);
  const status: ToolCallStatus = item.result ? item.result.status : item.waiting ? "waiting" : "running";
  const written = writtenFile(item.call.name, item.call.args, item.result?.status);
  // 외부 내용 배지. 웹 결과는 외부 웹 내용, 그 밖은 외부 내용이고, 그 뒤의 쓰기가 승인을 받는 런타임이면 안내를 더한다
  const outsideKind = item.result?.untrusted;
  const guarded = useContext(ToolSafetyContext);
  const badgeKey = outsideKind ? OUTSIDE_BADGE_KEY[outsideKind] : null;
  const outsideHint = badgeKey ? [text[`${badgeKey}Hint`], ...(guarded ? [text.writeNeedsApproval] : [])].join(" ") : "";
  return (
    // 접힌 호출은 내용만큼의 작은 줄이고, 펼치면 인자와 결과를 담으려 전사 폭을 다 쓴다 (P81 6절)
    <div
      className={cn("rounded-tile border border-line bg-surface-strong px-2.5 py-1.5 text-[12.5px]", open ? "self-stretch" : "max-w-full self-start")}
      data-testid={testId}
      data-role="tool_call"
      data-status={status}
    >
      <button
        type="button"
        className="flex w-full items-center gap-2 rounded-item text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus-ring"
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
        data-testid={`${testId}-toggle`}
      >
        <Badge variant={status === "success" ? "ok" : status === "error" ? "error" : status === "waiting" ? "warn" : "info"}>
          {text.status[status]}
        </Badge>
        <span className="font-mono font-semibold text-heading">{item.call.name}</span>
        {item.call.agent ? <span className="text-muted">({item.call.agent})</span> : null}
        {badgeKey ? (
          <Badge variant="warn" title={outsideHint} data-testid={`${testId}-${badgeKey}`}>
            {text[badgeKey]}
          </Badge>
        ) : null}
        <span className="ml-auto pl-3 text-muted">{open ? text.collapse : text.expand}</span>
      </button>
      {written ? <WrittenFileDownload path={written} testId={`${testId}-download`} /> : null}
      {/* 다른 Agent 를 부른 호출은 그 Agent 의 실행이다 (P46 6.3절). 답이 왔거나 거절된 뒤에 그 기록을 연다 */}
      {item.result && isAgentCallTool(item.call.name) ? <AgentRunLink tool={item.call.name} testId={`${testId}-view-run`} /> : null}
      {open ? (
        <div className="mt-2 flex flex-col gap-1.5" data-testid={`${testId}-detail`}>
          <div className="text-[11px] font-semibold text-muted">{text.args}</div>
          <CodePre tone="sunken">{JSON.stringify(item.call.args, null, 2)}</CodePre>
          {item.result ? (
            <>
              <div className="text-[11px] font-semibold text-muted">{text.result}</div>
              <CodePre tone="sunken" className="max-h-48">
                {item.result.content}
              </CodePre>
            </>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
