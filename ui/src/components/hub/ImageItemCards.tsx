/**
 * image 항목 상세의 두 카드 (P55 8절). 컨테이너 카드는 stdio, args, env, Secret env 이름과 값이 있는지를 보이고, 만든 사람과
 * tenant-admin 에게(`permissions.replace_secret_env`) "Secret 값 바꾸기" 를 연다. 카탈로그 출처는 종류와 상관없이 요약 카드에 있다.
 * 허용 카드는 허브 키가 이 항목을 허용한 기록(`source.admission`)이고, 허브 이미지 설정이 켜진 뒤 만든 항목에만 있다.
 */
import { useState } from "react";

import { replaceHubSecretEnv } from "@/api/resources";
import type { HubAdmission, HubItem } from "@/api/types";
import { ReplaceValuesDialog } from "@/components/hub/ReplaceValuesDialog";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { useT } from "@/i18n";
import { useUserLabel } from "@/lib/userLabel";
import { formatDate, ownValue } from "@/lib/utils";

/** 실행 기록(`dev.daegam.hub.run`)을 서명에 적힌 꼴, 공백으로 이은 `key=value` 로 (6절). */
export function runText(run: HubAdmission["run"]): string {
  return Object.entries(run)
    .map(([k, v]) => `${k}=${v}`)
    .join(" ");
}

export function ImageContainerCard({ item }: { item: HubItem }) {
  const t = useT();
  const text = t.hub.detail;
  const userLabel = useUserLabel();
  const [open, setOpen] = useState(false);
  if (item.kind !== "image") return null;
  const source = item.source;
  const secrets = source.secret_env ?? [];
  const noneSet = !secrets.some((e) => e.set);
  return (
    <Card data-testid="hub-image-card">
      <CardHeader>
        <CardTitle>{t.hub.new.image}</CardTitle>
        {item.permissions.replace_secret_env && secrets.length ? (
          <Button variant="secondary" data-testid="replace-secrets-button" onClick={() => setOpen(true)}>
            {text.replaceSecrets}
          </Button>
        ) : null}
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        <dl className="grid grid-cols-[9rem_1fr] gap-y-1 text-sm">
          <dt className="text-muted">{text.labels.stdio}</dt>
          <dd data-testid="hub-image-stdio">{source.stdio ? t.detail.summaryFields.yes : t.detail.summaryFields.no}</dd>
          {source.args?.length ? (
            <>
              <dt className="text-muted">{text.labels.args}</dt>
              <dd className="flex flex-wrap gap-1" data-testid="hub-image-args">
                {source.args.map((arg, i) => (
                  <code key={i} className="rounded-chip bg-sunken px-1.5 font-mono text-xs">
                    {arg}
                  </code>
                ))}
              </dd>
            </>
          ) : null}
          {source.env?.length ? (
            <>
              <dt className="text-muted">{text.labels.env}</dt>
              <dd className="flex flex-col font-mono text-xs" data-testid="hub-image-env">
                {source.env.map((e) => (
                  <span key={e.name} className="break-all">
                    {e.name}={e.value}
                  </span>
                ))}
              </dd>
            </>
          ) : null}
          {secrets.length ? (
            <>
              <dt className="text-muted">{text.labels.secretEnv}</dt>
              <dd className="flex flex-col gap-0.5" data-testid="hub-image-secret-env">
                {secrets.map((e) => (
                  <span key={e.name} className="flex items-center gap-2">
                    <span className="font-mono text-xs">{e.name}</span>
                    <Badge variant={e.set ? "ok" : "warn"} data-testid={`hub-secret-${e.name}`}>
                      {e.set ? text.secretSet.set : text.secretSet.unset}
                    </Badge>
                  </span>
                ))}
              </dd>
            </>
          ) : null}
        </dl>
        {item.secret_env_updated_by && item.secret_env_updated_at ? (
          <p className="text-xs text-muted" data-testid="hub-secrets-replaced">
            {text.replacedBy(userLabel(item.secret_env_updated_by), formatDate(t, item.secret_env_updated_at))}
          </p>
        ) : null}
      </CardContent>
      {/* 열려 있는 동안만 둔다. 닫힌 대화상자에 값이나 오류가 남지 않는다. 아직 값이 하나도 없으면 Secret 이 없어 서버가 모든
          이름의 값을 받아야 만든다(409 secret_missing). 그때는 칸이 모두 필수이고 안내도 그 말이다 */}
      {open ? (
        <ReplaceValuesDialog
          item={item}
          title={text.replaceSecrets}
          hint={noneSet ? text.replaceSecretsAllRequired : text.replaceSecretsHint}
          fields={secrets.map((e) => ({ name: e.name, label: e.name, required: noneSet, testId: `replace-secret-${e.name}` }))}
          saveLabel={text.replaceSecretsSave}
          testId="replace-secrets"
          save={(values) => replaceHubSecretEnv(item.id, values)}
          onClose={() => setOpen(false)}
        />
      ) : null}
    </Card>
  );
}

export function AdmissionCard({ item }: { item: HubItem }) {
  const t = useT();
  const text = t.hub.detail;
  const userLabel = useUserLabel();
  const a = item.source.admission;
  if (!a) return null;
  return (
    <Card data-testid="hub-admission-card">
      <CardHeader>
        <CardTitle>{text.admissionTitle}</CardTitle>
        <Badge variant={a.provenance === "verified" ? "ok" : "info"} data-testid="hub-admission-provenance">
          {ownValue(text.admission, a.provenance) ?? a.provenance}
        </Badge>
      </CardHeader>
      <CardContent>
        <dl className="grid grid-cols-[9rem_1fr] gap-y-1 text-sm">
          <dt className="text-muted">{text.labels.digest}</dt>
          <dd className="break-all font-mono text-xs" data-testid="hub-admission-digest">
            {a.digest}
          </dd>
          <dt className="text-muted">{text.labels.upstream}</dt>
          <dd className="break-all font-mono text-xs" data-testid="hub-admission-upstream">
            {a.upstream}
          </dd>
          <dt className="text-muted">{text.labels.run}</dt>
          <dd className="break-all font-mono text-xs" data-testid="hub-admission-run">
            {runText(a.run)}
          </dd>
          <dt className="text-muted">{text.labels.admittedAt}</dt>
          <dd data-testid="hub-admission-at">
            {formatDate(t, a.at)} <span className="text-xs text-muted">{t.shared.byUser(userLabel(a.by))}</span>
          </dd>
        </dl>
      </CardContent>
    </Card>
  );
}
