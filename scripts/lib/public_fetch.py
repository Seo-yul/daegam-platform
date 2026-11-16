"""공개 사이트에서 받기 (P58, D70). image-report.sh 와 cpython-record.sh 가 함께 쓴다.

자격 증명을 싣지 않는다. 시스템 신뢰 목록에 DAEGAM_CA_FILE(사내 방화벽 CA)이 있으면 더한다.
그 파일을 읽지 못하면 받을 때마다 OSError 다. image-report 는 그것을 "확인 못 함" 으로 적고
cpython-record 는 멈춘다. 기본 User-Agent(Python-urllib)는 python-build-standalone 다운로드
CDN 이 403 으로 거절하므로 이름을 붙인다.

맥의 시스템 python3(3.9)에서도 돌아야 하므로 3.10 이후 문법을 쓰지 않는다.
"""

from __future__ import annotations

import os
import ssl
import urllib.request

USER_AGENT = "daegam-scripts"


def context() -> ssl.SSLContext:
    """시스템 신뢰 목록에 DAEGAM_CA_FILE 을 더한 TLS 문맥. 그 파일을 읽지 못하면 OSError 다."""
    ctx = ssl.create_default_context()
    ca = os.environ.get("DAEGAM_CA_FILE")
    if ca:
        try:
            ctx.load_verify_locations(cafile=ca)
        except OSError as e:  # ssl.SSLError 도 OSError 다
            raise OSError(f"DAEGAM_CA_FILE({ca})을 읽지 못했다: {e}") from e
    return ctx


def fetch(url: str, timeout: float) -> bytes:
    """url 의 본문. 실패하면 urllib 의 예외(HTTPError, URLError, 시간 초과)나 context() 의
    예외를 그대로 올린다."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout, context=context()) as r:
        return r.read()
