"""허브의 카탈로그 스냅숏 (P55 4절, D97 초안).

ToolHive 의 공개 카탈로그(toolhive-catalog) 릴리스 하나를 옮긴 `daegam/data/hub_catalog.json` 이다.
`scripts/hub_catalog_snapshot.py` 가 만들고 커밋하므로 갱신마다 검토하는 diff 가 되며, 릴리스 때
갱신한다. 실행 중에는 아무것도 인터넷에서 받지 않는다. 이미지와 함께 실려 바뀌지
않으므로 한 번 읽어 둔다.

`GET /api/hub/catalog` 가 스냅숏 전체를 돌려준다. 이미지 서버마다 허브 레지스트리로 옮긴 참조를
함께 실어 화면이 참조 규칙을 갖지 않게 한다(`mirrored_reference`). 그 경로는 미러 작업과 검증기도
쓰는 정규화(`normalize_image`) 하나로 짓는다 (6절).

원격 서버의 자격 증명 표시는 스냅숏의 칸에서 짓는다. OAuth 가 있으면 사람마다 로그인해야 하고
(`needs_login`), 비밀이든 아니든 필요한 헤더나 주소의 비밀 변수가 있으면 자격 증명이 필요하다
(`needs_credentials`). 로그인이 필요한 원격은 아직 가져올 수 없다. 자격 증명이 비밀
헤더 하나뿐인 원격은 그 헤더를 고정 헤더(`auth: header`, P86)로 싣는 entry 항목으로 가져오며, 그
헤더 이름이 `import_header` 다 (P86 A3). 화면이 이 칸만 보고 헤더를 골라 가져올 수 있는지 알게 해
규칙을 따로 갖지 않게 한다. 주소에 비밀 변수가 있거나, 필요한 헤더가 둘 이상이거나, 필요한 헤더
하나가 비밀이 아닌 원격은 가져올 수 없다. 주소 템플릿이 있는 원격은 두 표시가 모두 거짓이어도
`catalog` 와 함께 만들 수 없다 (4절).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field

from daegam.errors import ServiceError

SNAPSHOT_FILE = Path(__file__).resolve().parents[1] / "data" / "hub_catalog.json"
# Docker Hub 의 정규 호스트와 그 별칭
DOCKER_HUB = "docker.io"
DOCKER_HUB_ALIASES = frozenset({"index.docker.io", "registry-1.docker.io"})
# 이미지 참조의 꼴 (distribution 의 reference 문법). 저장소 마디는 소문자다
_HOST = re.compile(r"[a-z0-9]([a-z0-9.-]*[a-z0-9])?(:[0-9]{1,5})?")
REPOSITORY_SEGMENT = re.compile(r"[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*")
_TAG = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}")
SHA256_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")


def is_registry_host(first: str) -> bool:
    """참조의 첫 마디가 레지스트리 호스트인가. 점이나 포트가 있거나 localhost 다."""
    return "." in first or ":" in first or first == "localhost"


def canonical_host(host: str) -> str:
    """레지스트리 호스트의 정규형. 소문자이고, Docker Hub 의 별칭은 `docker.io`, 기본 포트 `:443`
    은 뗀다."""
    host = host.lower()
    if host.endswith(":443"):
        host = host[: -len(":443")]
    return DOCKER_HUB if host in DOCKER_HUB_ALIASES else host


class CatalogEnv(BaseModel):
    """카탈로그가 선언한 env 변수 하나."""

    model_config = ConfigDict(extra="ignore")

    name: str
    description: str = ""
    required: bool = False
    secret: bool = False
    default: str | None = None


class CatalogHeader(BaseModel):
    """원격 서버가 요구하는 헤더 하나 (구조로 온 것과 사람이 판정한 것)."""

    model_config = ConfigDict(extra="ignore")

    name: str
    description: str = ""
    required: bool = False
    secret: bool = False


class CatalogImage(BaseModel):
    model_config = ConfigDict(extra="ignore")

    repository: str
    tag: str


class CatalogServer(BaseModel):
    """스냅숏의 서버 하나 (4절). 이미지 서버와 원격 서버가 한 모양을 쓰고 자기 칸만 채운다."""

    model_config = ConfigDict(extra="ignore")

    name: str
    title: str
    description: str = ""
    kind: Literal["image", "remote"]
    tier: str = ""
    status: str = ""
    tags: list[str] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list)
    repository_url: str | None = None
    # 소비자가 말하는 전송. stdio 컨테이너는 프록시가 streamable-http 로 낸다
    transport: Literal["streamable-http", "sse"]
    # image
    image: CatalogImage | None = None
    stdio: bool = False
    port: int | None = None
    args: list[str] = Field(default_factory=list)
    args_variables: bool = False
    env: list[CatalogEnv] = Field(default_factory=list)
    platforms: list[str] = Field(default_factory=list)
    provenance: dict[str, Any] | None = None
    # remote
    url: str | None = None
    url_template: bool = False
    url_secret_variables: bool = False
    headers: list[CatalogHeader] = Field(default_factory=list)
    oauth_config: dict[str, Any] | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def needs_login(self) -> bool:
        """사람마다 로그인해야 쓰는 원격이다 (7절)."""
        return self.kind == "remote" and self.oauth_config is not None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def needs_credentials(self) -> bool:
        """비밀이든 아니든 필요한 헤더나 주소의 비밀 변수가 있는 원격이다 (7절)."""
        return self.kind == "remote" and (
            self.url_secret_variables or any(h.required for h in self.headers)
        )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def import_header(self) -> str | None:
        """이 원격을 가져오는 entry 항목이 고정 헤더(`auth: header`)로 실어야 할 헤더 이름 (P86 A3).
        필요한 자격 증명이 비밀 헤더 하나뿐인 원격만 이름이 있고, 항목 만들기는 이 이름이 있을
        때만 자격 증명이 필요한 원격을 받는다. 자격 증명이 필요 없거나, 로그인이 필요하거나, 주소에
        비밀 변수나 템플릿이 있거나, 필요한 헤더가 둘 이상이거나, 필요한 헤더 하나가 비밀이 아니면
        null 이다. 항목은 헤더를 하나만 싣는다 (P86 3절)."""
        if not self.needs_credentials or self.needs_login:
            return None
        if self.url_secret_variables or self.url_template:
            return None
        required = [h for h in self.headers if h.required]
        if len(required) != 1 or not required[0].secret:
            return None
        return required[0].name


class Catalog(BaseModel):
    model_config = ConfigDict(extra="ignore")

    catalog: str
    release: str
    # 스크립트가 받은 릴리스 자산의 sha256. 같은 자산에서 다시 만든 스냅숏인지 본다
    asset_sha256: str | None = None
    last_updated: str | None = None
    license: str
    servers: list[CatalogServer]

    def server(self, name: str) -> CatalogServer | None:
        return next((s for s in self.servers if s.name == name), None)


@lru_cache(maxsize=1)
def snapshot() -> Catalog:
    """이미지에 실린 스냅숏. 바뀌지 않으므로 한 번 읽는다."""
    return Catalog.model_validate_json(SNAPSHOT_FILE.read_text(encoding="utf-8"))


@dataclass(frozen=True)
class ImageRef:
    """정규화한 이미지 참조 (6절). host 는 소문자이고 Docker Hub 는 `docker.io` 다."""

    host: str
    repository: str
    tag: str | None = None
    digest: str | None = None

    @property
    def mirror_path(self) -> str:
        """허브 레지스트리 접두사 아래의 경로 `<host>/<repository>`. 호스트의 `:port` 는 경로
        글자로 쓸 수 없어 `_port` 다."""
        return f"{self.host.replace(':', '_')}/{self.repository}"

    def mirror_repository(self, prefix_path: str) -> str:
        """허브 레지스트리의 사본 저장소 `<접두사의 경로>/<host>/<repository>`. 접두사에 경로가
        없으면 prefix_path 는 비어 있다. 미러의 3단계, 검증기의 묶음, 카탈로그 응답의 옮긴 참조가
        이 하나를 쓴다 (6절)."""
        return f"{prefix_path}/{self.mirror_path}" if prefix_path else self.mirror_path


def normalize_image(ref: str) -> ImageRef:
    """이미지 참조의 정규화 (6절). 호스트가 없으면 `docker.io` 이고, Docker Hub 의 한 마디 이름에는
    `library/` 를 붙이며, 호스트는 `canonical_host` 다. 카탈로그 응답의 옮긴 참조, 스냅숏 스크립트의
    레지스트리 읽기, 미러의 0·3단계, 검증기의 묶음이 이 하나를 쓴다. 꼴은 보지 않는다. 사람이 준
    참조는 `parse_image` 로 읽는다."""
    name, _, digest = ref.strip().partition("@")
    tag = None
    last = name.rsplit("/", 1)[-1]
    if ":" in last:
        name, _, tag = name.rpartition(":")
    first, sep, rest = name.partition("/")
    if sep and is_registry_host(first):
        host, repository = canonical_host(first), rest
    else:
        host, repository = DOCKER_HUB, name
    if host == DOCKER_HUB and "/" not in repository:
        repository = f"library/{repository}"
    return ImageRef(host=host, repository=repository, tag=tag or None, digest=digest or None)


def parse_image(ref: str) -> ImageRef:
    """사람이나 서명이 준 이미지 참조를 정규화하고 꼴을 본다 (6절). 호스트, 저장소 마디(소문자,
    빈 마디와 `.`·`..` 없음), 태그, `sha256:<hex>` digest 가 distribution 의 문법에 맞아야 하고,
    아니면 ValueError 다. 레지스트리에 묻기 전에 보므로 요청 경로가 접두사 밖으로 나가지 않는다."""
    image = normalize_image(ref)
    if not _HOST.fullmatch(image.host):
        raise ValueError(f"image reference {ref!r} has an invalid registry host")
    if any(not REPOSITORY_SEGMENT.fullmatch(s) for s in image.repository.split("/")):
        raise ValueError(
            f"image reference {ref!r} has a repository segment that is not a lower-case name"
        )
    if image.tag is not None and not _TAG.fullmatch(image.tag):
        raise ValueError(f"image reference {ref!r} has an invalid tag")
    if image.digest is not None and not SHA256_DIGEST.fullmatch(image.digest):
        raise ValueError(f"image reference {ref!r} has a digest that is not sha256:<hex>")
    return image


def mirrored_reference(prefix: str | None, image: CatalogImage | None) -> str | None:
    """카탈로그 이미지를 허브 레지스트리로 옮긴 참조 `<prefix>/<host>/<repository>:<tag>`. 접두사가
    없으면(허브 이미지 설정 전) None 이다."""
    if not prefix or image is None:
        return None
    host, _, path = prefix.rstrip("/").partition("/")
    ref = normalize_image(f"{image.repository}:{image.tag}")
    return f"{host}/{ref.mirror_repository(path)}:{image.tag}"


def catalog_out(catalog: Catalog, *, admission: bool, prefix: str | None) -> dict[str, Any]:
    """`GET /api/hub/catalog` 의 답. admission 은 허브 이미지 설정이 있어 카탈로그 이미지를 등록할
    수 있는가다. 허브 이미지 설정(`hub_images`, P55 B3) 전에는 거짓이고 옮긴 참조가 없다."""
    return {
        "catalog": catalog.catalog,
        "release": catalog.release,
        "last_updated": catalog.last_updated,
        "license": catalog.license,
        "admission": admission,
        "servers": [
            {**s.model_dump(mode="json"), "mirrored_image": mirrored_reference(prefix, s.image)}
            for s in catalog.servers
        ],
    }


# SSE 로만 말하는 외부 MCP 서버를 거절하는 코드 (P54b 15절 3). 만들기(services/hub.py)와 카탈로그
# 가져오기가 함께 쓴다
OUTSIDE_SSE_UNSUPPORTED = "outside_sse_unsupported"


def catalog_remote(
    catalog: Catalog,
    name: str,
    *,
    url: str | None,
    transport: str | None,
    header_name: str | None = None,
    error_cls: type[ServiceError],
) -> CatalogServer:
    """entry 항목이 적은 카탈로그 서버 (5절). 지금 스냅숏의 원격 서버여야 하고, 주소와 전송이
    그 서버의 것과 같아야 한다. 그래야 상세가 뒷받침하지 못하는 출처를 말하지 않는다. 로그인이
    필요한 원격은 가져올 수 없다. 자격 증명이 필요한 원격은 그 서버의
    `import_header` 를 고정 헤더로 싣는 항목만 받는다 (P86 A3). header_name 은 항목이 `auth: header`
    로 싣는 헤더 이름이고 없으면 None 이다. HTTP 헤더 이름이라 대소문자를 가리지 않는다. SSE 로만
    말하는 서버는 422 outside_sse_unsupported 이고 나머지는 모두 422 invalid_value 다."""
    server = catalog.server(name)
    if server is None or server.kind != "remote":
        raise error_cls(422, "invalid_value", f"catalog has no remote server {name!r}")
    if server.transport == "sse":
        # SSE 로만 말하는 원격은 허브가 받지 않는다 (P54b 15절 3). 항목의 전송과 상관없다
        raise error_cls(
            422,
            OUTSIDE_SSE_UNSUPPORTED,
            f"catalog server {name} speaks only SSE, which the hub does not support",
        )
    if server.url_template:
        # 채운 주소는 스냅숏의 주소와 같을 수 없다 (4절)
        raise error_cls(422, "invalid_value", f"catalog server {name} has a URL template")
    if (url or "").rstrip("/") != (server.url or "").rstrip("/") or transport != server.transport:
        raise error_cls(
            422,
            "invalid_value",
            f"source.url and source.transport must be those of catalog server {name}",
        )
    if server.needs_login:
        raise error_cls(
            422,
            "invalid_value",
            f"catalog server {name} needs a sign-in and cannot be imported yet",
        )
    if server.needs_credentials:
        want = server.import_header
        if want is None:
            raise error_cls(
                422,
                "invalid_value",
                f"catalog server {name} cannot be imported: a remote that needs credentials is "
                "imported only when they are one required secret header",
            )
        if (header_name or "").lower() != want.lower():
            raise error_cls(
                422,
                "invalid_value",
                f"catalog server {name} can be imported only as an entry item with auth header "
                f"{want}",
            )
    return server
