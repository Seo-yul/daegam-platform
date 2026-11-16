/**
 * Advanced 편집 컴포넌트. 계약은 리소스 종류가 아니라 문서 묶음이다 (docs/archive/plan/phase-7-ui.md).
 *   documents[]: {kind, name, namespace, rendered, overrides, final, live, drift[], drift_managers[], locked[], ignored_by_operator[], error}
 *   콜백: onSave(doc, yaml) / onReset(doc) / onAbsorb(doc)
 * MCP Server 는 문서 하나(MCPServer), Gateway 는 여러 개(vMCP + 그룹·Entry) 라 탭으로 보여 준다.
 * Monaco 는 이 컴포넌트가 lazy import 한다. 부모는 이 컴포넌트 자체를 lazy 로 들여온다.
 */
import { lazy, Suspense, useEffect, useMemo, useState } from "react";
import { parse as parseYaml } from "yaml";

import { ApiError } from "@/api/client";
import type { Manifest, ManifestDocument } from "@/api/types";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Alert, Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Table, TBody, TD, TH, THead, TR } from "@/components/ui/table";
import { TAB_TONE } from "@/components/ui/tabs";
import { type Messages, useT } from "@/i18n";
import { diffJson, showValue, type DiffEntry } from "@/lib/diff";
import { cn } from "@/lib/utils";
import { toYaml } from "@/lib/yaml";
import { linesForPaths } from "@/lib/yamlPaths";

import type { LineMark } from "./MonacoYaml";

const MonacoYaml = lazy(() => import("./MonacoYaml"));

export interface SaveResult {
  overrides: Record<string, unknown>;
  final: Manifest;
  diff: string[];
  validation: string;
  note?: string;
}

export interface AdvancedEditorProps {
  documents: ManifestDocument[];
  onSave: (doc: ManifestDocument, yaml: string) => Promise<SaveResult>;
  onReset: (doc: ManifestDocument) => Promise<unknown>;
  onAbsorb: (doc: ManifestDocument) => Promise<unknown>;
  /** 저장·드리프트 처리 뒤 부모가 문서를 다시 받아오게 한다. */
  onChanged?: () => unknown;
}

type View = "edit" | "overrides" | "drift";

export function AdvancedEditor(props: AdvancedEditorProps) {
  const t = useT();
  const { documents } = props;
  const [active, setActive] = useState(0);
  const doc = documents[Math.min(active, documents.length - 1)];
  if (!doc) return <p className="text-sm text-muted">{t.shared.advancedEditor.noDocuments}</p>;
  return (
    <div className="flex flex-col gap-3" data-testid="advanced-editor">
      {documents.length > 1 ? (
        <div className="flex gap-1 border-b border-line" role="tablist">
          {documents.map((d, i) => (
            <button
              key={`${d.kind}/${d.name}`}
              role="tab"
              type="button"
              aria-selected={i === active}
              data-testid={`doc-tab-${d.kind}`}
              className={cn(
                "-mb-px flex flex-col items-start gap-0.5 rounded-t-item border-b-2 px-3 py-1.5 text-left focus-visible:ring-2 focus-visible:ring-focus-ring focus-visible:outline-none",
                i === active ? TAB_TONE.active : TAB_TONE.idle,
              )}
              onClick={() => setActive(i)}
            >
              {/* 종류는 갈래 이름이고 실제 리소스는 name 이다. Gateway 는 문서가 여섯이라 둘이 같은
                  무게로 한 줄에 붙으면 어느 탭이 어느 객체인지 안 갈리고, 이름이 길어 세 줄씩 접힌다.
                  위아래로 쌓으면 두 줄 안에 들어가고 무게로 갈린다 */}
              <span className={cn("text-[11px]", i === active ? "text-muted" : "text-dim")}>{d.kind}</span>
              <span className={cn("font-mono text-xs", i === active ? "font-semibold text-heading" : "text-muted")}>{d.name}</span>
            </button>
          ))}
        </div>
      ) : null}
      <DocumentEditor key={`${doc.kind}/${doc.name}`} doc={doc} {...props} />
    </div>
  );
}

interface ErrorPanel {
  code: string;
  detail: string;
  path?: string;
}

/** 편집기 YAML 을 읽지 못한 까닭. 매핑이 아닌 것은 그릴 때 사전의 말로 적고, 파서 오류는 파서의 말(detail)을 싣는다. */
type YamlProblem = { code: "not_mapping" } | { code: "parse"; detail: string };

