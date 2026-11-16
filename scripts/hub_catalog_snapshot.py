"""toolhive-catalog 릴리스 하나를 허브의 카탈로그 스냅숏으로 옮긴다 (P55 4절).

그 릴리스의 자산 `toolhive-registry-upstream.json` 을 정한 주소에서 받아 그 sha256 을 적고, 서버마다
허브가 쓰는 칸만 남긴 JSON 을 `platform/daegam/data/hub_catalog.json` 에 쓴다. 지원 중단
(`Deprecated`) 서버는 뺀다. 라이선스는 그 태그의 저장소 라이선스이고, 이미지마다 레지스트리의
매니페스트 목록을 익명으로 읽어 아키텍처를 적는다. 읽기 하나가 실패하면 쓰지 않고 멈춘다.
`--no-registry` 는 아키텍처를 읽지 않는다. 받기는 `scripts/lib/public_fetch.py` 가 하므로 사내
방화벽 뒤에서는 DAEGAM_CA_FILE 을 둔다. 스냅숏은 커밋하므로 갱신마다 검토하는 diff 가 되고, 실행
중에는 아무것도 인터넷에서 받지 않는다. 갱신은 릴리스 때 한다.

    uv run --project platform python -I scripts/hub_catalog_snapshot.py <release> [--no-registry]

원격 서버의 자격 증명은 구조로 다 오지 않는다. 헤더는 `headers` 칸이 있을 때만 구조이고, 나머지는
설명 글(`overview`)에만 있다 (F0 항목 2). 그래서 글에 헤더·키·토큰 낱말이 있는 원격은 사람이 읽어
판정한 것을 `scripts/hub_catalog_review.json` 에 적는다. 판정하지 않은 원격이 낱말에 걸리면
스크립트는 실패한다. 새 릴리스의 낱말 일치를 읽고 판정을 더하라는 뜻이다.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

from daegam.services.hub_catalog import normalize_image

# -I 가 PYTHONPATH 를 보지 않으므로 scripts/lib 를 직접 더한다
sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
import oci_registry  # noqa: E402
from public_fetch import fetch  # noqa: E402

OUT = Path(__file__).resolve().parents[1] / "platform" / "daegam" / "data" / "hub_catalog.json"
CATALOG_REPO = "stacklok/toolhive-catalog"
ASSET = "toolhive-registry-upstream.json"
ASSET_URL = "https://github.com/{repo}/releases/download/{release}/{asset}"
LICENSE_URL = "https://api.github.com/repos/{repo}/license?ref={release}"
TIMEOUT = 30.0
VAR = re.compile(r"\{[A-Za-z_][A-Za-z0-9_]*\}")
HEADER_WORDS = re.compile(
    r"\b(header|x-api-key|api key|api-key|bearer|authorization|token)\b", re.I
)

# 사람이 설명 글을 읽고 판정한 원격의 자격 증명 (서버 이름 → 판정). `headers` 는 그 서버가 요구하는
# 헤더이고 `url_secret` 은 키를 주소(질의 문자열)에 넣는 서버다. `none` 은 낱말에 걸렸지만 자격
# 증명 없이 쓰는 서버다. `evidence` 는 판정의 근거로 읽은 글이다. 구조로 온 `headers` 칸은 여기
# 적지 않아도 읽는다
REVIEW_FILE = Path(__file__).resolve().with_name("hub_catalog_review.json")
REVIEWED_REMOTES: dict[str, dict[str, Any]] = json.loads(REVIEW_FILE.read_text(encoding="utf-8"))


def meta(server: dict[str, Any]) -> dict[str, Any]:
    """ToolHive 가 `_meta` 아래 두는 칸 (서버마다 값이 하나다)."""
    block = server["_meta"]["io.modelcontextprotocol.registry/publisher-provided"][
        "io.github.stacklok"
    ]
    return next(iter(block.values()))


def short_name(server: dict[str, Any]) -> str:
    return server["name"].split("/", 1)[-1]


def split_ref(ref: str) -> tuple[str, str]:
    """`registry/repo:tag` 를 (저장소, 태그)로 나눈다. 태그가 없으면 latest 다. 저장소는 적힌
    그대로다. 정규화는 플랫폼의 `services/hub_catalog.py` `normalize_image` 가 한다."""
    last = ref.rsplit("/", 1)[-1]
    if ":" in last:
        repo, _, tag = ref.rpartition(":")
        return repo, tag
    return ref, "latest"


class RegistryReadFailed(RuntimeError):
    """이미지의 매니페스트 목록을 읽지 못했다. 빈 아키텍처로 스냅숏을 쓰지 않는다."""


def platforms(repository: str, tag: str) -> list[str]:
    """이미지의 아키텍처 (`linux/arm64` 꼴). 읽기는 `scripts/lib/oci_registry.py` 의 익명
    클라이언트가 하고, 호스트 규칙은 플랫폼의 `normalize_image` 다. 읽지 못하면
    `RegistryReadFailed` 다."""
    ref = normalize_image(f"{repository}:{tag}")
    try:
        raw, _, _ = oci_registry.Client(ref.host).manifest(ref.repository, tag)
        doc = json.loads(raw)
    except (oci_registry.RegistryError, OSError, ValueError) as e:
        raise RegistryReadFailed(f"{repository}: manifest {e}") from e
    found = sorted(
        {
            f"{m['platform']['os']}/{m['platform']['architecture']}"
            for m in doc.get("manifests") or []
            if m.get("platform") and m["platform"].get("os") != "unknown"
        }
    )
    if not found:
        raise RegistryReadFailed(f"{repository}: no platforms in the manifest list")
    return found


def _env(defs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "name": e["name"],
            "description": e.get("description") or "",
            "required": bool(e.get("isRequired")),
            "secret": bool(e.get("isSecret")),
            "default": e.get("default"),
        }
        for e in defs
    ]


def _args(m: dict[str, Any], pkg: dict[str, Any]) -> list[str]:
    """컨테이너 인자. ToolHive 의 `args` 다음에 패키지 인자를 이름(있으면)과 값 순으로 붙인다."""
    out = [str(a) for a in m.get("args") or []]
    for a in pkg.get("packageArguments") or []:
        if a.get("type") == "named" and a.get("name"):
            out.append(str(a["name"]))
        if a.get("value") is not None:
            out.append(str(a["value"]))
    return out


def _common(server: dict[str, Any], m: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": short_name(server),
        "title": server.get("title") or short_name(server),
        "description": server.get("description") or "",
        "tier": m.get("tier") or "",
        "status": m.get("status") or "",
        "tags": list(m.get("tags") or []),
        "tools": list(m.get("tools") or []),
        "repository_url": (server.get("repository") or {}).get("url"),
    }


def image_server(server: dict[str, Any], *, read_registry: bool) -> dict[str, Any]:
    m = meta(server)
    pkg = server["packages"][0]
    repository, tag = split_ref(pkg["identifier"])
    transport = pkg["transport"]["type"]
    stdio = transport == "stdio"
    port = None
    if not stdio:
        found = re.search(r":(\d+)", pkg["transport"].get("url") or "")
        port = int(found.group(1)) if found else None
    args = _args(m, pkg)
    return {
        **_common(server, m),
        "kind": "image",
        "image": {"repository": repository, "tag": tag},
        "stdio": stdio,
        # 소비자가 말하는 전송이다. stdio 컨테이너는 프록시가 streamable-http 로 낸다 (5절)
        "transport": "streamable-http" if stdio else transport,
        "port": port,
        "args": args,
        "args_variables": any(VAR.search(a) for a in args),
        "env": _env(pkg.get("environmentVariables") or []),
        "platforms": platforms(repository, tag) if read_registry else [],
        "provenance": m.get("provenance"),
    }


def remote_server(server: dict[str, Any]) -> dict[str, Any]:
    m = meta(server)
    remote = server["remotes"][0]
    name = short_name(server)
    reviewed = REVIEWED_REMOTES.get(name, {})
    headers = [
        {
            "name": h["name"],
            "description": h.get("description") or "",
            "required": bool(h.get("isRequired")),
            "secret": bool(h.get("isSecret")),
        }
        for h in remote.get("headers") or []
    ]
    known = {h["name"].lower() for h in headers}
    for h in reviewed.get("headers") or []:
        if h["name"].lower() not in known:
            headers.append({**h, "required": True, "secret": True})
    variables = remote.get("variables") or {}
    secret_url = bool(reviewed.get("url_secret")) or any(
        bool(v.get("isSecret")) for v in variables.values()
    )
    return {
        **_common(server, m),
        "kind": "remote",
        "url": remote["url"],
        "transport": remote["type"],
        "url_template": bool(VAR.search(remote["url"])),
        "url_secret_variables": secret_url,
        "headers": headers,
        "oauth_config": m.get("oauth_config"),
    }


def unreviewed(servers: list[dict[str, Any]]) -> list[str]:
    """설명 글이 헤더·키·토큰을 말하는데 판정이 없는 원격. OAuth 원격과 구조로 온 헤더만 있는 원격은
    판정이 없어도 된다."""
    out = []
    for s in servers:
        m = meta(s)
        if not s.get("remotes") or m.get("oauth_config") or short_name(s) in REVIEWED_REMOTES:
            continue
        if HEADER_WORDS.search(m.get("overview") or ""):
            out.append(short_name(s))
    return out


def convert(
    doc: dict[str, Any],
    release: str,
    *,
    license_id: str,
    asset_sha256: str | None = None,
    read_registry: bool = True,
) -> dict[str, Any]:
    """릴리스 문서를 스냅숏으로 옮긴다. 이름 순이고 지원 중단 서버는 뺀다."""
    servers = [s for s in doc["data"]["servers"] if meta(s).get("status") != "Deprecated"]
    missing = unreviewed(servers)
    if missing:
        raise SystemExit(
            "remotes whose overview mentions a header, key or token have no reviewed entry in "
            f"{REVIEW_FILE.name}: {', '.join(missing)}"
        )
    out = []
    for s in servers:
        if s.get("packages"):
            out.append(image_server(s, read_registry=read_registry))
        elif s.get("remotes"):
            out.append(remote_server(s))
    return {
        "catalog": CATALOG_REPO,
        "release": release,
        "asset_sha256": asset_sha256,
        "last_updated": (doc.get("meta") or {}).get("last_updated"),
        "license": license_id,
        "servers": sorted(out, key=lambda s: s["name"]),
    }


def main(argv: list[str]) -> int:
    args = [a for a in argv if not a.startswith("--")]
    if len(args) != 1:
        print(__doc__, file=sys.stderr)
        return 2
    release = args[0]
    raw = fetch(ASSET_URL.format(repo=CATALOG_REPO, release=release, asset=ASSET), TIMEOUT)
    licence = json.loads(fetch(LICENSE_URL.format(repo=CATALOG_REPO, release=release), TIMEOUT))
    try:
        snapshot = convert(
            json.loads(raw),
            release,
            license_id=licence["license"]["spdx_id"],
            asset_sha256=hashlib.sha256(raw).hexdigest(),
            read_registry="--no-registry" not in argv,
        )
    except RegistryReadFailed as e:
        print(f"snapshot not written: {e}", file=sys.stderr)
        return 1
    OUT.write_text(json.dumps(snapshot, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    images = [s for s in snapshot["servers"] if s["kind"] == "image"]
    print(f"{OUT}: {len(snapshot['servers'])} servers ({len(images)} images)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
