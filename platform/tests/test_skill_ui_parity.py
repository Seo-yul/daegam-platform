"""D64: 화면의 ui/src/lib/limits.ts 의 `SKILL_LIMITS` 는 서버 형식 검사와 API 의 상한을 손으로 옮긴
사본이다.

화면은 이 값으로 검사 문장의 숫자를 채우고, 가져오기 파일과 리비전 메모를 고른 자리에서 막는다.
어긋나면 문장이 서버와 다른 상한을 말하거나, 화면이 통과시킨 파일을 서버가 413·422 로 거절한다.
서버 쪽(services/skill_format.py, api/skills.py)이 원본이므로 화면 쪽을 그 값에 맞춘다.
"""

from __future__ import annotations

import re

from daegam.api.skills import NOTE_MAX
from daegam.services import skill_format as fmt
from daegam.units import KIB, MIB
from tests.helpers.source import UI_LIB, ts_object

UI_COPY = UI_LIB / "limits.ts"
FIX = "ui/src/lib/limits.ts 의 SKILL_LIMITS 를 서버 skill_format.py·api/skills.py 값에 맞춘다"
_ENTRY_RE = re.compile(r"(\w+)\s*:\s*(\d+)")


def test_skill_limits_match_the_server() -> None:
    limits = {k: int(v) for k, v in ts_object(UI_COPY, "SKILL_LIMITS", _ENTRY_RE).items()}
    assert limits == {
        "files": fmt.MAX_FILES,
        "skillMdKib": fmt.MAX_SKILL_MD_BYTES // KIB,
        "fileKib": fmt.MAX_FILE_BYTES // KIB,
        "revisionKib": fmt.MAX_REVISION_BYTES // KIB,
        "importMib": fmt.MAX_IMPORT_BYTES // MIB,
        "zipEntries": fmt.ZIP_LIMITS["max_entries"],
        "description": fmt.MAX_DESCRIPTION,
        "compatibility": fmt.MAX_COMPATIBILITY,
        "license": fmt.MAX_LICENSE,
        "metadataKeys": fmt.MAX_METADATA_KEYS,
        "allowedTools": fmt.MAX_ALLOWED_TOOLS,
        "note": NOTE_MAX,
    }, FIX


def test_units_are_whole_so_the_copy_is_exact() -> None:
    """화면은 KiB·MiB 정수로 적으므로 서버 값이 그 단위로 나눠떨어져야 사본이 정확하다. zip 의 풀린
    크기 상한은 화면 문장이 리비전 합계로 적는다."""
    for limit in (fmt.MAX_SKILL_MD_BYTES, fmt.MAX_FILE_BYTES, fmt.MAX_REVISION_BYTES):
        assert limit % KIB == 0
    assert fmt.MAX_IMPORT_BYTES % MIB == 0
    assert fmt.ZIP_LIMITS["max_total"] == fmt.MAX_REVISION_BYTES
