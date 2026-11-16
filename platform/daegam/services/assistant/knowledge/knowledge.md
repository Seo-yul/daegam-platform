# Knowledge Base
문서를 올려 에이전트가 검색하게 하거나, Knowledge Base 관련 422·409 를 받았거나, 캔버스의 knowledge 노드를 채울 때 읽는다.

## 개념
- Knowledge Base 는 테넌트 리소스다. 문서(txt·md·csv·tsv·json·html·pdf·docx·pptx·xlsx·hwpx, 또는 공개 URL)를 잘라 임베딩해 postgres Database 의 pgvector 테이블에 넣는다. 옛 이진 형식(doc·xls·ppt·hwp)과 스캔 문서의 글자 인식은 하지 않는다.
- 필드는 {name, display_name, data_service_id, embedding{provider, model, dimensions}, chunking{size, overlap}, limits{max_chunks}} 이고 목록·상세에 status{documents, indexed, failed, chunks, tokens, last_indexed_at} 집계가 붙는다.
- data_service_id 는 kind 가 postgres 인 Database 여야 한다. redis 는 invalid_data_service_kind 다. Database 는 list_data_services 로 고르고 verify_data_service 의 extensions.vector 로 pgvector 가 있는지 미리 볼 수 있다.
- embedding.provider 는 Platform Settings 의 provider 이름이다. kind 가 openai 또는 stub 이어야 하고 anthropic·openrouter·zai 는 embedding_not_supported 다. model 을 비우면 provider 의 embedding_model(기본 text-embedding-3-small, stub 은 stub)이고 dimensions 는 알려진 모델이면 자동이다. 모르는 모델은 dimensions 를 직접 적어야 한다(dimensions_required).
- embedding 과 data_service_id 는 만든 뒤 바꿀 수 없다. 모델을 바꾸려면 새 Knowledge Base 를 만든다.
- chunking 은 글자 수다(size 200~4000 기본 1200, overlap 0~size/2 기본 150). 바꾸면 모든 문서가 pending 으로 돌아가 다시 인덱싱된다.

## 문서
- 문서는 add_knowledge_document_url 로 더한다(공개 http(s) URL 만. 사설·클러스터 주소는 url_private 등 url_* 로 거부된다). 파일 업로드는 화면의 일이므로 navigate 로 /knowledge-bases/<id> 를 열어 안내한다.
- URL 문서는 승인 카드를 거친다. 남의 문서를 플랫폼에 심는 일이기 때문이다.
- 문서 상태는 pending → indexing → indexed 또는 failed 다. 삭제는 deleting 이고 워커가 행·본문·문서를 지운다. 상한은 문서 10MB, Knowledge Base 당 200 개, 문서당 청크 5000, 전체 limits.max_chunks 다.
- 상태는 list_knowledge_documents 또는 get_knowledge_document 로 본다. failed 문서의 error{code, detail} 을 읽고 원인을 고친 뒤 reindex_knowledge_base 로 다시 인덱싱한다.
- 텍스트 형식이 아닌 문서의 글은 플랫폼 밖의 문서 파싱 서비스(daegam-docparse)가 뽑는다. 그 서비스가 바쁘거나 연결할 수 없으면 인덱서가 잠시 기다렸다 다시 부르고, 그래도 안 되면 extract_unavailable 로 실패한다.
- 인덱싱은 플랫폼 파드가 Database 에 직접 접속해 한다. 원격 Database 는 플랫폼 네임스페이스에서 그 호스트로 egress 가 있어야 한다.

## 순서
1. list_data_services 로 postgres Database 를 고른다. 없으면 create_data_service 로 등록하고 verify_data_service 로 확인한다.
2. get_platform_settings 로 임베딩을 지원하는 provider(openai·stub)를 고른다.
3. create_knowledge_base 를 부른다. 생성이 그 자리에서 Database 에 접속해 확장·테이블을 만든다.
4. add_knowledge_document_url 로 문서를 더하고 get_knowledge_base 의 status.indexed 가 오를 때까지 기다린다.
5. 캔버스에서 agent → knowledge 간선으로 검색 Tool 을 붙인다. 노드 data 는 {knowledge_base_id, tool_name, description, top_k, min_score} 다.

## 흔한 오류와 대처
| code | 뜻 | 대처 |
|---|---|---|
| unknown_data_service (422) | 그 id 의 Database 가 없다 | list_data_services 로 다시 고른다 |
| invalid_data_service_kind (422) | Database 가 postgres 가 아니다 | postgres Database 를 고른다 |
| unknown_provider (422) | provider 이름이 목록에 없다 | get_platform_settings 로 이름을 확인한다 |
| embedding_not_supported (422) | anthropic·openrouter·zai provider 를 골랐다. 에이전트 검증에서는 만든 뒤 provider 의 kind 가 바뀌었다 | openai 또는 stub provider 를 고른다 |
| provider_no_key (422) | provider 의 Secret 이나 api_key 항목이 없다 | Secret 이름을 확인한다. 값은 묻지 않는다 |
| dimensions_required (422) | 모르는 임베딩 모델이다 | embedding.dimensions 를 적는다 |
| db_unreachable·auth_failed·unknown_database (422) | Database 에 붙지 못했다 | verify_data_service 로 확인하고 접속 정보를 고친다 |
| pgvector_missing (422) | vector 확장을 만들 수 없다 | 슈퍼유저(DBA)가 미리 CREATE EXTENSION vector 를 해야 한다 |
| embedding_immutable (422) | embedding 을 바꾸려 했다 | 새 Knowledge Base 를 만든다 |
| unsupported_format·too_many_documents (422) | 형식 밖이거나 200 개를 넘었다 | 형식을 맞추거나 문서를 줄인다 |
| encrypted_document·invalid_archive·archive_too_large·extract_timeout (문서 failed) | 암호가 걸렸거나, 파일이 손상됐거나, 압축을 푼 크기가 크거나, 읽는 데 30초를 넘었다 | 암호를 풀거나 문서를 나눠 다시 업로드하도록 안내한다 |
| extract_failed (문서 failed) | 문서 파싱 서비스가 그 파일을 읽지 못했다 | 원본 프로그램에서 다시 저장하거나 다른 형식으로 바꿔 다시 업로드하도록 안내한다 |
| empty_document (문서 failed) | 뽑은 글이 비었다. 스캔한 PDF 처럼 글자 층이 없는 문서다 | 글자가 든 판(OCR 을 거친 PDF 등)으로 다시 업로드하도록 안내한다 |
| extract_unavailable (문서 failed) | 문서 파싱 서비스가 바쁘거나 연결할 수 없었다 | 잠시 뒤 reindex_knowledge_base 로 다시 인덱싱한다 |
| indexing (409) | 인덱싱 중인 문서가 있다 | 끝난 뒤 다시 시도한다 |
| deleting (409) | 삭제가 시작된 Knowledge Base 다 | 새 Knowledge Base 를 만든다 |
| in_use (409) | Agent 가 쓰고 있다(또는 Database 를 Knowledge Base 가 쓴다) | 참조를 먼저 지운다 |
| table_not_dropped (경고) | 테이블을 못 지워 orphaned_table 로 남았다 | Database 가 닿을 때 delete_knowledge_base 를 다시 부른다 |
