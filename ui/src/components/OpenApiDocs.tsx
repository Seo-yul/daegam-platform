/**
 * 자체 렌더 OpenAPI 문서 뷰어. 외부 뷰어(swagger-ui·redoc) 없이 화면 테마로 그린다.
 * 왼쪽은 태그별 오퍼레이션 목록(검색), 오른쪽은 고른 오퍼레이션의 파라미터·요청 본문·응답·SOAP 계약이다.
 * 문서 해석은 lib/openapi.ts 의 순수 함수가 맡는다. 부모(ApiDocsDialog·PlatformApiDocsDialog)가 이 파일을 lazy 로 들여온다.
 */
import { Fragment, useMemo, useState, type ReactNode } from "react";

import type { OpenApiDocument } from "@/api/types";
import { Badge, type BadgeProps } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { Table, TBody, TD, TH, THead, TR } from "@/components/ui/table";
import { useT } from "@/i18n";
import {
  filterGroups,
  groupByTag,
  infoOf,
  operationParameters,
  requestBodyOf,
  responsesOf,
  serversOf,
  soapContractOf,
  tokenizeText,
  type ContentView,
  type HttpMethod,
  type OperationEntry,
  type SchemaRow,
} from "@/lib/openapi";
import { cn } from "@/lib/utils";

export interface OpenApiDocsProps {
  doc: OpenApiDocument;
  /** 처음 선택할 오퍼레이션(operationId). 없거나 문서에 없으면 첫 오퍼레이션이다. */
  initialOperationId?: string | null;
  /**
   * operationId 옆에 "이 값이 MCP Tool 이름이 됩니다" 를 보이는가. Backend API 문서에서만 참이다. 플랫폼 자신의
   * API 문서(P59)의 operationId 는 Tool 이름이 되지 않는다.
   */
  toolNames?: boolean;
}

/** 메서드 배지 색: GET 시안, POST 에메랄드, PUT 앰버, DELETE 로즈, PATCH 바이올렛. */
const METHOD_VARIANT: Record<HttpMethod, BadgeProps["variant"]> = {
  get: "info",
  post: "ok",
  put: "warn",
  delete: "error",
  patch: "accent",
  head: "muted",
  options: "muted",
  trace: "muted",
};

export function MethodBadge({ method, className }: { method: HttpMethod; className?: string }) {
  return (
    <Badge variant={METHOD_VARIANT[method]} className={cn("w-16 justify-center font-mono uppercase", className)}>
      {method}
    </Badge>
  );
}

