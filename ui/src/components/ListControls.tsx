/**
 * 목록 화면 공통 검색 + 페이지네이션 (검색칸은 머리 아래 도구 줄, 페이지 단추는 표 아래 바).
 * API 가 전체 목록을 주므로 클라이언트 쪽에서 filter/slice 한다. 서버 쪽 페이징은 범위 밖이다.
 */
import { useCallback, useEffect, useMemo, useState, type ReactNode } from "react";

import { StatusDot } from "@/components/ui/badge";
import { useT } from "@/i18n";
import { listModeKey, readItem, writeItem } from "@/lib/storage";
import { cn } from "@/lib/utils";

export const PAGE_SIZE = 10;

export interface ListView {
  query: string;
  setQuery: (q: string) => void;
  page: number;
  setPage: (p: number) => void;
  pageCount: number;
  total: number;
  /** 현재 페이지의 1-based 시작·끝 (총 0이면 둘 다 0) */
  start: number;
  end: number;
}

export interface ListViewOf<T> extends ListView {
  pageItems: T[];
}

/** 이름 기준 검색 + 페이지 slice. 검색어가 바뀌면 1페이지로 돌아간다. */
export function useListView<T>(items: T[], getText: (item: T) => string, pageSize = PAGE_SIZE): ListViewOf<T> {
  const [query, setQueryRaw] = useState("");
  const [page, setPage] = useState(1);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return items;
    return items.filter((it) => getText(it).toLowerCase().includes(q));
    // getText 는 인라인 화살표 함수라 의존성에서 뺀다 (items·query 로 충분)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [items, query]);

  const total = filtered.length;
  const pageCount = Math.max(1, Math.ceil(total / pageSize));
  const current = Math.min(page, pageCount);

  // 행 삭제로 마지막 페이지가 비면 상태도 마지막 페이지로 내린다. 그래야 항목이 다시 늘어도 빈 페이지로 튀지 않는다.
  useEffect(() => {
    if (page > pageCount) setPage(pageCount);
  }, [page, pageCount]);
  const startIdx = (current - 1) * pageSize;
  const pageItems = filtered.slice(startIdx, startIdx + pageSize);

  return {
    query,
    setQuery: (q: string) => {
      setQueryRaw(q);
      setPage(1);
    },
    page: current,
    setPage,
    pageCount,
    total,
    start: total === 0 ? 0 : startIdx + 1,
    end: total === 0 ? 0 : startIdx + pageItems.length,
    pageItems,
  };
}

/**
 * 목록의 툴바 한 줄 (D44 둘째). 검색칸과 필터 칩이 여기 선다.
 *
 * 머리에는 만들기 하나만 남기려고 검색칸을 내린 자리다. 좁은 창에서는 줄을 바꾼다.
 */
export function ListToolbar({ children }: { children: ReactNode }) {
  return (
    <div className="mb-4 flex flex-wrap items-center gap-2.5" data-testid="list-toolbar">
      {children}
    </div>
  );
}

/** 검색칸의 크기별 모양. md 는 목록 툴바의 칸이고 sm 은 대화 목록처럼 좁은 머리에 서는 칸이다. */
const SEARCH_SIZE = {
  md: { box: "h-[34px] w-[300px] max-w-full gap-2 rounded-chip px-2.5", icon: 15, input: "text-[13.5px]" },
  sm: { box: "h-7 w-full gap-1.5 rounded-item px-2.5", icon: 12, input: "text-xs" },
} as const;

/** 검색칸 (P81: 300px, h34, radius 8). 화면 읽기 프로그램의 이름은 따로 주지 않으면 자리표시 글이다. */
export function SearchInput({
  value,
  onChange,
  placeholder = "Search…",
  ariaLabel,
  size = "md",
  maxLength,
  className,
  testId = "list-search",
}: {
  value: string;
  onChange: (v: string) => void;
  placeholder?: string;
  ariaLabel?: string;
  size?: keyof typeof SEARCH_SIZE;
  maxLength?: number;
  className?: string;
  testId?: string;
}) {
  const s = SEARCH_SIZE[size];
  return (
    <div className={cn("flex items-center border border-field-line bg-surface focus-within:border-focus-ring focus-within:ring-2 focus-within:ring-focus-ring/35", s.box, className)}>
      <svg width={s.icon} height={s.icon} viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" className="shrink-0 text-label" aria-hidden>
        <circle cx="7" cy="7" r="4.5" />
        <path d="M10.5 10.5L14 14" />
      </svg>
      <input
        type="search"
        data-testid={testId}
        aria-label={ariaLabel ?? placeholder}
        className={cn("w-full bg-transparent text-fg placeholder:text-label focus:outline-none", s.input)}
        placeholder={placeholder}
        maxLength={maxLength}
        value={value}
        onChange={(e) => onChange(e.target.value)}
      />
    </div>
  );
}

