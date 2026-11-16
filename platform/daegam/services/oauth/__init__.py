"""MCP 클라이언트의 OAuth 인가 서버 (D59, P33).

플랫폼이 사람 토큰을 내는 모드(local, oidc)에서 플랫폼이 MCP 인가 명세의 인가 서버가 된다. oidc
모드에서 데이터 플레인이 아직 IdP 만 믿는 동안은 잠들어 아무것도 내지 않는다 (P53b 6.5절).
등록(`clients`), 인가 요청(`authorize`), 동의(`consent`), 토큰·갱신·회수와 연결(`tokens`),
오류(`errors`), 감사 재료(`audit`)가 파일 하나씩이고, 라우터(`api/oauth.py`,
`api/oauth_grants.py`)는 HTTP 모양만 맡는다. 서명과 돌아갈 곳의 규칙은 서비스가 아닌
`auth/oauth_keys.py`·`auth/oauth_redirect.py` 에 있다.
"""
