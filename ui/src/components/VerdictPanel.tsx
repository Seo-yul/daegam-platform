import { Fragment, useState, type ReactNode } from "react";

import type { CheckResult, Verdict } from "@/api/types";
import { Badge, levelVariant } from "@/components/ui/badge";
import { Chevron } from "@/components/ui/icons";
import { Table, TBody, TD, TH, THead, TR } from "@/components/ui/table";
import { useT } from "@/i18n";
import { checkRefs, countCheckResults, describeCheck, groupCheckResults, roundtripTools, verdictItemText, type CheckGroup, type RoundtripTool } from "@/lib/verifyChecks";

/** verify/convert 응답의 verdict. level 배지 + items(code·count·message). 메시지는 verdictItemText 가 고른다. */
export function VerdictPanel({ verdict }: { verdict: Verdict }) {
  const t = useT();
  const text = t.backendApis.verdict;
  return (
    <div className="flex flex-col gap-2" data-testid="verdict-panel">
      <div className="flex items-center gap-2">
        <span className="text-sm font-medium">{text.title}</span>
        <Badge variant={levelVariant(verdict.level)} data-testid="verdict-level">
          {verdict.level}
        </Badge>
      </div>
      {verdict.items.length ? (
        <Table data-testid="verdict-items">
          <THead>
            <tr>
              {/* 좁은 카드에서 짧은 머리글이 글자마다 줄을 바꾼다. 머리글은 한 줄로 둔다 */}
              <TH className="whitespace-nowrap">{text.columns.level}</TH>
              <TH className="whitespace-nowrap">{text.columns.code}</TH>
              <TH className="whitespace-nowrap">{text.columns.count}</TH>
              <TH className="whitespace-nowrap">{text.columns.message}</TH>
            </tr>
          </THead>
          <TBody>
            {verdict.items.map((it) => (
              <TR key={it.code} data-testid={`verdict-item-${it.code}`}>
                <TD>
                  <Badge variant={levelVariant(it.level)}>{it.level}</Badge>
                </TD>
                <TD className="font-mono text-xs">{it.code}</TD>
                <TD>{it.count}</TD>
                <TD className="text-fg-soft">{verdictItemText(t, it)}</TD>
              </TR>
            ))}
          </TBody>
        </Table>
      ) : (
        <p className="text-sm text-muted">{text.noItems}</p>
      )}
    </div>
  );
}

const COLUMNS = 5;

/** warn·fail 은 기본 펼침, pass·skip 은 기본 접힘이다. */
function openByDefault(r: CheckResult): boolean {
  return r.status === "warn" || r.status === "fail";
}

/**
 * 저장된 버전의 verify 리포트. 체크를 그룹으로 묶고 행마다 제목·id·status·심각도·message 를 보이며,
 * 행을 누르면 풀이·location·refs·data 가 펼쳐진다. REST 스펙이면 x-soap 그룹은 한 줄로 접는다.
 */
export function CheckResultsTable({ results, kind }: { results: CheckResult[]; kind?: string | null }) {
  const t = useT();
  const text = t.backendApis.verdict;
  const groups = groupCheckResults(results);
  const counts = countCheckResults(results);
  const tools = roundtripTools(results.find((r) => r.id === "fastmcp.roundtrip"));
  const soapNotApplicable = kind === "rest";

  // 펼침 상태는 results 가 바뀌면 기본값으로 돌아간다 (이전 렌더의 results 와 비교한다)
  const [state, setState] = useState<{ for: CheckResult[]; open: Record<string, boolean>; soap: boolean }>({
    for: results,
    open: {},
    soap: false,
  });
  const open = state.for === results ? state.open : {};
  const soapOpen = state.for === results ? state.soap : false;
  const isOpen = (r: CheckResult) => open[r.id] ?? openByDefault(r);
  const toggle = (r: CheckResult) => setState({ for: results, open: { ...open, [r.id]: !isOpen(r) }, soap: soapOpen });
  const toggleSoap = () => setState({ for: results, open, soap: !soapOpen });

  return (
    <div className="flex flex-col gap-2">
      <div data-testid="report-summary" className="flex flex-wrap items-center gap-x-3 gap-y-1 text-sm">
        <span className="font-medium text-fg">{text.summary(counts.total, counts.pass, counts.warn, counts.fail, counts.skip)}</span>
        {tools ? <span className="text-fg-soft">{text.toolCount(tools.length)}</span> : null}
      </div>
      <Table data-testid="check-results">
        <THead>
          <tr>
            <TH className="w-8 pr-0">
              <span className="sr-only">{text.expand}</span>
            </TH>
            <TH className="whitespace-nowrap">{text.checkColumns.check}</TH>
            <TH className="whitespace-nowrap">{text.checkColumns.status}</TH>
            <TH className="whitespace-nowrap">{text.checkColumns.severity}</TH>
            <TH className="whitespace-nowrap">{text.checkColumns.message}</TH>
          </tr>
        </THead>
        <TBody>
          {groups.map(({ group, rows }) => {
            const collapsible = group === "x-soap" && soapNotApplicable;
            return (
              <Fragment key={group}>
                <GroupRow group={group} collapsible={collapsible} open={soapOpen} onToggle={toggleSoap} />
                {collapsible && !soapOpen
                  ? null
                  : rows.map((r) => <CheckRow key={r.id} result={r} open={isOpen(r)} onToggle={() => toggle(r)} tools={tools} />)}
              </Fragment>
            );
          })}
        </TBody>
      </Table>
    </div>
  );
}

