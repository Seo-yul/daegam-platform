"""기능 `skills` 의 init 단계다(D64, P39 7절).

받기 스크립트가 agent.json 을 받은 뒤 `run(config_dir, fetch)` 를 부른다. 문서 `skills[]` 의
Skill 을 차례로 `fetch("/skills/<name>")`(플랫폼 `GET /api/agents/{id}/skills/{name}`)로 하나씩
받고, digest 를 다시 셈해 agent.json 의 값과 같은지 본 뒤 붙인 에이전트마다
`<config_dir>/skills/main/<name>/…` 와 `<config_dir>/skills/sub-<i>/<name>/…`(i 는 subagents[] 의
차례)에 바로 쓴다. 메모리에는 한 번에 Skill 하나만 둔다. 에이전트마다 따로 쓰는 것은 deepagents 의
Skill 소스가 디렉터리 단위라서다. 그래야 붙인 것만 목록에 오른다.

다시 불려도 같은 결과를 낸다. init 이 도중에 실패하면 kubelet 은 같은 파드에서 init 을 다시 띄우고
emptyDir 인 설정 볼륨은 그 사이에 남는다. 0555 디렉터리 위에는 다시 쓸 수 없으므로 지난 시도가
남긴 `skills/` 와 `.skills-*` 를 쓰기를 되돌려 지우고, 새 임시 디렉터리에 다 쓴 뒤 파일 0444·
디렉터리 0555 로 잠가 같은 부모 안에서 `skills/` 로 이름을 바꾼다. 같은 부모 안의 이름 바꾸기라
바뀐 권한에 막히지 않는다.

연결 실패, 본문을 읽다 끊긴 응답, 5xx 는 1·2·4초 뒤 세 번 다시 한다. 그래도 안 되거나 받은 것이
틀리면 받기 스크립트처럼 `코드: 문구` 를 표준 오류와 `/dev/termination-log` 에 쓰고 0 이 아닌
값으로 끝나 파드가 뜨지 않는다. 다음 시도는 처음부터 다시 한다. 코드는 단계마다 정해 둔다. 받기
실패는 플랫폼 오류의 코드(없으면 `http_<상태>`)나 `unreachable`, agent.json 의 핀과 받은 응답이
틀리면 `skill_bundle_invalid`·`skill_path_invalid`·`skill_digest_mismatch`, 설정 볼륨에 쓰다 난
OSError 는 `skills_write_failed` 다.

init 이 이 모듈을 import 하므로 표준 라이브러리만 쓴다. 핀과 응답 모양은 jsonschema 없이 손으로
보고, 이름·경로 정규식과 핀·응답의 키는 이미지 안 스키마의 `$defs/skill_name`·`$defs/skill_path`·
`$defs/skill_pin`·`$defs/skill_bundle` 에서 읽는다. 디렉터리 배치의 원본은 이 모듈이고 런타임의
skills.py 가 같은 이름을 가져다 쓴다.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import time
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from http.client import HTTPException, InvalidURL
from pathlib import Path
from typing import Any
from urllib.error import HTTPError

SKILLS_DIR = "skills"
"""설정 볼륨 안에서 Skill 을 두는 디렉터리다."""
STAGING_PREFIX = ".skills-"
MAIN_FOLDER = "main"
SUB_FOLDER = "sub-{index}"
SKILL_FILE = "SKILL.md"
CONFIG_FILE = "agent.json"
SCHEMA_PATH = Path(__file__).resolve().parents[2] / "schema" / "agent.schema.json"

FILE_MODE = 0o444
DIR_MODE = 0o555
UNLOCKED_DIR_MODE = 0o755

RETRY_DELAYS: tuple[float, ...] = (1.0, 2.0, 4.0)
"""연결 실패와 5xx 뒤에 쉬는 초다. 처음 시도를 더해 네 번까지 받는다."""
SERVER_ERROR = 500
MAX_BUNDLE_BYTES = 4 * 1024 * 1024
"""Skill 하나의 응답 본문 상한이다. 리비전은 512 KiB 이고 JSON 이스케이프로 늘어날 몫을 둔다."""

TERMINATION_LOG = "/dev/termination-log"

DIGEST_MISMATCH = "skill_digest_mismatch"
BUNDLE_INVALID = "skill_bundle_invalid"
PATH_INVALID = "skill_path_invalid"
UNREACHABLE = "unreachable"
WRITE_FAILED = "skills_write_failed"

Fetch = Callable[[str], Any]
"""받기 스크립트의 `fetch(path)` 다. 에이전트 설정 경로 아래 path 를 열어 응답(읽기 흐름)을 준다."""


class StepError(Exception):
    """이 단계를 멈추는 이유다. code 는 termination log 의 앞머리다."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


