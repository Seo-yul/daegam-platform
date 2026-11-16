import type { HTMLAttributes, ReactNode, TdHTMLAttributes, ThHTMLAttributes } from "react";
import { Link } from "react-router";

import { CopyButton } from "@/components/ui/copy-button";
import { useT } from "@/i18n";
import { displayName, secondaryName, type Named } from "@/lib/names";
import { cn } from "@/lib/utils";

export function Table({ className, ...props }: HTMLAttributes<HTMLTableElement>) {
  return (
    // relative 가 없으면 안쪽 sr-only(absolute)의 containing block 이 body 가 되어 이 컨테이너를
    // 빠져나가고, 표가 화면보다 넓을 때 문서 전체에 가로 스크롤이 생긴다.
    <div className="relative w-full overflow-x-auto">
      <table className={cn("w-full caption-bottom text-[13.5px]", className)} {...props} />
    </div>
  );
}

/** 표 머리 (P81 4절): 떠 있는 면 위의 12.5px 보조 글자. 대문자·자간은 한글 머리에 맞지 않아 두지 않는다 */
export function THead({ className, ...props }: HTMLAttributes<HTMLTableSectionElement>) {
  return (
    <thead
      className={cn(
        "border-b border-line bg-surface-strong text-left text-[12.5px] font-semibold text-muted",
        className,
      )}
      {...props}
    />
  );
}

export function TBody({ className, ...props }: HTMLAttributes<HTMLTableSectionElement>) {
  return <tbody className={cn("divide-y divide-line", className)} {...props} />;
}

export function TR({ className, ...props }: HTMLAttributes<HTMLTableRowElement>) {
  return <tr className={cn("hover:bg-hover", className)} {...props} />;
}

export function TH({ className, scope = "col", ...props }: ThHTMLAttributes<HTMLTableCellElement>) {
  return <th scope={scope} className={cn("h-[38px] px-4 py-2 font-semibold whitespace-nowrap", className)} {...props} />;
}

export function TD({ className, ...props }: TdHTMLAttributes<HTMLTableCellElement>) {
  return <td className={cn("px-4 py-3 align-top", className)} {...props} />;
}

/**
 * 목록의 행 (D44 넷째). 높이 54px 을 행이 정하고 칸의 위아래 여백을 그에 맞춰 줄인다.
 *
 * 자식 선택자라 `TD` 의 기본값(py-3·align-top)을 우선순위로 이긴다. 목록 여덟만 같은 리듬을 갖고
 * 상세·감사처럼 빽빽한 표는 지금 여백을 그대로 쓴다.
 */
export function ListTR({ className, ...props }: HTMLAttributes<HTMLTableRowElement>) {
  return <TR className={cn("h-[54px] [&>td]:py-2 [&>td]:align-middle", className)} {...props} />;
}

/**
 * 이름 칸 (D44 넷째). 표시 이름 아래에 K8s 이름을 고정폭으로 둔다.
 *
 * 둘을 한 줄에 붙여 두었더니 표시 이름이 긴 행에서 뒤 칸이 다 밀렸다. 위아래로 나누면 52px 안에
 * 그대로 들어가고 두 이름이 서로를 가리지 않는다. `children` 은 이름 옆에 서는 배지 자리다.
 */
export function NameTD({
  to,
  item,
  children,
  className,
  ...props
}: { to: string; item: Named; children?: ReactNode } & TdHTMLAttributes<HTMLTableCellElement>) {
  const secondary = secondaryName(item);
  return (
    <TD className={cn("max-w-[20rem]", className)} {...props}>
      <div className="flex items-center gap-2">
        <Link className="truncate font-semibold text-heading underline-offset-[3px] hover:underline" to={to}>
          {displayName(item)}
        </Link>
        {children}
      </div>
      {secondary ? <div className="truncate font-mono text-xs text-muted">{secondary}</div> : null}
    </TD>
  );
}

/**
 * URL 칸 (D44 넷째). 고정폭으로 줄이고 주소가 있을 때만 복사를 붙인다.
 *
 * 잘린 주소는 title 로 전체를 읽는다. 상세로 들어가지 않고 목록에서 바로 주소를 집어 가는 일이
 * 잦아 복사가 칸 안에 있어야 한다. 복사는 글자가 아니라 아이콘이다. 좁은 칸에서 낱말이 붙으면
 * 정작 주소가 더 잘린다.
 */
export function UrlTD({ url, testId, copyTestId }: { url?: string | null; testId?: string; copyTestId?: string }) {
  if (!url) {
    return (
      <TD className="text-dim" data-testid={testId}>
        -
      </TD>
    );
  }
  return (
    <TD className="max-w-xs" data-testid={testId}>
      <span className="flex items-center gap-1.5">
        <span className="truncate font-mono text-xs" title={url}>
          {url}
        </span>
        <CopyButton value={url} testId={copyTestId} />
      </span>
    </TD>
  );
}

/** 오른쪽 끝의 헤더 없는 액션 열(휴지통 등). 폭은 아이콘 하나만큼이다. */
export function ActionTH({ className, ...props }: ThHTMLAttributes<HTMLTableCellElement>) {
  const t = useT();
  return (
    <TH className={cn("w-12 text-right", className)} {...props}>
      <span className="sr-only">{t.ui.table.actions}</span>
    </TH>
  );
}

export function ActionTD({ className, ...props }: TdHTMLAttributes<HTMLTableCellElement>) {
  return <TD className={cn("py-2 text-right whitespace-nowrap", className)} {...props} />;
}

export function EmptyRow({ colSpan, children }: { colSpan: number; children: React.ReactNode }) {
  return (
    <tr>
      <td colSpan={colSpan} className="px-4 py-8 text-center text-sm text-muted">
        {children}
      </td>
    </tr>
  );
}