function GroupRow({
  group,
  collapsible,
  open,
  onToggle,
}: {
  group: CheckGroup;
  collapsible: boolean;
  open: boolean;
  onToggle: () => void;
}) {
  const t = useT();
  const info = t.backendApis.checks.group[group];
  return (
    <tr data-testid={`check-group-${group}`} className="bg-surface">
      <td colSpan={COLUMNS} className="px-4 py-2">
        {collapsible ? (
          <button
            type="button"
            data-testid={`check-group-toggle-${group}`}
            aria-expanded={open}
            className="flex w-full items-center gap-2 text-left"
            onClick={onToggle}
          >
            <Chevron open={open} />
            <span className="text-xs font-medium text-muted">{info.title}</span>
            <span className="text-xs text-muted">{t.backendApis.verdict.notApplicable}</span>
          </button>
        ) : (
          <div className="flex flex-wrap items-baseline gap-x-2">
            <span className="text-xs font-medium text-muted">{info.title}</span>
            <span className="text-xs text-muted">{info.description}</span>
          </div>
        )}
      </td>
    </tr>
  );
}

function CheckRow({
  result: r,
  open,
  onToggle,
  tools,
}: {
  result: CheckResult;
  open: boolean;
  onToggle: () => void;
  tools: RoundtripTool[] | null;
}) {
  const t = useT();
  const text = t.backendApis.verdict;
  const info = describeCheck(t, r.id);
  const detailId = `check-detail-${r.id}`;
  const quiet = r.status === "pass" || r.status === "skip";
  return (
    <>
      <TR data-testid={`check-${r.id}`} className="cursor-pointer" onClick={onToggle}>
        <TD className="w-8 pr-0">
          <button
            type="button"
            data-testid={`check-toggle-${r.id}`}
            aria-expanded={open}
            aria-controls={detailId}
            aria-label={text.details(info.title)}
            className="rounded-item p-1 text-muted hover:text-fg"
            onClick={(e) => {
              e.stopPropagation();
              onToggle();
            }}
          >
            <Chevron open={open} />
          </button>
        </TD>
        <TD>
          <div className="font-medium text-fg">{info.title}</div>
          <div className="font-mono text-[11px] text-muted">{r.id}</div>
        </TD>
        <TD>
          <Badge variant={levelVariant(r.status)} data-testid={`check-status-${r.id}`}>
            {r.status}
          </Badge>
        </TD>
        <TD>
          {info.severity ? (
            <Badge
              variant={quiet ? "muted" : info.severity === "error" ? "error" : "warn"}
              data-testid={`check-severity-${r.id}`}
              title={text.severityTitle[info.severity]}
            >
              {t.backendApis.checks.severity[info.severity]}
            </Badge>
          ) : (
            <span className="text-dim">-</span>
          )}
        </TD>
        <TD className="text-fg-soft">{r.message ?? ""}</TD>
      </TR>
      {open ? (
        <tr id={detailId} data-testid={detailId} className="border-t-0 bg-surface">
          <td />
          <td colSpan={COLUMNS - 1} className="px-4 pb-4 pt-1">
            <CheckDetail result={r} description={info.description} tools={r.id === "fastmcp.roundtrip" ? tools : null} />
          </td>
        </tr>
      ) : null}
    </>
  );
}

/** 백틱으로 감싼 부분은 code 로 그린다. */
function inlineCode(text: string): ReactNode[] {
  return text.split("`").map((part, i) =>
    i % 2 === 1 ? (
      <code key={i} className="rounded bg-code px-1 font-mono text-[12px] text-code-fg">
        {part}
      </code>
    ) : (
      <Fragment key={i}>{part}</Fragment>
    ),
  );
}

