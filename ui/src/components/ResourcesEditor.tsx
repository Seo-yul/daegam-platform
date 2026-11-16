import { useId } from "react";

import { Input, Label, Textarea } from "@/components/ui/input";
import { type Messages, useT } from "@/i18n";
import { parseJsonObject } from "@/lib/json";
import { cn } from "@/lib/utils";

/**
 * 런타임 resources(K8s 컨테이너 resources) 편집기. Gateway·MCP Server 폼과 Basic 편집 폼이 같이 쓴다.
 * 기본은 Requests/Limits × CPU/Memory 네 칸이고, 네 칸에 담기지 않는 키(ephemeral-storage, 확장 리소스)가 있으면
 * JSON 모드로만 편집한다. 페이지는 ResourcesDraft 를 상태로 들고 저장 때 resourcesFromDraft 로 본문을 만든다.
 */

/** JSON 칸의 보기. 쿠버네티스 resources 값이라 번역하지 않는다. */
const JSON_EXAMPLE = '{"requests": {"cpu": "250m"}, "limits": {"cpu": "500m", "memory": "256Mi"}}';

export type ResourceKind = "cpu" | "memory";
export type ResourceTier = "requests" | "limits";

export interface ResourceFields {
  reqCpu: string;
  reqMem: string;
  limCpu: string;
  limMem: string;
}

export interface ResourcesDraft {
  mode: "form" | "json";
  fields: ResourceFields;
  /** JSON 모드의 textarea 원문. 폼 모드에서는 쓰지 않는다. */
  json: string;
}

export const EMPTY_FIELDS: ResourceFields = { reqCpu: "", reqMem: "", limCpu: "", limMem: "" };
export const EMPTY_DRAFT: ResourcesDraft = { mode: "form", fields: EMPTY_FIELDS, json: "" };

/** 네 칸의 표. 입력칸·오류 줄·testid(`<testId>-req-cpu`)·예시가 모두 이것 하나를 본다. */
const FIELD_KEYS: { key: keyof ResourceFields; tier: ResourceTier; kind: ResourceKind; short: "req-cpu" | "req-mem" | "lim-cpu" | "lim-mem"; placeholder: string }[] = [
  { key: "reqCpu", tier: "requests", kind: "cpu", short: "req-cpu", placeholder: "250m" },
  { key: "reqMem", tier: "requests", kind: "memory", short: "req-mem", placeholder: "128Mi" },
  { key: "limCpu", tier: "limits", kind: "cpu", short: "lim-cpu", placeholder: "500m" },
  { key: "limMem", tier: "limits", kind: "memory", short: "lim-mem", placeholder: "256Mi" },
];

// ---- quantity ---------------------------------------------------------------

// K8s quantity 중 폼이 받는 부분집합이다. 지수 표기(1e3)와 n/u 접미사는 JSON 모드로 넘긴다.
const CPU_RE = /^(\d+(?:\.\d+)?|\.\d+)(m?)$/;
const MEM_RE = /^(\d+(?:\.\d+)?|\.\d+)(Ki|Mi|Gi|Ti|Pi|Ei|k|M|G|T|P|E)?$/;
const MEM_SCALE: Record<string, number> = {
  "": 1,
  k: 1e3,
  M: 1e6,
  G: 1e9,
  T: 1e12,
  P: 1e15,
  E: 1e18,
  Ki: 1024,
  Mi: 1024 ** 2,
  Gi: 1024 ** 3,
  Ti: 1024 ** 4,
  Pi: 1024 ** 5,
  Ei: 1024 ** 6,
};

/** 폼이 받지 못하는 quantity 의 갈래. 사전의 `detail.resourcesEditor.problem` 이 갈래마다 안내 문장을 둔다. */
export type QuantityProblem = "cpu" | "milli" | "memoryUnit" | "memory";

