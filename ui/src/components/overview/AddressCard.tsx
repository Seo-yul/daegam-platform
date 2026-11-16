/**
 * "주소" 카드 (D44 셋째). 개요 탭의 첫 카드다.
 *
 * 올려 놓고 나서 사람이 제일 먼저 찾는 것이 "그래서 어디로 부르면 되나" 인데, 그 값은 K8s
 * conditions 표 아래 `url` 한 줄에 묻혀 있었다. 카드 하나로 앞에 세우고 안 주소와 밖 주소, 그 둘을 부를 때
 * 필요한 인증을 나란히 적는다. 주소는 오목한 칸에 복사 단추와 함께 서고, 덩이 사이는 점선이 가른다 (P81 6절).
 *
 * 밖 주소는 `ExposureStatus` 의 `ExternalUrl` 을 그대로 쓴다. "외부 노출 안 함" 문구와 testid 가
 * 노출 테스트의 계약이라 여기서 다시 쓰지 않는다. 노출이 꺼져 있을 때만 켜러 갈 곳을 옆에 붙인다.
 */
import type { ReactNode } from "react";

import { ExternalUrl } from "@/components/ExposureStatus";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { CopyButton } from "@/components/ui/copy-button";
import { useT } from "@/i18n";
import { cn } from "@/lib/utils";

export interface AddressCardProps {
  /** 클러스터 안 주소. 플랫폼이 발급한 서비스 토큰이 있어야 부를 수 있다. */
  url: string | null | undefined;
  /** 안 주소의 이름. MCP Server·Gateway 는 엔드포인트고 Agent 는 채팅 주소다. 기본은 사전의 "클러스터 안 주소" 다. */
  innerLabel?: string;
  /** 안 주소를 어떻게 부르는지 한 줄. */
  innerHint?: ReactNode;
  externalUrl: string | null | undefined;
  /** 노출이 꺼져 있을 때 켜러 갈 곳. 없으면 안내만 남는다. */
  onExpose?: () => void;
  /** 인증 방식 한 줄. 도메인마다 경계가 달라 페이지가 적는다. */
  auth?: ReactNode;
  /** 노출 배지 묶음(`ExposureBadges`). 노출을 껐으면 null 이라 머리가 비어 있다. */
  badges?: ReactNode;
  testId?: string;
  urlTestId?: string;
  copyTestId?: string;
  externalTestId?: string;
  externalCopyTestId?: string;
}

/** 값 한 덩이의 이름표. 카드 제목보다 한 단계 작은 보조 글자다 (P81 6절). */
function RowLabel({ children }: { children: ReactNode }) {
  return <div className="text-[12.5px] font-medium text-muted">{children}</div>;
}

/** 주소 한 줄을 담는 오목한 칸. 복사 단추는 칸의 오른쪽 끝에 붙는다. */
const ADDRESS_FIELD = "mt-1.5 flex min-h-[38px] items-center gap-2 rounded-tile border border-line bg-sunken py-1 pr-1.5 pl-3";

/** 덩이 사이의 점선. 첫 덩이는 위에 선이 없다. */
const GROUP = "border-dashed border-line not-first:border-t not-first:pt-4";

export function AddressCard({
  url,
  innerLabel,
  innerHint,
  externalUrl,
  onExpose,
  auth,
  badges,
  testId = "address-card",
  urlTestId = "status-url",
  copyTestId = "copy-url",
  externalTestId,
  externalCopyTestId,
}: AddressCardProps) {
  const t = useT();
  const text = t.detail.addressCard;
  return (
    <Card data-testid={testId}>
      <CardHeader>
        <CardTitle>{text.title}</CardTitle>
        {badges}
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        <div className={GROUP}>
          <RowLabel>{innerLabel ?? text.inner}</RowLabel>
          <div className={ADDRESS_FIELD}>
            <span className="min-w-0 flex-1 font-mono text-xs break-all text-heading" data-testid={urlTestId}>
              {url ?? "-"}
            </span>
            {url ? <CopyButton value={url} label={text.innerCopy} testId={copyTestId} /> : null}
          </div>
          {innerHint ? <p className="mt-1.5 text-xs text-muted">{innerHint}</p> : null}
        </div>
        <div className={GROUP}>
          <RowLabel>{text.external}</RowLabel>
          <div className={externalUrl ? cn(ADDRESS_FIELD, "[&>span]:flex-1 [&>span]:justify-between") : "mt-1.5 flex flex-wrap items-center gap-2"}>
            <ExternalUrl url={externalUrl} testId={externalTestId} copyTestId={externalCopyTestId} />
            {!externalUrl && onExpose ? (
              <button
                type="button"
                className="rounded-item text-xs text-link underline underline-offset-2 hover:text-link-hover focus-visible:ring-2 focus-visible:ring-focus-ring focus-visible:outline-none"
                data-testid="address-expose"
                onClick={onExpose}
              >
                {text.expose}
              </button>
            ) : null}
          </div>
        </div>
        {auth ? (
          <div className={GROUP}>
            <RowLabel>{text.auth}</RowLabel>
            <p className="mt-1.5 text-xs text-muted" data-testid="address-auth">
              {auth}
            </p>
          </div>
        ) : null}
      </CardContent>
    </Card>
  );
}
