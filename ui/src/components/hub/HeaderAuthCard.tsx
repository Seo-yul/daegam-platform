import { useState } from "react";

import { setHubHeaderValue } from "@/api/resources";
import type { HubItem } from "@/api/types";
import { ReplaceValuesDialog } from "@/components/hub/ReplaceValuesDialog";
import { Button } from "@/components/ui/button";
import { Alert, Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { useT } from "@/i18n";
import { formatDate } from "@/lib/utils";

/**
 * header 항목의 인증 카드 (P86 6.1·6.3절). 헤더 이름, 값이 있는지, 플랫폼이 값을 실어 마지막으로 불러 본 답을 보인다. 값 바꾸기는
 * 서버가 판정한 `permissions.replace_header` 가 열고, 값은 다시 보이지 않는다.
 */
export function HeaderAuthCard({ item }: { item: HubItem }) {
  const t = useT();
  const text = t.hub.detail;
  const [open, setOpen] = useState(false);
  const auth = item.auth;
  if (!auth || auth.kind !== "header") return null;
  const outcome = auth.check?.outcome;
  return (
    <Card data-testid="hub-auth-card">
      <CardHeader>
        <CardTitle>{t.hub.new.labels.auth}</CardTitle>
        {item.permissions.replace_header ? (
          <Button variant="secondary" data-testid="replace-header-button" onClick={() => setOpen(true)}>
            {text.auth.replaceHeader}
          </Button>
        ) : null}
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        <dl className="grid grid-cols-[9rem_1fr] gap-y-1 text-sm">
          <dt className="text-muted">{text.labels.auth}</dt>
          <dd className="font-mono text-xs">{auth.kind}</dd>
          <dt className="text-muted">{text.labels.headerName}</dt>
          <dd className="font-mono text-xs" data-testid="hub-header-name">
            {auth.header_name}
          </dd>
          <dt className="text-muted">{text.labels.headerSet}</dt>
          <dd data-testid="hub-header-set">{auth.set ? t.detail.summaryFields.yes : t.detail.summaryFields.no}</dd>
          <dt className="text-muted">{text.labels.check}</dt>
          <dd data-testid="hub-header-check">
            {auth.check ? (
              <>
                <span className="font-mono text-xs">{auth.check.outcome}</span> <span className="text-xs text-muted">{formatDate(t, auth.check.at)}</span>
              </>
            ) : (
              "-"
            )}
          </dd>
        </dl>
        {outcome === "header_refused" ? (
          <Alert variant="warn" data-testid="hub-header-refused">
            {text.auth.checkRefused}
          </Alert>
        ) : null}
        {outcome === "unreachable" ? (
          <Alert variant="warn" data-testid="hub-header-unreachable">
            {text.auth.checkUnreachable}
          </Alert>
        ) : null}
      </CardContent>
      {/* 열려 있는 동안만 둔다. 닫힌 대화상자에 값이나 오류가 남지 않는다 */}
      {open ? (
        <ReplaceValuesDialog
          item={item}
          title={text.auth.replaceHeader}
          fields={[{ name: "value", label: t.hub.new.labels.headerValue, hint: t.hub.new.headerValueHint, required: true, testId: "replace-header-value" }]}
          saveLabel={t.shared.save}
          testId="replace-header"
          save={(values) => setHubHeaderValue(item.id, values.value)}
          onClose={() => setOpen(false)}
        />
      ) : null}
    </Card>
  );
}
