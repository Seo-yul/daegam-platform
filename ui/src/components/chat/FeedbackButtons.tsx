/**
 * 끝난 답변의 엄지 둘과 의견 창 (P36 6절). `done` 줄의 오른쪽 끝에 서고, 부르는 쪽이 `TurnFeedback`(useTurnFeedback)을 넘길
 * 때만 그린다.
 *
 * 좋아요는 누르면 바로 저장하고, 평가가 있으면 옆에 "의견 남기기" 가 선다. 아쉬워요는 작은 창에서 이유와 의견을 받은 뒤
 * 저장하고, 창을 닫으면 아무것도 남기지 않는다. 눌린 엄지를 다시 누르면 평가를 거둔다. 의견은 이 에이전트의 승인 권한이
 * 있는 사람이 읽으므로 칸 아래에 개인정보를 적지 말라고 안내한다.
 *
 * 의견 창에는 이 질문과 답변을 평가 사례로 쓰는 동의 스위치가 있다(P45 8절). 처음에는 꺼져 있고, 안내가 복사하는 것, 읽는 사람,
 * 남는 기간, 거부해도 아무 일이 없다는 것을 말한다. 남는 기간은 서버가 세션 평가와 함께 보낸 설치의 값이라(`retentionDays`)
 * 세션의 첫 평가에도 맞다. 그 값이 없으면 플랫폼 기본값을 적는다(lib/feedback.ts). 엄지만 누른 저장은 남긴 동의를 그대로 싣고,
 * 창의 저장은 스위치의 값을 싣는다. 끄고 저장하면 서버가 복사한 사례를 지운다.
 *
 * data-testid: 엄지 줄 `{testId}-feedback`, 좋아요 `{testId}-up`, 아쉬워요 `{testId}-down`, 의견 단추 `{testId}-comment`, 창
 * `feedback-dialog`, 이유 칩 `feedback-tag-{이름}`, 의견 칸 `feedback-comment`, 동의 스위치 `feedback-evaluation-use`, 보내기
 * `feedback-submit`, 엄지 저장 실패 `{testId}-error`.
 */
import { useMutation } from "@tanstack/react-query";
import { ThumbsDown, ThumbsUp } from "lucide-react";
import { useId, useState } from "react";

import type { AgentFeedback, FeedbackRating, FeedbackTag } from "@/api/types";
import { ErrorMessage } from "@/components/ErrorMessage";
import { Button } from "@/components/ui/button";
import { IconButton } from "@/components/ui/icon-button";
import { Field, Textarea } from "@/components/ui/input";
import { Modal } from "@/components/ui/modal";
import { useT } from "@/i18n";
import { FEEDBACK_COMMENT_MAX_CHARS, FEEDBACK_RETENTION_DEFAULT_DAYS, FEEDBACK_TAGS } from "@/lib/feedback";

import { describeChatError } from "./items";
import { type FeedbackValue, type TurnFeedback, keptConsent } from "./useTurnFeedback";

export function FeedbackButtons({ turnId, feedback, testId }: { turnId: string; feedback: TurnFeedback; testId: string }) {
  const t = useT();
  const text = t.chat.feedback;
  const mine = feedback.of(turnId);
  const [asking, setAsking] = useState<FeedbackRating | null>(null);
  // 엄지 한 번의 저장이다. 값이 null 이면 평가를 거둔다
  const change = useMutation({ mutationFn: (value: FeedbackValue | null) => (value ? feedback.save(turnId, value) : feedback.withdraw(turnId)) });
  const busy = change.isPending;

  function press(rating: FeedbackRating) {
    if (mine?.rating === rating) return change.mutate(null);
    if (rating === "down") return ask("down");
    // 평가 사례로 쓰는 동의(P45 8절)는 그 사람이 남긴 값을 그대로 보낸다. 동의를 바꾸는 스위치는 의견 창에 있다
    change.mutate({ rating: "up", tags: [], comment: null, ...keptConsent(mine) });
  }

  /** 의견 창을 연다. 창의 저장은 창 안에 결과를 보이므로 엄지 옆에 남은 앞 저장의 실패는 걷는다. */
  function ask(rating: FeedbackRating) {
    change.reset();
    setAsking(rating);
  }

  return (
    <span className="ml-auto flex items-center gap-0.5" data-testid={`${testId}-feedback`}>
      {change.error ? (
        <span className="mr-1 text-err-fg" role="alert" data-testid={`${testId}-error`}>
          {describeChatError(t, change.error)}
        </span>
      ) : null}
      {mine ? (
        <Button size="sm" variant="ghost" className="h-6 px-2 text-[11px]" disabled={busy} data-testid={`${testId}-comment`} onClick={() => ask(mine.rating)}>
          {text.comment}
        </Button>
      ) : null}
      <IconButton label={text.rating.up} pressed={mine?.rating === "up"} disabled={busy} testId={`${testId}-up`} className="size-6" onClick={() => press("up")}>
        <ThumbsUp size={13} aria-hidden />
      </IconButton>
      <IconButton label={text.rating.down} pressed={mine?.rating === "down"} disabled={busy} testId={`${testId}-down`} className="size-6" onClick={() => press("down")}>
        <ThumbsDown size={13} aria-hidden />
      </IconButton>
      {asking ? (
        <FeedbackDialog
          rating={asking}
          initial={mine}
          days={feedback.retentionDays ?? FEEDBACK_RETENTION_DEFAULT_DAYS}
          onSubmit={(value) => feedback.save(turnId, value)}
          onClose={() => setAsking(null)}
        />
      ) : null}
    </span>
  );
}

