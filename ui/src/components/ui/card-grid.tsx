/**
 * 목록의 카드 모양 (D47). 표의 한 행이 카드 하나가 된다. 카드는 고르러 오는 사람을 위한 카탈로그다.
 *
 * 표를 상자에 넣기만 하면 카드가 아니다. 라벨과 값이 같은 무게로 늘어서면 눈이 먼저 잡을 것이 없어
 * 표보다 느리게 읽힌다. 그래서 카드는 한 리소스를 위에서 아래로 넷으로 나눈다.
 *
 * - 머리: 이름과 그 아래 흐린 한 줄(`byline`, 누가 언제 고쳤는지·어디에 공유됐는지).
 * - 몸(`body`): 무엇을 하는 리소스인지. 설명 한 줄, Agent 의 구성 태그, 허브 항목의 Tool 이름이다.
 * - 알림(`note`): 손볼 일(오류·경고·승인 대기)이 있을 때만 서는 한 줄.
 * - 바닥(`footer`): 종류·리비전 같은 짧은 사실, 오른쪽 끝에 상태.
 *
 * 상태는 문제가 있을 때만 보인다(`status`). 정상인 카드가 다 같은 초록 배지를 달면 문제 있는 카드가
 * 묻힌다. 정상이면 부르는 쪽이 `quietStatus` 를 주어 상태를 낭독기에만 남긴다.
 *
 * 동작은 오른쪽 위 더보기 하나로 모은다 (D44 둘째). 카드마다 버튼을 늘어놓으면 카드 수만큼 곱해져
 * 목록이 버튼밭이 된다.
 *
 * 카드 전체가 누르는 곳이다. 이름 글자만 링크로 두었더니 사람들이 카드를 누르고 아무 일도 일어나지
 * 않았다. 그렇다고 카드를 `<a>` 로 감싸면 안에 있는 더보기와 링크가 링크 안의
 * 링크가 된다. 그래서 링크는 여전히 이름 하나이고, 그 링크의 `::after` 가 카드를 덮는다. 낭독기에는
 * 링크가 하나로 남고 마우스에는 카드 전체가 과녁이 된다.
 *
 * 그 덮개보다 위에 있어야 하는 더보기는 `relative` 를 받는다. 덮개는 이름 링크의 것이고 이름 링크가 DOM 에서 가장
 * 앞이라, 뒤에 오는 `relative` 형제가 자연히 이긴다. 이름 링크 자신에게는 `relative` 를 주면 안 된다. 그러면
 * `::after` 가 카드가 아니라 글자를 덮는다. 카드 안에 다른 링크를 두려면 그것도 `relative` 를 받아야 한다.
 */
import { Fragment, type HTMLAttributes, type ReactNode } from "react";
import { Link } from "react-router";

import { ActionMenu, type MenuItem } from "@/components/ui/action-menu";
import { Badge } from "@/components/ui/badge";
import { type Messages, useT } from "@/i18n";
import { displayName, secondaryName, type Named } from "@/lib/names";
import { cn, formatAgo } from "@/lib/utils";

/**
 * 격자. 폭에 따라 1·2·3열이다. 표와 같은 `view.pageItems` 를 받는다.
 *
 * `compact` 는 작은 카드(`ListCard` 의 `compact`)의 격자다. 열 수를 화면 폭이 아니라 목록이 실제로 받은 폭으로 정한다.
 * 사이드바가 256px 을 먹어 화면 폭 1280 에서도 목록은 960px 이라, 화면 폭으로 네 칸을 열면 카드가 230px 로 좁아진다.
 * 목록 폭이 1024px(최대 1120) 이상일 때만 네 칸이다.
 *
 * 같은 줄의 카드는 높이를 맞춘다. 바닥 줄이 `mt-auto` 로 아래에 붙어 줄마다 같은 높이에 선다.
 */
export function CardGrid({ children, testId, className, compact = false }: { children: ReactNode; testId?: string; className?: string; compact?: boolean }) {
  const grid = (
    <div
      className={cn("grid items-stretch gap-3", compact ? "@xl:grid-cols-2 @3xl:grid-cols-3 @5xl:grid-cols-4" : "sm:grid-cols-2 xl:grid-cols-3", className)}
      data-testid={testId}
    >
      {children}
    </div>
  );
  // 컨테이너 질의는 격자 자신이 아니라 바깥 상자의 폭을 본다
  return compact ? <div className="@container">{grid}</div> : grid;
}

/** 머리의 흐린 줄에 쓰는 "누가 · 언제 고쳤는지". 둘 다 없으면 null 이다. */
export function editedLine(t: Messages, by: string | null | undefined, at: string | null | undefined): string | null {
  const ago = formatAgo(t, at);
  const parts = [by, ago ? t.ui.cardGrid.edited(ago) : null].filter(Boolean);
  return parts.length ? parts.join(" · ") : null;
}

/**
 * 태그. 무엇이 들어 있는지를 갈래의 색으로 보인다(Agent 의 모델·Tool 소스·지식·메모리·가드레일은 캔버스 노드 색).
 * 칩은 무채색이고 갈래 색은 작은 네모 점에만 쓴다. 칩마다 바탕을 물들이면 카드 하나에 여럿이 모일 때 무지개가 된다
 * (P81 4절). 긴 모델 id 는 카드 폭에서 자른다.
 */
