"""이미지를 digest 로 고정하는 판정 (D53, P85 6절).

판정은 scripts/lib/image_pin.py 에 있고 scripts/resolve-images.sh 가 레지스트리에서 받은 인덱스
응답의 Docker-Content-Digest 와 본문, 노드 아키텍처를 넘긴다. 여기서는 레지스트리 모양의 인덱스
본문을 손으로 만들어 넘긴다.
"""

from __future__ import annotations

import hashlib
import json

import pytest
from image_pin import DIGEST_HEADER, pin

REF = "daegam-platform:d614fab"
INDEX_TYPE = "application/vnd.oci.image.index.v1+json"


def _index(*arches: str) -> bytes:
    manifests = [
        {"digest": f"sha256:{i:064x}", "platform": {"os": "linux", "architecture": arch}}
        for i, arch in enumerate(arches)
    ]
    index = {"schemaVersion": 2, "mediaType": INDEX_TYPE, "manifests": manifests}
    return json.dumps(index).encode()


def _digest(body: bytes) -> str:
    return "sha256:" + hashlib.sha256(body).hexdigest()


@pytest.mark.parametrize("arch", ["arm64", "amd64"])
def test_stamp_tag_is_pinned_to_the_index_digest(arch: str) -> None:
    body = _index("amd64", "arm64")

    assert pin(REF, _digest(body), body, arch) == f"{REF}@{_digest(body)}"


def test_rebuild_under_the_same_stamp_renders_a_different_image() -> None:
    """코드가 그대로인 다시 굽기는 같은 도장 태그를 새 인덱스로 덮는다. 렌더한 글자가 달라야 파드
    템플릿이 바뀌어 롤아웃된다."""
    before, after = _index("amd64", "arm64"), _index("arm64", "amd64")

    assert pin(REF, _digest(before), before, "arm64") != pin(REF, _digest(after), after, "arm64")


@pytest.mark.parametrize(
    ("body", "arch", "reason"),
    [
        (_index("amd64"), "arm64", "arm64 없음: ['amd64']"),
        (_index("arm64"), "amd64", "amd64 없음: ['arm64']"),
        # 단일 매니페스트는 manifests 가 없어 아키텍처를 알 수 없다
        (json.dumps({"schemaVersion": 2, "layers": []}).encode(), "arm64", "arm64 없음: []"),
    ],
)
def test_index_without_the_node_arch_is_refused(body: bytes, arch: str, reason: str) -> None:
    with pytest.raises(ValueError) as e:
        pin(REF, _digest(body), body, arch)

    assert str(e.value) == reason


@pytest.mark.parametrize("digest", [None, "", "sha256:" + "0" * 64, "sha512:abc"])
def test_digest_other_than_the_body_hash_is_refused(digest: str | None) -> None:
    body = _index("arm64")

    with pytest.raises(ValueError, match=DIGEST_HEADER):
        pin(REF, digest, body, "arm64")
