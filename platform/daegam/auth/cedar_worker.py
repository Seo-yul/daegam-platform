"""Cedar 글을 파싱하는 하위 프로세스 (T63). 플랫폼이 판정마다 하나씩 띄운다
(`services/cedar_check.py`).

Cedar 엔진(cedarpy)은 네이티브 코드라 어떤 입력에서는 프로세스째 죽는다(겹친 괄호). 그래서 플랫폼
프로세스는 엔진을 import 하지 않고, 이 하위 프로세스가 문서 파싱의 하위 프로세스와 같은 틀
(`documents/sandbox.py`)로 스스로를 가둔 뒤 문장들을 파싱한다. 차례는 `documents/worker.py` 와 같다.
가두고, 준비 바이트를 내고, 요청 한 줄과 문장들(JSON 목록)을 읽고, 답 한 덩이를 낸다. 답은
`{"ok": true, "result": {}}` 이거나 몇째 문장이 왜 틀렸는지를 실은
`{"ok": false, "code": "cedar_invalid", "detail"}` 다. 문장은 `auth/cedar.py` `statement_code` 의
검사를 지난 것이다.

이 하위 프로세스가 지키는 것은 엔진의 충돌에서 플랫폼 프로세스를 떼어 두는 것뿐이고 가두기의 경계는
아니다. 플랫폼 프로세스는 dumpable 이고, ServiceAccount 토큰을 마운트하며, 비밀을 env 로 읽는다.
"""

from __future__ import annotations

import json
import sys
from typing import Any, BinaryIO

import cedarpy

from daegam.auth.cedar import CEDAR_INVALID, MAX_POLICY_CHARS
from daegam.documents.sandbox import READY, confine, read_document, wait_readable

MAX_REQUEST_LINE = 256
# 문장들(JSON 목록)의 상한(바이트). 플랫폼은 ensure_ascii=False 로 보내므로 글자 하나가 UTF-8 로
# 많아야 네 바이트(BMP 밖의 글자)이고, JSON 이 이스케이프하는 제어 문자(`\u00XX`)도 여섯 바이트다.
# 따옴표와 쉼표는 문장마다 붙지만 문장은 두 글자보다 짧지 않다
MAX_INPUT_BYTES = 6 * MAX_POLICY_CHARS + 1024


def verdict(codes: list[str]) -> dict[str, Any]:
    """문장마다 Cedar 정책 하나로 파싱되는가. 슬롯(`?principal`)을 쓴 템플릿은 연결하기 전까지
    아무것도 허용하지 않으므로 틀린 것으로 본다."""
    for i, code in enumerate(codes, start=1):
        try:
            parsed = json.loads(cedarpy.policies_to_json_str(code))
        except ValueError as e:
            return {"ok": False, "code": CEDAR_INVALID, "detail": f"statement {i}: {e}"}
        except RecursionError:
            # 엔진은 받았지만 그 결과(JSON)가 파이썬이 읽기에 너무 깊다. `1+1+…` 같은 긴 사슬이다
            detail = f"statement {i}: the expression nests too deeply"
            return {"ok": False, "code": CEDAR_INVALID, "detail": detail}
        if parsed.get("templates"):
            detail = f"statement {i}: a template with slots is not a policy"
            return {"ok": False, "code": CEDAR_INVALID, "detail": detail}
    return {"ok": True, "result": {}}


def _answer(stdin: BinaryIO) -> dict[str, Any]:
    wait_readable(stdin)
    try:
        request = json.loads(stdin.readline(MAX_REQUEST_LINE))
        codes = json.loads(read_document(stdin, MAX_INPUT_BYTES + 1))
    except ValueError:
        codes = None
        request = None
    if (
        not isinstance(request, dict)
        or request.get("task") != "parse"
        or not isinstance(codes, list)
        or not all(isinstance(c, str) for c in codes)
    ):
        return {"ok": False, "code": "extract_failed", "detail": "malformed worker request"}
    return verdict(codes)


def main() -> int:
    confine()
    out = sys.stdout.buffer
    out.write(READY)
    out.flush()
    out.write(json.dumps(_answer(sys.stdin.buffer)).encode())
    out.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