@dataclass(frozen=True)
class TextRule:
    """스키마의 문자열 정의 하나(pattern·maxLength)다."""

    pattern: re.Pattern[str]
    max_length: int

    def accepts(self, value: Any) -> bool:
        return (
            isinstance(value, str)
            and len(value) <= self.max_length
            and self.pattern.fullmatch(value) is not None
        )


@dataclass(frozen=True)
class Contract:
    """스키마에서 읽은 핀과 응답의 규칙이다."""

    name: TextRule
    path: TextRule
    pin_keys: frozenset[str]
    keys: frozenset[str]
    max_files: int


def load_contract(schema_path: Path = SCHEMA_PATH) -> Contract:
    defs = json.loads(schema_path.read_text(encoding="utf-8"))["$defs"]
    bundle = defs["skill_bundle"]
    return Contract(
        name=_text_rule(defs["skill_name"]),
        path=_text_rule(defs["skill_path"]),
        pin_keys=frozenset(defs["skill_pin"]["required"]),
        keys=frozenset(bundle["required"]),
        max_files=bundle["properties"]["files"]["maxItems"],
    )


def _text_rule(definition: dict[str, Any]) -> TextRule:
    return TextRule(re.compile(definition["pattern"]), definition["maxLength"])


def agent_folders(doc: dict[str, Any]) -> list[tuple[str, list[str]]]:
    """에이전트마다 (폴더, 붙인 Skill 이름)이다. 메인은 `main`, 서브에이전트는 차례대로
    `sub-<i>` 다."""
    agents = [(MAIN_FOLDER, doc["main"])]
    agents += [(SUB_FOLDER.format(index=i), sub) for i, sub in enumerate(doc["subagents"])]
    return [(folder, list(agent.get("skills", []))) for folder, agent in agents]


def skill_digest(files: Iterable[tuple[str, str]]) -> str:
    """경로 순으로 `경로 NUL sha256(내용) LF` 를 이은 바이트의 sha256 이다. 규칙의 원본은
    agent.schema.json 의 `$defs/skill_digest` 설명이고 플랫폼이 같은 값을 따로 셈한다."""
    h = hashlib.sha256()
    for path, content in sorted(files):
        h.update(f"{path}\0{hashlib.sha256(content.encode()).hexdigest()}\n".encode())
    return f"sha256:{h.hexdigest()}"


def run(config_dir: str, fetch: Fetch) -> None:
    """받기 스크립트가 부르는 자리다. 실패하면 이유를 남기고 프로세스를 끝낸다."""
    try:
        install(Path(config_dir), fetch)
    except StepError as e:
        _stop(e.code, e.message)


def install(config_dir: Path, fetch: Fetch) -> Path:
    """`<config_dir>/skills/` 를 처음부터 다시 만든다. 다 쓴 디렉터리를 돌려준다. 문서 읽기, 받기,
    검사, 쓰기의 실패는 단계마다 정한 코드의 StepError 다."""
    contract = load_contract()
    pins, folders = _document(config_dir / CONFIG_FILE, contract)
    with _writing():
        for stale in _stale(config_dir):
            _remove(stale)
        staging = Path(tempfile.mkdtemp(prefix=STAGING_PREFIX, dir=config_dir))
    for pin in pins:
        name = pin["name"]
        targets = [folder for folder, names in folders if name in names]
        if not targets:
            continue
        files = _checked_files(_receive(fetch, name), pin, contract)
        with _writing():
            for folder in targets:
                _write(staging / folder / name, files)
    with _writing():
        _lock(staging)
        target = config_dir / SKILLS_DIR
        staging.rename(target)
    return target