function yamlProblemText(t: Messages, problem: YamlProblem): string {
  return problem.code === "not_mapping" ? t.shared.advancedEditor.notMapping : problem.detail;
}

function toErrorPanel(e: unknown): ErrorPanel {
  if (e instanceof ApiError) return { code: e.code, detail: e.detail, path: e.extra<string>("path") };
  if (e instanceof Error) return { code: "error", detail: e.message };
  return { code: "error", detail: String(e) };
}

function DocumentEditor({ doc, onSave, onReset, onAbsorb, onChanged }: AdvancedEditorProps & { doc: ManifestDocument }) {
  const t = useT();
  const text = t.shared.advancedEditor;
  const [view, setView] = useState<View>("edit");
  const [yaml, setYaml] = useState(() => toYaml(doc.final));
  const [baseline, setBaseline] = useState(() => toYaml(doc.final));
  const [parseError, setParseError] = useState<YamlProblem | null>(null);
  const [errors, setErrors] = useState<ErrorPanel[]>([]);
  const [saved, setSaved] = useState<SaveResult | null>(null);
  const [busy, setBusy] = useState<"save" | "reset" | "absorb" | null>(null);
  const [reveal, setReveal] = useState<number | null>(null);

  // 부모가 문서를 다시 받아오면(저장·드리프트 처리 뒤) 편집 중이 아닐 때만 본문을 갱신한다
  useEffect(() => {
    const next = toYaml(doc.final);
    if (yaml === baseline) setYaml(next);
    setBaseline(next);
  }, [doc.final]); // eslint-disable-line react-hooks/exhaustive-deps

  const dirty = yaml !== baseline;

  // 편집 중인 yaml 을 파싱해 rendered 와 비교 (클라이언트 overrides 미리보기)
  const parsed = useMemo<{ value: Manifest | null; error: YamlProblem | null }>(() => {
    try {
      const v: unknown = parseYaml(yaml);
      if (!v || typeof v !== "object" || Array.isArray(v)) return { value: null, error: { code: "not_mapping" } };
      return { value: v as Manifest, error: null };
    } catch (e) {
      return { value: null, error: { code: "parse", detail: (e as Error).message } };
    }
  }, [yaml]);
  const overridesDiff: DiffEntry[] = useMemo(() => (parsed.value ? diffJson(doc.rendered, parsed.value) : []), [parsed.value, doc.rendered]);
  const driftDiff: DiffEntry[] = useMemo(() => (doc.live ? diffJson(doc.final, doc.live) : []), [doc.final, doc.live]);

  // 잠긴/무시 경로 → 줄 번호. 키 입력마다 도는 자리라 한 번 파싱으로 둘을 함께 풀고 나눈다
  const guardedPaths = useMemo(() => [...doc.locked, ...doc.ignored_by_operator], [doc.locked, doc.ignored_by_operator]);
  const guardedLines = useMemo(() => linesForPaths(yaml, guardedPaths), [yaml, guardedPaths]);
  const lockedLines = useMemo(() => guardedLines.slice(0, doc.locked.length), [guardedLines, doc.locked.length]);
  const ignoredLines = useMemo(() => guardedLines.slice(doc.locked.length), [guardedLines, doc.locked.length]);
  // 저장본 쪽은 저장본이 바뀔 때만 푼다
  const baselineLines = useMemo(() => new Map(linesForPaths(baseline, guardedPaths).map((p) => [p.path, p])), [baseline, guardedPaths]);

  /*
   * 잠긴 줄을 고쳤는가. 저장해야 422 를 보는 것은 너무 늦다.
   *
   * 같은 해석기로 저장본과 편집본에서 같은 경로를 풀고 그 줄의 글자를 견준다. 서버의 판정을 여기서
   * 다시 구현하지 않는다. 두 벌이 미묘하게 어긋나면 화면이 거짓말을 하게 된다.
   *
   * 이것은 미리 알리는 것이지 보증이 아니다. 잠긴 블록 안쪽 깊은 곳만 고치면 그 줄의 글자는 그대로라
   * 여기서 놓친다. 그래도 저장할 때 서버가 잡는다.
   */
  const touchedLocked = useMemo(() => {
    if (yaml === baseline) return [];
    const wasText = baseline.split("\n");
    const nowText = yaml.split("\n");
    return guardedLines
      .filter((now) => {
        const was = baselineLines.get(now.path);
        if (!was) return false;
        if (was.lines.length !== now.lines.length) return true;
        return was.lines.some((l, i) => (wasText[l - 1] ?? "") !== (nowText[now.lines[i] - 1] ?? ""));
      })
      .map((p) => p.path);
  }, [yaml, baseline, guardedLines, baselineLines]);
  const errorLines = useMemo(() => linesForPaths(yaml, errors.map((e) => e.path).filter((p): p is string => Boolean(p))), [yaml, errors]);
  const marks: LineMark[] = useMemo(() => {
    const out: LineMark[] = [];
    for (const p of lockedLines) for (const l of p.lines) out.push({ line: l, kind: "locked", title: text.mark.locked(p.path) });
    for (const p of ignoredLines) for (const l of p.lines) out.push({ line: l, kind: "ignored", title: text.mark.ignored(p.path) });
    for (const p of errorLines) for (const l of p.lines) out.push({ line: l, kind: "error", title: text.mark.error(p.path) });
    return out;
  }, [lockedLines, ignoredLines, errorLines, text]);

  async function run<T>(kind: NonNullable<typeof busy>, fn: () => Promise<T>, after: (r: T) => void) {
    setBusy(kind);
    setErrors([]);
    try {
      const r = await fn();
      after(r);
      await onChanged?.();
    } catch (e) {
      setErrors([toErrorPanel(e)]);
    } finally {
      setBusy(null);
    }
  }

  function doSave() {
    if (parsed.error) {
      setParseError(parsed.error);
      return;
    }
    setParseError(null);
    void run(
      "save",
      () => onSave(doc, yaml),
      (r) => {
        setSaved(r);
        const next = toYaml(r.final);
        setYaml(next);
        setBaseline(next);
      },
    );
  }

  const hasDrift = Boolean(doc.drift && doc.drift.length);

  return (
    <div className="grid gap-4 lg:grid-cols-[1fr_18rem]" data-testid={`advanced-doc-${doc.kind}`}>
      <div className="flex min-w-0 flex-col gap-3">
        <div className="flex flex-wrap items-center gap-2">
          <div className="flex gap-1 rounded-md border border-line p-0.5" role="tablist">
            {(
              [
                ["edit", text.view.edit],
                ["overrides", text.view.overrides(overridesDiff.length)],
                ["drift", text.view.drift(doc.drift?.length ?? null)],
              ] as [View, string][]
            ).map(([v, label]) => (
              <button
                key={v}
                role="tab"
                type="button"
                aria-selected={view === v}
                data-testid={`view-${v}`}
                className={cn("rounded px-2.5 py-1 text-xs", view === v ? "bg-accent-soft border border-accent-line text-heading" : "text-muted hover:bg-hover")}
                onClick={() => setView(v)}
              >
                {label}
              </button>
            ))}
          </div>
          {/* 셋을 무게로 가른다. 종류는 갈래, 네임스페이스는 자리, 이름이 그 객체다 */}
          <span className="flex items-center gap-1.5 text-xs" data-testid="doc-identity">
            <span className="text-muted">{doc.kind}</span>
            <span className="font-mono text-dim">{doc.namespace}/</span>
            <span className="font-mono font-semibold text-heading">{doc.name}</span>
          </span>
          {dirty ? (
            <Badge variant="warn" data-testid="dirty-badge">
              {text.dirty}
            </Badge>
          ) : null}
          {doc.error ? (
            <Badge variant="error" title={doc.error} data-testid="doc-error-badge">
              {text.liveUnreadable}
            </Badge>
          ) : null}
          <div className="ml-auto flex gap-2">
            <Button size="sm" variant="ghost" data-testid="discard-button" disabled={!dirty || busy !== null} onClick={() => setYaml(baseline)}>
              {text.discard}
            </Button>
            <Button size="sm" data-testid="save-button" disabled={!dirty || busy !== null} onClick={doSave}>
              {busy === "save" ? t.shared.saving : text.save}
            </Button>
          </div>
        </div>

        {touchedLocked.length ? (
          <Alert variant="warn" data-testid="touched-locked">
            <div className="font-medium">{text.touchedLocked}</div>
            <ul className="mt-1 list-disc pl-5 font-mono text-xs">
              {touchedLocked.map((path) => (
                <li key={path}>{path}</li>
              ))}
            </ul>
          </Alert>
        ) : null}
        {parseError ? (
          <Alert variant="error" data-testid="parse-error">
            {text.yamlPrefix} {yamlProblemText(t, parseError)}
          </Alert>
        ) : null}
        {errors.length ? (
          <Alert variant="error" data-testid="error-panel">
            <ul className="flex flex-col gap-1">
              {errors.map((e, i) => (
                <li key={i} data-testid={`error-${e.code}`}>
                  <span className="font-mono text-xs">{e.code}</span>: {e.detail}
                  {e.path ? (
                    <button
                      type="button"
                      className="ml-2 font-mono text-xs underline"
                      data-testid="error-path"
                      onClick={() => {
                        const l = errorLines.find((p) => p.path === e.path)?.lines[0];
                        if (l) setReveal(l);
                        setView("edit");
                      }}
                    >
                      {e.path}
                    </button>
                  ) : null}
                </li>
              ))}
            </ul>
          </Alert>
        ) : null}
        {saved ? (
          <Alert variant="ok" data-testid="saved-panel">
            <div className="flex flex-wrap items-center gap-2">
              <span>{text.saved(saved.diff.length)}</span>
              <Badge variant="info" data-testid="validation-badge" title={saved.note}>
                {text.validation(saved.validation)}
              </Badge>
            </div>
            {saved.note ? <p className="mt-1 text-xs">{saved.note}</p> : null}
            {saved.diff.length ? (
              <ul className="mt-1 list-disc pl-5 font-mono text-xs" data-testid="saved-diff">
                {saved.diff.map((p) => (
                  <li key={p}>{p}</li>
                ))}
              </ul>
            ) : null}
          </Alert>
        ) : null}
        {view === "edit" ? (
          <Suspense fallback={<div className="h-[28rem] animate-pulse rounded-md bg-hover" data-testid="editor-loading" />}>
            <MonacoYaml value={yaml} onChange={setYaml} marks={marks} testId="yaml-editor" revealLine={reveal} lineNumbers={false} />
          </Suspense>
        ) : null}

        {view === "overrides" ? (
          <DiffTable
            testId="overrides-diff"
            title={text.overridesTitle}
            entries={overridesDiff}
            empty={parsed.error ? text.unreadable(yamlProblemText(t, parsed.error)) : text.noOverrides}
            serverPaths={saved?.diff ?? (doc.overrides && Object.keys(doc.overrides).length ? undefined : [])}
          />
        ) : null}

        {view === "drift" ? (
          <div className="flex flex-col gap-3" data-testid="drift-view">
            {doc.error ? <Alert variant="error">{doc.error}</Alert> : null}
            {doc.live === null && !doc.error ? <Alert variant="warn">{text.noLive}</Alert> : null}
            {doc.drift_managers.length ? (
              <Alert variant="warn" data-testid="drift-managers">
                {text.managers(doc.drift_managers.join(", "))}
              </Alert>
            ) : null}
            {hasDrift ? (
              <div className="flex gap-2">
                <Button size="sm" variant="secondary" data-testid="drift-reset" disabled={busy !== null} onClick={() => void run("reset", () => onReset(doc), () => undefined)}>
                  {busy === "reset" ? text.resetting : text.reset}
                </Button>
                <Button size="sm" variant="outline" data-testid="drift-absorb" disabled={busy !== null} onClick={() => void run("absorb", () => onAbsorb(doc), () => undefined)}>
                  {busy === "absorb" ? text.absorbing : text.absorb}
                </Button>
              </div>
            ) : null}
            <DiffTable
              testId="drift-diff"
              title={text.driftTitle}
              entries={driftDiff}
              empty={doc.drift === null ? text.driftUnknown : text.noDrift}
              serverPaths={doc.drift ?? undefined}
            />
          </div>
        ) : null}
      </div>

      <aside className="flex flex-col gap-3 text-xs" data-testid="path-panel">
        <PathList
          title={text.locked.title}
          hint={text.locked.hint}
          items={lockedLines}
          testId="locked-list"
          onJump={(l) => {
            setReveal(l);
            setView("edit");
          }}
        />
        <PathList
          title={text.ignored.title}
          hint={text.ignored.hint}
          items={ignoredLines}
          testId="ignored-list"
          onJump={(l) => {
            setReveal(l);
            setView("edit");
          }}
        />
        <Card>
          <CardHeader>
            <CardTitle className="text-xs">{text.currentOverrides}</CardTitle>
          </CardHeader>
          <CardContent>
            {Object.keys(doc.overrides ?? {}).length ? (
              <pre className="max-h-40 overflow-auto font-mono text-[11px]" data-testid="current-overrides">
                {toYaml(doc.overrides)}
              </pre>
            ) : (
              <span className="text-muted">{text.none}</span>
            )}
          </CardContent>
        </Card>
      </aside>
    </div>
  );
}

