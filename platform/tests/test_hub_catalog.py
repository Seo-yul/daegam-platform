"""P55 4절: 카탈로그 스냅숏의 변환, 참조 정규화, `GET /api/hub/catalog`. P86 A3: 자격 증명이 비밀
헤더 하나뿐인 원격을 그 헤더로 가져오는 규칙."""

from __future__ import annotations

import json

import pytest

from daegam.services.hub import HubError
from daegam.services.hub_catalog import (
    SNAPSHOT_FILE,
    Catalog,
    CatalogHeader,
    CatalogImage,
    CatalogServer,
    catalog_remote,
    mirrored_reference,
    normalize_image,
    snapshot,
)
from daegam.services.outside_headers import check_header_name
from tests.helpers.consts import HUB, REPO
from tests.helpers.source import runtime_module

script = runtime_module(REPO / "scripts" / "hub_catalog_snapshot.py")

STACKLOK = "io.modelcontextprotocol.registry/publisher-provided"


def _server(name: str, meta: dict, **body) -> dict:
    """릴리스 자산 꼴의 서버 하나. 메타는 ToolHive 가 두는 자리에 둔다."""
    return {
        "name": f"io.github.stacklok/{name}",
        "title": name.title(),
        "description": f"{name} server",
        "_meta": {STACKLOK: {"io.github.stacklok": {"key": {"status": "Active", **meta}}}},
        **body,
    }


def _release(*servers: dict) -> dict:
    return {"meta": {"last_updated": "2026-10-26T00:00:00Z"}, "data": {"servers": list(servers)}}


def test_convert_keeps_the_fields_section_4_names() -> None:
    doc = _release(
        _server(
            "stdio-one",
            {"tier": "Official", "tools": ["a", "b"], "args": ["--x"], "provenance": {"r": 1}},
            packages=[
                {
                    "identifier": "ghcr.io/org/img:1.2",
                    "transport": {"type": "stdio"},
                    "environmentVariables": [
                        {"name": "TOKEN", "isSecret": True, "isRequired": True},
                        {"name": "MODE", "default": "ro", "description": "mode"},
                    ],
                    "packageArguments": [
                        {"type": "named", "name": "--key", "value": "{API_KEY}"},
                    ],
                }
            ],
        ),
        _server(
            "http-one",
            {},
            packages=[
                {
                    "identifier": "nginx",
                    "transport": {"type": "sse", "url": "http://localhost:9090"},
                }
            ],
        ),
        _server("old", {"status": "Deprecated"}, packages=[{"identifier": "x", "transport": {}}]),
        _server(
            "plain-remote",
            {"oauth_config": {"authorize_url": "https://a"}},
            remotes=[{"type": "streamable-http", "url": "https://r.example.com/mcp"}],
        ),
    )
    out = script.convert(doc, "v0.1.0", license_id="Apache-2.0", read_registry=False)
    assert [s["name"] for s in out["servers"]] == ["http-one", "plain-remote", "stdio-one"]
    assert out["release"] == "v0.1.0" and out["license"] == "Apache-2.0"
    stdio = out["servers"][2]
    assert stdio["image"] == {"repository": "ghcr.io/org/img", "tag": "1.2"}
    assert stdio["stdio"] is True and stdio["transport"] == "streamable-http"
    assert stdio["args"] == ["--x", "--key", "{API_KEY}"] and stdio["args_variables"] is True
    assert stdio["env"][0] == {
        "name": "TOKEN",
        "description": "",
        "required": True,
        "secret": True,
        "default": None,
    }
    assert stdio["provenance"] == {"r": 1} and stdio["tools"] == ["a", "b"]
    http = out["servers"][0]
    assert http["image"] == {"repository": "nginx", "tag": "latest"}
    assert (http["transport"], http["port"], http["stdio"]) == ("sse", 9090, False)
    remote = out["servers"][1]
    assert remote["oauth_config"] == {"authorize_url": "https://a"}


