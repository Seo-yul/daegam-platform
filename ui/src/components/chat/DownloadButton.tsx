/**
 * 다운로드 단추와 실패한 까닭 한 줄 (P36 7절·10절). 인용 원문, 파일 쓰기 도구 카드, 세션 파일 서랍의 행, Skill 내보내기가 함께 쓴다.
 *
 * 받는 동안은 `disabled` 대신 `aria-disabled` 로 알리고 그동안의 누름은 버린다. `disabled` 를 걸면 누른 단추가 초점을 잃어 서랍과
 * 인용 창의 Esc·Tab 이 멈추고, 키보드로 누른 사람이 빠져나오지 못한다. 실패하면 단추 다음에 사람 문구가 `{testId}-error` 로 선다.
 * 그 줄은 `basis-full` 이라 flex 줄에서는 단추 아래로 내려간다.
 */
import { useMutation } from "@tanstack/react-query";
import { Download } from "lucide-react";
import type { ReactNode } from "react";

import { Button, type ButtonProps } from "@/components/ui/button";
import { useT } from "@/i18n";
import { cn } from "@/lib/utils";

import { describeChatError } from "./items";

interface DownloadButtonProps {
  /** 파일을 받아 저장한다. 실패하면 던진다. */
  download: () => Promise<void>;
  testId: string;
  /** 낭독기 이름. 단추 글자만으로 무엇을 받는지 모를 때 준다. */
  label?: string;
  variant?: ButtonProps["variant"];
  /** 기본은 작은 단추다. 상세 제목 줄처럼 다른 단추와 나란히 서는 자리는 그 크기를 준다. */
  size?: ButtonProps["size"];
  /** 아이콘 없이 글자만인가. 서랍 행처럼 단추가 여럿 붙는 자리가 쓴다. */
  plain?: boolean;
  className?: string;
  /** 단추 글자. 없으면 "다운로드" 다. */
  children?: ReactNode;
}

export function DownloadButton({ download, testId, label, variant = "outline", size = "sm", plain = false, className, children }: DownloadButtonProps) {
  const t = useT();
  const save = useMutation({ mutationFn: download });
  return (
    <>
      <Button
        size={size}
        variant={variant}
        aria-disabled={save.isPending || undefined}
        aria-label={label}
        className={cn("aria-disabled:opacity-50", className)}
        data-testid={testId}
        onClick={() => (save.isPending ? undefined : save.mutate())}
      >
        {plain ? null : <Download size={13} aria-hidden />}
        {children ?? t.chat.download}
      </Button>
      {save.error ? (
        <p className="basis-full text-xs text-err-fg" role="alert" data-testid={`${testId}-error`}>
          {describeChatError(t, save.error)}
        </p>
      ) : null}
    </>
  );
}
