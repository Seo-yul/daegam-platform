"""`/files/upload`·`/files/download`·`/files/list` (P40 3절).

파일 하나는 10 MiB 까지, 목록은 500 개까지다. 작업 공간은 tmpfs 라 이벤트 루프 안에서 바로 읽고
쓴다. 서버에는 스레드가 없어야 하므로(procs.py) 스레드 풀로 넘기지 않는다. 명령이 만든 FIFO 를 열다
막히지 않게 O_NONBLOCK 으로 열고 보통 파일이 아니면 거절한다. 심볼릭 링크는 따라가지 않는다.
"""

from __future__ import annotations

import base64
import binascii
import errno
import os
import stat
from pathlib import Path

from daegam_sandbox.paths import PathRefused, Resolver

MAX_FILE = 10 * 1024 * 1024
MAX_FILES_PER_CALL = 64
# 내려받기 한 번이 싣는 바이트의 합. 넘는 파일은 too_large 로 답한다
MAX_DOWNLOAD_TOTAL = 32 * 1024 * 1024
MAX_LIST = 500
# 목록을 만들며 들여다볼 항목 수. 명령이 파일을 아주 많이 만들어도 이벤트 루프를 오래 잡지 않는다
MAX_WALK = 20_000
MAX_EXCLUDE = 16

# deepagents 의 FileOperationError 글자와, 그 밖에 이 서버가 쓰는 글자
FILE_NOT_FOUND = "file_not_found"
PERMISSION_DENIED = "permission_denied"
IS_DIRECTORY = "is_directory"
INVALID_PATH = "invalid_path"
NOT_A_FILE = "not_a_file"
TOO_LARGE = "too_large"
INVALID_CONTENT = "invalid_content"

_OPEN_FLAGS = os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)


class BadRequest(ValueError):
    """요청 모양이 틀렸다(400)."""


def _os_error(e: OSError) -> str:
    if isinstance(e, FileNotFoundError):
        return FILE_NOT_FOUND
    if isinstance(e, IsADirectoryError):
        return IS_DIRECTORY
    if isinstance(e, PermissionError):
        return PERMISSION_DENIED
    if e.errno in (errno.ELOOP, errno.ENOTDIR, errno.ENAMETOOLONG):
        return INVALID_PATH
    if e.errno == errno.ENXIO:
        return NOT_A_FILE
    return f"os_error_{e.errno}"


def _list_of(body: dict, key: str, limit: int) -> list:
    items = body.get(key)
    if not isinstance(items, list) or len(items) > limit:
        raise BadRequest(f"{key} 는 {limit} 개까지의 배열이다")
    return items


def upload(resolver: Resolver, body: dict) -> dict:
    results = []
    for item in _list_of(body, "files", MAX_FILES_PER_CALL):
        if not isinstance(item, dict):
            raise BadRequest("files 의 항목은 객체다")
        path = item.get("path")
        results.append(
            {"path": path, "error": _upload_one(resolver, path, item.get("content_b64"))}
        )
    return {"results": results}


def _upload_one(resolver: Resolver, path: object, content: object) -> str | None:
    if not isinstance(content, str):
        return INVALID_CONTENT
    if len(content) > (MAX_FILE // 3 + 1) * 4:
        return TOO_LARGE
    try:
        data = base64.b64decode(content, validate=True)
    except (binascii.Error, ValueError):
        return INVALID_CONTENT
    if len(data) > MAX_FILE:
        return TOO_LARGE
    try:
        _, real = resolver.resolve(path)
    except PathRefused:
        return INVALID_PATH
    try:
        real.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(real, os.O_WRONLY | os.O_CREAT | _OPEN_FLAGS, 0o644)
    except OSError as e:
        return _os_error(e)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return NOT_A_FILE
        os.ftruncate(fd, 0)
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view) :]
    except OSError as e:
        return _os_error(e)
    finally:
        os.close(fd)
    return None


def download(resolver: Resolver, body: dict) -> dict:
    results = []
    budget = MAX_DOWNLOAD_TOTAL
    for path in _list_of(body, "paths", MAX_FILES_PER_CALL):
        data, error = _read_one(resolver, path, min(MAX_FILE, budget))
        if data is not None:
            budget -= len(data)
        results.append(
            {
                "path": path,
                "content_b64": None if data is None else base64.b64encode(data).decode("ascii"),
                "error": error,
            }
        )
    return {"results": results}


def _read_one(resolver: Resolver, path: object, limit: int) -> tuple[bytes | None, str | None]:
    try:
        _, real = resolver.resolve(path)
    except PathRefused:
        return None, INVALID_PATH
    try:
        fd = os.open(real, os.O_RDONLY | _OPEN_FLAGS)
    except OSError as e:
        return None, _os_error(e)
    try:
        mode = os.fstat(fd).st_mode
        if stat.S_ISDIR(mode):
            return None, IS_DIRECTORY
        if not stat.S_ISREG(mode):
            return None, NOT_A_FILE
        chunks, size = [], 0
        while size <= limit:
            chunk = os.read(fd, min(1 << 20, limit + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
        if size > limit:
            return None, TOO_LARGE
        return b"".join(chunks), None
    except OSError as e:
        return None, _os_error(e)
    finally:
        os.close(fd)


def list_files(resolver: Resolver, body: dict) -> dict:
    """작업 공간의 보통 파일. 심볼릭 링크는 따라가지도 싣지도 않는다. 경로 순이다."""
    exclude = body.get("exclude", [])
    if (
        not isinstance(exclude, list)
        or len(exclude) > MAX_EXCLUDE
        or not all(isinstance(n, str) and n and "/" not in n and "\0" not in n for n in exclude)
    ):
        raise BadRequest(f"exclude 는 {MAX_EXCLUDE} 개까지의 맨 위 이름이다")
    _, root = resolver.resolve("/workspace")
    found: list[dict] = []
    walked = 0
    truncated = False
    stack = [root]
    while stack and not truncated:
        here = stack.pop()
        try:
            entries = list(os.scandir(here))
        except OSError:
            continue
        for entry in entries:
            walked += 1
            if walked > MAX_WALK or len(found) > MAX_LIST:
                truncated = True
                break
            if here == root and entry.name in exclude:
                continue
            try:
                if entry.is_symlink():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    stack.append(Path(entry.path))
                elif entry.is_file(follow_symlinks=False):
                    st = entry.stat(follow_symlinks=False)
                    found.append(
                        {
                            "path": resolver.virtual(Path(entry.path)),
                            "size": st.st_size,
                            "modified": st.st_mtime,
                        }
                    )
            except OSError:
                continue
    found.sort(key=lambda f: f["path"])
    if len(found) > MAX_LIST:
        found, truncated = found[:MAX_LIST], True
    return {"files": found, "truncated": truncated}
