/**
 * 답의 인용 번호 칩, 말풍선 아래 "출처" 줄, 칩을 누르면 뜨는 작은 창 (P36 7절, D61).
 *
 * 채팅 패널과 세션 탭이 `CitationScope` 로 전사 전체의 번호 표(lib/citations.ts)와 원문을 받을 세션을 준다. 말풍선의 마크다운은
 * 표에 있는 `[n]` 만 칩으로 그리고, "출처" 줄은 그 답이 쓴 번호를 처음 나온 차례로 모은다. 표가 비면(비서, 옛 런타임, 인용하지
 * 않은 대화) 아무것도 바뀌지 않는다.
 *
 * 작은 창은 문서 이름, 출처(URL 호스트나 Knowledge Base 이름), 조각 차례와 관련도, 발췌 300자를 보인다. URL 문서는 "원문 열기"
 * 이고 링크 규칙(lib/markdownLinks)을 지난 바깥 주소만 새 탭으로 연다. 올린 문서는 "원문 다운로드" 이고 런타임이 `citations` 를
 * 알렸을 때만 선다. 도는 리비전에 가림 가드레일이 있는 에이전트(상태의 `sources_masked`)는 단추 대신 원문을 열 수 없다는 안내를
 * 보인다. 플랫폼도 같은 판정으로 403 source_masked 를 내므로 상태를 읽기 전에 눌렀으면 그 안내가 오류 자리에 남는다.
 *
 * data-testid: 칩 `citation-chip-{n}`, 출처 줄의 칩 `citation-source-{n}`, 창 `citation-popover`, 원문 열기 `citation-open`,
 * 원문 다운로드 `citation-download`, 가림 안내 `citation-masked`, 다운로드 실패 `citation-download-error`(DownloadButton). 출처 줄은 말풍선 testId 에
 * `-sources` 를 붙인다.
 */
import { ExternalLink } from "lucide-react";
import { createContext, useCallback, useContext, useId, useLayoutEffect, useMemo, useRef, useState, type KeyboardEvent, type ReactNode } from "react";
import { createPortal } from "react-dom";

import { downloadCitationSource } from "@/api/agentChat";
import type { ChatBase } from "@/api/client";
import { restoreFocus } from "@/components/ui/focus";
import { POPOVER_PANEL_CLASS, usePopover } from "@/components/ui/usePopover";
import { useT, type Messages } from "@/i18n";
import { citationTable, citedNumbers, type CitationMarkdown, type CitationTable, type CitedSource } from "@/lib/citations";
import { classifyLink } from "@/lib/markdownLinks";
import { RUNTIME_CITATIONS, type ChatFeatures } from "@/lib/runtimeFeatures";
import { cn } from "@/lib/utils";

import { DownloadButton } from "./DownloadButton";
import type { ChatItem } from "./items";

const EMPTY_TABLE: CitationTable = new Map();

/** 원문 다운로드가 부를 채팅 라우트와 세션이다. */
interface DownloadTarget {
  chat: ChatBase;
  sessionId: string;
}

interface CitationScopeValue {
  table: CitationTable;
  /** 표가 비어 있으면 없다. 말풍선이 마크다운에 그대로 넘긴다. */
  markdown?: CitationMarkdown;
  /** 원문 다운로드가 부를 채팅 라우트와 세션. 받을 수 없으면 null 이다. */
  download: DownloadTarget | null;
  /** 도는 리비전이 도구 결과나 답을 가려 원문을 열지 않는가. */
  masked: boolean;
}

const CitationContext = createContext<CitationScopeValue>({ table: EMPTY_TABLE, download: null, masked: false });

/**
 * 전사 전체의 번호 표를 아래 말풍선에 준다. 마크다운에 넘기는 칩 설정은 표에만 기댄다. 세션이 정해지거나(첫 턴의 done) 상태가
 * 바뀌어도 말풍선이 다시 파싱하지 않는다. 원문 다운로드는 런타임이 `citations` 를 알렸을 때만 서고, 가림은 도는 리비전의 사실이다.
 */