/** 값이 올바르면 null, 아니면 무엇이 틀렸는지. 빈 값은 올바르다(생략). */
function quantityProblem(kind: ResourceKind, raw: string): QuantityProblem | null {
  const v = raw.trim();
  if (!v) return null;
  if (kind === "cpu") {
    const m = CPU_RE.exec(v);
    if (!m) return "cpu";
    // 밀리코어는 정수만 받는다. 0.5m 같은 값은 K8s 가 올림해 버려 적은 값과 달라진다.
    if (m[2] === "m" && m[1].includes(".")) return "milli";
    return null;
  }
  if (!MEM_RE.test(v)) return /^\d+(\.\d+)?(K|KB|MB|GB|TB|KiB|MiB|GiB|TiB)$/i.test(v) ? "memoryUnit" : "memory";
  return null;
}

/** 값이 올바르면 null, 아니면 사용자에게 보여 줄 안내 문장을 돌려준다. 빈 값은 올바르다(생략). */
export function validateQuantity(t: Messages, kind: ResourceKind, raw: string): string | null {
  const problem = quantityProblem(kind, raw);
  return problem ? t.detail.resourcesEditor.problem[problem] : null;
}

/** 비교용 숫자. CPU 는 밀리코어, 메모리는 바이트. 올바르지 않거나 비어 있으면 null. */
export function parseQuantity(kind: ResourceKind, raw: string): number | null {
  const v = raw.trim();
  if (!v || quantityProblem(kind, v)) return null;
  if (kind === "cpu") {
    const m = CPU_RE.exec(v)!;
    const n = Number(m[1]);
    return m[2] === "m" ? n : n * 1000;
  }
  const m = MEM_RE.exec(v)!;
  return Number(m[1]) * MEM_SCALE[m[2] ?? ""];
}

// ---- fields <-> resources dict -----------------------------------------------

export type ResourcesDict = Record<string, unknown>;

/** 네 칸을 resources dict 로 조립한다. 빈 칸은 키를 넣지 않고, 전부 비면 null(클러스터 기본). */
export function assembleResources(fields: ResourceFields): ResourcesDict | null {
  const out: Record<string, Record<string, string>> = {};
  for (const f of FIELD_KEYS) {
    const v = fields[f.key].trim();
    if (!v) continue;
    (out[f.tier] ??= {})[f.kind] = v;
  }
  return Object.keys(out).length ? out : null;
}

/**
 * resources dict 를 네 칸으로 푼다. 네 칸에 담기지 않는 키는 extras 에 경로("requests.ephemeral-storage")로 모은다.
 * cpu/memory 값이 숫자면 문자열로 바꾼다(K8s 는 JSON 숫자도 quantity 로 받는다).
 */
export function fieldsFromResources(value: unknown): { fields: ResourceFields; extras: string[] } {
  const fields = { ...EMPTY_FIELDS };
  const extras: string[] = [];
  if (!value || typeof value !== "object" || Array.isArray(value)) return { fields, extras };
  for (const [tier, group] of Object.entries(value as Record<string, unknown>)) {
    if ((tier !== "requests" && tier !== "limits") || !group || typeof group !== "object" || Array.isArray(group)) {
      extras.push(tier);
      continue;
    }
    for (const [name, q] of Object.entries(group as Record<string, unknown>)) {
      const f = FIELD_KEYS.find((k) => k.tier === tier && k.kind === name);
      if (!f || (typeof q !== "string" && typeof q !== "number")) {
        extras.push(`${tier}.${name}`);
        continue;
      }
      fields[f.key] = String(q);
    }
  }
  return { fields, extras };
}

export function draftFromResources(value: unknown): ResourcesDraft {
  const { fields, extras } = fieldsFromResources(value);
  if (extras.length) return { mode: "json", fields, json: JSON.stringify(value, null, 2) };
  return { mode: "form", fields, json: "" };
}

