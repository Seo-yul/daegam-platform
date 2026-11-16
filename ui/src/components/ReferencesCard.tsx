/**
 * "참조" 카드 (D43 넷째). 이 리소스를 참조하는 리소스 목록이다. 상세 아홉의 개요 탭이 같은 카드를 쓴다.
 *
 * 화면에서 답할 수 없던 질문이 "이걸 바꾸거나 지우면 무엇이 깨지나" 였다. 서버가
 * `GET /{리소스}/{id}/references` 로 한 모양을 주므로 카드도 하나다. 종류별로 묶어 이름을
 * 링크로 보이고, 정의가 붙드는 것과 배포본이 붙드는 것을 갈라 적는다. 읽기 토큰은 갈 곳이
 * 없으므로 개수만 말한다.
 */
import { useQuery } from "@tanstack/react-query";
import { Link2 } from "lucide-react";
import { Link } from "react-router";

import { listReferences } from "@/api/resources";
import type { Reference } from "@/api/types";
import { StateBlock } from "@/components/StateBlock";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { useT } from "@/i18n";
import type { DeletableResource } from "@/lib/deletion";
import { PATH_SEGMENT } from "@/lib/deletion";
import { groupByKind, holdText, pinsBehind, referenceLabel, referencePath, summarizeReferences } from "@/lib/references";
import { referencesKey } from "@/lib/queryKeys";
import { useTenant } from "@/lib/tenant";

/** 상세 아홉과 대화상자가 같은 캐시를 보게 하는 조회 하나. */
export function useReferences(resource: DeletableResource, id: string, enabled = true) {
  const tenant = useTenant();
  return useQuery({
    queryKey: referencesKey(tenant, PATH_SEGMENT[resource], id),
    queryFn: () => listReferences(resource, id),
    enabled: enabled && Boolean(id),
  });
}

export function ReferencesCard({
  resource,
  id,
  title,
  hint,
  latestRevision,
  testId = "references-card",
}: {
  resource: DeletableResource;
  id: string;
  /** Cluster 만 "이 클러스터에 배포된 것" 으로 다르게 부른다. 기본은 사전의 "참조" 다. */
  title?: string;
  hint?: string;
  /** Skill 의 최신 리비전. 주면 그보다 낮게 핀한 행에 배지를 단다. */
  latestRevision?: number;
  testId?: string;
}) {
  const t = useT();
  const text = t.detail.referencesCard;
  const q = useReferences(resource, id);
  return (
    <Card data-testid={testId}>
      <CardHeader>
        <CardTitle>{title ?? text.title}</CardTitle>
        {hint ? <span className="text-xs text-muted">{hint}</span> : null}
      </CardHeader>
      <CardContent>
        <StateBlock
          loading={q.isLoading}
          error={q.error}
          empty={q.data && q.data.length === 0 ? text.empty : undefined}
          illustration={
            /* 빈 자리는 고리 그림 한 칸으로 비어 있음을 보인다. 장식이라 화면 읽기 프로그램에는 문장만 간다 */
            <span aria-hidden className="grid size-10 place-items-center rounded-tile border border-line bg-sunken text-muted">
              <Link2 size={18} />
            </span>
          }
          onRetry={() => void q.refetch()}
        >
          <ReferenceList refs={q.data ?? []} latestRevision={latestRevision} testId={testId} />
        </StateBlock>
      </CardContent>
    </Card>
  );
}

/**
 * 삭제·배포 해제 대화상자 안에 끼우는 참조 목록. 서버가 409 로 막을 때까지 기다리지 않고 지금
 * 무엇이 이것을 붙들고 있는지 먼저 보인다. 열려 있을 때만 받는다.
 */
export function ReferencesNotice({
  resource,
  id,
  open,
  lead,
  testId = "dialog-references",
}: {
  resource: DeletableResource;
  id: string | null | undefined;
  open: boolean;
  /** 목록 앞에 세울 문장. 없으면 종류와 개수를 말한다. */
  lead?: string;
  testId?: string;
}) {
  const t = useT();
  const q = useReferences(resource, id ?? "", open && Boolean(id));
  const refs = q.data ?? [];
  if (!open || !refs.length) return null;
  return (
    <div className="mt-1 rounded-chip border border-warn-line bg-warn-soft px-3 py-2 text-xs text-warn-fg" data-testid={testId}>
      <div className="mb-1">{lead ?? t.detail.referencesCard.notice(summarizeReferences(t, refs))}</div>
      <ReferenceList refs={refs} testId={testId} />
    </div>
  );
}

/** 목록만. 대화상자는 카드 껍데기 없이 이것만 쓴다. */
export function ReferenceList({ refs, latestRevision, testId = "references" }: { refs: Reference[]; latestRevision?: number; testId?: string }) {
  const t = useT();
  if (!refs.length) return null;
  return (
    <dl className="grid grid-cols-[9rem_1fr] gap-y-2 text-sm" data-testid={`${testId}-list`}>
      {groupByKind(refs).map((group) => (
        <div key={group.kind} className="contents">
          <dt className="text-muted">{referenceLabel(t, group.kind)}</dt>
          <dd>
            {group.kind === "token" ? (
              <span data-testid={`${testId}-token`}>{t.detail.referencesCard.tokens(group.items.length)}</span>
            ) : (
              <ul className="flex flex-col gap-1">
                {group.items.map((ref) => (
                  <li key={ref.id} className="flex flex-wrap items-center gap-2">
                    <ReferenceName reference={ref} testId={testId} />
                    {ref.state ? (
                      <Badge variant="muted" data-testid={`${testId}-state-${ref.id}`}>
                        {ref.state}
                      </Badge>
                    ) : null}
                    {/* Skill 참조는 어느 쪽이 붙드는지보다 몇 번 리비전을 핀했는지가 쓸모 있다 (D64) */}
                    <span className="text-xs text-muted" data-testid={`${testId}-revision-${ref.id}`}>
                      {holdText(t, ref)}
                    </span>
                    {ref.pins && latestRevision !== undefined && pinsBehind(ref.pins, latestRevision) ? (
                      <Badge variant="warn" data-testid={`${testId}-behind-${ref.id}`}>
                        {t.skills.newerRevision(latestRevision)}
                      </Badge>
                    ) : null}
                  </li>
                ))}
              </ul>
            )}
          </dd>
        </div>
      ))}
    </dl>
  );
}

function ReferenceName({ reference, testId }: { reference: Reference; testId: string }) {
  const path = referencePath(reference.kind, reference.id);
  const name = reference.display_name || reference.name;
  if (!path) return <span>{name}</span>;
  return (
    <Link className="underline-offset-2 hover:underline" to={path} data-testid={`${testId}-link-${reference.id}`}>
      {name}
    </Link>
  );
}