/**
 * 잠긴·무시되는 경로 목록.
 *
 * 목록은 CR 종류마다 정해진 카탈로그라 이 문서에 없는 경로가 대부분이다. 없는 것까지 나란히 세우면
 * 정작 지금 문서에 있는 것이 묻히므로, 있는 것만 펴 두고 나머지는 접는다. 접은 쪽도 지우지는 않는다.
 * "이 CR 에서 무엇이 잠겨 있나" 를 보러 오는 사람이 있다.
 */
function PathList({ title, hint, items, testId, onJump }: { title: string; hint: string; items: { path: string; lines: number[] }[]; testId: string; onJump: (line: number) => void }) {
  const t = useT();
  const text = t.shared.advancedEditor;
  const present = items.filter((p) => p.lines.length);
  const absent = items.filter((p) => !p.lines.length);
  return (
    <Card>
      <CardHeader className="flex-col items-start gap-1">
        <CardTitle className="text-xs">{title}</CardTitle>
        <p className="text-[11px] font-normal text-muted">{hint}</p>
      </CardHeader>
      <CardContent>
        <ul className="flex flex-col gap-0.5 font-mono text-[11px]" data-testid={testId}>
          {present.map((p) => (
            <li key={p.path} data-testid={`${testId}-item`} className="flex items-start justify-between gap-2">
              <span className="break-all">{p.path}</span>
              <button type="button" className="shrink-0 text-muted underline" onClick={() => onJump(p.lines[0])} data-testid={`${testId}-line`}>
                L{p.lines.join(",")}
              </button>
            </li>
          ))}
        </ul>
        {present.length === 0 ? <p className="text-[11px] text-muted">{text.notInDocument}</p> : null}
        {absent.length ? (
          <details className="mt-2 text-[11px] text-muted">
            <summary data-testid={`${testId}-absent-toggle`}>{text.absent(absent.length)}</summary>
            <ul className="mt-1 flex flex-col gap-0.5 font-mono" data-testid={`${testId}-absent`}>
              {absent.map((p) => (
                <li key={p.path} className="break-all">
                  {p.path}
                </li>
              ))}
            </ul>
          </details>
        ) : null}
      </CardContent>
    </Card>
  );
}

