/**
 * 허브 이미지 카드 (P55 6·8절). Platform Settings 아래에 놓인다.
 *
 * 레지스트리 경로(허브 접두사), 허브 키의 공개 키, 회수한 키의 지문, 거부한 digest 를 이 카드의 단추 하나가 저장한다. 바뀐 것이
 * 있을 때만 Platform Settings PATCH(`hub_images`)를 보내고, 읽기 자격 증명은 그 뒤에 따로인 경로로 관리 Secret 에 넣는다(D56).
 * 경로의 호스트가 바뀌면 서버가 옛 자격 증명을 지우므로 PATCH 가 먼저다. 저장 단추와 고친 값은 웹 Tool 카드처럼 서버의 이 절이
 * 바뀔 때만 다시 채우고, 입력한 자격 증명은 저장이 끝나야 지운다. 그래서 자격 증명 넣기가 실패해도 오류와 입력이 남는다. 칸 아래의
 * 계산한 값(공개 키의 지문, 거부한 digest 를 쓰는 항목, 제품 이미지 경로와 겹치는 클러스터)은 모든 테넌트를 가로지르므로 플랫폼
 * 관리자만 읽는다.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";

import { getHubImagesStatus, patchPlatformSettings, putHubImagesCredential } from "@/api/resources";
import type { HubImagesSettings, HubImagesStatus, PlatformSettings, PlatformSettingsPatch } from "@/api/types";
import { ErrorMessage } from "@/components/ErrorMessage";
import { TextRows } from "@/components/TextRows";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Alert, Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Field, Input } from "@/components/ui/input";
import { useT } from "@/i18n";
import { HUB_CATALOG_KEY, HUB_IMAGES_STATUS_KEY, PLATFORM_SETTINGS_KEY } from "@/lib/queryKeys";

interface HubImagesForm {
  prefix: string;
  publicKeys: string[];
  revokedKeys: string[];
  refusedDigests: string[];
  username: string;
  password: string;
}

type HubImagesPatch = NonNullable<PlatformSettingsPatch["hub_images"]>;

function formOf(hub: HubImagesSettings): Omit<HubImagesForm, "username" | "password"> {
  return {
    prefix: hub.prefix ?? "",
    publicKeys: [...(hub.public_keys ?? [])],
    revokedKeys: [...(hub.revoked_keys ?? [])],
    refusedDigests: [...(hub.refused_digests ?? [])],
  };
}

/** 폼 → `hub_images` PATCH. 빈 줄은 뺀다. 저장된 값과 같으면 null 이라 보내지 않는다. 자격 증명은 PATCH 에 실리지 않는다. */
export function hubImagesPatch(hub: HubImagesSettings, form: HubImagesForm): HubImagesPatch | null {
  const lines = (rows: string[]) => rows.map((r) => r.trim()).filter(Boolean);
  const next = { prefix: form.prefix.trim() || null, public_keys: lines(form.publicKeys), revoked_keys: lines(form.revokedKeys), refused_digests: lines(form.refusedDigests) };
  const saved = { prefix: hub.prefix ?? null, public_keys: hub.public_keys ?? [], revoked_keys: hub.revoked_keys ?? [], refused_digests: hub.refused_digests ?? [] };
  return JSON.stringify(next) === JSON.stringify(saved) ? null : next;
}

