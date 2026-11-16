/**
 * Skill 가져오기의 파일 자리 (D64, P39 4절 "가져오기"). zip 이나 SKILL.md 하나를 끌어 놓거나 선택한다.
 *
 * 끌어 놓기는 채팅 첨부와 같은 훅(`useFileDrop`)이다. 끌어 놓은 파일은 선택 창의 accept 를 거치지 않으므로 같은 확장자 목록으로
 * 거른다. 상한(`SKILL_IMPORT_MAX_BYTES`)을 넘는 파일은 업로드한 뒤 413 을 받는 대신 고른 자리에서 되돌린다.
 * 내용 검사는 서버가 가져올 때 하고, 걸리면 422 의 issue 가 저장 오류 자리에 선다. 되돌린 까닭은 상태에 파일과 함께 코드로 두고
 * 문장은 그릴 때 만든다.
 */
import { useState } from "react";

import { useFileDrop } from "@/components/chat/AttachControls";
import { fileButtonClass } from "@/components/ui/field";
import { type Messages, useT } from "@/i18n";
import { fileTooLargeText, matchesAccept } from "@/lib/files";
import { SKILL_IMPORT_ACCEPT, SKILL_IMPORT_MAX_BYTES } from "@/lib/skills";
import { cn, formatBytes, formatMib } from "@/lib/utils";

/** 되돌린 파일과 그 까닭. 받는 형식이 아니거나 상한을 넘었다. */
interface Refusal {
  reason: "type" | "size";
  file: File;
}

/** 고른 파일을 가져오지 않는 까닭. 가져올 수 있으면 null 이다. */
function refusal(file: File): Refusal | null {
  if (!matchesAccept(file.name, SKILL_IMPORT_ACCEPT)) return { reason: "type", file };
  if (file.size > SKILL_IMPORT_MAX_BYTES) return { reason: "size", file };
  return null;
}

/** 되돌린 까닭의 문장. 그릴 때 만들어 언어를 바꾸면 따라 바뀐다. */
function refusalText(t: Messages, { reason, file }: Refusal): string {
  const text = t.skills.importField;
  if (reason === "type") return text.wrongType(file.name);
  return fileTooLargeText(t, file, text.sizeLimit(formatMib(SKILL_IMPORT_MAX_BYTES)), { format: formatMib });
}

export function SkillImportField({ file, onFile, disabled = false, testId = "skill-import" }: { file: File | null; onFile: (file: File | null) => void; disabled?: boolean; testId?: string }) {
  const t = useT();
  const text = t.skills.importField;
  const [refused, setRefused] = useState<Refusal | null>(null);
  function pick(next: File | undefined) {
    if (!next) return;
    const turnedAway = refusal(next);
    setRefused(turnedAway);
    onFile(turnedAway ? null : next);
  }
  const drop = useFileDrop(disabled ? undefined : (files) => pick(files[0]));

  return (
    <div className="flex flex-col gap-2">
      <div
        {...drop.props}
        data-testid={`${testId}-drop`}
        className={cn(
          "flex flex-col items-center gap-2 rounded-card border border-dashed px-4 py-8 text-center text-sm text-muted",
          drop.over ? "border-accent bg-accent-soft" : "border-field-line",
        )}
      >
        <p>{text.hint}</p>
        <input
          type="file"
          accept={SKILL_IMPORT_ACCEPT}
          aria-label={text.file}
          data-testid={`${testId}-file`}
          disabled={disabled}
          className={cn("text-sm", fileButtonClass)}
          onChange={(e) => {
            pick(e.target.files?.[0]);
            // 같은 파일을 다시 선택해도 onChange 가 오게 비운다
            e.target.value = "";
          }}
        />
        {file ? (
          <p className="font-mono text-xs text-fg" data-testid={`${testId}-chosen`}>
            {file.name} · {formatBytes(file.size)}
          </p>
        ) : null}
      </div>
      {refused ? (
        <p className="text-xs text-warn-fg" role="alert" data-testid={`${testId}-refused`}>
          {refusalText(t, refused)}
        </p>
      ) : null}
    </div>
  );
}