const DATA_PREVIEW_LIMIT = 4000;

function CheckDetail({
  result: r,
  description,
  tools,
}: {
  result: CheckResult;
  description: string;
  tools: RoundtripTool[] | null;
}) {
  const t = useT();
  const refs = checkRefs(r);
  const location = typeof r.location === "string" && r.location ? r.location : null;
  const hasData = r.data !== undefined && r.data !== null;
  const json = hasData && !tools ? JSON.stringify(r.data, null, 2) : null;
  return (
    <div className="flex flex-col gap-2 text-[13px]">
      {description ? <p className="text-fg-soft">{inlineCode(description)}</p> : null}
      {location ? (
        <div className="flex gap-2">
          <span className="text-muted">{t.backendApis.verdict.location}</span>
          <code className="font-mono text-xs text-fg">{location}</code>
        </div>
      ) : null}
      {refs.length ? (
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-muted">{t.backendApis.verdict.refs}</span>
          {refs.map((ref) => (
            <a
              key={ref.url}
              href={ref.url}
              target="_blank"
              rel="noreferrer"
              className="text-link underline-offset-2 hover:text-link-hover hover:underline"
            >
              {ref.id}
            </a>
          ))}
        </div>
      ) : null}
      {tools ? <RoundtripToolsTable tools={tools} /> : null}
      {json ? (
        <pre className="max-h-64 overflow-auto rounded-chip bg-code p-3 font-mono text-[12px] text-code-fg">
          {json.length > DATA_PREVIEW_LIMIT ? t.backendApis.verdict.truncated(json.slice(0, DATA_PREVIEW_LIMIT), json.length - DATA_PREVIEW_LIMIT) : json}
        </pre>
      ) : null}
    </div>
  );
}

function RoundtripToolsTable({ tools }: { tools: RoundtripTool[] }) {
  const t = useT();
  if (!tools.length) return <p className="text-muted">{t.backendApis.verdict.noRoundtripTools}</p>;
  return (
    <Table data-testid="roundtrip-tools">
      <THead>
        <tr>
          <TH>{t.shared.columns.tool}</TH>
          <TH>{t.detail.openApiDocs.parameters}</TH>
        </tr>
      </THead>
      <TBody>
        {tools.map((tool) => (
          <TR key={tool.name} data-testid={`roundtrip-tool-${tool.name}`}>
            <TD className="font-mono text-xs">{tool.name}</TD>
            <TD>
              {tool.params.length ? (
                <ul className="flex flex-wrap gap-1">
                  {tool.params.map((p) => (
                    <li key={p.name}>
                      <Badge variant="muted" className="font-mono">
                        {p.name}
                        {p.type ? <span className="text-dim">: {p.type}</span> : null}
                        {p.required ? <span className="text-warn-fg">*</span> : null}
                      </Badge>
                    </li>
                  ))}
                </ul>
              ) : (
                <span className="text-dim">-</span>
              )}
            </TD>
          </TR>
        ))}
      </TBody>
    </Table>
  );
}

/**
 * Tool 이름 태그. onSelect 가 있으면 태그가 버튼이 되어 누르면 그 Tool(operationId)을 넘긴다. `limit` 를 주면 앞의
 * 그만큼만 보이고 나머지는 "외 n개" 로 센다(목록 카드, MCP Server 의 백엔드 요약).
 */
export function ToolList({ names, onSelect, limit, testId = "tool-list" }: { names: string[]; onSelect?: (name: string) => void; limit?: number; testId?: string }) {
  const t = useT();
  const text = t.backendApis.verdict;
  if (!names.length) return <p className="text-sm text-muted">{text.noTools}</p>;
  const shown = limit === undefined ? names : names.slice(0, limit);
  const rest = names.length - shown.length;
  return (
    <ul className="flex flex-wrap items-center gap-1" data-testid={testId}>
      {shown.map((n) => (
        <li key={n}>
          {onSelect ? (
            <button
              type="button"
              data-testid={`tool-${n}`}
              title={text.toolTitle}
              className="rounded-badge focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus-ring"
              onClick={() => onSelect(n)}
            >
              <Badge variant="muted" className="cursor-pointer font-mono hover:border-accent-line hover:text-fg">
                {n}
              </Badge>
            </button>
          ) : (
            <Badge variant="muted" className="font-mono" data-testid={`tool-${n}`}>
              {n}
            </Badge>
          )}
        </li>
      ))}
      {rest > 0 ? <li className="text-xs text-muted">{text.moreTools(rest)}</li> : null}
    </ul>
  );
}
