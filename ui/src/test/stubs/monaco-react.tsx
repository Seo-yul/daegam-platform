/** 테스트용 @monaco-editor/react 대체. 편집기를 textarea 로 바꾼다 (vite.config.ts test.alias). */
import { useEffect, useRef, useState } from "react";

export interface StubEditorProps {
  value?: string;
  onChange?: (value: string | undefined) => void;
  options?: { readOnly?: boolean };
}

export default function Editor(props: StubEditorProps) {
  return (
    <textarea
      data-testid="monaco"
      value={props.value ?? ""}
      readOnly={props.options?.readOnly}
      onChange={(e) => props.onChange?.(e.target.value)}
    />
  );
}

export interface StubDiffEditorProps {
  original?: string;
  modified?: string;
  options?: { readOnly?: boolean; renderSideBySide?: boolean };
  onMount?: (editor: unknown, monaco: unknown) => void;
}

/**
 * DiffEditor 대체: original·modified 두 textarea. onMount 에는 revealLineInCenter 만 있는 가짜 편집기를 넘기고,
 * 호출된 쪽과 줄 번호를 `monaco-reveal` 에 적어 "이동" 을 검사할 수 있게 한다.
 */
export function DiffEditor(props: StubDiffEditorProps) {
  const [reveal, setReveal] = useState("");
  const onMount = useRef(props.onMount);
  onMount.current = props.onMount;
  useEffect(() => {
    const fake = {
      getOriginalEditor: () => ({ revealLineInCenter: (line: number) => setReveal(`original:${line}`) }),
      getModifiedEditor: () => ({ revealLineInCenter: (line: number) => setReveal(`modified:${line}`) }),
      updateOptions: () => undefined,
    };
    onMount.current?.(fake, {});
  }, []);
  return (
    <div data-testid="monaco-diff" data-side-by-side={String(props.options?.renderSideBySide ?? true)}>
      <textarea data-testid="monaco-original" readOnly value={props.original ?? ""} />
      <textarea data-testid="monaco-modified" readOnly value={props.modified ?? ""} />
      <span data-testid="monaco-reveal">{reveal}</span>
    </div>
  );
}

export const loader = { config: () => undefined };
export type OnMount = (editor: unknown, monaco: unknown) => void;
export type DiffOnMount = (editor: unknown, monaco: unknown) => void;
