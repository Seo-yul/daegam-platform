"""이미지를 digest 로 고정하는 판정 (D53, P85 6절). 레지스트리의 인덱스 응답만 본다.

scripts/resolve-images.sh 의 파이썬 조각이 `PYTHONPATH="$HERE/lib" python3 -` 로 읽고,
scripts/lib/llm_gateway_images.py 가 게이트웨이 사본을 같은 판정으로 본다.
platform/tests/test_image_pin.py 가 단위 테스트한다. 맥의 시스템 python3(3.9)에서도
돌아야 하므로 3.10 이후 문법을 쓰지 않는다.

배포는 `이름:도장@sha256:…` 으로 렌더한다. 태그는 사람이 읽는 이름이고 노드가 당기는 것은
digest 다. 코드가 그대로인 다시 굽기가 같은 도장 태그를 새 digest 로 덮으면 파드 템플릿이 바뀌어
롤아웃되므로, `IfNotPresent` 노드가 캐시한 옛 이미지를 계속 쓰지 않는다. 태그가 지워져도 digest 로
당긴다.
"""

from __future__ import annotations

import hashlib
import json
import urllib.request
from typing import Any, Optional, Tuple

# 여러 아키텍처 인덱스만 받는다. 단일 매니페스트면 노드 아키텍처 판이 있는지 이 응답으로 알 수 없다
INDEX_ACCEPT = (
    "application/vnd.oci.image.index.v1+json, "
    "application/vnd.docker.distribution.manifest.list.v2+json"
)
DIGEST_HEADER = "Docker-Content-Digest"


def fetch_index(registry: Any, repository: str, reference: str) -> Tuple[Optional[str], bytes]:
    """레지스트리(`registry_auth.Registry`)의 `<프로젝트>/<repository>` 에서 reference(태그나
    digest)의 index 응답 (Docker-Content-Digest, 본문)을 받는다. resolve-images.sh 의 제품 이미지와
    llm_gateway_images.py 의 게이트웨이 사본이 쓴다. HTTP 오류는 urllib 의 것이 그대로 난다."""
    path = f"/v2/{registry.project}/{repository}/manifests/{reference}"
    with urllib.request.urlopen(registry.request(path, INDEX_ACCEPT), timeout=30) as resp:
        return resp.headers.get(DIGEST_HEADER), resp.read()


def pin(ref: str, digest: str | None, body: bytes, arch: str) -> str:
    """`이름:도장` 과 그 인덱스 응답(Docker-Content-Digest, 본문) -> `이름:도장@sha256:…`.

    arch 는 그 이미지를 당길 노드의 아키텍처(`arm64`, `amd64`)다. 인덱스에 그 판이 없거나, digest 가
    본문의 sha256 과 다르면 ValueError 다. 노드는 그 digest 로 매니페스트를 받아 같은 계산으로
    맞대므로 어긋난 digest 로는 당기지 못한다.
    """
    index = json.loads(body)
    arches = {m.get("platform", {}).get("architecture") for m in index.get("manifests", [])}
    if arch not in arches:
        raise ValueError(f"{arch} 없음: {sorted(a for a in arches if a)}")
    want = "sha256:" + hashlib.sha256(body).hexdigest()
    if digest != want:
        raise ValueError(f"{DIGEST_HEADER} {digest!r} 가 본문의 {want} 와 다르다")
    return f"{ref}@{digest}"
