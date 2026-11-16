"""차트 시험(test_chart_*.py)이 대감 차트를 helm 으로 렌더하는 도우미 (P85 12절).

helm 이 없으면 건너뛰지 않고 실패한다. 셸 lint 가 shellcheck 없이 실패하는 것과 같다.
렌더는 클러스터에 닿지 않는다. KUBECONFIG 를 비워 helm 이 어느 클러스터도 부르지 못하게 한다.
"""

from __future__ import annotations

import functools
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.helpers.consts import PLATFORM_NS, REPO

CHART = REPO / "charts" / "daegam"
# 세 Secret 을 existingSecret 으로 적은 값. 클러스터 없는 렌더는 이것이 있어야 한다 (P85 5절)
OFFLINE = CHART / "ci" / "offline-values.yaml"


def helm(*args: str) -> subprocess.CompletedProcess[str]:
    """helm 을 클러스터 없이 부른다. 끝 코드는 부르는 쪽이 본다."""
    exe = shutil.which("helm")
    if exe is None:
        pytest.fail(
            "helm 이 없다. 차트 시험은 helm 으로 렌더한다 "
            "(brew install helm, CI 는 .github/workflows/platform.yml 이 고정한 판을 깐다)"
        )
    env = {k: v for k, v in os.environ.items() if not k.startswith(("HELM_", "KUBE"))}
    env["KUBECONFIG"] = os.devnull
    return subprocess.run([exe, *args], env=env, capture_output=True, text=True, check=False)


@functools.cache
def _template(
    chart: Path,
    values: tuple[Path, ...],
    sets: tuple[tuple[str, str], ...],
    namespace: str,
    flags: tuple[str, ...],
) -> subprocess.CompletedProcess[str]:
    args = ["-n", namespace, "-f", str(OFFLINE), *flags]
    for path in values:
        args += ["-f", str(path)]
    for key, value in sets:
        args += ["--set", f"{key}={value}"]
    return helm("template", "daegam", str(chart), *args)


def _sets(sets: dict[str, Any] | None) -> tuple[tuple[str, str], ...]:
    def text(value: Any) -> str:
        return str(value).lower() if isinstance(value, bool) else str(value)

    return tuple(sorted((key, text(value)) for key, value in (sets or {}).items()))


def render(
    *values: Path,
    sets: dict[str, Any] | None = None,
    namespace: str = PLATFORM_NS,
    chart: Path = CHART,
) -> list[dict[str, Any]]:
    """offline-values.yaml 위에 values 파일과 sets(`--set` 의 키와 값)를 얹은 렌더의 객체들. 렌더가
    실패하면 시험이 실패한다. chart 는 chart_copy 가 만든 사본을 줄 때만 쓴다."""
    out = _template(chart, values, _sets(sets), namespace, ())
    assert out.returncode == 0, out.stderr
    return [d for d in yaml.safe_load_all(out.stdout) if d]


def render_template(
    name: str, *values: Path, sets: dict[str, Any] | None = None
) -> list[dict[str, Any]]:
    """offline-values.yaml 렌더에서 templates/<name> 이 낸 객체만. helm template --show-only 와
    같다."""
    out = _template(CHART, values, _sets(sets), PLATFORM_NS, ("--show-only", f"templates/{name}"))
    assert out.returncode == 0, out.stderr
    return [d for d in yaml.safe_load_all(out.stdout) if d]


def render_error(
    *values: Path,
    sets: dict[str, Any] | None = None,
    namespace: str = PLATFORM_NS,
    chart: Path = CHART,
    flags: tuple[str, ...] = (),
) -> str:
    """멈춰야 하는 렌더의 오류 글. 렌더가 성공하면 시험이 실패한다. flags 는 helm template 의
    플래그다(`--skip-schema-validation` 이면 스키마 뒤의 템플릿 받침을 본다)."""
    out = _template(chart, values, _sets(sets), namespace, flags)
    assert out.returncode != 0, "렌더가 멈추지 않았다"
    return out.stderr


def chart_copy(tmp_path: Path, template: str) -> Path:
    """시험만의 정의 template 을 더한 차트 사본. `_aa_` 로 시작하는 파일의 정의가 차트의 같은 이름
    정의를 이긴다(Helm 이 템플릿 파일을 읽는 순서다. `zz_` 로는 이기지 못한다)."""
    copy = tmp_path / "daegam"
    shutil.copytree(CHART, copy)
    (copy / "templates" / "_aa_test.tpl").write_text(template, encoding="utf-8")
    return copy


def live_cluster(objects: dict[tuple[str, str], dict[str, Any]]) -> str:
    """클러스터에 닿은 렌더처럼 보이게 하는 시험 정의. daegam.online 을 참으로, daegam.live 가
    objects((kind, name) 마다의 산 객체)를 돌려주게 덮는다. 없는 객체는 lookup 처럼 {} 다."""
    fixtures = json.dumps({f"{kind}/{name}": obj for (kind, name), obj in objects.items()})
    assert "`" not in fixtures
    return (
        '{{- define "daegam.online" -}}true{{- end }}\n'
        '{{- define "daegam.live" -}}\n'
        f"{{{{- $f := `{fixtures}` | fromJson -}}}}\n"
        '{{- toJson (get $f (printf "%s/%s" (index . 1) (index . 2)) | default dict) -}}\n'
        "{{- end }}\n"
    )


def values_file(tmp_path: Path, values: dict[str, Any]) -> Path:
    """values 를 담은 values 파일. --set 으로 적기 어려운 목록과 객체를 넘길 때 쓴다."""
    path = tmp_path / "values.yaml"
    path.write_text(yaml.safe_dump(values), encoding="utf-8")
    return path


def named(docs: list[dict[str, Any]], kind: str, name: str) -> dict[str, Any]:
    return next(d for d in docs if d["kind"] == kind and d["metadata"]["name"] == name)


def config_data(docs: list[dict[str, Any]]) -> dict[str, str]:
    return named(docs, "ConfigMap", "daegam-platform-config")["data"]


def pod_specs(docs: list[dict[str, Any]]) -> list[tuple[str, dict[str, Any]]]:
    return [
        (f"{d['kind']}/{d['metadata']['name']}", d["spec"]["template"]["spec"])
        for d in docs
        if d["kind"] in ("Deployment", "StatefulSet")
    ]


def containers(spec: dict[str, Any]) -> list[dict[str, Any]]:
    return spec.get("initContainers", []) + spec["containers"]


def pod_template(docs: list[dict[str, Any]], name: str) -> dict[str, Any]:
    return named(docs, "Deployment", name)["spec"]["template"]
