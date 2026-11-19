import { ApiError } from "@/api/client";
import { Alert } from "@/components/ui/card";
import { type Messages, useT } from "@/i18n";
import { gateReason } from "@/lib/codeExecution";
import { errorText, REF_KEYS, refLines, type ReferenceNames } from "@/lib/errorText";

/**
 * 화면에 보일 오류. text 만으로 무엇이 잘못됐고 무엇을 하면 되는지 알 수 있어야 한다.
 *
 * detail 과 code 는 text 를 못 만들었을 때의 뒷받침이다. 사람이 읽는 문장에 섞지 않고 따로 두어
 * 화면 구석에 작게 붙인다.
 */
export interface DescribedError {
  /** 한국어 한 문장. 오류 코드도 영어 원문도 들어가지 않는다. */
  text: string;
  /** 표에 없는 코드일 때 서버가 준 영어 detail. 표에 있으면 없다. */
  detail?: string;
  /** 서버 오류 코드. 디버깅용이고 문장의 일부가 아니다. */
  code?: string;
  /** 409 참조 목록. "MCP Server 주문" 처럼 이름이고, 이름을 모르면 "MCP Server 2개" 다. code mode 의 422 는 걸린 이름이다. */
  refs: string[];
}

/**
 * code mode 검사의 422 가 싣는 이름 (P56 6.2절). `names` 는 걸린 광고 Tool 이름에서 그 까닭으로 가는 표이고, `backends` 는 Tool 이름을
 * 모르는 허브 항목 이름이다. 이름은 데이터라 그대로 보인다. 다른 코드의 `backends` 는 모양이 달라 읽지 않는다.
 */
function codeModeNames(error: ApiError): string[] {
  if (!error.code.startsWith("code_mode_")) return [];
  const names = error.extra<Record<string, string>>("names");
  const backends = error.extra<string[]>("backends");
  return [...(names && typeof names === "object" ? Object.keys(names) : []), ...(Array.isArray(backends) ? backends : [])];
}

/**
 * 오류를 한 줄 문장과 참조 목록으로 푼다. 409 참조 관계 오류는 서버가 id 만 싣는다. 화면이 이미
 * 받아 둔 `references` 를 `names` 로 넘기면 그 이름으로 바꿔 보이고, 없으면 종류와 개수만 말한다.
 */
export function describeError(t: Messages, error: unknown, names?: ReferenceNames): DescribedError {
  if (error instanceof ApiError) {
    // 코드 실행 게이트의 409 는 까닭의 문장이다(P40 8절). 캔버스와 Agent 경고가 같은 문장을 쓴다
    const reason = error.code === "sandbox_gate_closed" ? gateReason(error.extra<string>("reason")) : null;
    const known = reason ? t.clusters.sandbox.reason[reason] : errorText(t, error.code);
    const refs: string[] = [];
    // 백엔드가 409 에 싣는 참조 키를 모두 푼다. 하나라도 빠지면 화면이 "묶여 있다" 고만 말하고
    // 무엇을 떼야 하는지 못 알려 준다 (platform/daegam/api/data_services.py 의 in_use 네 갈래와 D32 의 knowledge_base_ids)
    for (const key of REF_KEYS) {
      const v = error.extra<string[]>(key);
      if (Array.isArray(v) && v.length) refs.push(...refLines(t, key, v, names));
    }
    for (const [key, label] of Object.entries(t.errors.refCountNames)) {
      const n = error.extra<number>(key);
      if (typeof n === "number" && n > 0) refs.push(t.errors.refCount(label, n));
    }
    refs.push(...codeModeNames(error));
    return {
      text: known ?? t.errors.fallback,
      detail: known ? undefined : error.detail,
      code: error.code,
      refs,
    };
  }
  if (error instanceof Error) return { text: error.message, refs: [] };
  return { text: String(error), refs: [] };
}

/** 문장 + 참조 목록. Alert 없이 다른 상자(대화상자 등) 안에 끼울 때 쓴다. */
export function ErrorBody({ error, names }: { error: unknown; names?: ReferenceNames }) {
  const t = useT();
  const { text, detail, code, refs } = describeError(t, error, names);
  return (
    <>
      <div>{text}</div>
      {detail ? <div className="mt-1 text-xs opacity-80">{detail}</div> : null}
      {refs.length ? (
        <ul className="mt-1 list-disc pl-4 text-xs">
          {refs.map((r) => (
            <li key={r}>{r}</li>
          ))}
        </ul>
      ) : null}
      {code ? (
        <div className="mt-1 text-right font-mono text-[10px] opacity-60" data-testid="error-code">
          {code}
        </div>
      ) : null}
    </>
  );
}

/** 뮤테이션·쿼리 오류를 한 줄로. 409 참조 관계 오류는 참조하는 리소스를 이름으로 같이 보여 준다. */
export function ErrorMessage({ error, testId, names }: { error: unknown; testId?: string; names?: ReferenceNames }) {
  if (!error) return null;
  return (
    <Alert variant="error" data-testid={testId ?? "error-message"}>
      <ErrorBody error={error} names={names} />
    </Alert>
  );
}
