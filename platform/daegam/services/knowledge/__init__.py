"""Knowledge Base 서비스 (D32, P15).

부품은 넷이다. `text` 가 텍스트를 자르고 토큰을 세며, `embeddings` 가 provider 행에서 langchain
`Embeddings` 클라이언트를 만들고, `stub_embedding` 은 런타임과 바이트 동일한 해시 임베딩이며,
`pg` 는 psycopg 비동기 접속과 pgvector 테이블·행 연산이다. 인덱서와 API 서비스는 이 위에 선다.
본문에서 글을 뽑는 일은 문서 파싱 서비스가 한다(D61, `services/docparse.py`).
"""
