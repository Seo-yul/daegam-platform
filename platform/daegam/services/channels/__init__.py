"""Agent 를 부르는 바깥 채널 (P47, D114 초안). 지금은 Microsoft Teams 의 1:1 채팅이다.

- `inbound.py`: Bot Connector 토큰의 검증기와 활동의 모양
- `bot.py`: 메시지 엔드포인트의 차례와 명령 (`연결`, `연결 끊기`, `도움말`)
- `links.py`: 연결 코드, 연결, 확인, 끊기, 정리
- `connector.py`: 봇 토큰과 Connector 호출, 올릴 주소의 규칙
- `voice.py`: 대화에 고정 줄 올리기
- `texts.py`, `particle.py`: 봇의 글과 조사
- `package.py`: Teams 앱 패키지
"""