export default function OpenApiDocs({ doc, initialOperationId, toolNames = true }: OpenApiDocsProps) {
  const t = useT();
  const text = t.detail.openApiDocs;
  const groups = useMemo(() => groupByTag(doc), [doc]);
  const all = useMemo(() => groups.flatMap((g) => g.operations), [groups]);
  const [query, setQuery] = useState("");
  const [selectedId, setSelectedId] = useState<string | null>(() =>
    initialOperationId && all.some((o) => o.id === initialOperationId) ? initialOperationId : (all[0]?.id ?? null),
  );
  const selected = all.find((o) => o.id === selectedId) ?? null;
  const visible = useMemo(() => filterGroups(groups, query), [groups, query]);
  const info = infoOf(doc);
  const servers = serversOf(doc);

  return (
    <div className="flex min-h-0 flex-1 flex-col" data-testid="api-docs">
      <header className="shrink-0 border-b border-line-strong px-5 py-3" data-testid="api-docs-info">
        <div className="flex flex-wrap items-baseline gap-2">
          <span className="text-sm font-semibold text-heading">{info.title}</span>
          {info.version ? (
            <Badge variant="muted" className="font-mono">
              {info.version}
            </Badge>
          ) : null}
          <span className="text-xs text-muted">{text.openapi} {typeof doc.openapi === "string" ? doc.openapi : "3.x"}</span>
        </div>
        {info.description ? <RichText text={info.description} className="mt-1 text-xs text-fg-soft" /> : null}
        {servers.length ? (
          <ul className="mt-1 flex flex-wrap gap-x-4 gap-y-0.5 text-xs">
            {servers.map((s) => (
              <li key={s.url} className="flex items-baseline gap-1.5">
                <span className="text-muted">{text.server}</span>
                <code className="font-mono text-fg">{s.url}</code>
                {s.description ? <span className="text-muted">{s.description}</span> : null}
              </li>
            ))}
          </ul>
        ) : null}
      </header>

      <div className="grid min-h-0 flex-1 grid-cols-[17rem_1fr]">
        <nav className="flex min-h-0 flex-col border-r border-line-strong" aria-label={text.operations}>
          <div className="shrink-0 p-3">
            <Input
              data-testid="api-docs-search"
              aria-label={text.search}
              placeholder={text.searchPlaceholder}
              className="h-8 text-xs"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
            />
          </div>
          <div className="min-h-0 flex-1 overflow-y-auto px-2 pb-3">
            {visible.length ? (
              visible.map((g) => (
                <section key={g.tag} className="mb-3" data-testid={`api-docs-tag-${g.tag}`}>
                  <div
                    className="px-2 py-1 text-xs font-medium text-muted"
                    title={g.description ?? undefined}
                  >
                    {g.tag}
                  </div>
                  <ul className="flex flex-col gap-0.5">
                    {g.operations.map((op) => {
                      const active = op.id === selectedId;
                      return (
                        <li key={`${op.method} ${op.path}`}>
                          <button
                            type="button"
                            data-testid={`api-docs-op-${op.id}`}
                            aria-current={active ? "true" : undefined}
                            title={op.summary ?? op.id}
                            className={cn(
                              "flex w-full items-center gap-2 rounded-item px-2 py-1.5 text-left text-xs focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus-ring",
                              active ? "bg-accent-soft text-heading" : "text-fg hover:bg-hover",
                            )}
                            onClick={() => setSelectedId(op.id)}
                          >
                            <MethodBadge method={op.method} className="shrink-0" />
                            <span className={cn("min-w-0 flex-1 truncate font-mono", op.deprecated && "line-through text-muted")}>
                              {op.path}
                            </span>
                          </button>
                        </li>
                      );
                    })}
                  </ul>
                </section>
              ))
            ) : (
              <p className="px-2 text-xs text-muted">{text.noMatch}</p>
            )}
          </div>
        </nav>

        <div className="min-h-0 overflow-y-auto px-5 py-4" data-testid="api-docs-detail">
          {selected ? (
            <OperationDetail key={selected.id} doc={doc} entry={selected} toolNames={toolNames} />
          ) : (
            <p className="text-sm text-muted">{text.noOperations}</p>
          )}
        </div>
      </div>
    </div>
  );
}

function Section({ title, extra, children, testId }: { title: string; extra?: ReactNode; children: ReactNode; testId?: string }) {
  return (
    <section className="flex flex-col gap-2" data-testid={testId}>
      <div className="flex items-center gap-2">
        <h3 className="text-xs font-medium text-muted">{title}</h3>
        {extra}
      </div>
      {children}
    </section>
  );
}

