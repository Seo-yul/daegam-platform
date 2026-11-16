/**
 * 대화 제목 검색칸 (P36 4절). 홈 챗의 대화 목록과 Agent 상세의 세션 탭이 같이 쓴다. 칸은 목록 화면의 `SearchInput` 이고
 * 여기서는 디바운스만 맡는다. 입력을 멈추고 300ms 가 지나면 앞뒤 공백을 뗀 글을 올리고, 위가 그 글로 서버의 제목 검색(`q`)을
 * 부른다. 같은 글은 다시 올리지 않는다. 비우면 검색을 끈다.
 */
import { useEffect, useRef, useState } from "react";

import { SearchInput } from "@/components/ListControls";
import { SESSION_TITLE_MAX_CHARS } from "@/lib/sessionTitle";

/** 입력을 멈추고 서버에 묻기까지 기다리는 시간이다. */
export const SESSION_SEARCH_DEBOUNCE_MS = 300;

export function SessionSearch({ onSearch, placeholder, testId, className }: { onSearch: (q: string) => void; placeholder: string; testId: string; className?: string }) {
  const [text, setText] = useState("");
  const sent = useRef("");
  useEffect(() => {
    const q = text.trim();
    if (q === sent.current) return;
    const timer = window.setTimeout(() => {
      sent.current = q;
      onSearch(q);
    }, SESSION_SEARCH_DEBOUNCE_MS);
    return () => window.clearTimeout(timer);
  }, [text, onSearch]);
  return <SearchInput size="sm" value={text} onChange={setText} placeholder={placeholder} maxLength={SESSION_TITLE_MAX_CHARS} testId={testId} className={className} />;
}
