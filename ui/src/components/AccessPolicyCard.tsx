/**
 * 접근 정책 카드 (docs/guide/hub-gateway.md 7절, D23). 허브 항목과 Skill(D64) 상세의 접근 탭이 같이 쓴다.
 *
 * manage 가 있으면 편집 폼이고 없으면 읽기 전용 요약이다. 저장 응답은 부르는 쪽이 상세 캐시에 넣어 permissions·scope 가
 * 바로 바뀌게 한다. 두 리소스가 다른 것은 동작의 뜻과 넓히는 scope, 다른 테넌트를 넣을 수 있는 동작, 항목을 가진 테넌트를 부르는
 * 말뿐이라 그것은 편집기 인자(`editor`)로 받는다.
 */
import { useMutation } from "@tanstack/react-query";
import { useEffect, useState, type ReactNode } from "react";

import { HUB_ACTIONS, type HubAccess, type HubAccessInput, type HubPermissions } from "@/api/types";
import { AccessPolicyEditor, type AccessPolicyEditorProps } from "@/components/AccessPolicyEditor";
import { ErrorMessage } from "@/components/ErrorMessage";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Alert, Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { useT } from "@/i18n";
import { describeSubjects, normalizeAccess, scopeHint } from "@/lib/hubAccess";
import { formatDate } from "@/lib/utils";

/** 정책을 가진 리소스가 함께 갖는 필드. 허브 항목과 Skill 이 같은 이름으로 낸다. */
export interface AccessSubject {
  id: string;
  scope: "tenant" | "shared";
  access: HubAccess;
  permissions: HubPermissions;
  access_updated_at: string | null;
  access_updated_by: string | null;
}

export type AccessEditorOptions = Pick<AccessPolicyEditorProps, "canShare" | "crossTenant" | "tenantActions" | "labels" | "shareScope" | "ownerLabel">;

export function AccessPolicyCard<T extends AccessSubject>({
  item,
  save: saveAccess,
  onSaved,
  editor,
  testId,
  notice,
}: {
  item: T;
  save: (access: HubAccessInput) => Promise<T>;
  /** 저장 응답을 받는다. 상세 캐시를 바꾸고 목록을 무효화하는 자리다. */
  onSaved: (updated: T) => Promise<void> | void;
  editor: AccessEditorOptions;
  /** 카드 머리의 testid 앞머리("hub" 면 `hub-access-card`). 폼 안의 testid 는 리소스와 무관하게 `access-*` 다. */
  testId: string;
  /** 편집기 위의 안내. 편집 중인 정책을 받는다 (header 항목의 사본 안내, P86 6.2절). */
  notice?: (draft: HubAccess) => ReactNode;
}) {
  const t = useT();
  const text = t.shared.accessPolicyCard;
  const labels = editor.labels ?? t.hub.access.actions;
  const owner = editor.ownerLabel ?? t.hub.access.owner;
  const [access, setAccess] = useState<HubAccess>(() => normalizeAccess(item.access));
  useEffect(() => {
    setAccess(normalizeAccess(item.access));
  }, [item.id, item.access_updated_at]); // eslint-disable-line react-hooks/exhaustive-deps

  const save = useMutation({ mutationFn: () => saveAccess(access), onSuccess: onSaved });

  return (
    <Card data-testid={`${testId}-access-card`}>
      <CardHeader>
        <CardTitle>{text.title}</CardTitle>
        <div className="flex items-center gap-2 text-xs text-muted">
          <Badge variant={item.scope === "shared" ? "accent" : "muted"} data-testid={`${testId}-access-scope`}>
            {item.scope}
          </Badge>
          <span data-testid={`${testId}-access-scope-hint`}>{scopeHint(t, item.scope, owner)}</span>
          <span className="flex items-center gap-1" data-testid={`${testId}-permissions`}>
            {HUB_ACTIONS.map((a) => (
              <Badge key={a} variant={item.permissions[a] ? "ok" : "muted"} data-testid={`${testId}-permission-${a}`}>
                {a}
              </Badge>
            ))}
          </span>
          {item.access_updated_at ? (
            <span data-testid={`${testId}-access-updated`}>
              {text.updated} {formatDate(t, item.access_updated_at)}
              {item.access_updated_by ? ` ${t.shared.byUser(item.access_updated_by)}` : ""}
            </span>
          ) : null}
        </div>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        {item.permissions.manage ? (
          <form
            className="flex flex-col gap-3"
            data-testid="access-form"
            onSubmit={(e) => {
              e.preventDefault();
              save.mutate();
            }}
          >
            {notice?.(access)}
            <AccessPolicyEditor value={access} onChange={setAccess} {...editor} disabled={save.isPending} testId="access" />
            {/* 지금까지 뜻을 읽은 것은 정책을 못 바꾸는 사람뿐이었다. 바꾸는 사람에게도 같은 문장을 준다 */}
            <div className="rounded-chip border border-line bg-surface px-3 py-2 text-xs" data-testid="access-preview">
              <div className="mb-1 font-medium text-heading">{text.preview}</div>
              <dl className="grid grid-cols-[5rem_1fr] gap-y-0.5">
                {HUB_ACTIONS.map((a) => (
                  <div key={a} className="contents">
                    <dt className="text-muted">{labels[a].title}</dt>
                    <dd data-testid={`access-preview-${a}`}>
                      {describeSubjects(t, a, access[a], owner)}
                      <span className="text-muted"> · {labels[a].hint}</span>
                    </dd>
                  </div>
                ))}
              </dl>
            </div>
            <ErrorMessage error={save.error} testId="access-error" />
            {save.isSuccess ? (
              <Alert variant="ok" data-testid="access-saved">
                {text.saved(save.data.scope, scopeHint(t, save.data.scope, owner))}
              </Alert>
            ) : null}
            <div className="flex justify-end">
              <Button type="submit" data-testid="access-save" disabled={save.isPending}>
                {save.isPending ? t.shared.saving : text.save}
              </Button>
            </div>
          </form>
        ) : (
          <dl className="grid grid-cols-[6rem_1fr] gap-y-1 text-sm" data-testid="access-readonly">
            {HUB_ACTIONS.map((a) => (
              <div key={a} className="contents">
                <dt className="text-muted">{labels[a].title}</dt>
                <dd data-testid={`access-readonly-${a}`}>{describeSubjects(t, a, normalizeAccess(item.access)[a], owner)}</dd>
              </div>
            ))}
            <dt className="text-muted"></dt>
            <dd className="text-xs text-muted">{text.manageRequired}</dd>
          </dl>
        )}
      </CardContent>
    </Card>
  );
}