/** 이유 칩(아쉬워요만)과 의견 칸이다. 보내기가 성공해야 닫히고 실패하면 창 안에 까닭을 보인다. */
function FeedbackDialog({
  rating,
  initial,
  days,
  onSubmit,
  onClose,
}: {
  rating: FeedbackRating;
  initial: AgentFeedback | undefined;
  /** 동의 안내가 적는 보존 기간(일). */
  days: number;
  onSubmit: (value: FeedbackValue) => Promise<void>;
  onClose: () => void;
}) {
  const t = useT();
  const text = t.chat.feedback;
  // 같은 평가를 고칠 때만 앞의 이유와 의견을 채운다. 좋아요에서 아쉬워요로 바꾸면 빈 칸에서 시작한다
  const same = initial?.rating === rating;
  const [tags, setTags] = useState<FeedbackTag[]>(same ? initial.tags : []);
  const [comment, setComment] = useState(same ? (initial.comment ?? "") : "");
  // 동의는 평가의 것이라 좋아요와 아쉬워요를 바꿔도 남긴 값에서 시작한다. 처음에는 꺼져 있다
  const [consent, setConsent] = useState(Boolean(initial?.evaluation_use));
  const consentHintId = useId();
  const send = useMutation({ mutationFn: onSubmit, onSuccess: onClose });
  const pending = send.isPending;
  const toggle = (tag: FeedbackTag) => setTags((prev) => (prev.includes(tag) ? prev.filter((other) => other !== tag) : [...prev, tag]));

  return (
    <Modal open title={rating === "up" ? text.comment : text.downTitle} testId="feedback-dialog" closeTestId="feedback-dialog-close" onClose={() => (pending ? undefined : onClose())}>
      <form
        className="flex flex-col gap-3 px-5 py-4"
        onSubmit={(e) => {
          e.preventDefault();
          send.mutate({ rating, tags: rating === "down" ? tags : [], comment: comment.trim() || null, ...(consent ? { evaluation_use: true } : {}) });
        }}
      >
        {rating === "down" ? (
          <fieldset className="flex flex-col gap-2">
            <legend className="mb-2 text-sm font-medium text-fg-soft">{text.tagsLegend}</legend>
            <div className="flex flex-wrap gap-1.5">
              {FEEDBACK_TAGS.map((tag) => (
                <Button key={tag} size="sm" variant={tags.includes(tag) ? "default" : "outline"} aria-pressed={tags.includes(tag)} data-testid={`feedback-tag-${tag}`} onClick={() => toggle(tag)}>
                  {text.tags[tag]}
                </Button>
              ))}
            </div>
          </fieldset>
        ) : null}
        <Field label={text.commentLabel} htmlFor="feedback-comment" hint={text.commentHint}>
          <Textarea id="feedback-comment" data-testid="feedback-comment" className="font-sans text-sm" maxLength={FEEDBACK_COMMENT_MAX_CHARS} value={comment} onChange={(e) => setComment(e.target.value)} />
        </Field>
        <div className="flex flex-col gap-1">
          <label className="flex items-start gap-2 text-sm font-medium text-fg-soft">
            <input
              type="checkbox"
              role="switch"
              className="mt-0.5"
              data-testid="feedback-evaluation-use"
              aria-checked={consent}
              aria-describedby={consentHintId}
              checked={consent}
              onChange={(e) => setConsent(e.target.checked)}
            />
            {text.evaluationConsent}
          </label>
          <p id={consentHintId} className="text-xs text-muted" data-testid="feedback-evaluation-use-hint">
            {text.evaluationConsentHint(days)}
          </p>
        </div>
        <ErrorMessage error={send.error} testId="feedback-error" />
        <div className="flex justify-end gap-2">
          <Button variant="ghost" disabled={pending} onClick={onClose}>
            {t.common.cancel}
          </Button>
          <Button type="submit" data-testid="feedback-submit" disabled={pending}>
            {pending ? text.sending : t.chat.composer.send}
          </Button>
        </div>
      </form>
    </Modal>
  );
}
