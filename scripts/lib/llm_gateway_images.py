"""LLM 게이트웨이 한 벌의 이미지 넷을 대감 차트의 값(charts/daegam/values.yaml 의 llmGateway.images)에서
읽는다 (P38 3절, P85 8절).

플랫폼 ConfigMap 의 `DAEGAM_LLM_GATEWAY_IMAGES`(`platform/daegam/store/llm_gateway.py`
`LlmGatewayImages` 꼴)와 설치의 레지스트리(REGISTRY) 사본을 쓰는 차트 값 파일을 만들고, 사본의 digest 가 원본과
같은지 본다. 로컬 배포(scripts/deploy-local.sh 의 LLM_GATEWAY_IMAGES_FROM=mirror), scripts/mirror-images.sh 와
사본을 쓰는 운영자의 배포가 쓴다. 값 파일을 PyYAML 로 읽으므로 플랫폼의 가상 환경(uv run --project platform
python)에서 돈다. platform/tests/test_llm_gateway_images.py 가 단위 테스트한다.

    llm_gateway_images.py json VALUES [REGISTRY [PULL_SECRET]]
        REGISTRY 가 없으면 원본 참조, 있으면 그 아래 third-party/ 사본의 ConfigMap JSON
    llm_gateway_images.py values VALUES REGISTRY [PULL_SECRET]
        사본을 쓰는 차트 값 파일(JSON). 차트가 그것으로 같은 ConfigMap JSON 을 렌더한다
    llm_gateway_images.py pin VALUES REGISTRY ARCH
        사본마다 레지스트리의 index 를 받아 노드 아키텍처 ARCH(arm64, amd64)의 판과 digest 를 확인하고 사본의
        ConfigMap JSON 을 찍는다
    llm_gateway_images.py plan VALUES REGISTRY
        이름마다 `이름 원본 사본 서명자` 한 줄(서명자가 없으면 -). mirror-images.sh 가 읽는다
"""

from __future__ import annotations

import json
import re
import sys
import urllib.error
from typing import Callable, Dict, List, Optional, Tuple

# 차트 값 llmGateway.images 의 이름과 ConfigMap JSON 의 필드
FIELDS = {
    "gateway": "gateway_image",
    "ratelimit": "ratelimit_image",
    "redis": "redis_image",
    "collector": "collector_image",
}
# 사본이 사는 곳. 레지스트리(`호스트/프로젝트`) 아래다
MIRROR_DIR = "third-party"
_REF = re.compile(r"^(?P<repo>[^\s:@]+(?::[0-9]+)?[^\s:@]*):(?P<tag>[\w][\w.-]*)@(?P<digest>sha256:[0-9a-f]{64})$")


class Image:
    """차트 값의 이미지 하나. 원본 저장소·태그·index digest 와 키 없는 서명의 신원(없으면 None)."""

    def __init__(self, key: str, ref: str, signer: Optional[str]):
        m = _REF.match(ref)
        if m is None:
            raise ValueError(
                f"llmGateway.images.{key}.image {ref!r} is not repository:tag@sha256:<digest>"
            )
        self.key = key
        self.repo, self.tag, self.digest = m["repo"], m["tag"], m["digest"]
        self.signer = signer

    @property
    def name(self) -> str:
        """사본의 이름. 원본 저장소의 마지막 마디다."""
        return self.repo.rsplit("/", 1)[-1]

    @property
    def source(self) -> str:
        return f"{self.repo}:{self.tag}@{self.digest}"

    def mirrored(self) -> str:
        """사본의 `이름:태그@digest`. 레지스트리 접두는 registry_prefix 가 붙인다."""
        return f"{self.name}:{self.tag}@{self.digest}"


def read_images(path: str) -> List[Image]:
    """차트 값 파일의 llmGateway.images 를 읽는다. 넷이 다 있어야 한다."""
    import yaml

    with open(path) as f:
        values = yaml.safe_load(f) or {}
    table = (values.get("llmGateway") or {}).get("images") or {}
    missing = [
        f"llmGateway.images.{k}.image" for k in FIELDS if not (table.get(k) or {}).get("image")
    ]
    if missing:
        raise ValueError(f"{path}: missing {', '.join(missing)}")
    return [Image(k, table[k]["image"], table[k].get("signer") or None) for k in FIELDS]


