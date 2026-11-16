/**
 * 도구 목록 (D48). 도구 하나가 접었다 펴는 한 덩어리다.
 *
 * 이름만 늘어놓으면 이 도구가 무엇을 받는지 알 수 없어, Gateway 에 무엇을 열어 줄지 정하려면 화면을
 * 떠나야 했다. 펼치면 설명 전문과 입력 인자가 이름·타입·필수·설명으로 선다.
 *
 * 접고 펴기는 `<details>` 가 맡는다. 직접 만들면 키보드와 낭독기 몫을 다시 써야 하는데 브라우저가 이미
 * 제대로 한다. 기본은 모두 접힘이다. 접힌 줄에도 설명 첫 줄이 보여야 무엇을 펼칠지 고를 수 있다.
 */
import { useMemo, useState } from "react";

import type { HubTool } from "@/api/types";
import { SearchInput } from "@/components/ListControls";
import { Badge } from "@/components/ui/badge";
import { useT } from "@/i18n";

/** JSON Schema 의 인자 하나를 화면이 쓰는 모양으로. 스키마가 object 가 아니면 줄이 서지 않는다. */
interface Arg {
  name: string;
  type: string;
  required: boolean;
  description?: string;
}

/**
 * 타입 한 낱말. `type` 이 배열이면 붙여 적고 배열이면 원소 타입까지 적는다. 무엇을 넣는지는
 * `string` 보다 `string[]` 이 말해 준다.
 */
function typeOf(schema: Record<string, unknown>): string {
  const t = schema.type;
  if (Array.isArray(t)) return t.join(" | ");
  if (typeof t === "string") {
    if (t !== "array") return t;
    const items = schema.items as Record<string, unknown> | undefined;
    const inner = items ? typeOf(items) : "";
    return inner ? `${inner}[]` : "array";
  }
  if (Array.isArray(schema.enum)) return "enum";
  if (schema.anyOf || schema.oneOf) return "anyOf";
  return "unknown";
}

function argsOf(schema: HubTool["input_schema"]): Arg[] {
  if (!schema || typeof schema !== "object") return [];
  const root = schema as Record<string, unknown>;
  if (!root.properties || typeof root.properties !== "object") return [];
  const required = new Set(Array.isArray(root.required) ? (root.required as string[]) : []);
  return Object.entries(root.properties as Record<string, unknown>).map(([name, raw]) => {
    const s = (raw ?? {}) as Record<string, unknown>;
    return { name, type: typeOf(s), required: required.has(name), description: typeof s.description === "string" ? s.description : undefined };
  });
}

/** 접힌 줄에 놓을 설명 한 줄. 줄바꿈 앞까지만 쓴다. */
function firstLine(text: string | null | undefined): string {
  return (text ?? "").split("\n")[0]!.trim();
}

export function ToolSchemaList({ tools, testId = "tool-schemas" }: { tools: HubTool[]; testId?: string }) {
  const t = useT();
  const text = t.hub.toolSchemas;
  const [query, setQuery] = useState("");
  const shown = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return tools;
    return tools.filter((tool) => `${tool.name} ${tool.title ?? ""} ${tool.description ?? ""}`.toLowerCase().includes(q));
  }, [tools, query]);

  return (
    <div className="flex flex-col gap-2" data-testid={testId}>
      {/* 여덟 개를 넘으면 접힌 목록도 길다. 이름으로 좁힐 길을 준다 */}
      {/* 기본 testid(list-search)는 목록 화면의 검색칸 것이다. 같은 화면에 둘이 있으면 골라내지 못한다 */}
      {tools.length > 8 ? <SearchInput value={query} onChange={setQuery} placeholder={text.search} testId="tool-search" /> : null}
      {shown.length ? (
        shown.map((tool) => <ToolRow key={tool.name} tool={tool} />)
      ) : (
        <p className="text-sm text-muted" data-testid="tool-search-empty">
          {text.noMatch(query)}
        </p>
      )}
    </div>
  );
}

