import type { Binding, DataService } from "@/api/types";
import { Button } from "@/components/ui/button";
import { Input, Select } from "@/components/ui/input";
import { useT } from "@/i18n";

/** Data Service 바인딩 편집. MCP Server 폼과 Basic 편집 폼이 같이 쓴다. */
export function BindingsEditor({
  value,
  onChange,
  dataServices,
  testId = "bindings",
}: {
  value: Binding[];
  onChange: (next: Binding[]) => void;
  dataServices: DataService[];
  testId?: string;
}) {
  const t = useT();
  const text = t.shared.bindingsEditor;
  function update(i: number, patch: Partial<Binding>) {
    onChange(value.map((b, j) => (j === i ? { ...b, ...patch } : b)));
  }
  return (
    <div className="flex flex-col gap-2" data-testid={testId}>
      {value.length === 0 ? <p className="text-xs text-muted">{text.empty}</p> : null}
      {value.map((b, i) => (
        <div key={i} className="grid grid-cols-[1fr_1fr_auto] items-center gap-2" data-testid={`${testId}-row-${i}`}>
          <Select
            aria-label={text.dataService}
            data-testid={`${testId}-ds-${i}`}
            value={b.data_service_id}
            onChange={(e) => update(i, { data_service_id: e.target.value })}
          >
            <option value="">{text.selectDataService}</option>
            {dataServices.map((d) => (
              <option key={d.id} value={d.id}>
                {d.name} ({d.kind})
              </option>
            ))}
          </Select>
          <Input
            aria-label={text.envPrefix}
            data-testid={`${testId}-prefix-${i}`}
            placeholder="ENV_PREFIX"
            value={b.env_prefix}
            onChange={(e) => update(i, { env_prefix: e.target.value.toUpperCase() })}
          />
          <Button size="sm" variant="ghost" data-testid={`${testId}-remove-${i}`} onClick={() => onChange(value.filter((_, j) => j !== i))}>
            {text.remove}
          </Button>
        </div>
      ))}
      <div>
        <Button
          size="sm"
          variant="outline"
          data-testid={`${testId}-add`}
          onClick={() => onChange([...value, { data_service_id: dataServices[0]?.id ?? "", env_prefix: "" }])}
        >
          {text.add}
        </Button>
      </div>
    </div>
  );
}