def test_convert_merges_structured_and_reviewed_remote_headers() -> None:
    doc = _release(
        _server(
            "21st-remote",
            {"overview": "send the x-api-key header"},
            remotes=[
                {
                    "type": "streamable-http",
                    "url": "https://21st.dev/api/mcp",
                    "headers": [{"name": "X-Extra", "isSecret": True}],
                }
            ],
        )
    )
    remote = script.convert(doc, "v1", license_id="Apache-2.0", read_registry=False)["servers"][0]
    assert [(h["name"], h["required"]) for h in remote["headers"]] == [
        ("X-Extra", False),
        ("x-api-key", True),
    ]


def test_convert_stops_on_a_remote_nobody_reviewed() -> None:
    """설명 글이 키를 말하는 원격은 사람이 판정해야 스냅숏이 된다 (F0 항목 2)."""
    doc = _release(
        _server(
            "new-remote",
            {"overview": "Authenticate with an API key"},
            remotes=[{"type": "streamable-http", "url": "https://n.example.com/mcp"}],
        )
    )
    with pytest.raises(SystemExit, match="new-remote"):
        script.convert(doc, "v1", license_id="Apache-2.0", read_registry=False)


def test_committed_snapshot_reads_and_holds_no_deprecated_server() -> None:
    raw = json.loads(SNAPSHOT_FILE.read_text(encoding="utf-8"))
    cat = snapshot()
    names = [s.name for s in cat.servers]
    assert names == sorted(names) and len(names) == len(set(names))
    assert len(cat.servers) == len(raw["servers"]) > 100
    assert all(s.status != "Deprecated" for s in cat.servers)
    assert cat.release == raw["release"] and cat.license == "Apache-2.0"
    images = [s for s in cat.servers if s.kind == "image"]
    # 스냅숏을 만들 때 이미지마다 아키텍처를 읽었다. 읽지 못하면 스크립트는 쓰지 않는다
    assert images and all(s.platforms for s in images)
    assert all(s.image is not None for s in images)
    assert raw["asset_sha256"] and len(raw["asset_sha256"]) == 64


def test_committed_servers_have_the_keys_the_script_writes() -> None:
    """커밋한 스냅숏의 서버마다 칸이 지금 스크립트가 그 종류에 쓰는 칸과 같다. 스크립트를 고치고
    스냅숏을 다시 만들지 않았으면 걸린다."""
    doc = _release(
        _server(
            "img", {}, packages=[{"identifier": "ghcr.io/a/b:1", "transport": {"type": "stdio"}}]
        ),
        _server("rem", {}, remotes=[{"type": "streamable-http", "url": "https://r.example/mcp"}]),
    )
    out = script.convert(doc, "v1", license_id="Apache-2.0", read_registry=False)
    keys = {s["kind"]: set(s) for s in out["servers"]}
    raw = json.loads(SNAPSHOT_FILE.read_text(encoding="utf-8"))
    assert set(raw) == set(out)
    for server in raw["servers"]:
        assert set(server) == keys[server["kind"]], server["name"]


@pytest.mark.parametrize(
    ("ref", "host", "repository", "tag", "digest", "path"),
    [
        ("nginx", "docker.io", "library/nginx", None, None, "docker.io/library/nginx"),
        ("user/repo:1", "docker.io", "user/repo", "1", None, "docker.io/user/repo"),
        ("docker.io/nginx:1", "docker.io", "library/nginx", "1", None, "docker.io/library/nginx"),
        ("index.docker.io/a/b", "docker.io", "a/b", None, None, "docker.io/a/b"),
        ("GHCR.io/Org/img:2", "ghcr.io", "Org/img", "2", None, "ghcr.io/Org/img"),
        (
            "Localhost:5000/x/y@sha256:ab",
            "localhost:5000",
            "x/y",
            None,
            "sha256:ab",
            "localhost_5000/x/y",
        ),
        ("reg.example.com:443/a:t@sha256:cd", "reg.example.com", "a", "t", "sha256:cd", None),
    ],
)
def test_normalize_image(ref, host, repository, tag, digest, path) -> None:
    got = normalize_image(ref)
    assert (got.host, got.repository, got.tag, got.digest) == (host, repository, tag, digest)
    if path:
        assert got.mirror_path == path


