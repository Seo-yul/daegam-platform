"""Platform Settings (싱글턴)."""

from __future__ import annotations

import logging
from typing import Annotated, Any, Protocol

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, ValidationError
from starlette.concurrency import run_in_threadpool

from daegam.api._common import (
    TENANT_RE,
    error,
    not_found,
    not_null,
    one_line,
    patched_fields,
    patched_values,
    require_secret_store,
)
from daegam.api.deps import User, require_platform_admin, require_scope
from daegam.api.schemas import ERROR_RESPONSES
from daegam.api.schemas.tenancy import (
    ApiKeyIn,
    DataPlaneAuthOut,
    HubImagesCredentialIn,
    HubImagesCredentialOut,
    HubImagesStatusOut,
    LLMProviderCheckOut,
    LLMProviderKeyOut,
    OtlpSettingsOut,
    OtlpStatusOut,
    PlatformSettingsOut,
    WebSearchCheckOut,
    WebSearchKeyOut,
)
from daegam.auth.principal import Principal
from daegam.errors import ServiceError, revision_conflict
from daegam.services.channels.links import CUT_BOT_CHANGED
from daegam.services.hub_images import (
    CREDENTIAL_PASSWORD,
    CREDENTIAL_SECRET,
    CREDENTIAL_USERNAME,
    check_hub_images,
    key_fingerprints,
    overlapping_clusters,
    prefix_host,
)
from daegam.services.llm_gateway import check_limits, check_upstreams
from daegam.services.llm_providers import (
    SECRET_KEY,
    check_provider,
    is_managed_secret,
    managed_secret_name,
    provider_named,
)
from daegam.services.web_search import SEARCH_KEY_SECRET
from daegam.services.web_settings import check_network, check_web_tools
from daegam.store.observability import OtlpSettings
from daegam.store.settings import (
    AssistantSettings,
    ChannelsSettings,
    Defaults,
    Guid,
    HubImagesSettings,
    LlmGatewaySettings,
    LLMProvider,
    MongoPlatformSettingsStore,
    NetworkSettings,
    OidcSettings,
    PlatformSettings,
    SkillsSettings,
    TeamsSettings,
    WebToolsSettings,
)

router = APIRouter(responses=ERROR_RESPONSES, prefix="/settings", tags=["settings"])


class SettingsDocStore[S](Protocol):
    """사람의 저장을 하는 설정 문서 스토어 (Platform Settings, 관측 설정)."""

    async def update(
        self, changes: dict[str, Any], *, updated_by: str | None = None, base: S | None = None
    ) -> S | None: ...


logger = logging.getLogger("daegam.api.settings")


def get_settings_store(request: Request) -> MongoPlatformSettingsStore:
    return request.app.state.platform_settings


SettingsStore = Annotated[MongoPlatformSettingsStore, Depends(get_settings_store)]