def _document(
    path: Path, contract: Contract
) -> tuple[list[dict[str, Any]], list[tuple[str, list[str]]]]:
    """agent.json 의 핀 skills[] 와 에이전트마다의 (폴더, 붙인 이름)이다. 받기 스크립트가 방금 받은
    문서지만 init 은 jsonschema 없이 돌므로 이 단계가 쓰는 모양을 여기서 본다. 핀의 이름은 디렉터리
    이름과 받기 주소가 되므로 `$defs/skill_name` 으로 한 번 더 본다."""
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
        pins, folders = list(doc.get("skills", [])), agent_folders(doc)
    except (OSError, ValueError, LookupError, TypeError, AttributeError) as e:
        raise StepError(BUNDLE_INVALID, f"{CONFIG_FILE} 를 읽을 수 없다: {e!r}") from e
    for i, pin in enumerate(pins):
        if not (
            isinstance(pin, dict)
            and set(pin) == contract.pin_keys
            and contract.name.accepts(pin["name"])
        ):
            message = f"{CONFIG_FILE} 의 skills[{i}] 가 skill_pin 모양이 아니다"
            raise StepError(BUNDLE_INVALID, message)
    return pins, folders


@contextmanager
def _writing() -> Iterator[None]:
    """설정 볼륨에 쓰는 자리다. 여기서 난 OSError 만 skills_write_failed 로 바꾼다."""
    try:
        yield
    except OSError as e:
        raise StepError(WRITE_FAILED, str(e)) from e


def _stale(config_dir: Path) -> list[Path]:
    paths = [config_dir / SKILLS_DIR, *config_dir.glob(f"{STAGING_PREFIX}*")]
    return [path for path in paths if path.exists()]


def _remove(root: Path) -> None:
    """지난 시도가 남긴 디렉터리를 지운다. 0555 디렉터리 안은 지울 수 없으므로 위에서부터 쓰기
    권한을 되돌린다."""
    root.chmod(UNLOCKED_DIR_MODE)
    for dirpath, dirnames, _ in os.walk(root):
        for name in dirnames:
            (Path(dirpath) / name).chmod(UNLOCKED_DIR_MODE)
    shutil.rmtree(root)


def _lock(root: Path) -> None:
    """파일은 0444, 디렉터리는 0555 로 바꾼다. 안쪽부터 잠가야 끝까지 들어간다."""
    for dirpath, dirnames, filenames in os.walk(root, topdown=False):
        for name in filenames:
            (Path(dirpath) / name).chmod(FILE_MODE)
        for name in dirnames:
            (Path(dirpath) / name).chmod(DIR_MODE)
    root.chmod(DIR_MODE)


def _receive(fetch: Fetch, name: str) -> bytes:
    """Skill 하나의 응답 본문이다. 연결 실패, 읽다 끊긴 본문, 5xx 는 RETRY_DELAYS 만큼 쉬며 다시
    받는다. 실패는 받기 쪽 코드의 StepError 다."""
    delays = list(RETRY_DELAYS)
    while True:
        try:
            return _read(fetch, f"/{SKILLS_DIR}/{name}")
        except HTTPError as e:
            if e.code < SERVER_ERROR or not delays:
                raise _http_error(e) from e
        except (ValueError, InvalidURL) as e:
            # 주소를 만들지 못했다. 다시 해도 같으므로 바로 멈춘다. InvalidURL 은 HTTPException 이라
            # 아래보다 먼저 받는다
            raise StepError(UNREACHABLE, str(e)) from e
        except (OSError, HTTPException) as e:
            # 읽다 끊긴 본문(IncompleteRead)은 OSError 가 아니라 HTTPException 이다
            if not delays:
                raise StepError(UNREACHABLE, str(getattr(e, "reason", e))) from e
        time.sleep(delays.pop(0))


