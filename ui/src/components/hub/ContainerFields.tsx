/**
 * image 항목의 컨테이너 칸 (P55 5·8절): stdio 선택과 Args, Env, Secret env 의 줄. 카탈로그에서 연 폼은 카탈로그가 그 변수에 단
 * 설명과 꼭 필요하다는 표시를 줄 옆에 보인다. Secret env 의 값은 저장할 때만 가고 다시 보이지 않는다. 폼의 꼴과 요청으로 바꾸기는
 * `lib/hubCatalog` 의 `ContainerForm`·`containerBody` 다.
 */
import { useId } from "react";

import type { HubCatalogServer } from "@/api/types";
import { TextRows } from "@/components/TextRows";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { useT } from "@/i18n";
import { type ContainerForm, type EnvRow, catalogVariable } from "@/lib/hubCatalog";

export function ContainerFields({ value, onChange, server }: { value: ContainerForm; onChange: (next: ContainerForm) => void; server?: HubCatalogServer | null }) {
  const t = useT();
  const text = t.hub.new;
  const set = (patch: Partial<ContainerForm>) => onChange({ ...value, ...patch });
  return (
    <>
      <label className="flex items-start gap-2 text-sm">
        <input type="checkbox" className="mt-1" data-testid="hub-stdio" checked={value.stdio} onChange={(e) => set({ stdio: e.target.checked })} />
        <span>
          {text.stdio}
          <span className="block text-xs text-muted">{text.stdioHint}</span>
        </span>
      </label>
      <TextRows label={text.args} values={value.args} onChange={(args) => set({ args })} testId="hub-arg" inputClassName="font-mono" />
      <EnvRows testId="hub-env" label={text.env} hint={text.envHint} rows={value.env} secret={false} server={server} onChange={(env) => set({ env })} />
      <EnvRows testId="hub-secret-env" label={text.secretEnv} hint={text.secretEnvHint} rows={value.secretEnv} secret server={server} onChange={(secretEnv) => set({ secretEnv })} />
    </>
  );
}

/** Env 나 Secret env 의 줄. 이름과 값이고, 카탈로그 서버가 있으면 그 변수의 설명과 꼭 필요하다는 표시가 줄에 선다. */
function EnvRows({
  testId,
  label,
  hint,
  rows,
  secret,
  server,
  onChange,
}: {
  testId: string;
  label: string;
  hint: string;
  rows: EnvRow[];
  secret: boolean;
  server?: HubCatalogServer | null;
  onChange: (rows: EnvRow[]) => void;
}) {
  const t = useT();
  const labels = t.hub.new.labels;
  const id = useId();
  const change = (i: number, patch: Partial<EnvRow>) => onChange(rows.map((r, j) => (j === i ? { ...r, ...patch } : r)));
  return (
    <fieldset className="flex flex-col gap-1.5" aria-describedby={`${id}-hint`} data-testid={testId}>
      <legend className="mb-1 text-sm font-medium text-fg-soft">{label}</legend>
      <p id={`${id}-hint`} className="text-xs text-muted">
        {hint}
      </p>
      {rows.map((row, i) => {
        const variable = catalogVariable(server, row.name);
        return (
          <div key={i} className="flex flex-col gap-0.5">
            <div className="flex items-center gap-2">
              <Input aria-label={`${label} ${labels.name}`} className="w-48 font-mono" autoComplete="off" data-testid={`${testId}-name-${i}`} value={row.name} onChange={(e) => change(i, { name: e.target.value })} />
              <Input
                aria-label={`${label} ${labels.value}`}
                className="min-w-0 flex-1"
                type={secret ? "password" : "text"}
                autoComplete={secret ? "new-password" : "off"}
                data-testid={`${testId}-value-${i}`}
                value={row.value}
                onChange={(e) => change(i, { value: e.target.value })}
              />
              {variable?.required ? (
                <Badge variant="warn" data-testid={`${testId}-required-${i}`}>
                  {t.detail.openApiDocs.required}
                </Badge>
              ) : null}
              <Button size="sm" variant="ghost" data-testid={`${testId}-remove-${i}`} onClick={() => onChange(rows.filter((_, j) => j !== i))}>
                {t.shared.remove}
              </Button>
            </div>
            {variable?.description ? (
              <p className="text-xs text-muted" data-testid={`${testId}-description-${i}`}>
                {variable.description}
              </p>
            ) : null}
          </div>
        );
      })}
      <div>
        <Button size="sm" variant="outline" data-testid={`${testId}-add`} onClick={() => onChange([...rows, { name: "", value: "" }])}>
          {t.shared.add}
        </Button>
      </div>
    </fieldset>
  );
}
