/**
 * Agent 의 외부 노출 섹션 (D27, resource-model 11절). Deploy 폼과 상세의 재배포 대화상자가 같이 쓴다.
 * 공용 `ExposureSection` 을 `requireOidc` 로 재사용한다. header 모드에서는 클러스터의 allow_unauthenticated 와
 * 무관하게 토글이 잠기고 "oidc 모드에서만" 안내가 보인다. 켜면 `cors_origins`(줄마다 하나, 소문자·형식 검사)와
 * `allowed_groups`(칩 입력)를 더 받는다. 비어 있으면 oidc 는 인증된 모든 사용자가 부를 수 있다는 경고를, local 은 플랫폼이
 * 그 테넌트의 MCP 그룹으로 채워 내리므로 테넌트 멤버만 부를 수 있다는 안내를 단다 (P32 2절).
 */
import type { AgentExposure, AgentExposureInput } from "@/api/types";
import { ChipInput } from "@/components/AccessPolicyEditor";
import { ExposureSection } from "@/components/ExposureSection";
import { Badge } from "@/components/ui/badge";
import { Alert } from "@/components/ui/card";
import { Field, Textarea } from "@/components/ui/input";
import { type Messages, useT } from "@/i18n";
import { useAuthState, type AuthMode } from "@/lib/auth";
import { EMPTY_EXPOSURE, exposureFormOf, toExposureInput, type ExposureErrors, type ExposureForm } from "@/lib/exposure";

// -- 폼 상태 ---------------------------------------------------------------------------

export interface AgentExposureForm extends ExposureForm {
  /** textarea 원문. 줄마다 origin 하나이고 검사·소문자화는 `parseCorsOrigins` 가 한다. */
  corsOrigins: string;
  allowedGroups: string[];
}

export const EMPTY_AGENT_EXPOSURE: AgentExposureForm = { ...EMPTY_EXPOSURE, corsOrigins: "", allowedGroups: [] };

/** 저장된 인스턴스의 exposure 스냅샷을 폼 상태로. */
export function agentExposureFormOf(exp: AgentExposure, namespace: string): AgentExposureForm {
  return { ...exposureFormOf(exp, namespace), corsOrigins: exp.cors_origins.join("\n"), allowedGroups: [...exp.allowed_groups] };
}

/** 서버 agent_exposure_from 과 같은 규칙이다. origin 은 scheme 과 host 만이고 경로가 없다. */
export const CORS_ORIGIN_RE = /^https?:\/\/[^/]+$/;

/** 줄마다 하나씩 읽어 소문자로 만든다. 빈 줄은 버리고 중복은 하나만 남긴다. 형식이 틀린 줄은 invalid 로 돌려준다. */
export function parseCorsOrigins(text: string): { origins: string[]; invalid: string[] } {
  const origins: string[] = [];
  const invalid: string[] = [];
  for (const raw of text.split(/\r?\n/)) {
    const line = raw.trim();
    if (!line) continue;
    const lower = line.toLowerCase();
    if (!CORS_ORIGIN_RE.test(lower)) {
      invalid.push(line);
      continue;
    }
    if (!origins.includes(lower)) origins.push(lower);
  }
  return { origins, invalid };
}

/** deploy·redeploy 본문의 `exposure`. 끄면 `{enabled: false}` 만 보낸다. */
export function toAgentExposureInput(f: AgentExposureForm): AgentExposureInput {
  const base = toExposureInput(f);
  if (!base.enabled) return base;
  return { ...base, cors_origins: parseCorsOrigins(f.corsOrigins).origins, allowed_groups: f.allowedGroups };
}

/** 켠 상태에서 클라이언트 규칙에 걸리는 것이 있으면 true. 저장 버튼을 잠그는 데 쓴다. */
export function agentExposureInvalid(f: AgentExposureForm): boolean {
  return f.enabled && parseCorsOrigins(f.corsOrigins).invalid.length > 0;
}

