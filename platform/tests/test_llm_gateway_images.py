"""LLM 게이트웨이 이미지 넷과 그 사본의 고정 (P38 3절, P85 8절).

참조는 대감 차트의 값(charts/daegam/values.yaml 의 llmGateway.images)이고 읽기와 고정은
scripts/lib/llm_gateway_images.py 다. 사본의 index 는 레지스트리 모양의 본문을 손으로 만들어 넘긴다.
"""

from __future__ import annotations

import hashlib
import json
import re
import urllib.error
from pathlib import Path

import pytest
import yaml
from llm_gateway_images import (
    FIELDS,
    check_mirrors,
    images_json,
    main,
    mirror_values,
    read_images,
)

from daegam.store.llm_gateway import LlmGatewayImages
from tests.helpers.chart import CHART, config_data, render
from tests.helpers.consts import REPO

VALUES = CHART / "values.yaml"
REGISTRY = "registry.example.com/daegam"
# 사본을 당길 노드의 아키텍처. 고정은 그 판이 인덱스에 있는지 본다
ARCH = "arm64"


def test_every_image_is_pinned_by_its_index_digest():
    images = read_images(str(VALUES))
    assert [i.key for i in images] == ["gateway", "ratelimit", "redis", "collector"]
    # 서명을 확인할 수 있는 둘은 그 신원이 태그와 같은 판을 가리킨다
    signed = {i.key: i for i in images if i.signer}
    assert sorted(signed) == ["collector", "gateway"]
    for img in signed.values():
        assert img.signer.endswith(f"@refs/tags/v{img.tag.removeprefix('v')}")


def test_the_gateway_tag_is_the_makefile_version():
    makefile = (REPO / "Makefile").read_text()
    version = re.search(r"^AGENTGATEWAY_VERSION \?= (\S+)$", makefile, re.M)
    gateway = next(i for i in read_images(str(VALUES)) if i.key == "gateway")
    assert version is not None and gateway.tag == version.group(1)


@pytest.mark.parametrize("registry", [None, REGISTRY])
def test_the_configmap_json_is_what_the_platform_accepts(registry):
    """원본 참조와 사본(LLM_GATEWAY_IMAGES_FROM=mirror, 사본을 쓰는 설치) 모두 플랫폼이 받는
    꼴이다."""
    images = LlmGatewayImages.model_validate(
        json.loads(images_json(read_images(str(VALUES)), registry))
    )
    if registry is None:
        assert images.ref(images.gateway_image).startswith("ghcr.io/agentgateway/agentgateway:")
    else:
        mirrored = f"{REGISTRY}/third-party/redis:8.2@sha256:"
        assert images.ref(images.redis_image).startswith(mirrored)


@pytest.mark.parametrize("pull_secret", [None, "registry-pull"])
def test_the_chart_renders_the_scripts_json_for_sources_and_mirrors(tmp_path, pull_secret):
    """차트가 렌더한 DAEGAM_LLM_GATEWAY_IMAGES 는 스크립트가 같은 값에서 만드는 JSON 과 같다.
    원본이든 mirror_values 의 값 파일을 얹은 사본이든 그렇다."""
    images = read_images(str(VALUES))
    rendered = json.loads(config_data(render())["DAEGAM_LLM_GATEWAY_IMAGES"])
    assert rendered == json.loads(images_json(images))
    mirror = tmp_path / "gateway-mirror.json"
    mirror.write_text(mirror_values(images, REGISTRY, pull_secret))
    rendered = json.loads(config_data(render(mirror))["DAEGAM_LLM_GATEWAY_IMAGES"])
    assert rendered == json.loads(images_json(images, REGISTRY, pull_secret))
    LlmGatewayImages.model_validate(rendered)


def test_the_cli_reads_the_chart_values(capsys):
    assert main(["llm_gateway_images.py", "plan", str(VALUES), REGISTRY]) == 0
    lines = [line.split() for line in capsys.readouterr().out.splitlines()]
    assert [line[0] for line in lines] == list(FIELDS)
    assert lines[0][2] == f"{REGISTRY}/third-party/agentgateway:v1.5.0"
    assert main(["llm_gateway_images.py", "values", str(VALUES), REGISTRY]) == 0
    values = json.loads(capsys.readouterr().out)["llmGateway"]["images"]
    assert values["registryPrefix"] == f"{REGISTRY}/third-party" and "pullSecret" not in values


def _index(*arches: str) -> bytes:
    manifests = [
        {"digest": f"sha256:{n:064x}", "platform": {"architecture": a}}
        for n, a in enumerate(arches)
    ]
    return json.dumps({"schemaVersion": 2, "manifests": manifests}).encode()