def _read(fetch: Fetch, path: str) -> bytes:
    with fetch(path) as response:
        body = response.read(MAX_BUNDLE_BYTES + 1)
    if len(body) > MAX_BUNDLE_BYTES:
        raise StepError(BUNDLE_INVALID, f"{path} 의 응답이 {MAX_BUNDLE_BYTES} 바이트를 넘는다")
    return body


def _http_error(e: HTTPError) -> StepError:
    """플랫폼 오류 본문 `{"detail": {"code", "detail"}}` 의 코드를 쓰고, 없으면 `http_<상태>` 다."""
    code, message = f"http_{e.code}", str(e)
    try:
        detail = json.loads(e.read()).get("detail") or {}
        code, message = detail.get("code") or code, detail.get("detail") or message
    except (OSError, HTTPException, ValueError, AttributeError):
        # 오류 본문을 못 읽었거나 플랫폼 모양이 아니다. 상태 코드로 남긴다
        pass
    return StepError(code, message)


def _checked_files(body: bytes, pin: dict[str, Any], contract: Contract) -> list[tuple[str, str]]:
    """응답을 `$defs/skill_bundle` 모양으로 보고 digest 가 agent.json 의 핀과 같은지 본 (경로,
    내용)이다."""
    name = pin["name"]
    try:
        bundle = json.loads(body)
    except ValueError as e:
        raise StepError(BUNDLE_INVALID, f"{name}: JSON 이 아니다") from e
    if not isinstance(bundle, dict) or set(bundle) != contract.keys:
        raise StepError(BUNDLE_INVALID, f"{name}: 응답의 키가 skill_bundle 과 다르다")
    if any(bundle[key] != pin[key] for key in ("name", "id", "revision")):
        raise StepError(BUNDLE_INVALID, f"{name}: 응답의 Skill 이 agent.json 의 핀과 다르다")
    files = _bundle_files(bundle["files"], name, contract)
    try:
        digest = skill_digest(files)
    except UnicodeEncodeError as e:
        # JSON 의 짝 없는 서로게이트(\ud800 등)는 UTF-8 로 쓸 수 없다
        raise StepError(BUNDLE_INVALID, f"{name}: content 를 UTF-8 로 바꿀 수 없다") from e
    if digest != pin["digest"]:
        raise StepError(DIGEST_MISMATCH, f"{name}: 받은 파일의 digest 가 agent.json 과 다르다")
    return files


def _bundle_files(raw: Any, name: str, contract: Contract) -> list[tuple[str, str]]:
    if not isinstance(raw, list) or not 1 <= len(raw) <= contract.max_files:
        raise StepError(
            BUNDLE_INVALID, f"{name}: files 가 1~{contract.max_files}개의 목록이 아니다"
        )
    files: list[tuple[str, str]] = []
    for item in raw:
        if not isinstance(item, dict) or set(item) != {"path", "content"}:
            raise StepError(BUNDLE_INVALID, f"{name}: files 항목의 키가 path·content 와 다르다")
        path, content = item["path"], item["content"]
        if not isinstance(content, str):
            raise StepError(BUNDLE_INVALID, f"{name}: content 가 문자열이 아니다")
        if not contract.path.accepts(path):
            raise StepError(PATH_INVALID, f"{name}: 경로 {path!r} 를 받을 수 없다")
        files.append((path, content))
    return files


def _write(skill_dir: Path, files: list[tuple[str, str]]) -> None:
    for path, content in files:
        target = skill_dir / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content.encode())


def _stop(code: str, message: str) -> None:
    """받기 스크립트의 `stop` 과 같다. 이유를 남기고 0 이 아닌 값으로 끝난다."""
    line = f"{code}: {message}"
    sys.stderr.write(line + "\n")
    try:
        with open(TERMINATION_LOG, "w", encoding="utf-8") as f:
            f.write(line)
    except OSError:
        pass
    sys.exit(1)
