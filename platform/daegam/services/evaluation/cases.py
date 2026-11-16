"""사례와 세트의 지문 (P45 7절).

사례의 지문은 내용(`CaseContent`)에서 기본값인 칸을 뺀 정렬 JSON 의 해시다. 뒤 판이 기본값을 가진
칸을 더해도 지문이 바뀌지 않는다. 세트의 지문은 실행에 닿는 설정(채점 모델, 반복 수)과 사례마다의
(id, 지문)으로 짓는다. 그래서 사례 하나만 바뀌어도 앞선 실행은 배포 게이트에서 낡고, 바뀌지 않은
사례는 기준선(11절)이 그대로 맞는다. 세트 이름은 실행에 닿지 않아 들지 않는다.
"""

from __future__ import annotations

from collections.abc import Iterable

from daegam.k8s.labels import content_digest, versioned_hash
from daegam.store.evaluation import CaseContent, EvaluationCase, EvaluationSet

# 지문을 짓는 규칙의 판. 규칙을 바꾸면 올린다
DIGEST_VERSION = "v1"


def case_digest(content: CaseContent) -> str:
    """사례 내용의 지문. 그래서 사례 모델(`CaseContent` 와 그 안의 모델)의 기본값은 바꾸지
    않는다."""
    return content_digest(content, DIGEST_VERSION)


def set_digest(item: EvaluationSet, cases: Iterable[tuple[str, str]]) -> str:
    """세트의 지문. cases 는 사례마다의 (id, 지문)이다."""
    body = {
        "grader": item.grader.model_dump() if item.grader is not None else None,
        "repeats": item.repeats,
        "cases": sorted([case_id, digest] for case_id, digest in cases),
    }
    return versioned_hash(body, DIGEST_VERSION)


def digests_of(cases: Iterable[EvaluationCase]) -> list[tuple[str, str]]:
    return [(c.id, c.digest) for c in cases]
