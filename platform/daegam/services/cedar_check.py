"""사람이 쓴 Cedar 글(authz_policy)의 저장 검사 (T63). Gateway 의 만들기·수정·복제·미리보기·배포·
재배포가 `GatewayValidator.check_authz_policy` 로 부른다. 렌더는 파싱하지 않는다.

글은 둘을 지나야 받아들여진다. `auth/cedar.py` `statement_code` 의 검사(길이, 괄호 깊이, NUL, 다른
공백, 닫히지 않은 문자열·주석)와 하위 프로세스(`auth/cedar_worker.py`)에서 도는 Cedar 엔진의
파싱이다. 엔진은 네이티브 코드라 알려지지 않은 입력에서도 죽을 수 있으므로 플랫폼 프로세스에서
돌리지 않는다. 하위 프로세스는 문서 파싱의 틀(`documents/sandbox.py` `run_worker`)로 띄우고 거두며,
한 번에 `MAX_WORKERS` 개까지만 돈다.

하위 프로세스가 지키는 것은 엔진의 충돌에서 플랫폼 프로세스를 떼어 두는 것뿐이다. 가두기의 경계는
아니다. 플랫폼 프로세스는 dumpable 이고, ServiceAccount 토큰을 마운트하며, 비밀을 env 로 읽는다.

판정은 확실한 것만 낸다. 검사에 걸리거나 엔진이 문장을 틀렸다고 답하면 CedarSyntaxError(422
authz_invalid)다. 하위 프로세스를 띄우지 못했거나, 준비 전에 끝났거나, 죽었거나, 모양이 틀린 답을
냈거나, `PARSE_TIMEOUT` 안에 답하지 않으면 CedarCheckUnavailable(503) 이고 맞다고도 틀렸다고도 보지
않는다.
"""

from __future__ import annotations

import asyncio
import json
import sys

from daegam.auth.cedar import CEDAR_INVALID, CedarSyntaxError, statement_code
from daegam.documents.result import ExtractError
from daegam.documents.sandbox import run_worker

WORKER_ARGV = (sys.executable, "-I", "-m", "daegam.auth.cedar_worker")
# 하위 프로세스 하나가 답할 시간(초). 띄우기와 엔진 import 가 대부분이고 파싱은 밀리초다
PARSE_TIMEOUT = 10.0
# 함께 도는 하위 프로세스 수. 하나가 30MB 남짓이고 플랫폼 파드의 메모리 한도는 1Gi 다. 자리를
# 기다리는 시간은 PARSE_TIMEOUT 에 들지 않는다
MAX_WORKERS = 2
_workers = asyncio.Semaphore(MAX_WORKERS)


class CedarCheckUnavailable(Exception):
    """파싱이 판정을 내지 못했다. 글이 맞는지 틀린지 모른다."""


async def parse_statements(text: str) -> None:
    """문장 하나라도 파싱되지 않으면 CedarSyntaxError, 판정을 내지 못하면 CedarCheckUnavailable
    이다."""
    codes = statement_code(text)
    if not codes:
        return
    data = json.dumps(codes, ensure_ascii=False).encode()
    try:
        async with _workers:
            await run_worker({"task": "parse"}, data, timeout=PARSE_TIMEOUT, argv=WORKER_ARGV)
    except ExtractError as e:
        if e.code == CEDAR_INVALID:
            raise CedarSyntaxError(e.detail) from e
        detail = e.detail or e.code
        raise CedarCheckUnavailable(f"the Cedar parser did not answer ({detail})") from e
    except OSError as e:
        raise CedarCheckUnavailable(f"the Cedar parser could not start ({e})") from e
