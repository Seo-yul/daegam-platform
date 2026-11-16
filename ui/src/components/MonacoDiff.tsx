/**
 * Monaco DiffEditor (원본 YAML | 채택 적용 YAML). SpecDiff 와 Skill 리비전 차이(D64)가 lazy import 한다 (번들이 크다).
 * monaco 를 띄우는 준비는 `monacoSetup` 이 맡는다. 편집기 넷 중 먼저 로드되는 것이 한 번 부른다.
 */
import { DiffEditor, type DiffOnMount } from "@monaco-editor/react";
import "monaco-editor/languages/definitions/markdown/register";
import "monaco-editor/languages/definitions/yaml/register";
import { useEffect, useRef } from "react";

import { monacoTheme, useTheme } from "@/lib/theme";

import { setupMonaco } from "./monacoSetup";

setupMonaco();

export interface RevealRequest {
  /** 1-based 줄 번호. 원본(왼쪽) 편집기 기준이다. 좌우는 같이 스크롤한다. */
  line: number;
  /** 같은 줄을 다시 눌러도 이동하도록 호출마다 바뀌는 값 */
  nonce: number;
}

export interface MonacoDiffProps {
  original: string;
  modified: string;
  /** false 면 인라인(한 열) diff. 칸 폭을 재서 정하는 것은 부르는 쪽(`SpecDiff`)이다 */
  sideBySide: boolean;
  /** 부르는 쪽이 좌우를 포기하는 폭(px). Monaco 의 자체 전환에도 같은 문턱을 준다 */
  inlineBreakpoint: number;
  height?: string;
  testId?: string;
  reveal?: RevealRequest | null;
  /** 문법. 기본은 스펙의 YAML 이고 Skill 파일은 markdown 이나 문법 없는 글이다. */
  language?: "yaml" | "markdown" | "plaintext";
}

export default function MonacoDiff({ original, modified, sideBySide, inlineBreakpoint, height = "32rem", testId, reveal, language = "yaml" }: MonacoDiffProps) {
  const editorRef = useRef<Parameters<DiffOnMount>[0] | null>(null);
  const theme = useTheme();

  const onMount: DiffOnMount = (editor) => {
    editorRef.current = editor;
    editor.updateOptions({ renderSideBySide: sideBySide });
  };

  // `@monaco-editor/react` 의 DiffEditor 는 뜬 뒤에 options 가 바뀌어도 다시 적용하지 않는다.
  // 칸 폭을 재서 좌우·인라인을 정하는 쪽(`SpecDiff`)이 뜬 뒤에 답을 바꾸므로 여기서 직접 넣는다
  useEffect(() => {
    editorRef.current?.updateOptions({ renderSideBySide: sideBySide });
  }, [sideBySide]);

  useEffect(() => {
    if (reveal && editorRef.current) editorRef.current.getOriginalEditor().revealLineInCenter(reveal.line);
  }, [reveal]);

  return (
    <div data-testid={testId} className="overflow-hidden rounded-md border border-line">
      {/* 인라인일 때 Monaco 는 원본 편집기를 없애지 않고 몇 픽셀로 남겨 둔다. 그러면 글자 한 칸씩만
          비어져 나오는 띠가 왼쪽에 생긴다. 그릴 것이 없는 칸이므로 아예 감춘다 */}
      {sideBySide ? null : <style>{`[data-testid="${testId}"] .editor.original { display: none !important; }`}</style>}
      <DiffEditor
        height={height}
        language={language}
        theme={monacoTheme(theme)}
        original={original}
        modified={modified}
        onMount={onMount}
        options={{
          readOnly: true,
          originalEditable: false,
          // 좁으면 Monaco 가 인라인 diff 로 바꾸는데, 인라인은 원본·수정본 줄 번호를 두 칸으로 낸다.
          // 같은 숫자가 나란히 서서 어느 쪽 줄인지 되레 헷갈린다. 어디를 볼지는 변경 목록의 "이동" 이
          // 맡으므로 번호를 걷는다 (`SpecEditor` 와 같은 판단)
          lineNumbers: "off",
          lineDecorationsWidth: 0,
          renderSideBySide: sideBySide,
          // 좁은 칸에서 좌우를 포기하는 판단은 SpecDiff 가 칸 폭을 재서 하고 sideBySide 로 넘긴다.
          // Monaco 의 자체 전환은 이 조합에서 듣지 않아 믿지 않는다. 받침으로만 두고 문턱은 SpecDiff 의
          // 것을 받아 두 곳의 숫자가 어긋나지 않게 한다
          useInlineViewWhenSpaceIsLimited: true,
          renderSideBySideInlineBreakpoint: inlineBreakpoint,
          minimap: { enabled: false },
          fontSize: 12,
          scrollBeyondLastLine: false,
          wordWrap: "on",
          diffWordWrap: "on",
          renderOverviewRuler: false,
          ignoreTrimWhitespace: false,
        }}
      />
    </div>
  );
}
