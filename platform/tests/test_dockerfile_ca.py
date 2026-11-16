"""이미지 굽기가 사내 방화벽 CA 를 빌드 비밀로만 받는지 본다 (docs/guide/images.md 3절).

`DAEGAM_CA_FILE` 이 있으면 make 의 이미지 대상과 push-images 가 그 파일을 빌드 비밀
`daegam_ca` 로 넘기고, Dockerfile 의 받기 단계만 그 비밀을 마운트해 시스템 CA 묶음에 그 CA 를
더한 묶음을 믿는다. CA 가 이미지 층에 남으면 사내 CA 가 배포되는 이미지에 실리고, 받기 단계
하나라도 빠지면 그 단계만 방화벽 뒤에서 실패한다. 그래서 받는 단계마다 비밀 마운트가 있는지,
CA 가 COPY·ADD·ENV 로 남지 않는지, 받기 도우미가 Dockerfile 마다 같은지, 비밀은 변수가 있을
때만 넘어가는지를 여기서 막는다."""

from __future__ import annotations

import os
import re
import shlex
import subprocess
from pathlib import Path

import pytest
from cpython_builds import dockerfile_pins

from tests.helpers.consts import REPO
from tests.helpers.images import product_dockerfiles
from tests.helpers.source import read_source

DOCKERFILES = product_dockerfiles()
SECRET_MOUNT = "--mount=type=secret,id=daegam_ca,required=false"
TMPFS_MOUNT = "--mount=type=tmpfs,target=/run/daegam-ca"
HELPER = "with-daegam-ca"
HELPER_PATH = f"/usr/local/bin/{HELPER}"
# 바깥에서 받는 명령. 이 명령을 부르는 RUN 은 비밀을 마운트해야 한다
DOWNLOADS = re.compile(
    r"\bpip install\b|\buv python install\b|\buv sync\b|\bnpm (?:ci|install)\b"
    r"|\bapk (?:upgrade|add)\b|\bapt-get\b.*\b(?:update|upgrade|install)\b|\bcurl\b|\bwget\b"
    r"|\bgit (?:clone|fetch|pull)\b|tiktoken\.get_encoding"
)
# 셸 출력 넘기기(> 경로). 비밀을 쓰는 RUN 은 tmpfs 아래에만 쓴다
REDIRECT = re.compile(r"(?<![0-9&])>\s*([^\s;&|)]+)")
# 이미지에 남으면 안 되는 CA 자리
CA_ENV = ("SSL_CERT_FILE", "PIP_CERT", "REQUESTS_CA_BUNDLE", "NODE_EXTRA_CA_CERTS")
IMAGE_TARGETS = (
    "image-platform",
    "image-ui",
    "image-runtime",
    "image-agent",
    "image-mock-oidc",
    "image-sandbox",
)
# uv 를 pip 로 받는 이미지. ui 는 node 와 nginx 라 uv 가 없다
PYTHON_DOCKERFILES = tuple(path for path in DOCKERFILES if path.parent.name != "ui")


def _instructions(path: Path) -> list[tuple[int, str]]:
    """(단계 번호, 지시어 한 줄) 목록이다. 역슬래시로 이은 줄을 하나로 잇고 주석 줄은 뺀다."""
    out: list[tuple[int, str]] = []
    stage, current = -1, ""
    for line in read_source(path).splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        current = f"{current} {stripped}" if current else stripped
        if current.endswith("\\"):
            current = current[:-1].rstrip()
            continue
        if current.startswith("FROM "):
            stage += 1
        out.append((stage, current))
        current = ""
    return out


def _last_stage(path: Path) -> int:
    return max(stage for stage, _ in _instructions(path))


def _helper_run(path: Path) -> str | None:
    """받기 도우미를 쓰는 RUN 이다. 없으면 None 이다."""
    runs = [text for _, text in _instructions(path) if f"> {HELPER_PATH}" in text]
    assert len(runs) <= 1, f"{path.relative_to(REPO)} 이 받기 도우미를 두 번 쓴다"
    return runs[0] if runs else None


def _helper_script(run: str) -> str:
    """도우미 RUN 의 printf '%s\\n' 이 쓰는 스크립트 글이다."""
    words = shlex.split(run.split(f"> {HELPER_PATH}", 1)[0].removeprefix("RUN "))
    assert words[:2] == ["printf", "%s\\n"], words[:2]
    return "\n".join(words[2:]) + "\n"


@pytest.mark.parametrize("dockerfile", DOCKERFILES, ids=lambda path: path.parent.name)
def test_every_download_step_mounts_the_optional_ca(dockerfile: Path) -> None:
    last = _last_stage(dockerfile)
    missing = []
    for stage, text in _instructions(dockerfile):
        if not text.startswith("RUN ") or not DOWNLOADS.search(text):
            continue
        uses_ca = HELPER in text or (stage == last and "/run/secrets/daegam_ca" in text)
        if SECRET_MOUNT not in text or not uses_ca:
            missing.append(text[:120])
    assert not missing, (
        f"{dockerfile.relative_to(REPO)} 의 받기 단계에 빌드 비밀 daegam_ca 가 없다: {missing}"
    )


@pytest.mark.parametrize("dockerfile", DOCKERFILES, ids=lambda path: path.parent.name)
def test_steps_with_the_secret_write_only_to_the_tmpfs(dockerfile: Path) -> None:
    """비밀을 읽는 RUN 은 tmpfs 를 마운트하고 그 아래에만 쓴다. 다른 자리에 쓰면 묶음이 층에 남는다.
    받기 도우미를 쓰는 RUN 은 비밀을 읽지 않고 도우미만 쓴다."""
    for _, text in _instructions(dockerfile):
        if not text.startswith("RUN ") or "daegam_ca" not in text or f"> {HELPER_PATH}" in text:
            continue
        assert SECRET_MOUNT in text and TMPFS_MOUNT in text, text[:120]
        targets = REDIRECT.findall(text)
        outside = [t for t in targets if not t.startswith("/run/daegam-ca/")]
        assert not outside, (outside, text[:120])


