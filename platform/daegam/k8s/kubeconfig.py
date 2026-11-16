"""등록한 kubeconfig 가 플랫폼 파드 안에서 명령을 돌리거나 파일을 읽지 못하게 한다 (P34).

kubernetes 클라이언트는 kubeconfig 를 거르지 않는다. `users[].user.exec` 와 `auth-provider` 의
`cmd-path` 는 플랫폼 파드 안에서 명령으로 돌고, `tokenFile`·`client-certificate`·`client-key`·
`certificate-authority` 는 파드의 파일 경로로 읽힌다. 원격 클러스터의 kubeconfig 는 남이 준 입력이라
그 필드를 받지 않는다. 같은 자격을 인라인으로 싣는 `token`·`*-data` 는 받는다.

등록 API 와 클라이언트 생성이 같은 `load_kubeconfig` 로 연다. 남이 준 YAML 이라 `yamlsafe` 로 열고,
문법 오류는 줄과 칸만 알린다. PyYAML 의 오류 글에는 틀린 줄이 그대로 들어 있어 그 줄의 토큰이나
비밀번호가 응답·preflight·로그로 나가기 때문이다. `kubecontext` 모드는 환경 변수로만 정하는 로컬
개발용이라 여기를 지나지 않는다.
"""

from __future__ import annotations

from typing import Any

import yaml

from daegam import yamlsafe

# kubeconfig 목록 → (원소 안의 맵, 그 맵에서 받지 않는 필드)
FORBIDDEN_FIELDS: dict[str, tuple[str, tuple[str, ...]]] = {
    "users": ("user", ("exec", "auth-provider", "tokenFile", "client-certificate", "client-key")),
    "clusters": ("cluster", ("certificate-authority",)),
}

_HINT = (
    "the platform does not run kubeconfig commands or read files from its pod; "
    "put the credentials inline (token, client-certificate-data, client-key-data, "
    "certificate-authority-data) or register the token of the ServiceAccount daegam-deployer "
    "that the chart's target: remote creates"
)


class KubeconfigRejected(ValueError):
    """받지 않는 kubeconfig. code 는 등록 API 의 422 코드이고 paths 는 걸린 금지 필드의 경로다.
    글에는 kubeconfig 의 값이 들어가지 않는다."""

    def __init__(self, code: str, detail: str, paths: list[str] | None = None):
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.paths = paths or []


def load_kubeconfig(text: str) -> dict[str, Any]:
    """kubeconfig 글 → dict. 열지 못하거나 매핑이 아니면 `invalid_yaml`, 명령·파일 참조가 있으면
    `invalid_value` 의 KubeconfigRejected 다. 원래 예외는 글에 비밀이 들 수 있어 잇지 않는다."""
    try:
        cfg = yamlsafe.safe_load(text)
    except yamlsafe.YAMLTooLarge:
        raise KubeconfigRejected(
            "invalid_yaml", "kubeconfig expands too far through aliases"
        ) from None
    except yaml.YAMLError as e:
        raise KubeconfigRejected("invalid_yaml", _syntax_error(e)) from None
    if not isinstance(cfg, dict):
        raise KubeconfigRejected("invalid_yaml", "kubeconfig is not a mapping")
    paths = forbidden_kubeconfig_paths(cfg)
    if paths:
        raise KubeconfigRejected(
            "invalid_value", f"kubeconfig has {', '.join(paths)}: {_HINT}", paths
        )
    return cfg


def _syntax_error(e: yaml.YAMLError) -> str:
    """오류 글 대신 자리만 알린다."""
    mark = getattr(e, "problem_mark", None)
    where = f" at line {mark.line + 1}, column {mark.column + 1}" if mark is not None else ""
    return f"kubeconfig is not valid YAML{where}"


def forbidden_kubeconfig_paths(cfg: Any) -> list[str]:
    """파싱한 kubeconfig 에서 받지 않는 필드의 경로(`users[0].user.exec`). 모양이 달라 볼 자리가
    없으면 빈 목록이다. 그런 kubeconfig 는 접속에서 따로 실패한다."""
    if not isinstance(cfg, dict):
        return []
    paths: list[str] = []
    for section, (inner, fields) in FORBIDDEN_FIELDS.items():
        entries = cfg.get(section)
        for i, entry in enumerate(entries if isinstance(entries, list) else []):
            body = entry.get(inner) if isinstance(entry, dict) else None
            if isinstance(body, dict):
                paths += [f"{section}[{i}].{inner}.{f}" for f in fields if f in body]
    return paths