def prefix_of(registry: str) -> str:
    return f"{registry.rstrip('/')}/{MIRROR_DIR}"


def images_json(
    images: List[Image], registry: Optional[str] = None, pull_secret: Optional[str] = None
) -> str:
    """ConfigMap 의 DAEGAM_LLM_GATEWAY_IMAGES. registry 가 없으면 원본 참조다."""
    out: Dict[str, str] = {"pull_policy": "IfNotPresent"}
    if registry:
        out["registry_prefix"] = prefix_of(registry)
    if pull_secret:
        out["pull_secret"] = pull_secret
    for img in images:
        out[FIELDS[img.key]] = img.mirrored() if registry else img.source
    return json.dumps(out, sort_keys=True)


def mirror_values(images: List[Image], registry: str, pull_secret: Optional[str] = None) -> str:
    """사본을 쓰는 차트 값 파일(JSON). 차트가 이것으로 렌더한 DAEGAM_LLM_GATEWAY_IMAGES 는
    images_json(images, registry, pull_secret) 과 같다."""
    out: Dict[str, object] = {"registryPrefix": prefix_of(registry)}
    if pull_secret:
        out["pullSecret"] = pull_secret
    for img in images:
        out[img.key] = {"image": img.mirrored()}
    return json.dumps({"llmGateway": {"images": out}}, indent=1, sort_keys=True)


Fetch = Callable[[str, str], Tuple[Optional[str], bytes]]


def check_mirrors(images: List[Image], registry: str, fetch: Fetch, arch: str) -> List[str]:
    """사본마다 차트 값의 index digest 로 index 를 받아 노드 아키텍처 arch 의 판과 digest(image_pin.pin)를 본다.
    움직이는 태그(`redis:8.2`)가 아니라 digest 로 읽으므로 옛 checkout 이나 되돌린 배포도 제
    고정을 지킨다. fetch(이름, reference)는 (Docker-Content-Digest, 본문)이다. 틀린 것의 까닭
    목록이다(비면 통과)."""
    from image_pin import pin

    bad = []
    for img in images:
        where = f"{prefix_of(registry)}/{img.name}@{img.digest}"
        try:
            pinned = pin(f"{img.name}:{img.tag}", *fetch(img.name, img.digest), arch)
        except urllib.error.URLError as e:
            bad.append(f"{where} ({e}); run make mirror-images")
            continue
        except ValueError as e:
            bad.append(f"{where} ({e})")
            continue
        if pinned != img.mirrored():
            bad.append(f"{where} answered {pinned.rsplit('@', 1)[1]}")
    return bad


def registry_fetch(registry: str) -> Fetch:
    """docker login 의 자격으로 사본의 index 를 받는 fetch."""
    from image_pin import fetch_index
    from registry_auth import Registry

    reg = Registry(registry)
    return lambda name, reference: fetch_index(reg, f"{MIRROR_DIR}/{name}", reference)


def main(argv: List[str]) -> int:
    cmd, path, rest = argv[1], argv[2], argv[3:]
    images = read_images(path)
    if cmd == "json":
        print(images_json(images, *rest[:2]))
        return 0
    if cmd == "values":
        print(mirror_values(images, *rest[:2]))
        return 0
    if cmd == "plan":
        for img in images:
            target = f"{prefix_of(rest[0])}/{img.name}:{img.tag}"
            print(img.key, img.source, target, img.signer or "-")
        return 0
    if cmd == "pin":
        bad = check_mirrors(images, rest[0], registry_fetch(rest[0]), rest[1])
        if bad:
            sys.exit("llm-gateway images: FAIL: " + "; ".join(bad))
        print(images_json(images, rest[0]))
        return 0
    sys.exit(f"unknown command {cmd!r}")


if __name__ == "__main__":
    sys.exit(main(sys.argv))
