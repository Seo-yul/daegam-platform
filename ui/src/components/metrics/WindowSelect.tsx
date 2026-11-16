/** 메트릭 탭의 기간 고르개. 탭의 수가 모두 이 기간 안의 것이다. 기간의 목록과 이름은 탭마다 다르다. */
import { Field, Select } from "@/components/ui/input";
import { useT } from "@/i18n";

export function WindowSelect<W extends string>({
  windows,
  names,
  value,
  onChange,
  testId,
}: {
  windows: readonly W[];
  names: Readonly<Record<W, string>>;
  value: W;
  onChange: (w: W) => void;
  testId: string;
}) {
  const t = useT();
  return (
    <div className="flex items-end justify-end">
      <Field label={t.detail.windowSelect.label} htmlFor={testId}>
        <Select id={testId} className="h-8 w-40" data-testid={testId} value={value} onChange={(e) => onChange(windows.find((w) => w === e.target.value) ?? value)}>
          {windows.map((w) => (
            <option key={w} value={w}>
              {names[w]}
            </option>
          ))}
        </Select>
      </Field>
    </div>
  );
}
