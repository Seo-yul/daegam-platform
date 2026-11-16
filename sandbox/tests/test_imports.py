"""서버(PID 1)가 싣는 모듈 (P40 F1, security/vex/sandbox.openvex.json).

샌드박스 이미지의 수용 목록은 SQLite·expat·zlib·ncurses 의 발견을 "작업 공간이 돌리려고 있는 코드만
닿는다" 는 까닭으로 받는다. 그 코드는 이미 파드 안에서 uid 1000 으로 돌고 파드가 경계다. 그 까닭은
서버가 그것에 닿는 모듈을 하나도 싣지 않을 때만 참이다. 그래서 서버의 모든 모듈을 `python -I` 로
실은 뒤의 모듈 집합을 여기서 고정한다. 서버가 새 모듈을 실으면 이 시험이 실패하고, 그 모듈이 닿는
라이브러리를 검토해 ALLOWED 에 더하거나 수용 목록의 statement 를 고친다. 서버가 닿는
것(hashlib·hmac·ssl 의 OpenSSL, ctypes 의 libffi, libc)에는 statement 가 없어 그 발견은 막는다.
"""

from __future__ import annotations

import json
import subprocess
import sys

from conftest import SOURCE

MODULES = ("__main__", "config", "files", "hardening", "http", "paths", "probes", "procs")
MODULES = (*MODULES, "selfcheck", "server")
# 수용 목록의 까닭이 기대는 것. 서버가 이 가운데 하나라도 실으면 그 까닭이 거짓이 된다
FORBIDDEN = frozenset(
    {
        "sqlite3",
        "_sqlite3",
        "pyexpat",
        "xml",
        "plistlib",
        "xmlrpc",
        "zlib",
        "gzip",
        "zipfile",
        "tarfile",
        "bz2",
        "_bz2",
        "lzma",
        "_lzma",
        "curses",
        "_curses",
        "_curses_panel",
        "readline",
        "tkinter",
        "_tkinter",
        "dbm",
        "_dbm",
        "_gdbm",
        "shutil",
    }
)
# 3.12(이미지)와 3.13(개발 머신)에서 서버가 싣는 최상위 모듈의 합. 이 밖의 것을 실으면 검토한다
ALLOWED = frozenset(
    {
        "__future__",
        "__main__",
        "_abc",
        "_ast",
        "_asyncio",
        "_blake2",
        "_codecs",
        "_collections",
        "_collections_abc",
        "_colorize",
        "_contextvars",
        "_ctypes",
        "_frozen_importlib",
        "_frozen_importlib_external",
        "_functools",
        "_hashlib",
        "_heapq",
        "_imp",
        "_io",
        "_json",
        "_locale",
        "_opcode",
        "_opcode_metadata",
        "_operator",
        "_posixsubprocess",
        "_signal",
        "_sitebuiltins",
        "_socket",
        "_sre",
        "_ssl",
        "_stat",
        "_string",
        "_struct",
        "_thread",
        "_tokenize",
        "_typing",
        "_warnings",
        "_weakref",
        "_weakrefset",
        "abc",
        "argparse",
        "array",
        "ast",
        "asyncio",
        "atexit",
        "base64",
        "binascii",
        "builtins",
        "codecs",
        "collections",
        "concurrent",
        "contextlib",
        "contextvars",
        "copy",
        "copyreg",
        "ctypes",
        "daegam_sandbox",
        "dataclasses",
        "dis",
        "encodings",
        "enum",
        "errno",
        "fcntl",
        "fnmatch",
        "functools",
        "genericpath",
        "gettext",
        "glob",
        "grp",
        "hashlib",
        "heapq",
        "hmac",
        "importlib",
        "inspect",
        "io",
        "ipaddress",
        "itertools",
        "json",
        "keyword",
        "linecache",
        "locale",
        "logging",
        "marshal",
        "math",
        "ntpath",
        "opcode",
        "operator",
        "os",
        "pathlib",
        "posix",
        "posixpath",
        "pwd",
        "re",
        "reprlib",
        "resource",
        "select",
        "selectors",
        "signal",
        "site",
        "socket",
        "ssl",
        "stat",
        "string",
        "struct",
        "subprocess",
        "sys",
        "sysconfig",
        "textwrap",
        "threading",
        "time",
        "token",
        "tokenize",
        "traceback",
        "types",
        "typing",
        "urllib",
        "warnings",
        "weakref",
        "zipimport",
    }
)


def _loaded() -> set[str]:
    """새 인터프리터를 venv 밖에서 -I 로 띄워 서버 모듈을 모두 실은 뒤의 최상위 모듈 이름."""
    boot = (
        f"import sys; sys.path.insert(0, {str(SOURCE)!r}); import importlib, json; "
        f"[importlib.import_module('daegam_sandbox.' + m) for m in {MODULES!r}]; "
        "print(json.dumps(sorted({n.split('.')[0] for n in sys.modules})))"
    )
    exe = getattr(sys, "_base_executable", sys.executable)
    done = subprocess.run([exe, "-I", "-c", boot], capture_output=True, text=True, check=True)
    return set(json.loads(done.stdout))


def test_the_server_loads_nothing_the_accepted_list_relies_on_it_not_loading() -> None:
    loaded = _loaded()
    assert loaded & FORBIDDEN == set()
    ours = {"daegam_sandbox", "__main__"}
    outside = sorted(m for m in loaded - ours if m not in sys.stdlib_module_names)
    assert outside == [], "서버는 표준 라이브러리만 싣는다"
    assert sorted(loaded - ALLOWED) == [], (
        "서버가 새 모듈을 싣는다. 그 모듈이 닿는 라이브러리를 검토하고 ALLOWED 나 "
        "security/vex/sandbox.openvex.json 을 고친다"
    )
    assert not ALLOWED & FORBIDDEN
