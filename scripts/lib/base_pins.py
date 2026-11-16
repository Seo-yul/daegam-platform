"""제품 이미지 Dockerfile 의 바탕 고정 (D55). scripts/base-pins.sh 가 부르고
platform/tests/test_base_pins.py 가 시험한다.

FROM 은 바탕을 `이름:태그@sha256:<digest>` 로 적는다. 태그는 사람이 읽으려고 남기고 굽기는
digest 만 받는다. 그래서 바탕이 다시 구워져도 고정을 옮기기 전에는 distroless 이미지 넷의 OS
패키지가 바뀌지 않고, 시험을 통과한 바탕으로 후보가 굽힌다. ui·mock-oidc·sandbox 는 굽기가 OS 패키지를
올리므로 고정이 그대로여도 바뀔 수 있다. 고정은 make base-pins 가 태그의 지금 index digest
(여러 아키텍처를 묶은 것)로 옮긴다. 태그 없이 digest 만 적힌 FROM 은 옮길 곳을 모르므로 받지
않는다. 맥의 시스템 python3(3.9)에서도 돌아야 하므로 3.10 이후 문법을 쓰지 않는다.

    python3 scripts/lib/base_pins.py [--check] <Dockerfile>...
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from collections.abc import Callable

# `FROM [--platform=…] 참조 [AS 이름]` 의 참조. Dockerfile 의 지시어는 대소문자를 가리지 않고 앞에
# 공백이 올 수 있다
_FROM = re.compile(r"^([ \t]*FROM[ \t]+(?:--platform=\S+[ \t]+)?)(\S+)", re.M | re.I)
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
# 마지막 `/` 뒤의 `:태그`. 레지스트리 주소의 포트(`host:5000/…`)는 태그가 아니다
_TAGGED = re.compile(r"^[^@\s]+:[^@\s/:]+$")

Lookup = Callable[[str], str]


def from_refs(text: str) -> list[str]:
    """Dockerfile 의 FROM 참조를 차례대로."""
    return [m.group(2) for m in _FROM.finditer(text)]


def split_ref(ref: str) -> tuple[str, str]:
    """`이름:태그@sha256:…` -> (이름:태그, digest).

    digest 가 없으면 빈 글자다. 태그가 없거나 digest 꼴이 아니면 ValueError 다.
    """
    image, _, digest = ref.partition("@")
    if not _TAGGED.match(image):
        raise ValueError(f"태그가 없는 바탕이다: {ref}. `이름:태그@sha256:…` 로 적는다")
    if digest and not _DIGEST.match(digest):
        raise ValueError(f"digest 꼴이 아니다: {ref}")
    return image, digest


def unpinned(text: str) -> list[str]:
    """digest 로 고정하지 않은 FROM 참조."""
    bad = []
    for ref in from_refs(text):
        try:
            _, digest = split_ref(ref)
        except ValueError:
            digest = ""
        if not digest:
            bad.append(ref)
    return bad


def repin(text: str, digests: dict[str, str]) -> str:
    """FROM 마다 `이름:태그` 의 digest 를 digests 의 것으로 바꾼다. 없던 고정은 붙인다."""

    def one(m: re.Match) -> str:
        image, _ = split_ref(m.group(2))
        return f"{m.group(1)}{image}@{digests[image]}"

    return _FROM.sub(one, text)


def raw_digest(image: str, raw: bytes) -> str:
    """태그가 가리키는 매니페스트의 날 바이트 -> 그 digest. 여러 아키텍처 index 만 받는다.

    digest 는 매니페스트 바이트의 sha256 이다. 한 아키텍처 매니페스트를 고정하면 다른 아키텍처의
    굽기가 실패하므로 index(`manifests` 가 있는 것)가 아니면 받지 않는다.
    """
    try:
        doc = json.loads(raw)
    except ValueError as e:
        raise RuntimeError(f"{image} 의 매니페스트가 JSON 이 아니다: {e}") from e
    if not isinstance(doc, dict) or not isinstance(doc.get("manifests"), list):
        raise RuntimeError(f"{image} 는 여러 아키텍처 index 가 아니다")
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def index_digest(image: str) -> str:
    """레지스트리가 지금 태그에 둔 index digest.

    `docker buildx imagetools inspect --raw` 는 index 하나만 받는다. `--format` 은 아키텍처마다의
    매니페스트까지 받아 Docker Hub 의 요청 한도를 금방 쓴다. 자격 증명은 docker login 이 둔 것이
    있으면 그것을, 없으면 익명으로 묻는다.
    """
    done = subprocess.run(
        ["docker", "buildx", "imagetools", "inspect", "--raw", image], capture_output=True
    )
    if done.returncode != 0 or not done.stdout:
        why = done.stderr.decode(errors="replace").strip()
        raise RuntimeError(f"{image} 의 digest 를 묻지 못했다: {why}")
    return raw_digest(image, done.stdout)


def main(paths: list[str], lookup: Lookup = index_digest, check: bool = False) -> int:
    """paths 의 FROM 고정을 옮기고 줄마다 그대로인지 옮겼는지 찍는다.

    모든 태그를 먼저 물으므로 하나라도 묻지 못하면 아무 파일도 쓰지 않는다. check 면 쓰지 않고
    지금 digest 와 다른 줄만 알리며, 그런 줄이 있으면 1 이다(릴리스 전 점검).
    """
    texts = {}
    digests: dict[str, str] = {}
    for path in paths:
        with open(path, encoding="utf-8") as f:
            texts[path] = f.read()
        for ref in from_refs(texts[path]):
            image, _ = split_ref(ref)
            if image not in digests:
                digests[image] = lookup(image)
    stale = 0
    for path, text in texts.items():
        for ref in from_refs(text):
            image, old = split_ref(ref)
            new = digests[image]
            short = old[:19] or "고정 없음"
            moving = "낡음" if check else "옮김"
            state = "그대로" if old == new else f"{moving} ({short} -> {new[:19]})"
            print(f"   {path}  {image}  {state}")
            stale += old != new
        pinned = repin(text, digests)
        if pinned != text and not check:
            with open(path, "w", encoding="utf-8") as f:
                f.write(pinned)
    if not stale:
        print("base-pins: 고정이 모두 지금 digest 다")
        return 0
    if check:
        print(f"base-pins: 지금 digest 와 다른 FROM 줄이 {stale} 개다. make base-pins 로 옮긴다")
        return 1
    print(f"base-pins: FROM 줄 {stale} 개를 옮겼다")
    return 0


if __name__ == "__main__":
    args = sys.argv[1:]
    check = "--check" in args
    try:
        sys.exit(main([a for a in args if a != "--check"], check=check))
    except (RuntimeError, ValueError) as e:
        print(f"base-pins: {e}", file=sys.stderr)
        sys.exit(1)
