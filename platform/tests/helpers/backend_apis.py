"""Backend API 를 올리는 테스트가 함께 쓰는 도우미.

`upload_backend` 는 업로드 응답을 그대로 주므로 업로드 규칙을 보는 테스트가 쓰고,
`uploaded_backend` 는 만든 문서만 필요한 준비 단계가 쓴다. 올리는 스펙 표본은 tests/fixtures 에
있다(`fixture_bytes`). 저장된 스펙 본문은 GridFS 버킷 spec_bodies 에서 읽는다(`stored_files`,
`stored_body`).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import gridfs
import httpx
from fastapi.testclient import TestClient

from daegam.store.backend_apis import BackendAPIVersion, ToolDetail
from tests.helpers.consts import BP

# 업로드에 쓰는 스펙 표본(WSDL·OpenAPI·Swagger)의 자리
FIXTURES = Path(__file__).parents[1] / "fixtures"

# calculator.wsdl Backend API 에 다는 SOAP 엔드포인트. MCP Server 가 SPEC2OPENAPI_ENDPOINT 로 받는다
CALC_ENDPOINT = "http://mock-soap.svc:18080/calc"


def backend_version(
    names: list[str], *, bp: str = "bp", version: int = 1, details: list[ToolDetail] | None = None
) -> BackendAPIVersion:
    """서비스를 거치지 않은 Backend API 버전 문서. names 는 그 버전의 operationId 다."""
    return BackendAPIVersion(
        backend_profile_id=bp,
        version=version,
        kind="openapi",
        source_format="openapi",
        source_ext="yaml",
        source_sha256="0" * 64,
        output_sha256="0" * 64,
        s2o_version="0",
        tool_count=len(names),
        tool_names=list(names),
        tool_details=details or [],
    )


def fixture_bytes(name: str) -> bytes:
    """tests/fixtures 의 스펙 표본 name 의 바이트."""
    return (FIXTURES / name).read_bytes()


def upload_backend(
    client: TestClient,
    name: str = "calc",
    fixture: str = "calculator.wsdl",
    *,
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
    **form: str,
) -> httpx.Response:
    """Backend API name 에 tests/fixtures 의 fixture 를 올린 응답이다. body 를 주면 그 바이트를
    fixture 라는 파일 이름으로 올린다. form 은 나머지 폼 필드(endpoint·auth·credential_secret·
    display_name)다."""
    content = fixture_bytes(fixture) if body is None else body
    return client.post(
        BP, data={"name": name, **form}, files={"file": (fixture, content)}, headers=headers
    )


def uploaded_backend(client: TestClient, *args: Any, **kw: Any) -> dict[str, Any]:
    """`upload_backend` 로 새로 만든 Backend API 문서. 201 이 아니면 그 자리에서 실패한다."""
    r = upload_backend(client, *args, **kw)
    assert r.status_code == 201, r.text
    return r.json()


def stored_files(mongo, profile_id: str) -> list[dict]:
    """GridFS spec_bodies 버킷에서 프로필의 파일 문서 목록."""
    return list(mongo["spec_bodies.files"].find({"metadata.profile_id": profile_id}))


def stored_body(mongo, filename: str) -> bytes:
    return (
        gridfs.GridFSBucket(mongo, bucket_name="spec_bodies")
        .open_download_stream_by_name(filename)
        .read()
    )
