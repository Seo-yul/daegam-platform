/**
 * 편집 중인 Skill 파일을 서버에서 검사한다 (D64, P39 4절). 입력이 멈추고 0.5초 뒤 `POST /skills/validate` 를 부른다.
 *
 * 규칙은 서버 한 곳이라 화면은 결과만 그린다. 앞서 보낸 검사가 늦게 돌아와도 마지막에 보낸 것의 결과만 남긴다. 그래야
 * 고친 뒤에 옛 오류가 다시 떠오르지 않는다. 아무것도 고치지 않은 틀에는 묻지 않는다. 쓰기도 전에 빨간 줄을 띄우지 않기
 * 위해서다(NameFields 와 같은 규칙).
 */
import { useEffect, useRef, useState } from "react";

import { validateSkill } from "@/api/skills";
import type { SkillFileInput, SkillValidation } from "@/api/types";
import { SKILL_VALIDATE_DELAY_MS } from "@/lib/skills";

export interface SkillValidationState {
  /** 마지막으로 받은 결과. 아직 없으면 null 이다. */
  result: SkillValidation | null;
  /** 검사를 부르지 못했다. 검사가 없다고 저장을 막지는 않는다. */
  error: unknown;
  /** 지금 글의 결과를 기다리는 중이다. */
  checking: boolean;
}

/** name 을 주면 그 이름의 Skill 에 넣을 새 리비전으로 본다(머리말 name 을 바꾸면 오류다). enabled 가 거짓이면 묻지 않는다. */
export function useSkillValidation(files: SkillFileInput[], name: string | undefined, enabled: boolean): SkillValidationState {
  const [state, setState] = useState<SkillValidationState>({ result: null, error: null, checking: false });
  const sent = useRef(0);
  useEffect(() => {
    if (!enabled) return;
    // 세대는 글이 바뀐 지금 올린다. 타이머가 돌 때 올리면 앞서 보낸 검사가 그사이 돌아와 고친 뒤의 글에 옛 결과를 그린다
    const id = ++sent.current;
    setState((s) => (s.checking ? s : { ...s, checking: true }));
    const timer = window.setTimeout(() => {
      validateSkill({ files, name }).then(
        (result) => id === sent.current && setState({ result, error: null, checking: false }),
        (error: unknown) => id === sent.current && setState((s) => ({ ...s, error, checking: false })),
      );
    }, SKILL_VALIDATE_DELAY_MS);
    return () => window.clearTimeout(timer);
  }, [files, name, enabled]);
  return state;
}
