"""리소스 이름 규칙 (D24). 라우터 열 곳이 이 모듈 하나를 쓴다.

`name` 은 K8s 이름이다. 소문자·숫자·하이픈 32자이고 하이픈은 양끝에 오지 않으며 연속되지 않는다.
이 규칙 안의 이름에 아래 `k8s_name()` 은 항등이라 화면의 미리보기와 실제 CR 이름이
같다. 상한 32 의 근거는 docs/guide/names.md 2절의 파생 이름 표다(노출 앞단 Service 가 고정
28자를 더한다). 생성 뒤에는 바꿀 수 없다. CR·Ingress 호스트·허브 항목 이름이 여기서
파생되기 때문이다.

`display_name` 은 화면에 보이는 이름이다. 앞뒤 공백을 지운 1~80자 자유 문자열이고 유일하지 않으며
언제든 바꿀 수 있다. 생성 본문에 없으면 `name` 으로 채운다.
"""

from __future__ import annotations

import hashlib
import re

from daegam.errors import error


def k8s_name(raw: str, *, prefix: str = "") -> str:
    """DNS-1123 label 로 정규화. 소문자, [a-z0-9-], 양끝 영숫자, 63자.

    테넌시 지식이 없는 순수한 이름 함수라 이름 규칙과 함께 여기 산다."""
    s = re.sub(r"[^a-z0-9-]+", "-", raw.lower()).strip("-")
    s = re.sub(r"-{2,}", "-", s)
    if prefix:
        s = f"{prefix}-{s}" if s else prefix
    return (s[:63].rstrip("-")) or "x"


def group_name_for(tenant: str) -> str:
    """테넌트 = MCPGroup 하나. 이름은 테넌트에서 결정적으로 만든다."""
    return k8s_name(tenant)


K8S_NAME_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
NAME_MAX = 32
DISPLAY_NAME_MAX = 80

NAME_RULE = (
    "name must be 1-32 lowercase letters, digits and single '-', "
    "starting and ending with a letter or digit"
)
DISPLAY_NAME_RULE = "display_name must be 1-80 characters after trimming whitespace"


def is_valid_name(name: str) -> bool:
    return len(name) <= NAME_MAX and K8S_NAME_RE.fullmatch(name) is not None


def validate_name(name: str) -> str:
    """생성 본문의 `name`. 앞뒤 공백을 지우고 규칙에 맞지 않으면 422 invalid_name."""
    name = name.strip()
    if not is_valid_name(name):
        raise error(422, "invalid_name", NAME_RULE)
    return name


def validate_display_name(display_name: str | None, *, default: str) -> str:
    """`display_name`. None 이면 default(보통 `name`), 있으면 trim 뒤 1~80자가 아니면 422
    invalid_display_name."""
    if display_name is None:
        return default
    value = display_name.strip()
    if not value or len(value) > DISPLAY_NAME_MAX:
        raise error(422, "invalid_display_name", DISPLAY_NAME_RULE)
    return value


def check_name_unchanged(name: str, current: str) -> str:
    """갱신 본문의 `name`. 저장값과 같으면 통과하고 다르면 422 name_immutable 이다. 저장된 이름은
    만들 때 `validate_name` 을 지났으므로 규칙은 다시 보지 않고 같은지만 본다."""
    if name.strip() != current:
        raise error(
            422,
            "name_immutable",
            "name cannot be changed after creation; create a new resource instead",
        )
    return current


def provider_slug(provider_name: str) -> str:
    """LLM provider 이름 → 그 provider 의 리소스 이름에 들어가는 slug (D56, P35 12절). provider
    이름은 대문자·점·밑줄을 받지만 Secret 이름은 소문자·숫자·하이픈이라 낮춰야 하고, 낮추며
    바뀌었으면 원래 이름의 해시를 붙여 서로 다른 provider 가 같은 slug 를 갖지 않게 한다. 플랫폼이
    관리하는 키 Secret 이름(`services/llm_providers.py` 의 `managed_secret_name`)과 런타임 파드의
    키 env 이름(`k8s/templates/agent.py` 의 `provider_key_env`)이 같은 이 함수를 쓴다."""
    slug = re.sub(r"-+", "-", re.sub(r"[^a-z0-9-]", "-", provider_name.lower())).strip("-")
    if slug != provider_name:
        slug = f"{slug}-{hashlib.sha256(provider_name.encode()).hexdigest()[:6]}".lstrip("-")
    return slug
