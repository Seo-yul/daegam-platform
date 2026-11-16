"""image 항목의 컨테이너 입력: args, env, Secret env 와 그 검사 (P55 5절, D97 초안).

허브의 image 항목은 남의 이미지를 돌린다. 서명(B3 의 허용)은 그 바이트를 보증하지만, 인자와 env 는
그 이미지가 무엇을 돌릴지를 바꾼다. 그래서 만들 때 플랫폼이나 ToolHive 가 정하는 이름, 코드를 싣거나
바꾸는 이름(인터프리터 옵션, preload, 셸 시작 변수), 값이 밖으로 나갈 수 있는 프록시와 TLS 신뢰
이름을 거절한다(`env_reserved`). 쓸 수 있는 `/tmp` 와 home 을 가리키는 `HOME`·`TMPDIR`·
`DOTNET_BUNDLE_EXTRACT_BASE_DIR` 는 열어 둔다. 카탈로그 서버로 허용된 이미지는 지금 스냅숏의 인자와
선언한 env 이름으로만 돈다(`check_catalog_image`). 그 검사는 허용이 카탈로그 서버를
이름 댈 때(B3) 부른다.

Secret env 의 값은 항목마다의 Secret(`store/hub.py` `secret_env_name`)에만 있고,
응답·문서·매니페스트·감사에 남지 않는다. 오류에도 값을 싣지 않는다.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping

from daegam.errors import ServiceError
from daegam.services.hub_catalog import CatalogServer
from daegam.store.hub import HubSource

# 컨테이너 env 이름 (POSIX 꼴). Secret 의 키로도 쓴다
ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
MAX_ARGS = 64
MAX_ARG_BYTES = 4 * 1024
MAX_ENV = 64
MAX_ENV_VALUE_BYTES = 8 * 1024
MAX_SECRET_VALUE_BYTES = 32 * 1024

# 고를 수 없는 env 이름. 비교는 대문자로 한다(`http_proxy` 처럼 소문자로 읽는 도구가 있다)
RESERVED_ENV: frozenset[str] = frozenset(
    {
        # ToolHive 의 프록시 러너가 MCP 컨테이너에 넣는다 (docs/research/toolhive.md)
        "MCP_TRANSPORT",
        "MCP_PORT",
        "MCP_HOST",
        "FASTMCP_PORT",
        # 실행할 프로그램을 바꾼다
        "PATH",
        # 코드를 싣거나 바꾼다
        "PYTHONPATH",
        "PYTHONHOME",
        "PYTHONSTARTUP",
        "PYTHONUSERBASE",
        "PYTHONWARNINGS",
        "PYTHONBREAKPOINT",
        "PYTHONINSPECT",
        "PYTHONPLATLIBDIR",
        "NODE_OPTIONS",
        "NODE_PATH",
        "JAVA_TOOL_OPTIONS",
        "_JAVA_OPTIONS",
        "JDK_JAVA_OPTIONS",
        "JAVA_OPTS",
        "CLASSPATH",
        "DOTNET_STARTUP_HOOKS",
        "DOTNET_ADDITIONAL_DEPS",
        "DOTNET_SHARED_STORE",
        "PERL5OPT",
        "PERL5LIB",
        "PERLLIB",
        "PERL5DB",
        "RUBYOPT",
        "RUBYLIB",
        "BASH_ENV",
        "ENV",
        "PROMPT_COMMAND",
        "SHELLOPTS",
        "BASHOPTS",
        "PS4",
        # 값이 밖으로 나갈 수 있는 프록시와 TLS 신뢰
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "FTP_PROXY",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "REQUESTS_CA_BUNDLE",
        "CURL_CA_BUNDLE",
        "NODE_EXTRA_CA_CERTS",
        "NODE_TLS_REJECT_UNAUTHORIZED",
        "GRPC_DEFAULT_SSL_ROOTS_FILE_PATH",
        "PYTHONHTTPSVERIFY",
        "GIT_SSL_NO_VERIFY",
        "GIT_SSL_CAINFO",
        "AWS_CA_BUNDLE",
        # 프로그램이 설정된 명령을 돌리게 한다. 나중에 tenant-admin 이 넣는 Secret 값을 만든 사람이
        # 정한 명령이 읽지 못하게 한다
        "GIT_SSH",
        "GIT_SSH_COMMAND",
        "GIT_ASKPASS",
        "SSH_ASKPASS",
        "GIT_EXEC_PATH",
        "GIT_PROXY_COMMAND",
        "GIT_EXTERNAL_DIFF",
        "GIT_PAGER",
        "GIT_EDITOR",
        "PAGER",
        "EDITOR",
        "VISUAL",
        "LESSOPEN",
        "LESSCLOSE",
        # 라이브러리를 싣는다
        "GCONV_PATH",
        "OPENSSL_CONF",
        "OPENSSL_ENGINES",
        "OPENSSL_MODULES",
        # 시작할 때 패키지를 받는 런타임의 색인을 바꾼다
        "PIP_INDEX_URL",
        "PIP_EXTRA_INDEX_URL",
        "PIP_TRUSTED_HOST",
        "PIP_CERT",
        "UV_INDEX",
        "UV_INDEX_URL",
        "UV_EXTRA_INDEX_URL",
        "UV_DEFAULT_INDEX",
    }
)
# 고를 수 없는 접두어. 플랫폼, ToolHive, kubelet 이 정하는 것, 동적 링커와 .NET 프로파일러, git 의
# 설정 변수(`GIT_CONFIG_COUNT`·`GIT_CONFIG_KEY_<n>`·`GIT_CONFIG_PARAMETERS` 와 설정 파일 경로),
# npm 설정이다
RESERVED_ENV_PREFIXES: tuple[str, ...] = (
    "DAEGAM_",
    "TOOLHIVE_",
    "KUBERNETES_",
    "LD_",
    "DYLD_",
    "CORECLR_",
    "COR_",
    "GIT_CONFIG",
    "NPM_CONFIG_",
)
# env 값에 쓸 수 있는 제어 문자는 탭뿐이다. ToolHive 는 줄바꿈이 든 env 값을 거절한다
# (`k8s/templates/common.py` `ca_env`)
_CONTROL = re.compile(r"[\x00-\x08\x0a-\x1f\x7f]")


def env_reserved(name: str) -> bool:
    upper = name.upper()
    return upper in RESERVED_ENV or upper.startswith(RESERVED_ENV_PREFIXES)


def _invalid(error_cls: type[ServiceError], detail: str) -> ServiceError:
    return error_cls(422, "invalid_value", detail)


def check_image_source(source: HubSource, error_cls: type[ServiceError]) -> None:
    """image 항목의 컨테이너 입력 (5절). stdio 는 port 를 받지 않고, 인자·env 의 수와 길이에 상한이
    있으며, env 값에는 줄바꿈과 제어 문자가 없다. env 이름은 env 와 Secret env 를 합쳐 겹치지 않고
    고를 수 있는 이름이어야 한다. 이름이 틀리거나 겹치면 422 invalid_value, 고를 수 없으면 422
    env_reserved 다."""
    if source.stdio and source.port is not None:
        raise _invalid(error_cls, "a stdio container takes no port")
    args = source.args or []
    if len(args) > MAX_ARGS or any(len(a.encode()) > MAX_ARG_BYTES for a in args):
        raise _invalid(
            error_cls, f"args hold at most {MAX_ARGS} strings of {MAX_ARG_BYTES} bytes each"
        )
    env = source.env or []
    names = [e.name for e in env] + source.secret_names()
    if len(names) > MAX_ENV:
        raise _invalid(error_cls, f"env and secret_env hold at most {MAX_ENV} names together")
    if any(len(e.value.encode()) > MAX_ENV_VALUE_BYTES for e in env):
        raise _invalid(error_cls, f"an env value is at most {MAX_ENV_VALUE_BYTES} bytes")
    if any(_CONTROL.search(e.value) for e in env):
        raise _invalid(error_cls, "an env value holds no line break or control character")
    seen: set[str] = set()
    for name in names:
        if not ENV_NAME.match(name):
            raise _invalid(error_cls, f"env name {name!r} is not a valid variable name")
        if name in seen:
            raise _invalid(error_cls, f"env name {name} is given twice")
        seen.add(name)
        if env_reserved(name):
            raise error_cls(
                422,
                "env_reserved",
                f"env name {name} is set by the platform or ToolHive, or can change the code "
                "that runs",
                env_name=name,
            )


def check_secret_values(
    values: Mapping[str, str], names: Iterable[str], error_cls: type[ServiceError]
) -> None:
    """Secret env 값. 이름은 항목의 Secret env 이름이어야 하고(422 invalid_value), 값은 비지 않고
    상한 안이어야 한다(422 invalid_value). 오류에 값을 싣지 않는다."""
    known = set(names)
    unknown = sorted(set(values) - known)
    if unknown:
        raise _invalid(error_cls, f"not a secret_env name of this item: {', '.join(unknown)}")
    for name, value in values.items():
        if not value or len(value.encode()) > MAX_SECRET_VALUE_BYTES:
            raise _invalid(
                error_cls,
                f"the value of {name} must be 1 to {MAX_SECRET_VALUE_BYTES} bytes",
            )


def check_catalog_image(
    server: CatalogServer | None,
    source: HubSource,
    *,
    values: Mapping[str, str],
    platform_admin: bool,
    error_cls: type[ServiceError],
) -> None:
    """카탈로그 서버로 허용된 image 항목의 규칙 (5절). 허용이 그 서버를 이름 댈 때만
    부른다(B3). 지금 스냅숏에 없는 서버나 인자에 변수가 있는 서버는 platform-admin 만 만든다(403
    catalog_server_admin_only). 인자는 그 서버의 것과 같아야 하고 env 이름은 선언한 변수여야 한다.
    다르면 platform-admin 만 된다(403 catalog_settings_admin_only). 비밀로 표시한 변수는 Secret env
    여야 하고(422 env_must_be_secret), 꼭 필요한 변수는 값이 있어야 한다(422 env_required_missing).
    마지막 둘은 platform-admin 에게도 같다."""
    if server is None or server.args_variables:
        if not platform_admin:
            raise error_cls(
                403,
                "catalog_server_admin_only",
                "this catalog server is not in the current catalog or its arguments need values; "
                "only a platform-admin can register it",
            )
        return
    declared = {e.name: e for e in server.env}
    plain = {e.name for e in source.env or []}
    secret = set(source.secret_names())
    differs = list(source.args or []) != server.args or not (plain | secret) <= set(declared)
    if differs and not platform_admin:
        raise error_cls(
            403,
            "catalog_settings_admin_only",
            "only a platform-admin can use args or env names that differ from the catalog",
        )
    for name in sorted(plain):
        if name in declared and declared[name].secret:
            raise error_cls(
                422,
                "env_must_be_secret",
                f"the catalog marks {name} as secret; put it in secret_env",
                env_name=name,
            )
    given = plain | {n for n in secret if n in values}
    missing = sorted(n for n, e in declared.items() if e.required and n not in given)
    if missing:
        raise error_cls(
            422,
            "env_required_missing",
            f"the catalog marks these variables as required: {', '.join(missing)}",
            env_names=missing,
        )