@pytest.mark.parametrize("dockerfile", PYTHON_DOCKERFILES, ids=lambda path: path.parent.name)
def test_the_uv_bootstrap_checks_the_wheel_hash(dockerfile: Path) -> None:
    """uv 는 CPython 과 uv.lock 의 해시를 확인하는 자리라, 사내 CA 를 믿는 굽기에서도 uv wheel
    자체를 PyPI 의 sha256 으로 확인한다. scripts/lib/cpython_builds.py 는 같은 uv== 글에서 판을
    읽는다."""
    bootstraps = [text for _, text in _instructions(dockerfile) if "uv==" in text]
    assert len(bootstraps) == 1, bootstraps
    run = bootstraps[0]
    assert "--require-hashes" in run and "--no-deps" in run, run[:160]
    assert len(re.findall(r"--hash=sha256:[0-9a-f]{64}\b", run)) >= 2, run[:160]
    version = re.search(r"\buv==([0-9][0-9A-Za-z.+-]*)", run)
    assert version is not None, run[:160]
    pins = dockerfile_pins(read_source(dockerfile))
    # mock-oidc 는 CPython 을 싣지 않아 핀을 읽지 않는다
    assert pins is None or pins[0] == version.group(1), (pins, version.group(1))


@pytest.mark.parametrize("dockerfile", DOCKERFILES, ids=lambda path: path.parent.name)
def test_no_ca_lands_in_an_image(dockerfile: Path) -> None:
    last = _last_stage(dockerfile)
    for stage, text in _instructions(dockerfile):
        word = text.split(" ", 1)[0]
        if word in ("COPY", "ADD"):
            assert not re.search(r"\.pem\b|\.crt\b|daegam_ca|proxy-ca", text), text
            if stage == last:
                assert HELPER_PATH not in text, text
        if word == "ENV":
            assert not any(name in text for name in CA_ENV), text
        if f"> {HELPER_PATH}" in text:
            assert stage != last, "받기 도우미는 빌드 단계에만 둔다. 마지막 단계면 이미지에 남는다"


def test_the_helper_is_the_same_in_every_dockerfile() -> None:
    runs = {path: _helper_run(path) for path in DOCKERFILES}
    lacking = [path.relative_to(REPO) for path, run in runs.items() if run is None]
    assert not lacking, f"받기 도우미가 없다: {lacking}"
    first = runs[DOCKERFILES[0]]
    differ = [path.relative_to(REPO) for path, run in runs.items() if run != first]
    assert not differ, f"받기 도우미가 다르다: {differ}. 고친 것을 나머지 파일에 그대로 복사한다"


def test_the_helper_trusts_the_ca_only_when_the_secret_is_there(tmp_path: Path) -> None:
    """도우미 스크립트를 경로만 바꿔 돌린다. 비밀이 있으면 시스템 묶음과 그 CA 를 이은 묶음을
    가리키고, 없으면 환경을 건드리지 않는다."""
    script = _helper_script(_helper_run(DOCKERFILES[0]) or "")
    secret, tmpfs, system = tmp_path / "secret", tmp_path / "tmpfs", tmp_path / "system.crt"
    tmpfs.mkdir()
    system.write_text("SYSTEM\n")
    script = (
        script.replace("/run/secrets/daegam_ca", str(secret))
        .replace("/run/daegam-ca", str(tmpfs))
        .replace("/etc/ssl/certs/ca-certificates.crt", str(system))
    )
    helper = tmp_path / HELPER
    helper.write_text(script)
    show = "printf '%s|%s|%s|%s' " + " ".join(f'"${{{name}:-}}"' for name in CA_ENV)
    env = {key: value for key, value in os.environ.items() if key not in CA_ENV}

    def run() -> list[str]:
        done = subprocess.run(
            ["sh", str(helper), "sh", "-c", show],
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )
        return done.stdout.split("|")

    assert run() == ["", "", "", ""]
    secret.write_text("CORPORATE\n")
    bundle = str(tmpfs / "bundle.pem")
    assert run() == [bundle, bundle, bundle, str(secret)]
    assert (tmpfs / "bundle.pem").read_text() == "SYSTEM\nCORPORATE\n"


@pytest.mark.parametrize("target", IMAGE_TARGETS)
def test_make_passes_the_secret_only_when_the_variable_is_set(target: str) -> None:
    # 셸이나 바깥 make 에서 온 값이 판정을 바꾸지 않게 뺀다
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in ("DAEGAM_CA_FILE", "MAKEFLAGS", "MFLAGS")
    }

    def dry(*args: str) -> str:
        done = subprocess.run(
            ["make", "-n", target, *args],
            cwd=REPO,
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )
        return done.stdout

    assert "--secret" not in dry()
    assert '--secret "id=daegam_ca,src=/tmp/ca.pem" ' in dry("DAEGAM_CA_FILE=/tmp/ca.pem")


def test_push_images_passes_the_secret_only_when_the_variable_is_set() -> None:
    script = read_source(REPO / "scripts" / "push-images.sh")
    guarded = '[ -z "${DAEGAM_CA_FILE:-}" ] || args+=(--secret "id=daegam_ca,src=$DAEGAM_CA_FILE")'
    assert guarded in script
