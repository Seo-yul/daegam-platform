"""Skill 을 다루는 테스트들이 함께 쓰는 도우미다(P39 7절).

고정물 폴더(`schema/fixtures/skills`)를 플랫폼의 Skill 응답과 핀 모양으로 만들고, init 단계
(`runtime/init_steps/skills.py`)가 읽는 agent.json 을 쓰며, 그 단계에 주는 가짜 fetch 와 그 단계가
남긴 트리의 지문을 준다.
"""

from __future__ import annotations

import io
import json
import stat
from http.client import IncompleteRead
from pathlib import Path
from typing import Any

from runtime.init_steps import skills as step
from tests.conftest import FIXTURES

SKILL_FIXTURES = FIXTURES / "skills"


def bundle_of(name: str, *, revision: int = 1) -> dict[str, Any]:
    """고정물 폴더 하나를 플랫폼의 Skill 응답(`$defs/skill_bundle`) 모양으로 만든다."""
    folder = SKILL_FIXTURES / name
    files = [
        {"path": f.relative_to(folder).as_posix(), "content": f.read_text(encoding="utf-8")}
        for f in sorted(folder.rglob("*"))
        if f.is_file()
    ]
    digest = step.skill_digest((f["path"], f["content"]) for f in files)
    return {
        "name": name,
        "id": f"sk-{name}",
        "revision": revision,
        "digest": digest,
        "files": files,
    }


def fixture_bundles() -> list[dict[str, Any]]:
    """두 테스트 모듈이 붙이는 고정물 Skill 둘(report-style·web-research)의 응답이다."""
    return [bundle_of("report-style"), bundle_of("web-research")]


def pin_of(bundle: dict[str, Any]) -> dict[str, Any]:
    size = sum(len(f["content"].encode()) for f in bundle["files"])
    return {**{k: bundle[k] for k in ("name", "id", "revision", "digest")}, "bytes": size}


def write_agent_json(config_dir: Path, doc: Any) -> Path:
    """init 단계가 읽는 자리(config_dir 의 agent.json)에 doc 을 쓰고 그 경로를 준다."""
    path = config_dir / step.CONFIG_FILE
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


class CutShort:
    """본문을 읽다 끊긴 응답이다. http.client 는 IncompleteRead 를 내고 그것은 OSError 가 아니다."""

    def __enter__(self) -> CutShort:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def read(self, size: int = -1) -> bytes:
        raise IncompleteRead(b"", 64)

    def close(self) -> None:
        return None


class FakeFetch:
    """Skill 이름마다 응답 본문을 준다. 부를 때마다 경로와 그때 임시 디렉터리에 있던 파일을
    적는다. failures 에 예외를 넣으면 그 차례에 던지고 응답을 넣으면 그것을 준다."""

    def __init__(self, config_dir: Path, bodies: dict[str, Any]) -> None:
        self.config_dir = config_dir
        self.bodies = bodies
        self.calls: list[str] = []
        self.staged: list[list[str]] = []
        self.failures: dict[str, list[Exception | CutShort]] = {}

    def __call__(self, path: str) -> io.BytesIO | CutShort:
        self.calls.append(path)
        self.staged.append(
            sorted(
                p.relative_to(staging).as_posix()
                for staging in self.config_dir.glob(f"{step.STAGING_PREFIX}*")
                for p in staging.rglob("*")
                if p.is_file()
            )
        )
        name = path.removeprefix(f"/{step.SKILLS_DIR}/")
        pending = self.failures.get(name)
        if pending:
            failure = pending.pop(0)
            if isinstance(failure, Exception):
                raise failure
            return failure
        body = self.bodies[name]
        return io.BytesIO(body if isinstance(body, bytes) else json.dumps(body).encode())


def fingerprint(root: Path) -> list[tuple[str, str, bytes]]:
    """경로·권한·내용이다. 다시 불린 단계가 같은 결과를 냈는지 견준다."""
    entries = [root, *root.rglob("*")]
    return sorted(
        (
            p.relative_to(root).as_posix(),
            oct(stat.S_IMODE(p.stat().st_mode)),
            p.read_bytes() if p.is_file() else b"",
        )
        for p in entries
    )