function OperationDetail({ doc, entry, toolNames }: { doc: OpenApiDocument; entry: OperationEntry; toolNames: boolean }) {
  const t = useT();
  const text = t.detail.openApiDocs;
  const params = operationParameters(doc, entry);
  const body = requestBodyOf(doc, entry);
  const responses = responsesOf(doc, entry);
  const soap = soapContractOf(entry);
  return (
    <div className="flex flex-col gap-6">
      <div data-testid="api-docs-operation">
        <div className="flex flex-wrap items-center gap-2">
          <MethodBadge method={entry.method} />
          <code className="font-mono text-sm text-heading">{entry.path}</code>
          {entry.deprecated ? (
            <Badge variant="warn" data-testid="api-docs-deprecated">
              {text.deprecated}
            </Badge>
          ) : null}
        </div>
        {entry.summary ? <p className="mt-2 text-sm font-medium text-fg">{entry.summary}</p> : null}
        <div className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-xs">
          <span className="text-muted">{text.operationId}</span>
          <code className="rounded bg-code px-1.5 py-0.5 font-mono text-[12px] text-code-fg" data-testid="api-docs-operation-id">
            {entry.operationId ?? "-"}
          </code>
          {toolNames ? (
            <span className="text-muted" data-testid="api-docs-tool-name">
              {text.toolName}
            </span>
          ) : null}
        </div>
        {entry.description ? <RichText text={entry.description} className="mt-2 text-[13px] text-fg-soft" /> : null}
      </div>

      <Section title={text.parameters} testId="api-docs-params">
        {params.length ? (
          <Table>
            <THead>
              <tr>
                <TH>{text.columns.name}</TH>
                <TH>{text.columns.in}</TH>
                <TH>{text.columns.type}</TH>
                <TH>{text.columns.description}</TH>
              </tr>
            </THead>
            <TBody>
              {params.map((p) => (
                <TR key={`${p.in}:${p.name}`} data-testid={`api-docs-param-${p.name}`}>
                  <TD className="font-mono text-xs">
                    <NameCell name={p.name} required={p.required} deprecated={p.deprecated} />
                  </TD>
                  <TD className="text-xs text-muted">{p.in}</TD>
                  <TD className="font-mono text-xs">{p.type}</TD>
                  <TD className="text-[13px] text-fg-soft">
                    {p.description ? <RichText text={p.description} /> : null}
                    <EnumChips values={p.enumValues} />
                  </TD>
                </TR>
              ))}
            </TBody>
          </Table>
        ) : (
          <p className="text-sm text-muted">{text.noParameters}</p>
        )}
      </Section>

      {body ? (
        <Section
          title={text.requestBody}
          testId="api-docs-request-body"
          extra={body.required ? <Badge variant="warn">{text.requiredBadge}</Badge> : null}
        >
          {body.description ? <RichText text={body.description} className="text-[13px] text-fg-soft" /> : null}
          {body.contents.length ? (
            body.contents.map((c) => <ContentBlock key={c.contentType} content={c} />)
          ) : (
            <p className="text-sm text-muted">{text.noBodySchema}</p>
          )}
        </Section>
      ) : null}

      <Section title={text.responses} testId="api-docs-responses">
        {responses.length ? (
          <div className="flex flex-col gap-3">
            {responses.map((r) => (
              <div key={r.status} className="flex flex-col gap-2" data-testid={`api-docs-response-${r.status}`}>
                <div className="flex flex-wrap items-center gap-2">
                  <Badge variant={statusVariant(r.status)} className="font-mono">
                    {r.status}
                  </Badge>
                  {r.description ? <span className="text-[13px] text-fg-soft">{r.description}</span> : null}
                </div>
                {r.contents.map((c) => (
                  <ContentBlock key={c.contentType} content={c} />
                ))}
              </div>
            ))}
          </div>
        ) : (
          <p className="text-sm text-muted">{text.noResponses}</p>
        )}
      </Section>

      {soap ? (
        <Section title={text.soap} testId="api-docs-soap">
          <dl className="grid grid-cols-[9rem_1fr] gap-y-1 text-xs">
            <Def label={text.soapFields.soapAction} value={soap.soapAction} />
            <Def label={text.soapFields.soapVersion} value={soap.soapVersion} />
            <Def label={text.soapFields.style} value={soap.style} />
            <Def label={text.soapFields.endpoint} value={soap.endpoint} />
            <Def label={text.soapFields.inputElement} value={soap.input?.element ?? null} hint={soap.input?.namespace ?? null} />
            <Def label={text.soapFields.outputElement} value={soap.output?.element ?? null} hint={soap.output?.namespace ?? null} />
          </dl>
        </Section>
      ) : null}
    </div>
  );
}

function Def({ label, value, hint }: { label: string; value: string | null; hint?: string | null }) {
  return (
    <>
      <dt className="text-muted">{label}</dt>
      <dd className="min-w-0 break-all font-mono text-fg">
        {value ?? <span className="text-dim">-</span>}
        {hint ? <span className="ml-2 text-muted">{hint}</span> : null}
      </dd>
    </>
  );
}

function statusVariant(status: string): BadgeProps["variant"] {
  if (status.startsWith("2")) return "ok";
  if (status.startsWith("3")) return "info";
  if (status.startsWith("4")) return "warn";
  if (status.startsWith("5")) return "error";
  return "muted";
}

