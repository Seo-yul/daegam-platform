"""파일 호출의 경로 풀이 (P40 1절, 3절).

파일 Tool 과 셸은 컨테이너의 실제 경로를 쓴다. `/workspace/...` 는 작업 공간이고, 이미지의
`/uploads` 와 `/skills` 는 `/workspace/uploads`·`/workspace/skills` 를 가리키는 심볼릭 링크라
여기서도 그 자리로 옮긴다. deepagents 의 큰 편집은 옛 글과 새 글을 `/tmp/.deepagents_edit_*` 에
올리므로 `/tmp/...` 도 받는다. `..`, 뿌리를 벗어나는 심볼릭 링크, NUL, 512 자를 넘는 경로는
거절한다.
"""

from __future__ import annotations

import os
from pathlib import Path

from daegam_sandbox.config import Layout

MAX_PATH = 512
INVALID_PATH = "invalid_path"


class PathRefused(Exception):
    """받지 않는 경로다. 답의 error 는 deepagents 의 `invalid_path` 다."""

    code = INVALID_PATH


class Resolver:
    """가상 경로(`/workspace/a.csv`) -> (정규화한 가상 경로, 실제 경로)."""

    def __init__(self, layout: Layout) -> None:
        self.layout = layout
        workspace = layout.workspace
        # 머리 -> (그 머리의 실제 자리, 벗어나면 안 되는 뿌리)
        self._roots: dict[str, tuple[Path, Path]] = {
            "workspace": (workspace, workspace),
            "uploads": (workspace / "uploads", workspace),
            "skills": (workspace / "skills", workspace),
            "tmp": (layout.tmp, layout.tmp),
        }

    def resolve(self, path: object, *, heads: tuple[str, ...] | None = None) -> tuple[str, Path]:
        """경로를 풀어 실제 자리를 준다. heads 를 주면 그 머리만 받는다."""
        if not isinstance(path, str) or not path or "\0" in path or len(path) > MAX_PATH:
            raise PathRefused(path)
        if not path.startswith("/"):
            path = f"/workspace/{path}"
        parts = [p for p in path.split("/") if p not in ("", ".")]
        if not parts or ".." in parts:
            raise PathRefused(path)
        head = parts[0]
        if head not in self._roots or (heads is not None and head not in heads):
            raise PathRefused(path)
        base, contain = self._roots[head]
        real = Path(os.path.realpath(base.joinpath(*parts[1:])))
        fence = os.path.realpath(contain)
        if str(real) != fence and not str(real).startswith(fence + os.sep):
            raise PathRefused(path)
        return "/" + "/".join(parts), real

    def virtual(self, real: Path) -> str:
        """작업 공간 안의 실제 경로 -> `/workspace/...`."""
        rel = Path(real).relative_to(os.path.realpath(self.layout.workspace))
        return "/workspace" if str(rel) == "." else f"/workspace/{rel.as_posix()}"
