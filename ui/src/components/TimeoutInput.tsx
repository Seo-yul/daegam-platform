/**
 * 백엔드 호출 타임아웃(초) 입력칸 (P50 7절). MCP Server 폼과 Gateway 폼이 같이 쓴다. 정수 초만 받고, 비우면 undefined 라
 * 서버나 배포가 기본값을 정한다.
 */
import { Input } from "@/components/ui/input";
import { TIMEOUT_MAX_SECONDS, TIMEOUT_MIN_SECONDS, timeoutFromInput } from "@/lib/timeouts";

export function TimeoutInput({
  id,
  value,
  onChange,
  placeholder,
}: {
  id: string;
  value: number | null | undefined;
  onChange: (seconds: number | undefined) => void;
  placeholder?: string;
}) {
  return (
    <Input
      id={id}
      data-testid={id}
      type="number"
      min={TIMEOUT_MIN_SECONDS}
      max={TIMEOUT_MAX_SECONDS}
      step={1}
      placeholder={placeholder}
      className="w-32"
      value={value ?? ""}
      onChange={(e) => onChange(timeoutFromInput(e.target.value))}
    />
  );
}
