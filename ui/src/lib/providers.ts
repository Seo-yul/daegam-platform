/**
 * LLM provider kind 의 기본값과 SDK 갈래 (D25). 서버 `store/settings.py` 의 `DEFAULT_MODELS`·`DEFAULT_BASE_URLS`·
 * `SDK_KINDS` 와 같은 값이고, 둘이 같은지는 platform/tests/test_provider_ui_parity.py 가 본다.
 */
import type { LLMProvider, ProviderRouting } from "@/api/types";
import type { Messages } from "@/i18n";

type Kind = LLMProvider["kind"];

/** 모델 칸을 비우면 서버가 쓰는 kind 기본 모델. */
export const DEFAULT_MODELS: Record<Kind, string> = {
  openai: "gpt-4o",
  anthropic: "claude-opus-5",
  openrouter: "moonshotai/kimi-k3",
  zai: "glm-5.3",
  stub: "stub",
};

/** 주소 칸을 비우면 서버가 쓰는 kind 기본 주소. 여기 없는 kind 는 SDK 기본이다. */
export const DEFAULT_BASE_URLS: Partial<Record<Kind, string>> = {
  openrouter: "https://openrouter.ai/api/v1",
  zai: "https://api.z.ai/api/paas/v4",
};

/** 다른 SDK 의 호환 API 를 쓰는 kind. openrouter 와 zai 는 OpenAI SDK 로 부른다. */
export const SDK_KINDS: Partial<Record<Kind, Kind>> = {
  openrouter: "openai",
  zai: "openai",
};

/** kind 가 쓰는 SDK. 추론 강도·temperature 처럼 SDK 따라 갈리는 칸은 kind 가 아니라 이 값으로 고른다. */
export function sdkKind(kind: Kind | null | undefined): Kind | null {
  return kind == null ? null : (SDK_KINDS[kind] ?? kind);
}

/** 모델 칸이 비었을 때 실제로 쓰일 모델. */
export function effectiveModel(p: Pick<LLMProvider, "kind" | "model"> | null | undefined): string | null {
  return p ? p.model || DEFAULT_MODELS[p.kind] : null;
}

/** 화면이 고치는 업체 선호(T19). 업체와 양자화는 쉼표로 적는다. */
export interface RoutingForm {
  order: string;
  quantizations: string;
  allow_fallbacks: boolean;
  require_parameters: boolean;
}

function words(text: string): string[] {
  return text.split(/[\s,]+/).filter(Boolean);
}

export function routingForm(r: ProviderRouting | null | undefined): RoutingForm {
  return {
    order: (r?.order ?? []).join(", "),
    quantizations: (r?.quantizations ?? []).join(", "),
    allow_fallbacks: r?.allow_fallbacks ?? true,
    require_parameters: r?.require_parameters ?? false,
  };
}

type Routing = Required<ProviderRouting>;

/**
 * 폼 → 저장할 값. 모두 기본값이면 null 이라 OpenRouter 의 기본을 따른다. 키 순서가 고정이라 원본과 글자로 견줄 수 있다.
 * 양자화 이름은 서버가 검사한다(모르는 이름은 422).
 */
export function routingFromForm(f: RoutingForm): Routing | null {
  const r: Routing = {
    order: words(f.order),
    allow_fallbacks: f.allow_fallbacks,
    require_parameters: f.require_parameters,
    quantizations: words(f.quantizations) as Routing["quantizations"],
  };
  const isDefault = !r.order.length && r.allow_fallbacks && !r.require_parameters && !r.quantizations.length;
  return isDefault ? null : r;
}

/** 업체 선호 한 줄 요약. 접힌 칸의 제목에 쓴다. */
export function routingSummary(t: Messages, f: RoutingForm): string {
  const text = t.ui.providers.routing;
  const r = routingFromForm(f);
  if (!r) return text.none;
  const parts: string[] = [];
  const order = r.order.join(" → ");
  if (r.order.length) parts.push(r.allow_fallbacks ? order : text.only(order));
  else if (!r.allow_fallbacks) parts.push(text.noFallback);
  if (r.require_parameters) parts.push(text.requireParameters);
  if (r.quantizations.length) parts.push(r.quantizations.join("·"));
  return parts.join(" · ");
}
