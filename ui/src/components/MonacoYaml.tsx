/**
 * Monaco YAML 편집기. 이 모듈은 Advanced 편집기에서만 lazy import 된다 (번들이 크다).
 * monaco 는 CDN 이 아니라 번들에서 로드한다 (같은 origin, 오프라인 클러스터 고려).
 */
import Editor, { type OnMount } from "@monaco-editor/react";
// 전체 monaco(editor.main, 언어 80여 개) 대신 코어 API + yaml 문법만 번들한다
import * as monaco from "monaco-editor/editor/editor.api";
import "monaco-editor/languages/definitions/yaml/register";
import { useEffect, useRef } from "react";

import { monacoTheme, useTheme } from "@/lib/theme";
import { cn } from "@/lib/utils";
import { setupMonaco } from "./monacoSetup";

/** 줄 표시의 배경과 여백 띠. 색의 까닭은 아래 컴포넌트의 주석에 있다. */
const LINE_STYLES = `
  .daegam-line-locked { background: rgba(100, 116, 139, 0.12); }
  .daegam-line-ignored { background: rgba(161, 161, 170, 0.12); }
  .daegam-line-error { background: rgba(239, 68, 68, 0.16); }
  .daegam-line-locked-glyph { background: #64748b; width: 4px !important; margin-left: 6px; }
  .daegam-line-ignored-glyph { background: #a1a1aa; width: 4px !important; margin-left: 6px; }
  .daegam-line-error-glyph { background: #ef4444; width: 4px !important; margin-left: 6px; }
`;

setupMonaco();

export interface LineMark {
  line: number;
  kind: "locked" | "ignored" | "error";
  title: string;
}

export interface MonacoYamlProps {
  value: string;
  onChange: (next: string) => void;
  marks?: LineMark[];
  readOnly?: boolean;
  height?: string;
  testId?: string;
  /** 바깥에서 특정 줄로 스크롤하고 싶을 때 (값이 바뀔 때마다) */
  revealLine?: number | null;
  /**
   * 줄 번호. 기본은 켬이다. 접기는 이것과 무관하게 남는다.
   *
   * 왼쪽 표시줄(glyph margin)은 `marks` 를 그리는 자리라 여기에 딸리지 않는다. `marks` 를 준 쪽만
   * 켜므로, 번호를 끈 채로도 잠긴 줄·무시된 줄 표시는 그대로 보인다.
   */
  lineNumbers?: boolean;
}

const CLASS: Record<LineMark["kind"], string> = {
  locked: "daegam-line-locked",
  ignored: "daegam-line-ignored",
  error: "daegam-line-error",
};

export default function MonacoYaml({ value, onChange, marks, readOnly, height = "28rem", testId, revealLine, lineNumbers = true }: MonacoYamlProps) {
  // marks 를 아예 주지 않은 쪽은 표시줄이 필요 없다. 빈 배열을 넘기는 쪽과는 다르다
  const showGlyphs = marks !== undefined;
  const lineMarks = marks ?? [];
  const editorRef = useRef<Parameters<OnMount>[0] | null>(null);
  const decorations = useRef<monaco.editor.IEditorDecorationsCollection | null>(null);
  // 편집기만 흰 바탕으로 남으면 다크 화면에서 그 칸만 도려낸 것처럼 보인다
  const theme = useTheme();

  const onMount: OnMount = (editor) => {
    editorRef.current = editor;
    // 붙여넣은 본문의 줄바꿈을 CRLF 로 바꿔 내보내고 있었다. 스펙 본문은 sha256 으로 같은지를
    // 가리므로(`source_sha256`), 같은 문서를 붙여넣어도 해시가 달라져 새 버전이 생긴다
    editor.getModel()?.setEOL(monaco.editor.EndOfLineSequence.LF);
    decorations.current = editor.createDecorationsCollection([]);
    applyMarks();
  };

  function applyMarks() {
    if (!decorations.current) return;
    decorations.current.set(
      lineMarks.map((m) => ({
        range: new monaco.Range(m.line, 1, m.line, 1),
        options: {
          isWholeLine: true,
          className: CLASS[m.kind],
          glyphMarginClassName: `${CLASS[m.kind]}-glyph`,
          hoverMessage: { value: m.title },
          glyphMarginHoverMessage: { value: m.title },
        },
      })),
    );
  }

  useEffect(applyMarks, [lineMarks]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    if (revealLine && editorRef.current) editorRef.current.revealLineInCenter(revealLine);
  }, [revealLine]);

  return (
    <div data-testid={testId} className={cn("overflow-hidden rounded-md border", readOnly ? "border-line" : "border-field-line")}>
      {/*
        빨강은 "여기가 틀렸다" 는 색이다. 잠긴 줄에 그것을 쓰면 아무것도 잘못되지 않았는데 문서의
        3분의 1이 오류로 보인다(MCPServer 100여 줄 중 30줄이 잠겨 있다). 잠김·무시는 "못 고친다"
        이지 "틀렸다" 가 아니므로 눌린 회색으로 내리고, 빨강은 진짜 오류에 돌려준다.

        잠김(슬레이트)과 무시(회색)는 둘 다 눌러 두되 톤을 나눈다. 뜻이 다르기 때문이다. 자세한
        것은 줄에 마우스를 올리면 나오는 글과 옆 패널이 말한다.
      */}
      <style>{LINE_STYLES}</style>
      <Editor
        height={height}
        theme={monacoTheme(theme)}
        language="yaml"
        value={value}
        onChange={(v) => onChange(v ?? "")}
        onMount={onMount}
        options={{
          readOnly,
          minimap: { enabled: false },
          lineNumbers: lineNumbers ? "on" : "off",
          glyphMargin: showGlyphs,
          lineDecorationsWidth: lineNumbers ? undefined : 0,
          fontSize: 12,
          scrollBeyondLastLine: false,
          tabSize: 2,
          wordWrap: "on",
        }}
      />
    </div>
  );
}
