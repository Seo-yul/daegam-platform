"""남이 준 YAML 을 여는 유일한 문. alias 폭탄을 문에서 막는다.

`yaml.safe_load` 는 태그 실행은 막지만 alias 는 막지 않는다. PyYAML 은 alias 를 같은 객체로
공유하므로 파싱은 싸고, 그 뒤에 트리로 펼치는 자리(`json.dumps`, FastMCP 라운드트립,
`key_paths`)에서 터진다.
실측으로 307B 가 1.1MB(3,591배), 697B 가 4.3GB 였다. 파드가 OOMKill 된다.

alias 의 개수를 세는 것은 답이 아니다. 깊이 6 에 alias 60 개면 펼친 크기가 백만 노드다. 대신
펼친 크기를 센다. 노드마다 "펼치면 몇 노드인가"를 기억해 두고, alias 를 만날 때마다 그 무게를
누계에 더한다. 누계가 상한을 넘으면 그 자리에서 `YAMLTooLarge` 다. 문서를 끝까지 읽지도 않는다.

PyYAML 의 composer 는 중첩 하나마다 재귀하므로 `[[[[…]]]]` 처럼 수백 겹 중첩된 짧은 글이
RecursionError 를 낸다. YAMLError 가 아니라서 호출자의 `except yaml.YAMLError` 를 빠져나가 500 이
된다. 이것도 `YAMLTooLarge` 로 바꾼다.

호출자는 그것을 자기 오류 코드(422 `invalid_yaml`·`unsupported`)로 바꾼다. `yaml.YAMLError` 의
자손이라 기존 `except yaml.YAMLError` 가 그대로 받는다.

우리가 쓴 YAML 은 이 문을 지나지 않아도 된다. 남이 준 것만 지난다. 등록한 kubeconfig 는 Secret 에서
다시 열 때도 남이 준 것이다(`k8s/kubeconfig.py`).
"""

from __future__ import annotations

from typing import Any

import yaml

# 펼친 노드 수 상한. 큰 OpenAPI 문서가 수만 노드다. 백만이면 JSON 으로 수십 MB 안이고,
# 깊이 6 짜리 폭탄(10^6)이 걸린다
MAX_EXPANDED_NODES = 1_000_000


class YAMLTooLarge(yaml.YAMLError):
    """펼친 크기가 상한을 넘었다. 문서를 열지 않고 끊는다."""


class _WeighingLoader(yaml.SafeLoader):
    def __init__(self, stream: Any):
        super().__init__(stream)
        self._expanded = 0
        self._weights: dict[int, int] = {}

    def _charge(self, n: int) -> None:
        self._expanded += n
        if self._expanded > MAX_EXPANDED_NODES:
            raise YAMLTooLarge(
                f"document expands to more than {MAX_EXPANDED_NODES} nodes through aliases"
            )

    def compose_node(self, parent: Any, index: Any) -> Any:
        if self.check_event(yaml.AliasEvent):
            # 기존 노드를 돌려준다. 펼치면 그 노드의 무게만큼이다
            node = super().compose_node(parent, index)
            self._charge(self._weights.get(id(node), 1))
            return node
        node = super().compose_node(parent, index)
        weight = 1
        if isinstance(node, yaml.SequenceNode):
            weight += sum(self._weights.get(id(c), 1) for c in node.value)
        elif isinstance(node, yaml.MappingNode):
            weight += sum(
                self._weights.get(id(k), 1) + self._weights.get(id(v), 1) for k, v in node.value
            )
        self._weights[id(node)] = weight
        self._charge(1)
        return node


def safe_load(text: str | bytes) -> Any:
    """`yaml.safe_load` 와 같되 펼친 크기와 중첩 깊이에 상한이 있다. 넘으면 YAMLTooLarge(YAMLError
    자손)."""
    try:
        return yaml.load(text, Loader=_WeighingLoader)  # noqa: S506 - SafeLoader 자손이다
    except RecursionError as e:
        raise YAMLTooLarge("document nests too deeply to read") from e
