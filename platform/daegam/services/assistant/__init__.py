"""플랫폼 안의 청지기 (D31, P14).

도구는 플랫폼 API 그 자체다. `catalog.py` 의 표가 (method, path) 에 이름·위험 등급·설명·비밀 필드를
붙이고, `schema.py` 가 기동 시 받은 OpenAPI dict 에서 인자 스키마를 뽑으며, `tools.py` 의 실행기가
같은 프로세스의 ASGI 앱을 사용자 신원으로 부른다. 이 패키지는 `daegam.api` 를 import 하지 않는다
(D30). 앱과 OpenAPI 문서는 배선(wiring)이 넘긴다.
"""