/** 저장 본문. 폼 모드는 네 칸의 quantity 를, JSON 모드는 파싱만 검사한다. */
export function resourcesFromDraft(t: Messages, draft: ResourcesDraft): { value: ResourcesDict | null; error: string | null } {
  if (draft.mode === "json") return parseJsonObject(draft.json);
  for (const f of FIELD_KEYS) {
    const err = validateQuantity(t, f.kind, draft.fields[f.key]);
    if (err) return { value: null, error: `${f.tier}.${f.kind}: ${err}` };
  }
  return { value: assembleResources(draft.fields), error: null };
}

/** limits 가 requests 보다 작은 리소스의 경고. 둘 다 올바른 값일 때만 본다. */
export function limitWarnings(t: Messages, fields: ResourceFields): string[] {
  const out: string[] = [];
  const pairs: [ResourceKind, string, string][] = [
    ["cpu", fields.reqCpu, fields.limCpu],
    ["memory", fields.reqMem, fields.limMem],
  ];
  for (const [kind, req, lim] of pairs) {
    const r = parseQuantity(kind, req);
    const l = parseQuantity(kind, lim);
    if (r !== null && l !== null && l < r) {
      out.push(t.detail.resourcesEditor.limitBelowRequest(kind, lim.trim(), req.trim()));
    }
  }
  return out;
}

/** 리소스 값이 비어 클러스터 기본값을 쓰는가. 비었으면 formatResources 가 사람 말 안내를 돌려주므로 고정폭으로 찍지 않는다. */
export function isDefaultResources(value: unknown): boolean {
  return !value || typeof value !== "object" || Array.isArray(value) || !Object.keys(value).length;
}

/** 상세 화면용 한 줄 요약. 예: `cpu 500m / memory 256Mi (limits cpu 1 / memory 512Mi)`. */
export function formatResources(t: Messages, value: unknown): string {
  const clusterDefault = t.shared.clusterDefault;
  if (isDefaultResources(value)) return clusterDefault;
  const v = value as Record<string, unknown>;
  const group = (g: unknown): string =>
    g && typeof g === "object" && !Array.isArray(g)
      ? Object.entries(g as Record<string, unknown>)
          .map(([k, q]) => `${k} ${typeof q === "string" || typeof q === "number" ? q : JSON.stringify(q)}`)
          .join(" / ")
      : JSON.stringify(g);
  const parts: string[] = [];
  const req = v.requests !== undefined ? group(v.requests) : "";
  const lim = v.limits !== undefined ? group(v.limits) : "";
  if (req) parts.push(req);
  if (lim) parts.push(req ? `(limits ${lim})` : `limits ${lim}`);
  for (const [k, q] of Object.entries(v)) {
    if (k !== "requests" && k !== "limits") parts.push(`${k} ${JSON.stringify(q)}`);
  }
  return parts.join(" ") || clusterDefault;
}

// ---- component ---------------------------------------------------------------

