import { createContext } from "react";
import { Link } from "react-router";

import { cn } from "@/lib/utils";

/*
 * 머리 줄의 위치 표시 (P81 5절). 지금 화면이 든 메뉴 묶음, 그 메뉴 항목, 상세면 리소스의 표시 이름이다. 표시 이름은
 * 화면이 `PageHeader` 로 알려 준다(TrailTitleContext). 메뉴에 없는 화면(계정 화면)은 그 화면의 제목 하나다.
 * 메뉴의 생김새는 Layout 이 쥐고, 여기는 화면 언어로 풀린 항목(`TrailEntry`)만 받는다.
 */

/** 화면이 자기 표시 이름을 알리는 곳. 화면이 사라지면 null 로 되돌린다. */
export const TrailTitleContext = createContext<(title: string | null) => void>(() => {});

/** 메뉴 항목 하나. label 과 group 은 화면 언어로 풀린 이름이고, 묶음이 없는 항목은 group 이 null 이다. */
export interface TrailEntry {
  to: string;
  label: string;
  group: string | null;
}

export interface Crumb {
  label: string;
  to?: string;
}

/** 경로가 든 메뉴 항목(가장 긴 경로가 이긴다)과 화면 제목으로 위치 표시를 만든다. 목록에서는 항목이 끝이고, 상세에서는 항목이 목록으로 가는 링크다. */
export function trailFor(entries: TrailEntry[], pathname: string, pageTitle: string | null): Crumb[] {
  const match = entries
    .filter((e) => pathname === e.to || pathname.startsWith(e.to + "/"))
    .sort((a, b) => b.to.length - a.to.length)[0];
  if (!match) return pageTitle ? [{ label: pageTitle }] : [];
  const crumbs: Crumb[] = match.group ? [{ label: match.group }] : [];
  if (pathname === match.to) return [...crumbs, { label: match.label }];
  return [...crumbs, { label: match.label, to: match.to }, ...(pageTitle ? [{ label: pageTitle }] : [])];
}

const CRUMB_LINK = "truncate rounded-item text-muted hover:text-heading focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus-ring";

export function Trail({ crumbs }: { crumbs: Crumb[] }) {
  return (
    <ol className="flex min-w-0 items-center gap-2 text-[13.5px] text-muted" data-testid="trail">
      {crumbs.map((c, i) => {
        const last = i === crumbs.length - 1;
        return (
          <li key={`${i}-${c.label}`} className="flex min-w-0 items-center gap-2">
            {i > 0 ? (
              <span className="text-line-strong" aria-hidden>
                /
              </span>
            ) : null}
            {c.to ? (
              <Link to={c.to} className={CRUMB_LINK}>
                {c.label}
              </Link>
            ) : (
              <span className={cn("truncate", last && "font-semibold text-heading")} aria-current={last ? "page" : undefined}>
                {c.label}
              </span>
            )}
          </li>
        );
      })}
    </ol>
  );
}