export function HubImagesCard({ orig, readOnly }: { orig: PlatformSettings; readOnly: boolean }) {
  const t = useT();
  const text = t.admin.platformSettings.hubImages;
  const queryClient = useQueryClient();
  const hub = orig.hub_images;
  const [form, setForm] = useState<HubImagesForm>(() => ({ ...formOf(hub), username: "", password: "" }));
  // 이 절이 서버에서 바뀌었을 때만 칸을 다시 채운다. 입력한 자격 증명은 남긴다
  const own = JSON.stringify(hub);
  useEffect(() => setForm((f) => ({ ...formOf(hub), username: f.username, password: f.password })), [own]); // eslint-disable-line react-hooks/exhaustive-deps
  // 플랫폼 관리자만 읽는다. 읽기 전용 화면은 부르지 않아 403 을 띄우지 않는다
  const status = useQuery({ queryKey: HUB_IMAGES_STATUS_KEY, queryFn: getHubImagesStatus, enabled: !readOnly });
  const patch = hubImagesPatch(hub, form);
  const credential = Boolean(form.username.trim() && form.password);
  // 자격 증명은 사용자 이름과 비밀번호가 함께 있어야 넣는다. 하나만 있으면 저장하지 않는다
  const partial = Boolean(form.username.trim()) !== Boolean(form.password);
  const save = useMutation({
    mutationFn: async () => {
      if (patch) await patchPlatformSettings({ hub_images: patch });
      if (credential) await putHubImagesCredential(form.username.trim(), form.password);
    },
    onSuccess: async () => {
      setForm((f) => ({ ...f, username: "", password: "" }));
      await Promise.all([PLATFORM_SETTINGS_KEY, HUB_IMAGES_STATUS_KEY, HUB_CATALOG_KEY].map((queryKey) => queryClient.invalidateQueries({ queryKey })));
    },
    // PATCH 는 됐는데 자격 증명 넣기에서 멈췄을 수 있다. 서버 상태를 다시 읽어 카드가 그것을 보이게 한다
    onError: () => queryClient.invalidateQueries({ queryKey: PLATFORM_SETTINGS_KEY }),
  });
  const set = (next: Partial<HubImagesForm>) => setForm((f) => ({ ...f, ...next }));
  const s = status.data;
  return (
    <Card className="mt-4" data-testid="ps-hub-images">
      <CardHeader>
        <CardTitle>{text.title}</CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        <p className="text-xs text-muted">{text.hint}</p>
        <ErrorMessage error={status.error} testId="ps-hub-images-status-error" />
        {s?.overlaps.map((o) => (
          <Alert key={o.cluster_id} variant="warn" data-testid="ps-hub-images-overlap">
            {text.overlapWarning(o.cluster_name)}
          </Alert>
        ))}
        <form
          className="flex flex-col gap-3"
          onSubmit={(e) => {
            e.preventDefault();
            if (!partial) save.mutate();
          }}
        >
          <fieldset className="contents" disabled={readOnly}>
            <Field label={text.prefix} hint={text.prefixHint} htmlFor="ps-hub-prefix">
              <Input id="ps-hub-prefix" data-testid="ps-hub-prefix" className="font-mono" autoComplete="off" value={form.prefix} onChange={(e) => set({ prefix: e.target.value })} />
            </Field>
            <TextRows
              testId="ps-hub-key"
              label={text.publicKeys}
              hint={text.publicKeysHint}
              values={form.publicKeys}
              multiline
              inputClassName="font-mono text-xs"
              onChange={(publicKeys) => set({ publicKeys })}
              footer={s?.keys.length ? <KeyFingerprints status={s} /> : null}
            />
            <TextRows testId="ps-hub-revoked" label={text.revokedKeys} hint={text.revokedKeysHint} values={form.revokedKeys} inputClassName="font-mono" onChange={(revokedKeys) => set({ revokedKeys })} />
            <TextRows
              testId="ps-hub-refused"
              label={text.refused}
              hint={text.refusedHint}
              values={form.refusedDigests}
              inputClassName="font-mono"
              onChange={(refusedDigests) => set({ refusedDigests })}
              note={(digest) => <RefusedUsage status={s} digest={digest.trim()} />}
            />
            <Field label={text.credential} hint={text.credentialHint} error={partial ? text.credentialBoth : null} htmlFor="ps-hub-username">
              <div className="flex flex-wrap items-center gap-2">
                <Input id="ps-hub-username" aria-label={text.username} placeholder={text.username} className="w-48" autoComplete="off" data-testid="ps-hub-username" value={form.username} onChange={(e) => set({ username: e.target.value })} />
                <Input
                  aria-label={text.password}
                  placeholder={hub.credential_set ? t.admin.platformSettings.provider.keyReplace : text.password}
                  className="w-48"
                  type="password"
                  autoComplete="new-password"
                  data-testid="ps-hub-password"
                  value={form.password}
                  onChange={(e) => set({ password: e.target.value })}
                />
                <Badge variant={hub.credential_set ? "ok" : "muted"} data-testid="ps-hub-credential-state">
                  {hub.credential_set ? text.credentialSet : text.credentialUnset}
                </Badge>
              </div>
            </Field>
            <ErrorMessage error={save.error} testId="ps-hub-images-error" />
            <div className="flex items-center gap-3">
              <Button type="submit" size="sm" disabled={save.isPending || partial || (!patch && !credential)} data-testid="ps-hub-images-save">
                {save.isPending ? t.shared.saving : t.shared.save}
              </Button>
              {save.isSuccess ? <span className="text-xs text-muted">{t.shared.saved}</span> : null}
            </div>
          </fieldset>
        </form>
      </CardContent>
    </Card>
  );
}

/** 저장된 공개 키의 지문. 회수한 지문에는 표시를 단다. */
function KeyFingerprints({ status }: { status: HubImagesStatus }) {
  const t = useT();
  return (
    <ul className="flex flex-col gap-0.5" data-testid="ps-hub-fingerprints">
      {status.keys.map((k) => (
        <li key={k.fingerprint} className="flex items-center gap-2">
          <span className="break-all font-mono text-[11px] text-muted">{k.fingerprint}</span>
          {k.revoked ? <Badge variant="warn">{t.admin.platformSettings.hubImages.revokedKeys}</Badge> : null}
        </li>
      ))}
    </ul>
  );
}

/** 거부한 digest 하나를 쓰는 항목. 저장된 digest 에만 계산한 값이 있다. */
function RefusedUsage({ status, digest }: { status: HubImagesStatus | undefined; digest: string }) {
  const t = useT();
  const usage = status?.refused.find((r) => r.digest === digest);
  if (!usage?.items.length) return null;
  return (
    <p className="text-xs text-warn-fg" data-testid={`ps-hub-refused-usage-${digest}`}>
      {t.admin.platformSettings.hubImages.refusedUsage(usage.items.length)}{" "}
      <span className="font-mono text-muted">{usage.items.map((i) => `${i.tenant}/${i.name}`).join(", ")}</span>
    </p>
  );
}
