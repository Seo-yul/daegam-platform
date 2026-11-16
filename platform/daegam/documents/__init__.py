"""문서에서 글을 뽑는 자리와 그것을 제어면 밖에서 돌리는 서비스 `daegam-docparse` (D32, D61).

- `formats` 받는 형식의 표. 첨부와 Knowledge Base 가 같은 표를 쓴다
- `result` 추출 결과와 오류의 모양
- `extract`·`ooxml`·`hwpx` 형식마다 글을 뽑는 함수
- `sandbox`·`worker` 요청마다 띄우는 하위 프로세스와 그 가두기
- `server` `daegam-docparse` 의 HTTP 서버(`python -m daegam.documents.server`)
- `selfcheck` 실제 파드에서 가두기를 확인하는 자기 점검

이 패키지는 api·services·store 를 부르지 않는다(`tests/test_layering.py`). 플랫폼은
`services/docparse.py` 로 서비스를 부른다. 여기의 import 는 가볍게 둔다. 설정(`daegam.config`)이
토큰의 env 이름을, 그 클라이언트가 포트와 env 이름을 아래 두 상수에서 읽는다.
"""

# `daegam-docparse` 가 듣는 포트와 요청을 확인하는 토큰의 env. 플랫폼도 같은 이름으로 토큰을 받는다
DOCPARSE_PORT = 9100
TOKEN_ENV = "DAEGAM_DOCPARSE_TOKEN"