export function CitationScope({ items, chat, sessionId, features, children }: { items: readonly ChatItem[]; chat: ChatBase; sessionId: string | null; features: ChatFeatures; children: ReactNode }) {
  const table = useCitationTable(items);
  const markdown = useMemo<CitationMarkdown | undefined>(() => (table.size ? { known: new Set(table.keys()), Chip: CitationChip } : undefined), [table]);
  const canDownload = features.has(RUNTIME_CITATIONS);
  const masked = features.sourcesMasked;
  const value = useMemo<CitationScopeValue>(
    () => ({ table, markdown, download: canDownload && sessionId ? { chat, sessionId } : null, masked }),
    [table, markdown, canDownload, masked, chat, sessionId],
  );
  return <CitationContext.Provider value={value}>{children}</CitationContext.Provider>;
}

/**
 * 전사의 번호 표. 출처를 든 카드 객체가 모두 그대로면 앞의 표를 돌려준다. 토큰마다 items 가 새 배열이어도 앞 카드들은 같은
 * 객체라 표가 그대로이고 앞 말풍선들이 다시 파싱하지 않는다. 출처는 결과 뒤에 오고 그 뒤로 카드가 바뀌지 않는다.
 *
 * 호출 id 로 견주지 않는다. provider 가 낸 id 는 세션마다 같을 수 있어(Kimi 계열은 `functions.<이름>:<n>` 을 대화마다 0부터
 * 센다) id 로 견주면 같은 id 를 쓰는 다른 세션으로 옮겨도 앞 세션의 표가 남는다.
 */
function useCitationTable(items: readonly ChatItem[]): CitationTable {
  const last = useRef<{ cards: readonly ChatItem[]; table: CitationTable }>({ cards: [], table: EMPTY_TABLE });
  const cards = items.filter((it) => it.kind === "tool_call" && Boolean(it.citations?.length));
  const prev = last.current.cards;
  if (cards.length !== prev.length || cards.some((card, i) => card !== prev[i])) last.current = { cards, table: citationTable(cards) };
  return last.current.table;
}

/** 말풍선이 마크다운에 넘길 번호 칩 설정. 표가 비면 undefined 다. */
export function useCitationMarkdown(): CitationMarkdown | undefined {
  return useContext(CitationContext).markdown;
}

/** 답 글 속의 번호 칩. 표에 없는 번호는 오지 않지만 오면 글자 그대로다. */
function CitationChip({ n }: { n: number }) {
  return (
    <CitationButton n={n} testId={`citation-chip-${n}`} className="mx-0.5 h-4 min-w-4 px-1 align-super text-[10px]">
      {n}
    </CitationButton>
  );
}

/** 말풍선 아래 "출처" 줄. 이 답이 쓴 번호를 처음 나온 차례로 문서 이름과 함께 모은다. 번호가 없으면 아무것도 없다. */
export function CitationSources({ text, testId }: { text: string; testId?: string }) {
  const t = useT();
  const words = t.chat.citations;
  const { table, markdown } = useContext(CitationContext);
  const numbers = useMemo(() => (markdown ? citedNumbers(text, markdown.known) : []), [markdown, text]);
  if (!numbers.length) return null;
  return (
    <div className="flex flex-wrap items-center gap-1 border-t border-line-soft pt-1.5 text-[11px] text-muted" data-testid={testId}>
      <span className="mr-0.5 font-semibold text-label">{words.sources}</span>
      {numbers.map((n) => (
        <CitationButton key={n} n={n} testId={`citation-source-${n}`} className="h-5 max-w-56 gap-1 px-1.5 text-[11px]">
          <span className="font-semibold text-accent">{n}</span>
          <span className="truncate">{table.get(n)?.title ?? words.untitled}</span>
        </CitationButton>
      ))}
    </div>
  );
}

/**
 * 누르면 그 번호의 작은 창을 여는 단추. 열리면 창이 포커스를 받아 낭독기가 내용을 읽는다. 바깥을 누르거나 Escape 면
 * 닫히고 Escape 는 단추로 포커스를 돌려준다.
 */