/**
 * 목록에서 `draft` 를 빼는 토글 (D42).
 *
 * 정의와 배포가 한 리소스가 되면서 아직 안 올린 것이 같은 표에 선다. 옛 목록에서 "인스턴스 목록"
 * 이 곧 "도는 것만" 이었던 자리를 이 토글이 대신한다.
 */
export function DeployedOnlyToggle({
  value,
  onChange,
  testId = "deployed-only",
}: {
  value: boolean;
  onChange: (v: boolean) => void;
  testId?: string;
}) {
  const t = useT();
  // 검색칸과 같은 줄에 서므로 높이도 같다 (D44 둘째). 켜면 쪽빛 옅은 면이 깔린다
  return (
    <label
      className={cn(
        "flex h-[34px] cursor-pointer items-center gap-2 rounded-chip border px-3 text-[13.5px] select-none",
        value ? "border-accent-line bg-accent-soft text-heading" : "border-transparent text-fg hover:bg-hover",
      )}
    >
      <input type="checkbox" data-testid={testId} className="size-3.5" checked={value} onChange={(e) => onChange(e.target.checked)} />
      {t.detail.listControls.deployedOnly}
    </label>
  );
}

/** 목록의 모양 (D47). 기본은 카드다. */
export type ListMode = "card" | "table";

/* 고른 모양은 목록마다 따로 남는다. 사이드바 접힘과 같은 방식으로 키는 lib/storage 에 있고 읽고 쓰는 것은 여기 있다. */
function readListMode(list: string): ListMode {
  return readItem(listModeKey(list)) === "table" ? "table" : "card";
}
function writeListMode(list: string, mode: ListMode): void {
  writeItem(listModeKey(list), mode);
}

/**
 * 이 목록을 무엇으로 볼 것인가 (D47).
 *
 * `list` 는 화면이 정하는 고정된 이름이라 훅이 사는 동안 바뀌지 않는다. 처음 한 번 읽고 바꿀 때마다 남긴다.
 */
export function useListMode(list: string): [ListMode, (mode: ListMode) => void] {
  const [mode, setMode] = useState<ListMode>(() => readListMode(list));
  const change = useCallback(
    (next: ListMode) => {
      setMode(next);
      writeListMode(list, next);
    },
    [list],
  );
  return [mode, change];
}

function ViewToggleButton({
  mode,
  current,
  label,
  onChange,
  children,
}: {
  mode: ListMode;
  current: ListMode;
  label: string;
  onChange: (mode: ListMode) => void;
  children: ReactNode;
}) {
  const active = mode === current;
  return (
    <button
      type="button"
      data-testid={`view-toggle-${mode}`}
      aria-pressed={active}
      aria-label={label}
      title={label}
      className={cn(
        "flex h-[26px] w-[30px] items-center justify-center rounded-badge transition-colors focus-visible:ring-2 focus-visible:ring-focus-ring focus-visible:outline-none",
        active ? "bg-surface text-heading shadow-hairline ring-1 ring-line" : "text-label hover:text-fg",
      )}
      onClick={() => onChange(mode)}
    >
      {children}
    </button>
  );
}

/**
 * 카드와 표를 고르는 세그먼트 (D47). 툴바의 오른쪽 끝에 선다.
 *
 * 카드는 하나를 통째로 읽기에 좋고 표는 값을 세로로 견주기에 좋아 어느 하나가 늘 낫지 않다. 그림만으로는
 * 두 모양의 뜻이 전해지지 않으므로 낱말은 aria-label 과 title 로 준다. 지금 고른 것은 aria-pressed 로 말한다.
 */
export function ViewToggle({ mode, onChange }: { mode: ListMode; onChange: (mode: ListMode) => void }) {
  const t = useT();
  const text = t.detail.listControls;
  return (
    <div
      className="ml-auto flex h-[32px] items-center gap-0.5 rounded-chip border border-line bg-sunken p-0.5"
      role="group"
      aria-label={text.viewToggle}
      data-testid="view-toggle"
    >
      <ViewToggleButton mode="card" current={mode} label={text.cardView} onChange={onChange}>
        <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.4" aria-hidden>
          <rect x="2" y="2" width="5" height="5" rx="1.2" />
          <rect x="9" y="2" width="5" height="5" rx="1.2" />
          <rect x="2" y="9" width="5" height="5" rx="1.2" />
          <rect x="9" y="9" width="5" height="5" rx="1.2" />
        </svg>
      </ViewToggleButton>
      <ViewToggleButton mode="table" current={mode} label={text.tableView} onChange={onChange}>
        <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.4" aria-hidden>
          <path d="M2.5 3.5h11M2.5 6.75h11M2.5 10h11M2.5 13.25h11" strokeLinecap="round" />
        </svg>
      </ViewToggleButton>
    </div>
  );
}

/**
 * 테이블 하단 페이지네이션 바: "1–n / 전체" + 페이지 칩 (28px, radius 7).
 *
 * 지금 쪽은 쪽빛이 아니라 한 겹 짙은 선과 굵은 먹 글자로 선다. 쪽빛은 링크와 포커스의 색이라 여기서 쓰면
 * 누를 수 있는 것과 지금 있는 곳이 같아 보인다 (P81 1절)
 */
