"""이미지를 고정하는 스크립트(scripts/resolve-images.sh 와 그것을 부르는 배포)를 레지스트리와
cosign 없이 돌리는 시험 도우미 (P85 6절).

- `FakeRegistry`: 이 컴퓨터의 평문 http 레지스트리(lib/registry_auth.py 는 127.0.0.1 만 http 로
  부른다). 받은 index 본문을 `/v2/<프로젝트>/<이름>/manifests/<태그나 digest>` 로 답하고
  Docker-Content-Digest 를 단다.
- `stand_in_bin`: 기록하는 cosign 을 둔 PATH 폴더. 고른 이미지의 확인만 실패한다.
- `checkout`: 스크립트가 도장을 읽는 자리(scripts/build-stamp.sh)만 고정 도장을 찍는 것으로 바꾼
  트리. 작업 트리에 커밋 안 된 변경이 있어도 도장이 -dirty 가 아니다. 나머지 파일은 저장소의
  것을 가리킨다.
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import json
import os
import shutil
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from tests.helpers.consts import REPO

PROJECT = "daegam"
INDEX_TYPE = "application/vnd.oci.image.index.v1+json"
# 고정 도장. 구성요소(scripts/build-stamp.sh 의 인자)마다 다르다
STAMPS = {
    "platform": "aaaa0001",
    "ui": "aaaa0002",
    "runtime": "aaaa0003",
    "agent": "aaaa0004",
    "mock-oidc": "aaaa0005",
    "web-tools": "aaaa0006",
    "sandbox": "aaaa0007",
}
# 구성요소의 이미지 이름 (scripts/lib/images.sh 의 spec)
IMAGES = {
    "platform": "daegam-platform",
    "ui": "daegam-ui",
    "runtime": "daegam-mcp-runtime",
    "agent": "daegam-agent-runtime",
    "web-tools": "daegam-web-tools",
    "sandbox": "daegam-sandbox",
}


def index(*arches: str, salt: str = "") -> bytes:
    """아키텍처마다 항목 하나인 OCI index 본문."""
    manifests = [
        {
            "digest": "sha256:" + hashlib.sha256(f"{salt}{arch}".encode()).hexdigest(),
            "platform": {"os": "linux", "architecture": arch},
        }
        for arch in arches
    ]
    return json.dumps(
        {"schemaVersion": 2, "mediaType": INDEX_TYPE, "manifests": manifests}
    ).encode()


def digest_of(body: bytes) -> str:
    return "sha256:" + hashlib.sha256(body).hexdigest()


class FakeRegistry:
    """manifests[(이름, 태그나 digest)] 를 답하는 레지스트리. 없는 것은 404 다. 받은 경로는 calls
    에 남는다."""

    def __init__(self) -> None:
        self.manifests: dict[tuple[str, str], bytes] = {}
        self.calls: list[str] = []
        registry = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - http.server 의 이름이다
                registry.calls.append(self.path)
                prefix = f"/v2/{PROJECT}/"
                name, _, ref = self.path.removeprefix(prefix).rpartition("/manifests/")
                body = registry.manifests.get((name, ref))
                if not self.path.startswith(prefix) or body is None:
                    self.send_response(404)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type", INDEX_TYPE)
                self.send_header("Docker-Content-Digest", digest_of(body))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args: object) -> None:
                pass

        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.host = f"127.0.0.1:{self._server.server_address[1]}"
        self.registry = f"{self.host}/{PROJECT}"

    def push_products(self, *arches: str) -> dict[str, str]:
        """제품 이미지 여섯을 고정 도장 태그로 둔다. 구성요소마다의 index digest 다."""
        out = {}
        for component, name in IMAGES.items():
            body = index(*arches, salt=name)
            self.manifests[(name, STAMPS[component])] = body
            out[component] = digest_of(body)
        return out

    def push(self, name: str, ref: str, body: bytes) -> str:
        self.manifests[(name, ref)] = body
        return digest_of(body)

    def __enter__(self) -> FakeRegistry:
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._server.shutdown()
        self._server.server_close()


@contextmanager
def fake_registry() -> Iterator[FakeRegistry]:
    with FakeRegistry() as registry:
        yield registry


def docker_home(home: Path, registry: FakeRegistry) -> None:
    """lib/registry_auth.py 가 읽는 ~/.docker/config.json 에 그 레지스트리의 자격을 둔다."""
    auth = base64.b64encode(b"e2e:secret").decode()
    (home / ".docker").mkdir(parents=True, exist_ok=True)
    config = {"auths": {registry.host: {"auth": auth}}}
    (home / ".docker" / "config.json").write_text(json.dumps(config))


COSIGN = """#!/bin/sh
# 기록하는 cosign. 인자를 줄 하나로 남기고, 마지막 인자(이미지)가 COSIGN_FAIL 을 담으면 실패한다
printf '%s\\n' "$*" >>"$COSIGN_LOG"
for last; do :; done
case "$last" in
  *"${COSIGN_FAIL:-no-image-fails}"*) echo "Error: no matching signatures" >&2; exit 1 ;;
esac
"""


def stand_in_bin(where: Path, **scripts: str) -> Path:
    """기록하는 cosign 과 scripts(이름마다 셸 스크립트 글)를 둔 PATH 폴더."""
    where.mkdir(parents=True, exist_ok=True)
    for name, text in {"cosign": COSIGN, **scripts}.items():
        path = where / name
        path.write_text(text)
        path.chmod(0o755)
    return where


def checkout(where: Path, *, links: tuple[str, ...] = ()) -> Path:
    """scripts/ 의 파일을 가리키되 build-stamp.sh 만 STAMPS 를 찍는 트리. links 는 저장소의 그
    경로를 그대로 가리킨다(`charts`, `ops` 처럼 스크립트가 읽는 폴더). 서명 공개 키는 커밋됐는지
    git 으로 보므로 시험은 그 사본(cosign.pub)을 COSIGN_PUB 로 준다."""
    for top in links:
        (where / top).parent.mkdir(parents=True, exist_ok=True)
        os.symlink(REPO / top, where / top)
    for path in (REPO / "scripts").rglob("*"):
        if path.is_dir() or "__pycache__" in path.parts:
            continue
        rel = path.relative_to(REPO)
        (where / rel).parent.mkdir(parents=True, exist_ok=True)
        if rel.as_posix() != "scripts/build-stamp.sh":
            os.symlink(path, where / rel)
    stamps = "\n".join(f"  {c}) echo {s} ;;" for c, s in STAMPS.items())
    stamp = where / "scripts" / "build-stamp.sh"
    stamp.write_text(f'#!/bin/sh\ncase "$1" in\n{stamps}\n  *) exit 2 ;;\nesac\n')
    stamp.chmod(0o755)
    shutil.copy(REPO / "deploy" / "cosign.pub", where / "cosign.pub")
    return where