function DiffTable({ title, entries, empty, testId, serverPaths }: { title: string; entries: DiffEntry[]; empty: string; testId: string; serverPaths?: string[] }) {
  const t = useT();
  const columns = t.shared.diffColumns;
  return (
    <div className="flex flex-col gap-2" data-testid={testId}>
      <div className="text-xs text-muted">{title}</div>
      {entries.length ? (
        <Table>
          <THead>
            <tr>
              <TH>{columns.path}</TH>
              <TH>{columns.before}</TH>
              <TH>{columns.after}</TH>
            </tr>
          </THead>
          <TBody>
            {entries.map((d) => (
              <TR key={d.path} data-testid={`${testId}-row`}>
                <TD className="font-mono text-xs">{d.path}</TD>
                <TD className="font-mono text-xs text-err-fg">{showValue(d.from)}</TD>
                <TD className="font-mono text-xs text-ok-fg">{showValue(d.to)}</TD>
              </TR>
            ))}
          </TBody>
        </Table>
      ) : (
        <p className="text-sm text-muted">{empty}</p>
      )}
      {serverPaths && serverPaths.length ? (
        <details className="text-xs text-muted">
          <summary>{t.shared.advancedEditor.serverPaths(serverPaths.length)}</summary>
          <ul className="mt-1 list-disc pl-4 font-mono" data-testid={`${testId}-server-paths`}>
            {serverPaths.map((p) => (
              <li key={p}>{p}</li>
            ))}
          </ul>
        </details>
      ) : null}
    </div>
  );
}