function ToolRow({ tool }: { tool: HubTool }) {
  const t = useT();
  const text = t.hub.toolSchemas;
  const args = argsOf(tool.input_schema);
  const summary = firstLine(tool.description);
  // 펼침을 React 상태로 들고 `onToggle` 로 되받는다. `open` 만 주면 React 가 그 속성을 통제해서,
  // 사람이 펼친 뒤 부모가 다시 그려질 때마다 도로 닫힌다 (클러스터 e2e 에서 잡혔다)
  const [open, setOpen] = useState(false);
  return (
    // testid 가 `tool-{이름}` 이 아닌 까닭은 VerdictPanel 의 `ToolList` 칩이 그 이름을 이미 쓰고,
    // 못 물어봤을 때 이 화면이 그 칩으로 떨어져서 둘이 같은 화면에 설 수 있기 때문이다
    <details
      className="group rounded-card border border-line-soft bg-surface open:bg-surface-strong"
      open={open}
      onToggle={(e) => setOpen(e.currentTarget.open)}
      data-testid={`tool-detail-${tool.name}`}
    >
      <summary className="flex cursor-pointer list-none items-start gap-2 px-3.5 py-2.5 focus-visible:ring-2 focus-visible:ring-focus-ring focus-visible:outline-none">
        <span className="min-w-0 flex-1">
          <span className="flex flex-wrap items-center gap-2">
            <span className="font-mono text-[13px] font-medium text-heading">{tool.name}</span>
            {/* title 은 사람이 읽는 이름이다. 이름과 같으면(실제 서버가 흔히 그렇다) 두 번 적지 않는다 */}
            {tool.title && tool.title !== tool.name ? <span className="truncate text-xs text-muted">{tool.title}</span> : null}
            {/*
              서버가 말하는 readOnlyHint·destructiveHint 는 일부러 안 보인다. MCP 명세가 믿을 수 없는
              서버의 annotations 로 판단하지 말라고 못박고, 이 화면이 바로 무엇을 열어 줄지 정하는
              자리다. 배지로 달면 사람이 힌트를 보증으로 읽는다.
            */}
            {tool.output_schema ? <Badge variant="muted">{text.outputSchema}</Badge> : null}
          </span>
          {/* 접혀 있을 때만 한 줄 요약이다. 펴면 아래에 전문이 선다 */}
          {summary ? <span className="mt-0.5 line-clamp-1 block text-xs text-muted group-open:hidden">{summary}</span> : null}
        </span>
        <span aria-hidden className="mt-0.5 shrink-0 text-label transition-transform group-open:rotate-180">
          <svg viewBox="0 0 16 16" width="14" height="14" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
            <path d="M4 6l4 4 4-4" />
          </svg>
        </span>
      </summary>
      <div className="flex flex-col gap-2.5 border-t border-line-soft px-3.5 py-3">
        {tool.description ? (
          <p className="whitespace-pre-wrap text-[13px] text-fg">{tool.description}</p>
        ) : (
          <p className="text-xs text-muted">{text.noDescription}</p>
        )}
        {tool.schema_omitted ? (
          <p className="text-xs text-warn-fg" data-testid={`tool-schema-omitted-${tool.name}`}>
            {text.schemaOmitted}
          </p>
        ) : args.length ? (
          <div>
            <div className="mb-1.5 text-xs font-medium text-label">{text.input}</div>
            <dl className="flex flex-col gap-1.5" data-testid={`tool-args-${tool.name}`}>
              {args.map((a) => (
                <div key={a.name}>
                  <dt className="flex flex-wrap items-center gap-1.5">
                    <span className="font-mono text-xs text-fg">{a.name}</span>
                    {/* 별표는 색이 아니라 낱말로도 뜻이 서야 한다 */}
                    {a.required ? (
                      <span className="text-xs text-err-fg" title={t.detail.openApiDocs.required}>
                        *<span className="sr-only">{t.detail.openApiDocs.required}</span>
                      </span>
                    ) : null}
                    <Badge variant="muted">{a.type}</Badge>
                  </dt>
                  {a.description ? <dd className="text-xs text-muted">{a.description}</dd> : null}
                </div>
              ))}
            </dl>
          </div>
        ) : (
          <p className="text-xs text-muted">{text.noArgs}</p>
        )}
      </div>
    </details>
  );
}