export function Pagination({ view }: { view: ListView }) {
  const t = useT();
  const chip =
    "flex size-7 items-center justify-center rounded-item border text-xs transition-colors focus-visible:ring-2 focus-visible:ring-focus-ring focus-visible:outline-none";
  const pages = pageWindow(view.page, view.pageCount);
  return (
    <div className="flex items-center justify-between border-t border-line py-2.5 pr-3 pl-4" data-testid="pagination">
      {/* 검색으로 걸러지면 몇 개가 남았는지 알린다. 눈으로는 표가 줄어드는 것이 보이지만 그렇지 않으면
          입력만 하고 결과를 모른 채로 남는다. 숫자만 읽히면 무슨 수인지 모르므로 낱말을 함께 둔다 */}
      <div className="text-xs text-label" data-testid="page-info" role="status" aria-live="polite" aria-atomic="true">
        <span aria-hidden>
          {view.start}–{view.end} / {view.total}
        </span>
        <span className="sr-only">{t.detail.listControls.pageInfo(view.total, view.start, view.end)}</span>
      </div>
      <div className="flex items-center gap-1.5">
        <button
          type="button"
          data-testid="page-prev"
          aria-label={t.detail.listControls.previousPage}
          disabled={view.page <= 1}
          className={cn(chip, "border-line text-muted hover:bg-hover hover:text-fg disabled:opacity-50")}
          onClick={() => view.setPage(view.page - 1)}
        >
          <svg width="12" height="12" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" aria-hidden>
            <path d="M10 4l-4 4 4 4" />
          </svg>
        </button>
        {pages.map((p, i) =>
          p === null ? (
            <span key={`gap-${i}`} className="px-0.5 text-xs text-dim">
              …
            </span>
          ) : (
            <button
              key={p}
              type="button"
              data-testid={`page-chip-${p}`}
              aria-current={p === view.page ? "page" : undefined}
              className={cn(
                chip,
                p === view.page
                  ? "border-line-strong bg-surface-strong font-bold text-heading"
                  : "border-line text-muted hover:bg-hover hover:text-fg",
              )}
              onClick={() => view.setPage(p)}
            >
              {p}
            </button>
          ),
        )}
        <button
          type="button"
          data-testid="page-next"
          aria-label={t.detail.listControls.nextPage}
          disabled={view.page >= view.pageCount}
          className={cn(chip, "border-line text-muted hover:bg-hover hover:text-fg disabled:opacity-50")}
          onClick={() => view.setPage(view.page + 1)}
        >
          <svg width="12" height="12" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" aria-hidden>
            <path d="M6 4l4 4-4 4" />
          </svg>
        </button>
      </div>
    </div>
  );
}

/** 1 … (p-1) p (p+1) … last 형태의 칩 목록. null 은 말줄임이다. */
export function pageWindow(page: number, pageCount: number): (number | null)[] {
  if (pageCount <= 7) return Array.from({ length: pageCount }, (_, i) => i + 1);
  const out: (number | null)[] = [1];
  if (page > 3) out.push(null);
  for (let p = Math.max(2, page - 1); p <= Math.min(pageCount - 1, page + 1); p++) out.push(p);
  if (page < pageCount - 2) out.push(null);
  out.push(pageCount);
  return out;
}

export interface CountItem {
  label: string;
  value: number;
  /** 상태색 점. 없으면 점 없이 낱말만 선다 (전체·허브 게시처럼 상태가 아닌 칸) */
  dot?: "ok" | "warn" | "err" | "muted";
  testId: string;
}

/**
 * 목록 위의 점 요약 한 줄 (D44 넷째).
 *
 * 타일 다섯이 표보다 먼저 눈에 들어와 정작 보러 온 목록을 나중에 읽게 했다. 같은 수를 한 줄로
 * 내려 표 앞의 자리를 비운다. 총계와 한마디는 제목 옆 요약이 맡고 여기는 갈래별 수만 늘어놓는다.
 */
export function CountLine({ items, testId }: { items: CountItem[]; testId?: string }) {
  return (
    <div className="mb-4 flex flex-wrap items-center gap-x-5 gap-y-2 text-[13px]" data-testid={testId}>
      {items.map((it) => (
        <span key={it.testId} className="inline-flex items-center gap-1.5">
          {it.dot === "muted" ? (
            <span aria-hidden className="inline-block h-[7px] w-[7px] rounded-full bg-label" />
          ) : it.dot ? (
            <StatusDot variant={it.dot} className="h-[7px] w-[7px]" />
          ) : null}
          <span className="text-muted">{it.label}</span>
          {/* 0 은 물러나고 있는 것만 또렷하게 읽힌다. 다섯 칸이 다 같은 굵기면 다시 하나씩 세게 된다 */}
          <span className={cn("tabular-nums", it.value ? "font-semibold text-fg" : "text-muted")} data-testid={it.testId}>
            {it.value}
          </span>
        </span>
      ))}
    </div>
  );
}