export function ResourcesEditor({
  value,
  onChange,
  testId,
  label = "Resources",
  hint,
}: {
  value: ResourcesDraft;
  onChange: (next: ResourcesDraft) => void;
  /** data-testid 접두사. 칸은 `<prefix>-req-cpu` 꼴, JSON textarea 는 `<prefix>-resources`, 토글은 `<prefix>-resources-json`. */
  testId: string;
  label?: string;
  /** 칸 아래 안내. 기본은 사전의 클러스터 기본값 안내이고, 빈 문자열이면 줄을 두지 않는다. */
  hint?: string;
}) {
  const t = useT();
  const text = t.detail.resourcesEditor;
  const uid = useId();
  const json = value.mode === "json" ? parseJsonObject(value.json) : null;
  const jsonExtras = json && !json.error ? fieldsFromResources(json.value).extras : [];
  // JSON 이 깨졌거나 폼에 담을 수 없는 키가 있으면 폼으로 돌아갈 수 없다. 돌아가면 그 내용을 잃기 때문이다.
  const lockReason = json?.error ? text.jsonError : jsonExtras.length ? text.jsonOnly(jsonExtras.join(", ")) : null;

  function toggle(on: boolean) {
    if (on) {
      const dict = assembleResources(value.fields);
      onChange({ ...value, mode: "json", json: dict ? JSON.stringify(dict, null, 2) : "" });
      return;
    }
    if (!json || json.error) return;
    const { fields, extras } = fieldsFromResources(json.value);
    if (extras.length) return;
    onChange({ ...value, mode: "form", fields });
  }

  const errors = FIELD_KEYS.map((f) => ({ ...f, error: validateQuantity(t, f.kind, value.fields[f.key]) })).filter((f) => f.error);
  const warnings = value.mode === "form" ? limitWarnings(t, value.fields) : [];
  const hintText = hint ?? text.hint;
  const tid = (suffix: string) => `${testId}-${suffix}`;

  return (
    <div className="flex flex-col gap-1" data-testid={tid("resources-editor")}>
      <div className="flex items-center justify-between gap-2">
        <Label htmlFor={value.mode === "json" ? tid("resources") : `${uid}-reqCpu`}>{label}</Label>
        <label className="flex items-center gap-1 text-xs text-muted">
          <input
            type="checkbox"
            data-testid={tid("resources-json")}
            checked={value.mode === "json"}
            disabled={Boolean(lockReason)}
            onChange={(e) => toggle(e.target.checked)}
          />
          {text.jsonMode}
        </label>
      </div>

      {value.mode === "json" ? (
        <>
          <Textarea
            id={tid("resources")}
            data-testid={tid("resources")}
            rows={5}
            placeholder={JSON_EXAMPLE}
            value={value.json}
            onChange={(e) => onChange({ ...value, json: e.target.value })}
          />
          {json?.error ? (
            <p className="text-xs text-err-fg" data-testid={tid("resources-error")}>
              {json.error}
            </p>
          ) : null}
          {lockReason ? (
            <p className="text-xs text-warn-fg" data-testid={tid("resources-json-notice")}>
              {lockReason}
            </p>
          ) : null}
        </>
      ) : (
        <>
          <div className="grid grid-cols-[5rem_1fr_1fr] items-center gap-2">
            <span />
            <span className="text-xs text-muted">{text.cpu}</span>
            <span className="text-xs text-muted">{text.memory}</span>
            {(["requests", "limits"] as ResourceTier[]).map((tier) => (
              <FieldRow key={tier} tier={tier} uid={uid} value={value} onChange={onChange} tid={tid} />
            ))}
          </div>
          {errors.map((f) => (
            <p key={f.key} className="text-xs text-err-fg" data-testid={tid(`${f.short}-error`)}>
              {f.tier}.{f.kind}: {f.error}
            </p>
          ))}
          {warnings.map((w) => (
            <p key={w} className="text-xs text-warn-fg" data-testid={tid("resources-warning")}>
              {w}
            </p>
          ))}
        </>
      )}
      {hintText ? <p className="text-xs text-muted">{hintText}</p> : null}
    </div>
  );
}

function FieldRow({
  tier,
  uid,
  value,
  onChange,
  tid,
}: {
  tier: ResourceTier;
  uid: string;
  value: ResourcesDraft;
  onChange: (next: ResourcesDraft) => void;
  tid: (suffix: string) => string;
}) {
  const text = useT().detail.resourcesEditor;
  return (
    <>
      <span className="text-xs font-medium text-fg-soft">{tier === "requests" ? text.requests : text.limits}</span>
      {FIELD_KEYS.filter((f) => f.tier === tier).map((c) => {
        const invalid = quantityProblem(c.kind, value.fields[c.key]) !== null;
        return (
          <Input
            key={c.key}
            id={`${uid}-${c.key}`}
            aria-label={`${tier} ${c.kind}`}
            aria-invalid={invalid || undefined}
            data-testid={tid(c.short)}
            className={cn("h-8 font-mono text-xs", invalid && "border-err-line focus-visible:ring-err/50")}
            placeholder={c.placeholder}
            value={value.fields[c.key]}
            onChange={(e) => onChange({ ...value, fields: { ...value.fields, [c.key]: e.target.value } })}
          />
        );
      })}
    </>
  );
}
