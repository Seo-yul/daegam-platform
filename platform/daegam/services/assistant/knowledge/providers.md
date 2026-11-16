# LLM provider
에이전트의 모델 노드에 provider 를 채우거나 provider 관련 422 를 받았거나 사용자가 API 키를 주려 할 때 읽는다.

## 규칙
- LLM 을 쓰는 모든 기능은 Platform Settings 의 llm_providers 에 등록된 provider 를 이름으로 고른다. 이름을 지어내지 않고 get_platform_settings 로 목록을 읽는다.
- provider 한 줄은 {name, kind, model?, api_key_secret?, base_url?} 다. kind 는 openai, anthropic, openrouter, zai, stub 다섯이다.
- api_key_secret 은 플랫폼 네임스페이스(보통 daegam-system)에 있는 K8s Secret 의 이름이다. 키 값이 아니다. Secret 안의 항목 이름은 api_key 다.
- 키는 Platform Settings 화면(/admin/settings)의 provider 줄 "API 키" 칸에 사람이 넣는다. 저장하면 플랫폼이 Secret daegam-llm-key-<이름> 을 만들어 키를 넣고 api_key_secret 을 그 이름으로 맞춘다. 운영자가 만든 Secret 은 "이미 있는 Secret 쓰기" 칸에 이름만 적는다.
- model 을 비우면 kind 별 기본 모델이다. openai 는 gpt-4o, anthropic 은 claude-opus-5, openrouter 는 moonshotai/kimi-k3, zai 는 glm-5.3 이다. base_url 은 OpenAI 호환 게이트웨이를 붙일 때만 쓴다.
- openrouter 는 OpenRouter(https://openrouter.ai/api/v1)를 OpenAI SDK 로 부르는 kind 다. base_url 을 비우면 그 주소이고, 모델 이름은 `moonshotai/kimi-k3` 처럼 OpenRouter 의 표기다. 추론 강도와 temperature 는 openai 와 같은 규칙이라 max 를 받지 않는다. Knowledge Base 임베딩 provider 로는 쓰지 않는다.
- zai 는 GLM 을 만든 Z.AI 의 API 를 OpenAI SDK 로 직접 부르는 kind 다. base_url 을 비우면 종량제 주소(https://api.z.ai/api/paas/v4)이고, 적으면 그 주소를 쓴다. GLM Coding Plan 구독은 Z.AI 가 지원하는 코딩 도구에서만 쓸 수 있으므로 구독 주소(/api/coding/paas/v4)를 권하지 않는다. 모델 이름은 glm-5.3 처럼 Z.AI 의 표기다. 추론 강도는 glm-5.3(low, high, max)과 glm-5.2(none 부터 max 까지)에만 보내고, 받지 않는 값은 가장 가까운 값으로 바꾼다. 다른 GLM 이름은 모델 기본값을 쓴다. Knowledge Base 임베딩 provider 로는 쓰지 않는다.
- openrouter 만 routing{order, allow_fallbacks, require_parameters, quantizations} 을 받는다. 같은 모델을 돌리는 여러 업체 가운데 무엇을 쓸지 정하는 업체 선호다. order 는 업체 slug(moonshotai, deepinfra/turbo) 목록이고, quantizations 는 int4·int8·fp4·mxfp4·nvfp4·fp6·fp8·mxfp8·fp16·bf16·fp32·unknown 가운데서 고른다. 다른 kind 에 routing 을 주거나 slug·양자화 이름이 틀리면 422 요청 검증 오류다(detail 이 목록이고 msg 에 이유가 있다). Agent 에는 다음 재배포나 파드 재시작부터 닿는다.
- defaults.spec_generator 가 스펙 다듬기의 기본 provider 이름이고 비어 있으면 첫 항목이다. 비서 자신은 defaults.assistant 를 쓰고 비어 있으면 defaults.spec_generator, 그것도 없으면 첫 항목이다.
- kind 가 stub 인 provider 는 키 없이 저장되는 실증용이다. 배포본이 DAEGAM_ALLOW_STUB_PROVIDER=true 일 때만 등록되고, 스펙 다듬기는 거부하며, 에이전트 검증은 키 검사를 면제한다.
- 에이전트 파드에는 provider 의 Secret 이 테넌트 네임스페이스로 복사돼 env 로 들어간다. 사용자가 키를 따로 넣을 곳은 없다.

## 키를 채팅으로 받지 않는 이유
- 채팅 기록은 세션 문서로 Mongo 에 남는다. 키가 문장에 섞이면 기록에 남는다.
- 플랫폼은 비밀을 값으로 보관하지 않고 Secret 참조만 둔다. 키 경로가 Secret 하나뿐이라 화면에서 어느 키가 쓰였는지 알 수 있다.
- 사용자가 키를 주려 하면 받지 않는다. provider 등록(이름·kind·model)까지 한 뒤 navigate 로 /admin/settings 를 열고 그 줄의 "API 키" 칸에 직접 넣도록 안내한다. 키를 넣는 경로는 비서 Tool 에 없다.
- provider 등록은 이름만 필요하므로 비서가 할 수 있다. 다만 Settings 변경은 destructive 등급이라 승인이 뜨고 settings:write 권한(platform-admin)이 있어야 한다.

## 등록과 확인
1. get_platform_settings 로 llm_providers 전체를 읽는다.
2. update_platform_settings 에 llm_providers 를 보낸다. 목록 전체를 바꾸는 필드라 기존 항목을 함께 보낸다. 이름은 유일해야 하고 `^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$` 이다. defaults 의 이름은 목록에 있어야 한다.
3. 연결 확인은 Platform Settings 화면(/admin/settings)의 "연결 확인" 버튼이다. Secret 존재, api_key 항목, models.list 한 번을 본다. openrouter 는 모델 목록이 키 없이 열려 있어 키 확인(/key)을 한 번 더 부른다. 연결 확인은 zai 의 잔액을 보지 않으므로, 잔액이 없으면 실제 호출에서 quota_exhausted 가 난다. 결과 code 는 ok, no_secret, no_api_key, auth, connection, provider_error 다.

## 흔한 오류와 대처
| code | 뜻 | 대처 |
|---|---|---|
| no_provider (422) | 등록된 provider 가 없다 | provider 를 등록하고 화면에서 키를 넣도록 안내한다 |
| unknown_provider (422) | 그 이름이 목록에 없다 | 목록에서 고른다 |
| provider_no_key (422) | api_key_secret 이 비었거나 Secret 이나 api_key 항목이 없다 | /admin/settings 의 그 줄에 키를 넣도록 안내한다. 값은 묻지 않는다 |
| no_secret_store (422) | home 클러스터가 없어 Secret 을 읽을 수 없다 | 운영자에게 알린다 |
| stub_provider_disallowed (422) | 배포본이 stub 을 허용하지 않는다 | 실제 provider 를 쓴다 |
| provider_not_supported (422) | 스펙 다듬기에 stub 을 골랐다 | 실제 provider 를 고른다 |
| invalid_value (422) | 이름이 겹치거나 defaults 가 목록 밖이다 | 목록을 다시 읽고 이름을 맞춘다 |
| unsupported_param (경고) | anthropic 모델에 temperature 를 줬다 | temperature 를 null 로 둔다 |

