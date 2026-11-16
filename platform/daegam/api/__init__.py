"""HTTP API 라우터.

모든 API 는 `/api` 아래에 있다. 루트 네임스페이스는 UI 몫이고 daegam-ui 컨테이너가 낸다(D52).
예외는 배포 매니페스트의 readiness/liveness probe 가 부르는 `/healthz` 와, 발급자 디스커버리·JWKS
인 `/.well-known/*` 다(D40·D41). 접두사 `API_PREFIX` 는 렌더된 CR 도 담아야 해서 코어인
`daegam/urls.py` 에 있다.
"""