export function CardTag({ accent, mono = false, children }: { accent: string; mono?: boolean; children: ReactNode }) {
  return (
    <Badge className={cn("max-w-full min-w-0 border-line bg-sunken font-medium text-fg", mono && "font-mono")}>
      <span aria-hidden className="size-1.5 shrink-0 rounded-[2px]" style={{ background: accent }} />
      <span className="truncate">{children}</span>
    </Badge>
  );
}

/**
 * 몸에 두는 설명 한 줄. 비었으면 비었다고 적는다. 빈칸으로 두면 카드가 덜 그려진 것처럼 보인다. 잘린 뒤는 상세에서
 * 읽는다. 카드는 이름 링크의 덮개 아래라 title 을 달아도 뜨지 않는다.
 */
export function CardText({ text, testId }: { text: string | null | undefined; testId?: string }) {
  const t = useT();
  return text ? (
    <p className="line-clamp-1 text-[13px] leading-normal text-fg-soft" data-testid={testId}>
      {text}
    </p>
  ) : (
    <p className="text-[13px] leading-normal text-label" data-testid={testId}>
      {t.ui.cardGrid.noDescription}
    </p>
  );
}

/**
 * 카드 하나.
 *
 * 나머지 prop 은 뿌리에 그대로 펼친다. 표의 행이 갖던 `data-testid`·`title`·`data-*` 가 카드에도 같이 붙어야
 * 모양과 무관하게 같은 것을 찾을 수 있다.
 */
export function ListCard({
  to,
  item,
  badge,
  byline,
  body,
  note,
  footer,
  status,
  quietStatus = false,
  compact = false,
  menu,
  className,
  ...props
}: {
  /** 이름이 데려갈 곳. */
  to: string;
  item: Named;
  /** 이름 옆에 서는 배지 자리. */
  badge?: ReactNode;
  /** 이름 아래 흐린 한 줄의 조각들. 빈 조각은 빼고 " · " 로 잇는다. 표시 이름과 다른 K8s 이름이 있으면 맨 앞에 선다. */
  byline?: ReactNode[];
  /** 무엇을 하는 리소스인지. 설명이면 `CardText` 를 쓴다. */
  body?: ReactNode;
  /** 손볼 일이 있을 때만 서는 한 줄. */
  note?: ReactNode;
  /** 바닥 줄의 짧은 사실들. */
  footer?: ReactNode;
  /** 바닥 오른쪽 끝의 상태. */
  status?: ReactNode;
  /** 정상이라 상태를 낭독기에만 남긴다. */
  quietStatus?: boolean;
  /** 작은 카드. 여백과 이름 글자가 작고 K8s 이름을 싣지 않는다. 항목이 많이 쌓이는 목록이 `CardGrid` 의 `compact` 와 함께 쓴다. */
  compact?: boolean;
  /** 더보기에 들어갈 동작(배포·삭제). 비어 있으면 더보기를 그리지 않는다. */
  menu?: MenuItem[];
} & HTMLAttributes<HTMLDivElement>) {
  const t = useT();
  const text = t.ui.cardGrid;
  const secondary = secondaryName(item);
  const name = displayName(item);
  const parts = [secondary && !compact ? <span className="font-mono">{secondary}</span> : null, ...(byline ?? [])].filter(Boolean);
  return (
    <div
      className={cn(
        "surface relative flex flex-col transition-[border-color,box-shadow] hover:border-line-strong hover:shadow-raised motion-reduce:transition-none",
        compact ? "rounded-tile gap-2 p-3.5" : "rounded-card gap-3 p-[18px]",
        className,
      )}
      {...props}
    >
      {/* 더보기(28px)가 있는 카드와 없는 카드의 머리 높이를 맞춘다. 안 맞추면 같은 줄의 카드가 들쭉날쭉하다 */}
      <div className="flex min-h-7 items-start gap-2">
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
            <Link
              className={cn(
                "truncate font-semibold text-heading underline-offset-2 after:absolute after:inset-0 after:content-[''] hover:underline",
                compact ? "text-[14px]" : "text-[15px]",
              )}
              to={to}
            >
              {name}
            </Link>
            {badge}
          </div>
          {parts.length ? (
            <div className="mt-0.5 truncate text-xs text-muted">
              {parts.map((part, i) => (
                <Fragment key={i}>
                  {i ? <span aria-hidden> · </span> : null}
                  {part}
                </Fragment>
              ))}
            </div>
          ) : null}
        </div>
        {menu?.length ? (
          <ActionMenu items={menu} label={text.menu(name)} triggerSize="sm" triggerLabel={text.menuTrigger(name)} triggerClassName="relative" triggerTestId={`card-menu-${item.name}`} />
        ) : null}
      </div>
      {body ? <div>{body}</div> : null}
      {note ? <div className="text-xs">{note}</div> : null}
      {footer || status ? (
        <div className="mt-auto flex flex-wrap items-center gap-x-2 gap-y-1 border-t border-line pt-2.5 text-xs text-muted">
          {footer}
          {status ? <span className={quietStatus ? "sr-only" : "ml-auto flex shrink-0 items-center gap-1.5"}>{status}</span> : null}
        </div>
      ) : null}
    </div>
  );
}