function CitationButton({ n, testId, className, children }: { n: number; testId: string; className: string; children: ReactNode }) {
  const t = useT();
  const text = t.chat.citations;
  const { table, download, masked } = useContext(CitationContext);
  const source = table.get(n);
  const [open, setOpen] = useState(false);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const panelRef = useRef<HTMLDivElement>(null);
  const panelId = useId();
  const close = useCallback(() => setOpen(false), []);
  const { style } = usePopover({ open, triggerRef, panelRef, onClose: close });
  useLayoutEffect(() => {
    if (open) panelRef.current?.focus();
  }, [open]);
  if (!source) return <>[{n}]</>;

  function onPanelKeyDown(e: KeyboardEvent<HTMLDivElement>) {
    if (e.key !== "Escape") return;
    e.preventDefault();
    close();
    restoreFocus(triggerRef.current);
  }

  return (
    <>
      <button
        ref={triggerRef}
        type="button"
        aria-expanded={open}
        aria-controls={open ? panelId : undefined}
        aria-label={text.chip(n, source.title ?? text.untitled)}
        data-testid={testId}
        onClick={() => setOpen((v) => !v)}
        className={cn("inline-flex items-center justify-center rounded-full border border-line bg-surface font-semibold leading-none text-accent hover:bg-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus-ring", className)}
      >
        {children}
      </button>
      {open
        ? createPortal(
            <div
              ref={panelRef}
              id={panelId}
              role="dialog"
              tabIndex={-1}
              aria-label={text.popover(n)}
              style={style}
              onKeyDown={onPanelKeyDown}
              data-testid="citation-popover"
              className={cn(POPOVER_PANEL_CLASS, "w-80 max-w-[calc(100vw-16px)] p-3")}
            >
              <CitationCard n={n} source={source} download={download} masked={masked} />
            </div>,
            document.body,
          )
        : null}
    </>
  );
}

/**
 * 작은 창의 내용. 원문 열기, 다운로드, 가림 안내 가운데 하나가 선다. 가림 가드레일이 있는 에이전트는 다운로드 단추 대신 플랫폼의
 * 403 source_masked 와 같은 글을 보인다.
 */
function CitationCard({ n, source, download, masked }: { n: number; source: CitedSource; download: DownloadTarget | null; masked: boolean }) {
  const t = useT();
  const text = t.chat.citations;
  const link = source.url ? classifyLink(source.url) : null;
  const external = link?.kind === "external" ? link : null;
  const origin = external?.title ?? source.kb?.name ?? null;
  const title = source.title ?? text.untitled;
  const chunk = chunkLine(t, source);

  return (
    <div className="flex flex-col gap-2 text-xs">
      <div className="flex flex-col gap-0.5">
        <span className="break-words text-sm font-semibold text-heading">{title}</span>
        {origin ? <span className="text-muted">{origin}</span> : null}
        {chunk ? <span className="text-muted">{chunk}</span> : null}
      </div>
      {source.excerpt ? <blockquote className="whitespace-pre-wrap break-words border-l-2 border-line-strong pl-2 text-fg">{source.excerpt}</blockquote> : null}
      {external ? (
        <a href={external.href} target="_blank" rel="noopener noreferrer nofollow" title={external.title} className="inline-flex items-center gap-1 self-start text-accent underline underline-offset-2 hover:no-underline" data-testid="citation-open">
          <ExternalLink size={12} aria-hidden />
          {text.open}
        </a>
      ) : source.kind === "knowledge" && masked ? (
        <p className="text-muted" data-testid="citation-masked">
          {t.errors.codes.source_masked}
        </p>
      ) : source.kind === "knowledge" && download ? (
        <DownloadButton download={() => downloadCitationSource(download.chat, download.sessionId, n, title)} className="self-start" testId="citation-download">
          {text.download}
        </DownloadButton>
      ) : null}
    </div>
  );
}

/** "13번째 조각 · 관련도 0.83". 조각 번호는 0 부터라 하나를 더한다. 모르는 칸은 뺀다. */
function chunkLine(t: Messages, source: CitedSource): string {
  const text = t.chat.citations;
  const parts: string[] = [];
  if (typeof source.chunk === "number") parts.push(text.chunk(source.chunk + 1));
  if (typeof source.score === "number") parts.push(text.score(source.score.toFixed(2)));
  return parts.join(" · ");
}