function ContentBlock({ content }: { content: ContentView }) {
  const t = useT();
  return (
    <div className="flex flex-col gap-1.5" data-testid={`api-docs-content-${content.contentType}`}>
      <div className="flex flex-wrap items-center gap-2 text-xs">
        <Badge variant="muted" className="font-mono">
          {content.contentType}
        </Badge>
        <span className="text-muted">{t.detail.openApiDocs.columns.type}</span>
        <code className="font-mono text-fg">{content.type}</code>
      </div>
      {content.rows.length ? <SchemaTable rows={content.rows} /> : null}
    </div>
  );
}

function NameCell({ name, required, deprecated }: { name: string; required: boolean; deprecated: boolean }) {
  const t = useT();
  return (
    <span className="inline-flex items-center gap-1">
      <span className={cn(deprecated && "line-through text-muted")}>{name}</span>
      {required ? (
        <span className="text-warn-fg" title={t.detail.openApiDocs.required}>
          *
        </span>
      ) : null}
      {deprecated ? <span className="text-[10px] text-muted">{t.detail.openApiDocs.deprecated}</span> : null}
    </span>
  );
}

function EnumChips({ values }: { values: string[] | null }) {
  if (!values?.length) return null;
  return (
    <ul className="mt-1 flex flex-wrap gap-1">
      {values.map((v) => (
        <li key={v}>
          <Badge variant="muted" className="font-mono">
            {v}
          </Badge>
        </li>
      ))}
    </ul>
  );
}

/** 스키마 속성 표. 중첩 객체는 depth 만큼 들여쓴 트리다. 순환은 이름만 남기고 자식을 펼치지 않는다. */
function SchemaTable({ rows }: { rows: SchemaRow[] }) {
  const t = useT();
  const text = t.detail.openApiDocs;
  return (
    <Table data-testid="api-docs-schema">
      <THead>
        <tr>
          <TH>{text.columns.property}</TH>
          <TH>{text.columns.type}</TH>
          <TH>{text.columns.description}</TH>
        </tr>
      </THead>
      <TBody>
        {rows.map((r, i) => (
          <TR key={`${r.depth}:${r.name}:${i}`} data-testid={`api-docs-schema-row-${r.name}`} data-depth={r.depth}>
            <TD className="font-mono text-xs">
              <span style={{ paddingLeft: `${r.depth * 1.25}rem` }} className="inline-flex items-center gap-1">
                {r.depth > 0 ? <span className="text-dim" aria-hidden>└</span> : null}
                <NameCell name={r.name} required={r.required} deprecated={r.deprecated} />
              </span>
            </TD>
            <TD className="font-mono text-xs">
              {r.type}
              {r.circular ? (
                <span className="ml-1 text-muted" title={text.circularHint}>
                  {text.circular}
                </span>
              ) : null}
            </TD>
            <TD className="text-[13px] text-fg-soft">
              {r.description ? <RichText text={r.description} /> : null}
              <EnumChips values={r.enumValues} />
            </TD>
          </TR>
        ))}
      </TBody>
    </Table>
  );
}

/** 단순 마크다운: 문단·줄바꿈·`code`·링크만 다룬다. 그 밖의 마크다운은 글자 그대로 보인다. */
export function RichText({ text, className }: { text: string; className?: string }) {
  const paragraphs = tokenizeText(text);
  return (
    <div className={cn("flex flex-col gap-1", className)}>
      {paragraphs.map((tokens, i) => (
        <p key={i}>
          {tokens.map((token, j) => {
            if (token.kind === "break") return <br key={j} />;
            if (token.kind === "code") {
              return (
                <code key={j} className="rounded bg-code px-1 font-mono text-[12px] text-code-fg">
                  {token.text}
                </code>
              );
            }
            if (token.kind === "link") {
              return (
                <a key={j} href={token.href} target="_blank" rel="noreferrer" className="text-link underline-offset-2 hover:text-link-hover hover:underline">
                  {token.text}
                </a>
              );
            }
            return <Fragment key={j}>{token.text}</Fragment>;
          })}
        </p>
      ))}
    </div>
  );
}