def test_mirrored_reference() -> None:
    image = CatalogImage(repository="ghcr.io/org/img", tag="1.0")
    assert mirrored_reference(None, image) is None
    assert (
        mirrored_reference("registry.example.com/hub/", image)
        == "registry.example.com/hub/ghcr.io/org/img:1.0"
    )
    assert mirrored_reference("p", CatalogImage(repository="nginx", tag="1")) == (
        "p/docker.io/library/nginx:1"
    )


def test_catalog_remote_checks_the_snapshot_server() -> None:
    cat = snapshot()
    plain = next(
        s
        for s in cat.servers
        if s.kind == "remote"
        and s.transport != "sse"
        and not s.needs_login
        and not s.needs_credentials
    )
    got = catalog_remote(
        cat, plain.name, url=plain.url, transport=plain.transport, error_cls=HubError
    )
    assert got.name == plain.name
    with pytest.raises(HubError) as e:
        catalog_remote(
            cat,
            plain.name,
            url="https://other.example.com/mcp",
            transport=plain.transport,
            error_cls=HubError,
        )
    assert e.value.code == "invalid_value"
    login = next(s for s in cat.servers if s.needs_login)
    with pytest.raises(HubError, match="sign-in"):
        catalog_remote(
            cat, login.name, url=login.url, transport=login.transport, error_cls=HubError
        )
    image = next(s for s in cat.servers if s.kind == "image")
    with pytest.raises(HubError, match="no remote server"):
        catalog_remote(cat, image.name, url=None, transport=None, error_cls=HubError)
    # SSE 로만 말하는 원격은 주소와 전송이 맞아도 가져오지 못한다 (P54b 15절 3)
    sse = next(s for s in cat.servers if s.kind == "remote" and s.transport == "sse")
    with pytest.raises(HubError) as e:
        catalog_remote(cat, sse.name, url=sse.url, transport=sse.transport, error_cls=HubError)
    assert (e.value.status, e.value.code) == (422, "outside_sse_unsupported")


KEY_URL = "https://key.example.com/mcp"


def _remote(*headers: CatalogHeader, **fields) -> CatalogServer:
    return CatalogServer.model_validate(
        {
            "name": "keyed",
            "title": "Keyed",
            "kind": "remote",
            "transport": "streamable-http",
            "url": KEY_URL,
            "headers": [h.model_dump() for h in headers],
            **fields,
        }
    )


KEY = CatalogHeader(name="X-Api-Key", required=True, secret=True)


@pytest.mark.parametrize(
    ("server", "want"),
    [
        (_remote(KEY), "X-Api-Key"),
        # 비밀이 아니거나 필요하지 않은 헤더는 따지지 않는다
        (_remote(KEY, CatalogHeader(name="X-Opt", secret=True)), "X-Api-Key"),
        (_remote(CatalogHeader(name="Authorization", secret=True)), None),
        (_remote(), None),
        (_remote(KEY, CatalogHeader(name="X-App-Key", required=True, secret=True)), None),
        (_remote(CatalogHeader(name="X-Region", required=True)), None),
        (_remote(KEY, url_secret_variables=True), None),
        (_remote(KEY, url_template=True), None),
        (_remote(KEY, oauth_config={"authorize_url": "https://a"}), None),
    ],
)
def test_import_header_names_the_one_secret_header_a_remote_needs(server, want) -> None:
    assert server.import_header == want
    assert server.model_dump(mode="json")["import_header"] == want


