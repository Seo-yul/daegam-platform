"""스펙 본문 저장. MongoDB GridFS 버킷 `spec_bodies`에
`{profile_id}/{version}/openapi.yaml`과 `{profile_id}/{version}/source.<ext>`로 둔다.

metadata에 profile_id/version/kind/role을 넣어 프로필 단위로 일괄 삭제한다. 한 프로필의 버전
쓰기는 서비스가 리스로 하나씩 돌린다.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from gridfs.asynchronous import AsyncGridFSBucket
from gridfs.errors import NoFile
from pymongo import ASCENDING
from pymongo.asynchronous.database import AsyncDatabase

BUCKET = "spec_bodies"
OPENAPI_FILENAME = "openapi.yaml"


@runtime_checkable
class SpecFiles(Protocol):
    async def init_schema(self) -> None: ...

    async def write(
        self,
        profile_id: str,
        version: int,
        *,
        openapi_yaml: str,
        source: bytes,
        source_ext: str,
        kind: str,
    ) -> None: ...

    async def read_openapi(self, profile_id: str, version: int) -> bytes | None: ...
    async def read_source(self, profile_id: str, version: int) -> bytes | None: ...
    async def delete_spec_bodies(self, profile_id: str) -> int: ...


def openapi_name(profile_id: str, version: int) -> str:
    return f"{profile_id}/{version}/{OPENAPI_FILENAME}"


def source_name(profile_id: str, version: int, ext: str) -> str:
    return f"{profile_id}/{version}/source.{ext}"


class GridFSSpecFiles:
    def __init__(self, db: AsyncDatabase, bucket_name: str = BUCKET):
        self.db = db
        self.bucket_name = bucket_name
        self.bucket = AsyncGridFSBucket(db, bucket_name=bucket_name)
        self.files = db[f"{bucket_name}.files"]

    async def init_schema(self) -> None:
        # filename·uploadDate와 chunks 인덱스는 GridFS가 첫 업로드 때 만든다. 삭제용만 추가한다.
        await self.files.create_index(
            [("metadata.profile_id", ASCENDING), ("metadata.version", ASCENDING)],
            name="ix_profile_version",
        )

    async def _delete_where(self, query: dict[str, Any]) -> int:
        """query 에 맞는 파일과 그 chunk 를 지운다. 지운 파일 수를 돌려준다."""
        ids = [d["_id"] async for d in self.files.find(query, {"_id": 1})]
        for file_id in ids:
            try:
                await self.bucket.delete(file_id)
            except NoFile:
                pass
        return len(ids)

    async def _get(self, filename: str) -> bytes | None:
        try:
            grid_out = await self.bucket.open_download_stream_by_name(filename)
        except NoFile:
            return None
        return await grid_out.read()

    async def write(
        self,
        profile_id: str,
        version: int,
        *,
        openapi_yaml: str,
        source: bytes,
        source_ext: str,
        kind: str,
    ) -> None:
        # 멈춘 쓰기가 남긴 이 버전의 본문을 먼저 지워 버전마다 원본 하나, 변환 본문 하나만 둔다
        await self._delete_where({"metadata.profile_id": profile_id, "metadata.version": version})
        base = {"profile_id": profile_id, "version": version, "kind": kind}
        await self.bucket.upload_from_stream(
            source_name(profile_id, version, source_ext),
            source,
            metadata={**base, "role": "source", "ext": source_ext},
        )
        await self.bucket.upload_from_stream(
            openapi_name(profile_id, version),
            openapi_yaml.encode("utf-8"),
            metadata={**base, "role": "openapi", "content_type": "text/yaml"},
        )

    async def read_openapi(self, profile_id: str, version: int) -> bytes | None:
        return await self._get(openapi_name(profile_id, version))

    async def read_source(self, profile_id: str, version: int) -> bytes | None:
        doc = await self.files.find_one(
            {
                "metadata.profile_id": profile_id,
                "metadata.version": version,
                "metadata.role": "source",
            },
            projection={"filename": 1},
        )
        if doc is None:
            return None
        return await self._get(doc["filename"])

    async def delete_spec_bodies(self, profile_id: str) -> int:
        """그 문서의 스펙 본문 파일 전부 삭제. 지운 파일 수를 돌려준다."""
        return await self._delete_where({"metadata.profile_id": profile_id})
