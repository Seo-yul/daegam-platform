/**
 * OpenRouter provider 의 업체 선호 칸 (T19). OpenRouter 는 같은 모델을 여러 업체가 돌리고 요청마다 하나를
 * 고른다. 여기서 정한 값은 요청 본문의 `provider` 로 실려 어느 업체를 쓸지 정한다. Platform Settings 의
 * provider 줄에서 접힌 칸으로 보인다.
 */
import { Input } from "@/components/ui/input";
import { useT } from "@/i18n";
import { routingSummary, type RoutingForm } from "@/lib/providers";

export function ProviderRoutingFields({ i, value, onChange }: { i: number; value: RoutingForm; onChange: (patch: Partial<RoutingForm>) => void }) {
  const t = useT();
  const text = t.detail.providerRouting;
  return (
    <details className="mt-1 text-[11px]" data-testid={`ps-provider-routing-${i}`}>
      <summary className="cursor-pointer text-muted" data-testid={`ps-provider-routing-summary-${i}`}>
        {routingSummary(t, value)}
      </summary>
      <div className="mt-1 flex min-w-[14rem] flex-col gap-1.5">
        <Input
          aria-label={text.labels.order}
          data-testid={`ps-provider-routing-order-${i}`}
          className="h-7 font-mono text-xs placeholder:font-sans"
          placeholder={text.order}
          value={value.order}
          onChange={(e) => onChange({ order: e.target.value })}
        />
        <label className="flex items-center gap-1.5">
          <input type="checkbox" data-testid={`ps-provider-routing-no-fallback-${i}`} checked={!value.allow_fallbacks} onChange={(e) => onChange({ allow_fallbacks: !e.target.checked })} />
          {text.noFallback}
        </label>
        <label className="flex items-center gap-1.5">
          <input type="checkbox" data-testid={`ps-provider-routing-require-${i}`} checked={value.require_parameters} onChange={(e) => onChange({ require_parameters: e.target.checked })} />
          {text.requireParameters}
        </label>
        <Input
          aria-label={text.labels.quantizations}
          data-testid={`ps-provider-routing-quant-${i}`}
          className="h-7 font-mono text-xs placeholder:font-sans"
          placeholder={text.quantizations}
          value={value.quantizations}
          onChange={(e) => onChange({ quantizations: e.target.value })}
        />
      </div>
    </details>
  );
}