class TeamsPatch(BaseModel):
    """`channels.teams` 의 PATCH (P47 6.1절). 보낸 칸만 바꾼다. Client Secret 은 따로인 경로다."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool | None = None
    app_id: Guid | None = None
    entra_tenant_id: Guid | None = None


class ChannelsPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    teams: TeamsPatch | None = None


class HubImagesPatch(BaseModel):
    """`hub_images` 의 PATCH (P55 6절). 보낸 칸만 바꾼다. 읽기 자격 증명은 따로인 경로다."""

    model_config = ConfigDict(extra="forbid")

    prefix: str | None = None
    public_keys: list[str] | None = None
    revoked_keys: list[str] | None = None
    refused_digests: list[str] | None = None


class SettingsPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    default_runtime_image: str | None = None
    default_agent_image: str | None = None
    default_model: str | None = None
    default_cluster_id: str | None = None
    namespace_template: str | None = None
    oidc: OidcSettings | None = None
    llm_providers: list[LLMProvider] | None = None
    defaults: Defaults | None = None
    assistant: AssistantSettings | None = None
    skills: SkillsSettings | None = None
    web_tools: WebToolsSettings | None = None
    network: NetworkSettings | None = None
    llm_gateway: LlmGatewaySettings | None = None
    default_web_tools_image: str | None = None
    channels: ChannelsPatch | None = None
    hub_images: HubImagesPatch | None = None


def _out(s: PlatformSettings) -> dict[str, Any]:
    return s.model_dump(mode="json")


@router.get(
    "", response_model=PlatformSettingsOut, dependencies=[Depends(require_scope("settings:read"))]
)
async def get_platform_settings(store: SettingsStore) -> dict[str, Any]:
    return _out(await store.get())


@router.get(
    "/data-plane-auth",
    response_model=DataPlaneAuthOut,
    dependencies=[Depends(require_scope("settings:write"))],
)
async def get_data_plane_auth(request: Request) -> dict[str, Any]:
    """인증 재적용의 진행과 이 설치의 install_id (P32 8절). 모든 모드에서 답한다. 클러스터 상태와
    막힌 소비자 목록이 모든 테넌트의 리소스를 싣기 때문에 platform-admin 만 부른다. `tick_at` 이
    2분보다 오래됐으면 리컨실이 돌지 않는 것이다."""
    return await request.app.state.data_plane_auth.status()


async def _save[S](
    store: SettingsDocStore[S],
    changes: dict[str, Any],
    user: Principal,
    before: S,
    *,
    document: str,
) -> S:
    """설정 문서 하나의 사람의 저장 (P68). before 를 읽은 판일 때만 맞는다. 검증이 틀리면 422
    invalid_value, 그사이 다른 저장이 지나갔으면 409 revision_conflict 이고 그 글이 문서(document)를
    말한다."""
    try:
        after = await store.update(changes, updated_by=user.sub, base=before)
    except ValidationError as e:
        raise error(422, "invalid_value", str(e)) from e
    if after is None:
        raise revision_conflict(error, document, "settings")
    return after


@router.get(
    "/observability/otlp",
    response_model=OtlpSettingsOut,
    dependencies=[Depends(require_scope("settings:read"))],
)
async def get_otlp_settings(request: Request) -> dict[str, Any]:
    """OTLP 내보내기 설정 (P37 15절). 저장한 적이 없으면 기본값(꺼짐)이다."""
    return (await request.app.state.observability_settings.get()).otlp.model_dump(mode="json")


@router.put(
    "/observability/otlp",
    response_model=OtlpSettingsOut,
    dependencies=[Depends(require_scope("settings:write"))],
)
async def put_otlp_settings(body: OtlpSettings, request: Request, user: User) -> dict[str, Any]:
    """OTLP 내보내기 설정을 통째로 바꾼다 (P37 15절). 런타임 기록이 나가는 곳이 바뀌므로 비서의
    설정 도구가 아니라 이 경로로 사람이 바꾸고, 쓰기는 감사에 남는다. 주소는 https 만, 평문 http 는
    클러스터 안의 `*.svc` 만 받는다. Platform Settings 문서와 따로인 문서 `observability` 다."""
    store = request.app.state.observability_settings
    after = await _save(
        store, {"otlp": body.model_dump()}, user, await store.get(), document="observability"
    )
    request.app.state.otlp_export.forget_config()
    return after.otlp.model_dump(mode="json")


@router.get(
    "/observability/otlp/status",
    response_model=OtlpStatusOut,
    dependencies=[Depends(require_scope("settings:read"))],
)
async def get_otlp_status(request: Request) -> dict[str, Any]:
    """OTLP 내보내기의 상태 (P37 15절). 설정 카드가 읽는다."""
    return await request.app.state.otlp_export.status()


@router.patch(
    "", response_model=PlatformSettingsOut, dependencies=[Depends(require_scope("settings:write"))]
)
async def patch_platform_settings(
    store: SettingsStore, patch: SettingsPatch, request: Request, user: User
) -> dict[str, Any]:
    sent = patched_fields(patch)
    # 검증은 모두 모델 값으로 끝내고, 저장할 dict 는 마지막에 한 번 덤프한다
    for k in (
        "default_runtime_image",
        "default_agent_image",
        "default_model",
        "namespace_template",
        "default_web_tools_image",
    ):
        value = getattr(patch, k)
        if k in sent and (value is None or not str(value).strip()):
            raise error(422, "invalid_value", f"{k} cannot be empty")
    if "namespace_template" in sent and "{tenant}" not in (patch.namespace_template or ""):
        raise error(422, "invalid_value", "namespace_template must contain {tenant}")
    if "default_cluster_id" in sent and patch.default_cluster_id is not None:
        cluster = await request.app.state.clusters.get(patch.default_cluster_id)
        if cluster is None:
            raise error(422, "unknown_cluster", patch.default_cluster_id)
    if "llm_providers" in sent:
        providers = not_null(patch.llm_providers, "llm_providers")
        stubs = [p.name for p in providers if p.is_stub]
        if stubs and not request.app.state.settings.allow_stub_provider:
            # e2e·로컬 실증용 예외다 (D25). 배포본이 허용하지 않으면 등록 자체를 막는다
            raise error(
                422,
                "stub_provider_disallowed",
                "kind: stub providers need DAEGAM_ALLOW_STUB_PROVIDER=true on this deployment",
                providers=stubs,
            )
    for section in (
        "defaults",
        "assistant",
        "skills",
        "web_tools",
        "network",
        "llm_gateway",
        "channels",
        "hub_images",
    ):
        if section in sent:
            not_null(getattr(patch, section), section)
    # 웹 Tool 과 네트워크의 값 뜻은 서비스가 본다 (P39b 15절). 정규화한 값을 저장한다
    if "web_tools" in sent:
        patch.web_tools = check_web_tools(not_null(patch.web_tools, "web_tools"))
    if "network" in sent:
        patch.network = check_network(not_null(patch.network, "network"))
    changes = patch.model_dump(include=sent - {"channels", "hub_images"})
    # 이 읽기가 저장의 기준이다. 그사이 다른 저장이 지나갔으면 409 이므로, 저장이 맞으면 before 는
    # 이 저장이 덮어쓴 값 그대로다 (_drop_orphan_keys 가 기댄다)
    before = await store.get()
    if patch.channels is not None:
        changes["channels"] = _channels(request, before.channels, patch.channels).model_dump()
    if patch.hub_images is not None:
        hub_images = await _hub_images(request, before, patch.hub_images)
        changes["hub_images"] = hub_images.model_dump()
    _check_gateway(request, before, changes)
    after = await _save(store, changes, user, before, document="platform")
    if "channels" in changes:
        await _after_teams_change(request, before.channels.teams, after.channels.teams)
    if before.hub_images.credential_set and not after.hub_images.credential_set:
        await _drop_hub_images_credential(request)
    if "llm_providers" in changes or "web_tools" in changes:
        await _drop_orphan_keys(request, before, after)
    if "web_tools" in changes:
        # 켠 Tool 의 허브 항목을 바로 만든다. 리컨실 단계도 틱마다 맞춘다 (P39b 13절)
        await request.app.state.platform_tools.sync(after)
    return _out(after)


def _channels(request: Request, before: ChannelsSettings, patch: ChannelsPatch) -> ChannelsSettings:
    """보낸 칸을 얹은 `channels` (P47 6.1절). Teams 를 켜려면 App ID, Entra 테넌트 id, Client Secret
    이 모두 있어야 하고(422 teams_not_configured) 사람을 확인하는 모드여야 한다(422
    channels_need_token_auth). F0.2 를 반영한 묶음 전에는 켜는 저장을 409 teams_not_ready 로
    거절한다."""
    if patch.teams is None:
        return before
    sent = patch.teams.model_fields_set
    if "enabled" in sent:
        not_null(patch.teams.enabled, "channels.teams.enabled")
    teams = before.teams.model_copy(update={k: getattr(patch.teams, k) for k in sent})
    if teams.enabled and not before.teams.enabled:
        if not teams.configured:
            raise error(
                422,
                "teams_not_configured",
                "enter the App ID, the Entra tenant id and the client secret first",
            )
        if not request.app.state.settings.token_auth:
            raise error(
                422, "channels_need_token_auth", "Teams works only in local or oidc auth mode"
            )
        raise error(409, "teams_not_ready", "Teams cannot be turned on before bundle A2 (P47)")
    return before.model_copy(update={"teams": teams})


async def _hub_images(
    request: Request, before: PlatformSettings, patch: HubImagesPatch
) -> HubImagesSettings:
    """보낸 칸을 얹은 `hub_images` (P55 6절). 값의 꼴은 모델이, 뜻(접두사, P-256 키, 제품 이미지
    접두사와의 겹침)은 `check_hub_images` 가 본다. 읽기 자격 증명의 표시는 이 저장이 켜지 않는다.
    접두사의 호스트가 바뀌면 자격 증명은 다른 호스트로 가지 않게 끈다(Secret 은 저장 뒤
    지운다)."""
    update = patched_values(patch, patch.model_fields_set, skip=())
    for k in ("public_keys", "revoked_keys", "refused_digests"):
        if k in update:
            not_null(update[k], f"hub_images.{k}")
    old = before.hub_images
    try:
        merged = HubImagesSettings.model_validate(
            {**old.model_dump(), **update, "credential_set": old.credential_set}
        )
    except ValidationError as e:
        raise error(422, "invalid_value", str(e)) from e
    clusters = await request.app.state.clusters.list()
    checked = check_hub_images(merged, clusters, before=old, error_cls=ServiceError)
    if old.credential_set and prefix_host(checked.prefix) != prefix_host(old.prefix):
        checked = checked.model_copy(update={"credential_set": False})
    return checked


async def _drop_hub_images_credential(request: Request) -> None:
    """접두사의 호스트가 바뀌어 끈 읽기 자격 증명의 Secret 을 지운다 (P55 6절). 지우기 실패는
    저장을 되돌리지 않고 기록만 한다. 표시가 꺼져 있어 그 값은 다시 쓰이지 않는다."""
    store = request.app.state.cluster_service.secret_store()
    if store is None:
        return
    try:
        await run_in_threadpool(
            store.delete, CREDENTIAL_SECRET, namespace=request.app.state.settings.namespace
        )
    except Exception as e:  # noqa: BLE001 - 청소 실패가 저장을 되돌리면 안 된다
        logger.warning("hub images credential Secret not deleted: %s", e)


async def _after_teams_change(
    request: Request, before: TeamsSettings, after: TeamsSettings
) -> None:
    """App ID 나 Entra 테넌트 id 가 바뀌면 모든 연결을 끊고 받아 둔 봇 토큰을 버린다. 두 값이 연결이
    든 Teams 계정을 이름 짓기 때문이다 (6.1절, 6.3절)."""
    if (before.app_id, before.entra_tenant_id) == (after.app_id, after.entra_tenant_id):
        return
    request.app.state.teams_connector.forget_tokens()
    cut = await request.app.state.channel_links.cut_all(CUT_BOT_CHANGED)
    if cut:
        logger.info("teams bot changed: %d links cut", cut)


def _check_gateway(request: Request, before: PlatformSettings, changes: dict[str, Any]) -> None:
    """LLM 게이트웨이의 한도와 켠 게이트웨이의 provider 주소를 본다 (P38 7절). 저장될
    설정 전체로 본다."""
    if not changes.keys() & {"llm_gateway", "llm_providers"}:
        return
    try:
        after = PlatformSettings.model_validate(before.model_dump() | changes)
    except ValidationError:
        # 문서 전체가 틀린 저장은 저장이 그 까닭과 함께 422 로 거절한다
        return
    check_limits(after.llm_gateway.limits, after, tenant_ok=lambda t: bool(TENANT_RE.fullmatch(t)))
    check_upstreams(after, request.app.state.settings)


def _one_line_key(body: ApiKeyIn) -> str:
    """앞뒤 공백을 뗀 키. 한 줄이 아니거나 공백·제어 문자가 있으면 422 invalid_api_key 다."""
    key = one_line(body.api_key)
    if key is None:
        raise error(422, "invalid_api_key", "the API key must be one line without spaces")
    return key


def _key_secrets(ps: PlatformSettings) -> set[str]:
    """provider 들과 웹 검색이 가리키는 키 Secret 이름."""
    names = {p.api_key_secret for p in ps.llm_providers if p.api_key_secret}
    if ps.web_tools.search.api_key_secret:
        names.add(ps.web_tools.search.api_key_secret)
    return names


def _managed(name: str) -> bool:
    """플랫폼이 만든 키 Secret 인가. provider 의 것과 웹 검색의 것이다."""
    return is_managed_secret(name) or name == SEARCH_KEY_SECRET


async def _drop_orphan_keys(
    request: Request, before: PlatformSettings, after: PlatformSettings
) -> None:
    """provider 를 지우거나 다른 Secret 을 가리키게 바꿔, 또는 웹 검색의 키 칸을 비워 아무도 쓰지
    않게 된 관리 Secret 을 지운다 (D56). 키가 든 Secret 을 남겨 두지 않는다. 운영자가 만든 Secret 은
    이름이 달라 대상이 아니다. 지우기 실패는 설정 저장을 되돌리지 않고 기록만 한다. 이 저장 뒤 다른
    저장이나 키 저장이 같은 이름을 다시 가리켰을 수 있으므로, 지우기 바로 전에 다시 읽어 아직 쓰는
    이름은 두고 간다."""
    orphans = {n for n in _key_secrets(before) - _key_secrets(after) if _managed(n)}
    store = request.app.state.cluster_service.secret_store() if orphans else None
    if store is None:
        return
    namespace = request.app.state.settings.namespace
    settings_store: MongoPlatformSettingsStore = request.app.state.platform_settings
    for name in sorted(orphans):
        if name in _key_secrets(await settings_store.get()):
            continue
        try:
            await run_in_threadpool(store.delete, name, namespace=namespace)
        except Exception as e:  # noqa: BLE001 - 청소 실패가 저장을 되돌리면 안 된다
            logger.warning("provider key Secret %s not deleted: %s", name, e)


@router.put(
    "/llm-providers/{name}/api-key",
    response_model=LLMProviderKeyOut,
    dependencies=[Depends(require_scope("settings:write"))],
)
async def put_llm_provider_key(
    store: SettingsStore, request: Request, name: str, body: ApiKeyIn, user: User
) -> dict[str, Any]:
    """provider 의 API 키를 받아 플랫폼 네임스페이스의 관리 Secret(`daegam-llm-key-<이름>`,
    항목 `api_key`)에 넣고 provider 의 `api_key_secret` 을 그 이름으로 맞춘다 (D56). 사용자는
    클러스터에 들어갈 수 없으므로 키를 넣는 길은 이것이다. 같은 provider 에 다시 넣으면 키를
    바꾼다. 키는 응답·감사·Mongo 에 남지 않는다. 감사 action 은 핸들러 이름이다."""
    ps = await store.get()
    cfg = provider_named(ps.llm_providers, name)
    if cfg is None:
        raise not_found(f"llm provider {name!r} is not configured")
    if cfg.is_stub:
        raise error(422, "stub_has_no_key", "kind: stub providers take no API key", provider=name)
    key = _one_line_key(body)
    secrets = require_secret_store(request, name)
    secret = managed_secret_name(name)
    namespace = request.app.state.settings.namespace
    await run_in_threadpool(secrets.put, secret, {SECRET_KEY: key}, namespace=namespace)
    # 그 provider 의 칸만 바꾼다. 읽어 둔 목록을 통째로 쓰면 Secret 을 쓰는 사이 더해진 provider 가
    # 지워진다 (P68 2절)
    after = await store.set_provider_key_secret(name, secret, updated_by=user.sub)
    if after is None:
        # 그사이 provider 가 지워졌다. 방금 쓴 키를 남겨 두지 않는다
        await run_in_threadpool(secrets.delete, secret, namespace=namespace)
        raise not_found(f"llm provider {name!r} is not configured")
    if cfg.api_key_secret != secret:
        await _drop_orphan_keys(request, ps, after)
    await _sync_gateway_key(request, name)
    return {"name": name, "api_key_secret": secret}


async def _sync_gateway_key(request: Request, name: str) -> None:
    """바꾼 키를 LLM 게이트웨이의 사본에 곧바로 넣는다 (P38 4절). 실패해도 저장은 그대로이고 다음
    리컨실 틱이 잇는다."""
    try:
        await request.app.state.llm_gateway.sync_provider(name)
    except Exception as e:  # noqa: BLE001 - 키 저장을 되돌리지 않는다
        logger.warning("llm gateway key sync for %s failed: %s", name, e)


@router.post(
    "/llm-providers/{name}/check",
    response_model=LLMProviderCheckOut,
    dependencies=[Depends(require_scope("settings:write"))],
)
async def check_llm_provider(store: SettingsStore, request: Request, name: str) -> dict[str, Any]:
    """저장된 provider 의 키 경로와 연결을 확인한다. 플랫폼 네임스페이스 Secret 의 존재·키 유무를
    본 뒤 `models.list` 를 한 번 부르고, 모델 목록을 키 없이 주는 게이트웨이(OpenRouter)는 키 확인
    경로를 한 번 더 부른다. 응답은 고정 코드뿐이다. Secret 저장소(home 클러스터)가
    없으면 job 생성과 같은 422 no_secret_store 다. 감사 action 은 핸들러 이름
    check_llm_provider 다."""
    cfg = (await store.get()).provider(name)
    if cfg is None:
        raise not_found(f"llm provider {name!r} is not configured")
    if cfg.is_stub:
        # 키도 연결도 없다. 등록돼 있다는 것 자체가 준비 상태다
        return {"ok": True, "code": "ok"}
    result = await check_provider(
        cfg,
        require_secret_store(request, cfg.name, verb="read"),
        namespace=request.app.state.settings.namespace,
        # 테스트가 httpx MockTransport 클라이언트를 꽂는 자리. 운영에서는 없다
        http_client=getattr(request.app.state, "llm_http_client", None),
    )
    return {"ok": result.ok, "code": result.code}


@router.put(
    "/web-tools/search/api-key",
    response_model=WebSearchKeyOut,
    dependencies=[Depends(require_scope("settings:write"))],
)
async def put_web_search_key(
    store: SettingsStore, request: Request, body: ApiKeyIn, user: User
) -> dict[str, Any]:
    """웹 검색 provider 의 키를 플랫폼 네임스페이스의 관리 Secret(`daegam-web-search-key`, 항목
    `api_key`)에 넣고 `web_tools.search.api_key_secret` 을 그 이름으로 맞춘다 (D56, P39b 15절). 키는
    응답·감사·Mongo 에 남지 않는다. searxng·stub 은 키가 없어도 된다. 감사 action 은 핸들러
    이름이다."""
    key = _one_line_key(body)
    secrets = require_secret_store(request, "web-search")
    namespace = request.app.state.settings.namespace
    await run_in_threadpool(secrets.put, SEARCH_KEY_SECRET, {SECRET_KEY: key}, namespace=namespace)
    before = await store.get()
    search = before.web_tools.search.model_copy(update={"api_key_secret": SEARCH_KEY_SECRET})
    web_tools = before.web_tools.model_copy(update={"search": search})
    after = await store.update(
        {"web_tools": web_tools.model_dump()}, updated_by=user.sub, base=before
    )
    if after is None:
        raise revision_conflict(error, "platform", "settings")
    request.app.state.web_search.forget_key()
    return {"api_key_secret": SEARCH_KEY_SECRET}


@router.post(
    "/web-tools/search/check",
    response_model=WebSearchCheckOut,
    dependencies=[Depends(require_scope("settings:write"))],
)
async def check_web_search(request: Request) -> dict[str, Any]:
    """저장된 검색 설정으로 질의 하나를 보내 본다. 고정 코드와 걸린 시간만 돌려준다 (P39b 15절)."""
    return await request.app.state.web_search.check()


@router.get(
    "/hub-images/status",
    response_model=HubImagesStatusOut,
    dependencies=[Depends(require_platform_admin)],
)
async def get_hub_images_status(store: SettingsStore, request: Request) -> dict[str, Any]:
    """허브 이미지 설정 카드의 계산한 값 (P55 6절). 공개 키의 지문과 회수 여부, 제품 이미지 접두사가
    허브 접두사와 겹치는 클러스터, 거부한 digest 마다 그것을 든 항목과 그 항목을 쓰는 Gateway 의
    수다. 모든 테넌트의 항목을 실으므로 platform-admin 만 부른다."""
    hub = (await store.get()).hub_images
    revoked = set(hub.revoked_keys)
    clusters = await request.app.state.clusters.list()
    return {
        "configured": hub.configured,
        "keys": [{"fingerprint": f, "revoked": f in revoked} for f in key_fingerprints(hub)],
        "overlaps": [
            {
                "cluster_id": c.id,
                "cluster_name": c.name,
                "registry_prefix": c.images.registry_prefix or "",
            }
            for c in overlapping_clusters(hub.prefix, clusters)
        ],
        "refused": await request.app.state.hub_service.refused_usage(hub.refused_digests),
    }


@router.put(
    "/hub-images/credential",
    response_model=HubImagesCredentialOut,
    dependencies=[Depends(require_scope("settings:write"))],
)
async def put_hub_images_credential(
    store: SettingsStore, request: Request, body: HubImagesCredentialIn, user: User
) -> dict[str, Any]:
    """허브 레지스트리의 읽기 자격 증명을 플랫폼 네임스페이스의 관리 Secret
    (`daegam-hub-images-read`, 항목 `username`·`password`)에 넣고 `hub_images.credential_set` 을
    켠다 (P55 6절, D56). 값은 응답·감사·Mongo 에 남지 않는다. 표시는 그 칸만 그 자리에서 쓴다."""
    username = one_line(body.username)
    if username is None:
        raise error(422, "invalid_value", "the user name must be one line without spaces")
    if body.password != body.password.strip() or any(
        ord(c) < 0x20 or ord(c) == 0x7F for c in body.password
    ):
        raise error(
            422,
            "invalid_value",
            "the password must be one line without control characters or surrounding spaces",
        )
    secrets = require_secret_store(request, "hub-images")
    namespace = request.app.state.settings.namespace
    await run_in_threadpool(
        secrets.put,
        CREDENTIAL_SECRET,
        {CREDENTIAL_USERNAME: username, CREDENTIAL_PASSWORD: body.password},
        namespace=namespace,
    )
    await store.mark_hub_images_credential(updated_by=user.sub)
    return {"credential_set": True}