def test_a_remote_that_needs_a_key_is_imported_only_with_its_header() -> None:
    """자격 증명이 비밀 헤더 하나뿐인 원격은 그 헤더를 싣는 항목만 받는다. 이름은 대소문자를 가리지
    않는다. 헤더가 둘이거나 주소에 비밀 변수가 있으면 이름을 대도 받지 않는다 (P86 A3)."""
    two = CatalogHeader(name="X-App-Key", required=True, secret=True)
    cat = Catalog(
        catalog="c",
        release="r",
        license="Apache-2.0",
        servers=[
            _remote(KEY),
            _remote(KEY, two, name="two"),
            _remote(KEY, name="url-secret", url_secret_variables=True),
        ],
    )

    def remote(name: str, header_name: str | None) -> CatalogServer:
        return catalog_remote(
            cat,
            name,
            url=KEY_URL,
            transport="streamable-http",
            header_name=header_name,
            error_cls=HubError,
        )

    assert remote("keyed", "x-api-key").name == "keyed"
    assert remote("keyed", "X-API-KEY").name == "keyed"
    for header_name in (None, "X-Other"):
        with pytest.raises(HubError, match="auth header X-Api-Key") as e:
            remote("keyed", header_name)
        assert e.value.code == "invalid_value"
    for name, header_name in (("two", "X-Api-Key"), ("url-secret", "X-Api-Key")):
        with pytest.raises(HubError, match="one required secret header") as e:
            remote(name, header_name)
        assert e.value.code == "invalid_value"


def test_every_import_header_of_the_snapshot_is_a_name_header_items_take() -> None:
    """화면이 고르게 하는 헤더 이름을 항목 만들기가 거절하지 않는다. 스냅숏을 새로 만들어 예약된
    이름이 들어오면 여기서 걸린다."""
    names = [s.import_header for s in snapshot().servers if s.import_header]
    assert names
    for name in names:
        assert check_header_name(name, HubError) == name


def test_catalog_route_returns_the_snapshot(client) -> None:
    """항목 id 경로보다 먼저 선언되어 404 가 아니다. 허브 이미지 설정 전이라 등록할 수 없고 옮긴
    참조도 없다."""
    r = client.get(f"{HUB}/catalog")
    assert r.status_code == 200, r.text
    body = r.json()
    cat = snapshot()
    assert body["release"] == cat.release and body["admission"] is False
    assert len(body["servers"]) == len(cat.servers)
    by_name = {s["name"]: s for s in body["servers"]}
    assert all(s["mirrored_image"] is None for s in body["servers"])
    assert by_name["github-remote"]["needs_login"] is True
    assert by_name["21st-remote"]["needs_credentials"] is True
    assert by_name["vastlint"]["needs_credentials"] is False
    # 화면은 이 칸으로 헤더를 골라 가져올 원격과 그 헤더를 안다 (P86 A3)
    assert by_name["21st-remote"]["import_header"] == "x-api-key"
    assert by_name["datadog-remote"]["import_header"] is None
    assert by_name["browserbase-remote"]["import_header"] is None
    assert by_name["vastlint"]["import_header"] is None
    assert by_name["github-remote"]["import_header"] is None
    assert by_name["fetch"]["import_header"] is None
    fetch = cat.server("fetch")
    assert fetch is not None and fetch.image is not None
    assert by_name["fetch"]["image"] == fetch.image.model_dump()


def test_a_failed_registry_read_stops_the_snapshot(monkeypatch) -> None:
    """방화벽이나 레지스트리 오류로 아키텍처를 읽지 못하면 빈 칸으로 쓰지 않고 멈춘다."""

    def missing(_client, repo, ref):
        raise script.oci_registry.RegistryError(404, f"GET {repo}@{ref}")

    monkeypatch.setattr(script.oci_registry.Client, "manifest", missing)
    doc = _release(
        _server(
            "img", {}, packages=[{"identifier": "ghcr.io/a/b:1", "transport": {"type": "stdio"}}]
        )
    )
    with pytest.raises(script.RegistryReadFailed, match="manifest 404"):
        script.convert(doc, "v1", license_id="Apache-2.0", read_registry=True)
