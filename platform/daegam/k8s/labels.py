"""플랫폼이 만드는 모든 K8s 리소스에 붙는 라벨과, 라벨·어노테이션 값으로 쓰는 짧은 판."""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

from pydantic import BaseModel

LABEL_TENANT = "daegam.devops.ai.kr/tenant"
LABEL_RESOURCE = "daegam.devops.ai.kr/resource"
LABEL_ID = "daegam.devops.ai.kr/id"
LABEL_MANAGED_BY = "app.kubernetes.io/managed-by"
MANAGED_BY = "daegam"
# 허브 shared 복제본 (P6 F3b): 원본 항목 id 와 렌더한 spec 의 해시. 원본 id 는 스윕의 삭제와
# 입양이 읽고, 해시를 읽는 코드는 없다
LABEL_SOURCE_ID = "daegam.devops.ai.kr/source-id"
LABEL_SOURCE_HASH = "daegam.devops.ai.kr/source-hash"
# 배포가 핀한 리비전 번호 (MCPServer·VirtualMCPServer·Agent Deployment)
LABEL_PROFILE_REVISION = "daegam.devops.ai.kr/profile-revision"
# 이 설치의 id (P32 8절). 한 클러스터를 두 설치가 함께 쓸 때 인증 재적용이 자기 객체만 고른다
LABEL_PLATFORM_ID = "daegam.devops.ai.kr/platform-id"
# 샌드박스 파드를 연 Agent 의 id (P40 2절). Agent 의 샌드박스 정책이 이 라벨로 고른다. Agent 를
# 고르는 셀렉터는 `id` 를 보므로 샌드박스 파드에는 맞지 않는다
LABEL_AGENT = "daegam.devops.ai.kr/agent"
# MCPServer 의 백엔드 파드 (T67). 값은 CR 이름이고 podTemplateSpec 으로 StatefulSet 파드에만
# 간다. 네임스페이스 정책이 이 라벨로 백엔드에 들어오는 길을 프록시로 좁힌다
# (`k8s/templates/tenant_policies.py`)
LABEL_MCP_BACKEND = "daegam.devops.ai.kr/mcp-backend"
# ToolHive 오퍼레이터가 자기 파드에 다는 종류 라벨. MCPServer 프록시 파드는 `mcpserver` 다.
# 프록시 Deployment 의 selector(`labelsForMCPServer`)에 들어 있어 0.51.2·0.52.0·0.53.0 의 모든
# 프록시 파드가 단다. vMCP 는 `virtualmcpserver`, MCPRemoteProxy 는 `mcpremoteproxy` 다
LABEL_APP_NAME = "app.kubernetes.io/name"
# ToolHive 가 만든 파드의 CR 이름
LABEL_APP_INSTANCE = "app.kubernetes.io/instance"
APP_MCPSERVER = "mcpserver"
# 객체 판. 적용한 기대 렌더의 해시이고 인증 재적용이 실물과 맞댄다 (P32 8절)
ANNOTATION_DESIRED_HASH = "daegam.devops.ai.kr/desired-hash"
# 인증 재적용의 두 판(객체 판·소비자 판)의 길이
REV_LENGTH = 12


def daegam_labels(*, tenant: str, resource: str, id: str) -> dict[str, str]:
    return {
        LABEL_TENANT: tenant,
        LABEL_RESOURCE: resource,
        LABEL_ID: id,
        LABEL_MANAGED_BY: MANAGED_BY,
    }


def ensure_labels(obj: dict[str, Any], *, tenant: str, resource: str, id: str) -> dict[str, Any]:
    """obj.metadata.labels 에 대감 라벨을 보장한다(제자리 수정 후 같은 dict 반환)."""
    meta = obj.setdefault("metadata", {})
    labels = meta.get("labels") or {}
    labels.update(daegam_labels(tenant=tenant, resource=resource, id=id))
    meta["labels"] = labels
    return obj


def label_selector(labels: dict[str, str]) -> str:
    return ",".join(f"{k}={v}" for k, v in sorted(labels.items()))


def stable_hash(value: Any, length: int) -> str:
    """정렬한 JSON 의 sha256 앞 length 자. 같은 값이면 어느 레플리카에서나 같은 판이 나온다.
    허브 복제본의 원본 해시와 인증 재적용의 두 판이 함께 쓴다. 저장되는 지문(`versioned_hash`)이 이
    출력에 기대므로, 이것을 바꾸면 그 지문들의 판(`SPEC_DIGEST_VERSION`, 평가 사례의
    `DIGEST_VERSION`, 비서 제안의 `DIGEST_VERSION`)을 올린다."""
    payload = json.dumps(value, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:length]


# 저장되는 지문의 길이. sha256 전부다
STORED_DIGEST_CHARS = 64


def versioned_hash(value: Any, version: str) -> str:
    """자르지 않은 `stable_hash` 에 판을 붙인 값이다. 저장해 두고 나중에 견주는 지문(Agent 리비전의
    내용, 평가 사례와 세트, P45, 기다리는 비서 제안, P48)이 쓴다. 판은 짓는 규칙의 이름이라 규칙을
    바꾸면 올리고, 판이 다른 지문은 같은 내용이어도 같지 않다."""
    return f"{version}:{stable_hash(value, STORED_DIGEST_CHARS)}"


def content_digest(model: BaseModel, version: str, *, include: set[str] | None = None) -> str:
    """모델 내용의 판 붙은 지문. 기본값인 칸을 뺀 덤프로 짓는다. 그래서 뒤 판이 기본값을 가진 칸을
    더해도 지문이 같고, 모델의 기본값을 바꾸면 새로 지은 지문이 저장된 지문과 소리 없이 달라진다.
    이것을 쓰는 모델은 기본값을 바꾸지 않는다."""
    body = model.model_dump(mode="json", include=include, exclude_defaults=True)
    return versioned_hash(body, version)


def keyed_hash(key: str, value: str, length: int) -> str:
    """value 를 key 로 건 HMAC-SHA256 의 앞 length 자. 테넌트 개발자가 보는 자리(Advanced 편집의
    파드 템플릿)에 플랫폼 소유 값의 판을 실을 때 쓴다. 키 없는 해시와 달리 값이 어디서 왔는지
    사전으로 되짚을 수 없다. Agent 의 설정 지문이 인스턴스 토큰을 키로 쓴다 (P35 13절)."""
    digest = hmac.new(key.encode("utf-8"), value.encode("utf-8"), hashlib.sha256)
    return digest.hexdigest()[:length]


def stamp_desired_hash(obj: dict[str, Any]) -> dict[str, Any]:
    """obj 에 객체 판 어노테이션을 싣는다(제자리 수정 후 같은 dict 반환). 값은 그 어노테이션을 뺀
    obj 전체의 판이라 렌더가 한 글자라도 바뀌면 달라진다."""
    meta = obj.setdefault("metadata", {})
    annotations = meta.pop("annotations", None) or {}
    others = {k: v for k, v in annotations.items() if k != ANNOTATION_DESIRED_HASH}
    # 판은 다른 어노테이션만 되돌려 놓은 obj 에서 센다. 판 어노테이션을 싣는 것은 그 뒤다
    if others:
        meta["annotations"] = others
    digest = stable_hash(obj, REV_LENGTH)
    meta["annotations"] = {**others, ANNOTATION_DESIRED_HASH: digest}
    return obj