def _table(tmp_path: Path, images: dict[str, str]) -> str:
    """llmGateway.images 만 담은 차트 값 파일. images 는 이름마다 참조다."""
    table = {key: {"image": ref} for key, ref in images.items()}
    path = tmp_path / "values.yaml"
    path.write_text(yaml.safe_dump({"llmGateway": {"images": table}}))
    return str(path)


def _pinned(tmp_path: Path, bodies: dict[str, bytes]) -> str:
    return _table(
        tmp_path,
        {k: f"ghcr.io/x/{k}:1.0@sha256:{hashlib.sha256(b).hexdigest()}" for k, b in bodies.items()},
    )


def _registry(bodies: dict[str, bytes], *, header=None):
    """사본 index 를 digest 로만 답하는 레지스트리. 받은 (이름, reference)를 calls 에 남긴다."""
    store = {k: {"sha256:" + hashlib.sha256(b).hexdigest(): b} for k, b in bodies.items()}
    calls: list[tuple[str, str]] = []

    def fetch(name: str, reference: str):
        calls.append((name, reference))
        body = store.get(name, {}).get(reference)
        if body is None:
            raise urllib.error.HTTPError(name, 404, "manifest unknown", {}, None)  # type: ignore[arg-type]
        return (header(body) if header else reference), body

    return fetch, calls


def _bodies(**override: bytes) -> dict[str, bytes]:
    return {k: override.get(k) or _index("amd64", ARCH, k) for k in FIELDS}


def test_mirrors_are_read_by_the_values_digest(tmp_path):
    """움직이는 태그가 아니라 차트 값의 digest 로 읽는다. 옛 checkout 이나 되돌린 배포도 제 고정을
    지킨다."""
    bodies = _bodies()
    images = read_images(_pinned(tmp_path, bodies))
    fetch, calls = _registry(bodies)
    assert check_mirrors(images, REGISTRY, fetch, ARCH) == []
    assert calls == [(img.name, img.digest) for img in images]


def test_a_digest_missing_from_the_registry_is_refused(tmp_path):
    bodies = _bodies()
    images = read_images(_pinned(tmp_path, bodies))
    fetch, _ = _registry({k: b for k, b in bodies.items() if k != "redis"})
    [bad] = check_mirrors(images, REGISTRY, fetch, ARCH)
    assert "third-party/redis@sha256:" in bad and "make mirror-images" in bad


def test_a_mirror_without_the_node_arch_or_with_a_wrong_header_is_refused(tmp_path):
    bodies = _bodies(gateway=_index("amd64"))
    images = read_images(_pinned(tmp_path, bodies))
    fetch, _ = _registry(bodies)
    assert len(check_mirrors(images, REGISTRY, fetch, ARCH)) == 1
    fine = _bodies()
    images = read_images(_pinned(tmp_path, fine))
    lying, _ = _registry(fine, header=lambda _b: "sha256:" + "0" * 64)
    assert len(check_mirrors(images, REGISTRY, lying, ARCH)) == len(FIELDS)


def test_fetch_index_asks_the_project_path(monkeypatch):
    """resolve-images.sh 의 제품 이미지와 게이트웨이 사본이 같은 받기를 쓴다."""
    import image_pin

    seen = {}

    class _Resp:
        headers = {image_pin.DIGEST_HEADER: "sha256:x"}

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def read(self):
            return b"{}"

    def urlopen(req, timeout):
        seen.update(url=req, timeout=timeout)
        return _Resp()

    class _Reg:
        project = "daegam"

        def request(self, path, accept):
            return (path, accept)

    monkeypatch.setattr(image_pin.urllib.request, "urlopen", urlopen)
    assert image_pin.fetch_index(_Reg(), "third-party/redis", "sha256:abc") == ("sha256:x", b"{}")
    assert seen["url"] == (
        "/v2/daegam/third-party/redis/manifests/sha256:abc",
        image_pin.INDEX_ACCEPT,
    )


def test_an_image_without_a_digest_or_a_missing_image_is_refused(tmp_path):
    with pytest.raises(ValueError, match="repository:tag@sha256"):
        read_images(_table(tmp_path, {k: f"ghcr.io/x/{k}:1.0" for k in FIELDS}))
    with pytest.raises(ValueError, match="llmGateway.images.redis.image"):
        read_images(_table(tmp_path, {"gateway": "ghcr.io/x/g:1.0@sha256:" + "a" * 64}))
