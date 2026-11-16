/**
 * Monaco JSON 편집기 (Agent 인스펙터의 response_format.json_schema). JSON 은 작은 monarch 토크나이저를 직접 등록하고
 * 편집기 틀(테두리·테마·줄 끝)은 MonacoText 를 그대로 쓴다. `vs/language/json` 은 전용 워커가 필요해 쓰지 않는다.
 * 이 모듈은 lazy import 된다. monaco 준비(`setupMonaco`)는 MonacoText 가 불러올 때 한다.
 */
import * as monaco from "monaco-editor/editor/editor.api";

import MonacoText from "./MonacoText";

const LANGUAGE = "daegam-json";
let registered = false;
function registerJson(): void {
  if (registered) return;
  registered = true;
  const langs = monaco.languages;
  if (!langs || langs.getLanguages().some((l) => l.id === LANGUAGE)) return;
  langs.register({ id: LANGUAGE });
  langs.setLanguageConfiguration(LANGUAGE, {
    brackets: [
      ["{", "}"],
      ["[", "]"],
    ],
    autoClosingPairs: [
      { open: "{", close: "}" },
      { open: "[", close: "]" },
      { open: '"', close: '"' },
    ],
  });
  langs.setMonarchTokensProvider(LANGUAGE, {
    tokenizer: {
      root: [
        [/"([^"\\]|\\.)*"(?=\s*:)/, "type"],
        [/"([^"\\]|\\.)*"/, "string"],
        [/\b(true|false|null)\b/, "keyword"],
        [/-?\d+(\.\d+)?([eE][+-]?\d+)?/, "number"],
        [/[{}[\]]/, "@brackets"],
        [/[,:]/, "delimiter"],
      ],
    },
  });
}

export interface MonacoJsonProps {
  value: string;
  onChange: (next: string) => void;
  readOnly?: boolean;
  height?: string;
  testId?: string;
}

export default function MonacoJson({ height = "16rem", ...props }: MonacoJsonProps) {
  registerJson();
  return <MonacoText {...props} language={LANGUAGE} height={height} />;
}
