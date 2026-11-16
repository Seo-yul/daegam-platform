/**
 * 스펙을 읽고 쓰는 코드 편집기. Backend API 등록·편집 화면이 쓴다.
 *
 * 문법 색과 접기와 줄 바꿈이 없으면 수백 줄짜리 OpenAPI 를 textarea 에서 읽을 수 없다. Monaco 를 쓰되 번들이
 * 커서 이 컴포넌트가 lazy 로 들여오고, 뜨기 전에는 자리를 잡아 두어 화면이 튀지 않게 한다
 * (`components/AdvancedEditor` 와 같은 규칙).
 *
 * 문법은 YAML 하나다. JSON 은 YAML 의 부분집합이라 같은 편집기에서 색이 맞고, 언어를 하나만 번들해
 * 무게를 아낀다 (`MonacoYaml` 이 코어 + yaml 문법만 싣는다).
 */
import { lazy, Suspense } from "react";

const MonacoYaml = lazy(() => import("./MonacoYaml"));

export function SpecEditor({
  value,
  onChange,
  readOnly,
  placeholder,
  height = "22rem",
  testId,
}: {
  value: string;
  /** 읽기 전용이면 주지 않아도 된다. */
  onChange?: (next: string) => void;
  readOnly?: boolean;
  /** 비어 있을 때 편집기 위에 겹쳐 보이는 글자. Monaco 에는 placeholder 가 없다. */
  placeholder?: string;
  height?: string;
  testId?: string;
}) {
  return (
    <div className="relative">
      <Suspense
        fallback={
          <div className="animate-pulse rounded-md border border-field-line bg-hover" style={{ height }} data-testid={testId ? `${testId}-loading` : undefined} />
        }
      >
        {/* 스펙을 읽고 쓰는 자리라 줄 번호를 쓸 일이 없다. 어디가 문제인지는 검사 결과가 제 자리에서 말한다 */}
        <MonacoYaml value={value} onChange={onChange ?? (() => {})} readOnly={readOnly} height={height} testId={testId} lineNumbers={false} />
      </Suspense>
      {/* 빈 편집기는 무엇을 넣는 곳인지 말해 주지 않는다. 글자는 클릭을 가로채지 않는다 */}
      {placeholder && !value ? (
        <span className="pointer-events-none absolute left-3 top-2 font-mono text-xs text-dim">{placeholder}</span>
      ) : null}
    </div>
  );
}