/**
 * allowed_groups 가 비었을 때 누가 부르는가와 그 배지의 색. local 은 플랫폼이 그 테넌트의 MCP 그룹으로 채워 내리므로 경계가
 * 테넌트라 안내이고, 그 밖은 IdP 가 인증한 모두라 경고다 (D27).
 */
export function emptyGroupsNote(t: Messages, mode: AuthMode): { text: string; variant: "info" | "warn" } {
  const text = t.shared.agentExposureSection;
  return mode === "local" ? { text: text.tenantMembers, variant: "info" } : { text: text.allUsers, variant: "warn" };
}

/** allowed_groups 가 비어 있을 때의 배지. Deploy 폼과 상세의 외부 호출 카드가 같은 문구를 쓴다. */
export function EmptyAllowedGroupsBadge({ testId }: { testId: string }) {
  const t = useT();
  const note = emptyGroupsNote(t, useAuthState().mode);
  return (
    <Badge variant={note.variant} className="whitespace-normal" data-testid={testId}>
      {note.text}
    </Badge>
  );
}

// -- 섹션 ----------------------------------------------------------------------------------

export interface AgentExposureSectionProps {
  clusterId: string | null;
  name: string;
  value: AgentExposureForm;
  onChange: (next: AgentExposureForm) => void;
  errors?: ExposureErrors;
  mode?: "create" | "edit";
  /** 복제 대화상자처럼 네임스페이스를 받지 않는 자리에서 그 칸을 감춘다. */
  showNamespace?: boolean;
}

export function AgentExposureSection({ clusterId, name, value, onChange, errors = {}, mode = "create", showNamespace = true }: AgentExposureSectionProps) {
  const t = useT();
  const text = t.shared.agentExposureSection;
  const auth = useAuthState();
  const headerMode = auth.mode === "header";
  const cors = parseCorsOrigins(value.corsOrigins);

  return (
    <div className="flex flex-col gap-3" data-testid="ai-exposure-section">
      {headerMode ? (
        <Alert variant="info" data-testid="ai-exposure-oidc-only">
          {text.oidcOnly}
        </Alert>
      ) : null}
      <ExposureSection
        clusterId={clusterId}
        name={name}
        value={value}
        // 공용 섹션은 자기 필드만 바꿔 돌려주므로 Agent 전용 필드는 여기서 합친다
        onChange={(next) => onChange({ ...value, ...next })}
        errors={errors}
        mode={mode}
        requireOidc
        urlPath=""
        showNamespace={showNamespace}
      >
        <Field label={text.labels.cors} htmlFor="ai-cors-origins" hint={text.corsHint}>
          <Textarea
            id="ai-cors-origins"
            data-testid="ai-cors-origins"
            className="min-h-16"
            placeholder={"https://app.example.com\nhttp://localhost:5173"}
            value={value.corsOrigins}
            aria-invalid={cors.invalid.length > 0 || Boolean(errors.cors)}
            onChange={(e) => onChange({ ...value, corsOrigins: e.target.value })}
          />
          {cors.invalid.length ? (
            <p className="text-xs text-err-fg" data-testid="ai-cors-origins-invalid">
              {text.corsInvalid(cors.invalid.join(", "))}
            </p>
          ) : null}
          {errors.cors ? (
            <p className="text-xs text-err-fg" data-testid="ai-cors-origins-error">
              {errors.cors}
            </p>
          ) : null}
        </Field>
        <Field label={text.labels.groups} htmlFor="ai-allowed-groups" hint={text.groupsHint(emptyGroupsNote(t, auth.mode).text)}>
          <ChipInput id="ai-allowed-groups" testId="ai-allowed-groups" values={value.allowedGroups} onChange={(allowedGroups) => onChange({ ...value, allowedGroups })} placeholder={text.groupsPlaceholder} />
          {value.allowedGroups.length === 0 ? <EmptyAllowedGroupsBadge testId="ai-allowed-groups-empty" /> : null}
        </Field>
      </ExposureSection>
    </div>
  );
}
