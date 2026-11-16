"""WSDL 이 선언한 soap:header 의 값을 Backend API 가 가리키는 Secret 에서 받는다 (T60).

Backend API 의 `soap_headers_secret` 은 Secret 이름뿐이다. 배포는 그 Secret 의 `soap_headers`
키를 backend 가 하나면 `SPEC2OPENAPI_SOAP_HEADERS`, 여럿이면 `DAEGAM_BACKEND_<i>_SOAP_HEADERS` 로
렌더하고 이름을 배포 스냅샷에 적는다. 그 칸이 없는 backend 의 렌더와 스냅샷은 그 칸이 생기기 전과
같다.
"""

from __future__ import annotations

import pytest

from daegam.k8s.templates.mcpserver import (
    SINGLE_SOAP_HEADERS_ENV,
    SOAP_HEADERS_KEY,
    backend_env,
)
from daegam.services.mcp_inputs import backend_apis_from
from daegam.store.mcp_servers import BackendInputs, MCPServerInputs
from tests.helpers.backend_apis import upload_backend, uploaded_backend
from tests.helpers.consts import BP, DEFAULT_NS, GV
from tests.helpers.http import detail_code
from tests.helpers.mcp_servers import (
    clone_and_deploy,
    mcp_server_env,
    two_backend_server,
)

HEADERS_SECRET = "calc-soap-headers"
PETS_SECRET = "pets-soap-headers"
# 1.0.0 의 `BackendInputs` 칸. 그 판은 낯선 칸을 거절하므로(extra="forbid") 스냅샷의 backend 항목은
# 이 밖의 칸을 가질 수 없다
BACKEND_INPUTS_1_0_0 = frozenset({"id", "name", "endpoint", "auth", "credential_secret"})


def _cr(fake_clusters, name: str) -> dict:
    return fake_clusters.by_name["edge"].get(GV, "MCPServer", name, DEFAULT_NS)


def _secret_refs(cr: dict) -> dict[str, tuple[str, str]]:
    """env 이름마다 (Secret, 키). spec.secrets 와 mcp 컨테이너 env 의 secretKeyRef 를 함께 본다."""
    spec = cr["spec"]
    refs = {s["targetEnvName"]: (s["name"], s["key"]) for s in spec.get("secrets") or []}
    for c in spec["podTemplateSpec"]["spec"]["containers"]:
        for e in c.get("env") or []:
            ref = (e.get("valueFrom") or {}).get("secretKeyRef")
            if ref:
                refs[e["name"]] = (ref["name"], ref["key"])
    return refs


def _snapshot(mongo, server_id: str) -> dict:
    return mongo["mcp_servers"].find_one({"_id": server_id})["deployed_inputs"]


def test_the_backend_api_keeps_only_the_secret_name(client):
    """PATCH 로 넣고 null 로 지운다. 응답은 이름뿐이고, Secret 이름 규칙을 어기면 422 다."""
    _cid, bp, _ds, _ms = mcp_server_env(client, ds=False)
    assert client.get(f"{BP}/{bp['id']}").json()["soap_headers_secret"] is None
    r = client.patch(f"{BP}/{bp['id']}", json={"soap_headers_secret": f" {HEADERS_SECRET} "})
    assert r.status_code == 200 and r.json()["soap_headers_secret"] == HEADERS_SECRET
    r = client.patch(f"{BP}/{bp['id']}", json={"soap_headers_secret": "Not_Valid"})
    assert detail_code(r) == (422, "invalid_soap_headers_secret")
    assert client.get(f"{BP}/{bp['id']}").json()["soap_headers_secret"] == HEADERS_SECRET
    r = client.patch(f"{BP}/{bp['id']}", json={"soap_headers_secret": None})
    assert r.status_code == 200 and r.json()["soap_headers_secret"] is None


def test_the_upload_form_takes_the_secret_name(client):
    """업로드 폼의 `soap_headers_secret` 은 PATCH 와 같은 검사를 거쳐 새 프로필에 저장된다. 있는
    프로필은 보낸 칸만 바뀌고, 보내지 않으면 저장된 이름이 그대로다. 틀린 이름은 422 다."""
    created = uploaded_backend(client, soap_headers_secret=f" {HEADERS_SECRET} ")
    assert client.get(f"{BP}/{created['id']}").json()["soap_headers_secret"] == HEADERS_SECRET
    assert upload_backend(client, endpoint="http://calc.svc/x").status_code == 200
    assert client.get(f"{BP}/{created['id']}").json()["soap_headers_secret"] == HEADERS_SECRET
    assert upload_backend(client, soap_headers_secret=PETS_SECRET).status_code == 200
    assert client.get(f"{BP}/{created['id']}").json()["soap_headers_secret"] == PETS_SECRET
    # 빈 칸은 보내지 않은 것이다. 이름을 지우는 길은 PATCH 다
    assert (
        upload_backend(client, endpoint="http://calc.svc/y", soap_headers_secret="").status_code
        == 200
    )
    assert client.get(f"{BP}/{created['id']}").json()["soap_headers_secret"] == PETS_SECRET
    r = upload_backend(client, "other", soap_headers_secret="Not_Valid")
    assert detail_code(r) == (422, "invalid_soap_headers_secret")


def test_one_backend_takes_the_key_as_the_spec2openapi_env(client, fake_clusters, mongo):
    """backend 하나는 `SPEC2OPENAPI_SOAP_HEADERS` 이고 스냅샷에 Secret 이름이 남는다. 자격증명과
    같은 Secret 이어도 된다."""
    _cid, bp, _ds, ms = mcp_server_env(
        client, ds=False, backend_auth="basic", soap_headers_secret="calc-creds"
    )
    inst = clone_and_deploy(client, ms).json()
    refs = _secret_refs(_cr(fake_clusters, inst["k8s"]["name"]))
    assert refs[SINGLE_SOAP_HEADERS_ENV] == ("calc-creds", SOAP_HEADERS_KEY)
    assert refs["SPEC2OPENAPI_PASSWORD"] == ("calc-creds", "password")
    stored = _snapshot(mongo, inst["id"])
    assert stored["soap_headers_secrets"] == {bp["id"]: "calc-creds"}
    # 스냅샷의 Backend API 는 같은 이름을 돌려준다(재렌더가 그것으로 렌더한다)
    rebuilt = backend_apis_from(MCPServerInputs.model_validate(stored), "t")
    assert [b.soap_headers_secret for b in rebuilt] == ["calc-creds"]


def test_the_snapshot_backend_entries_stay_readable_by_older_platforms():
    """옛 판의 `BackendInputs` 는 낯선 칸을 거절한다. 그래서 이름은 backend 항목이 아니라 스냅샷
    위에 있고, 비어 있으면 저장하지 않는다."""
    inputs = MCPServerInputs(
        backends=[BackendInputs(id="b1", name="calc", credential_secret="c")],
        soap_headers_secrets={"b1": "s"},
    )
    dumped = inputs.model_dump(mode="json")
    assert set(dumped["backends"][0]) <= BACKEND_INPUTS_1_0_0
    assert dumped["soap_headers_secrets"] == {"b1": "s"}
    assert "soap_headers_secrets" not in MCPServerInputs(backends=[]).model_dump(mode="json")


@pytest.mark.parametrize(
    ("first", "second"),
    [(HEADERS_SECRET, PETS_SECRET), (HEADERS_SECRET, None)],
    ids=["both", "first-only"],
)
def test_each_backend_takes_only_its_own_key(client, fake_clusters, mongo, first, second):
    """여러 backend 는 자기 순번의 env 로 자기 Secret 을 받고, 칸이 없는 backend 에는 그 env 가
    없다. 한 backend 의 값이 다른 backend 의 봉투로 가지 않는다."""
    _cid, bp, _ds, ms = mcp_server_env(client, ds=False, soap_headers_secret=first)
    inst, pets = two_backend_server(client, ms, bp, pets_soap_headers_secret=second)
    refs = _secret_refs(_cr(fake_clusters, inst["k8s"]["name"]))
    soap = {name: ref for name, ref in refs.items() if name.endswith(SOAP_HEADERS_KEY.upper())}
    expected = {
        backend_env(i, SOAP_HEADERS_KEY): (secret, SOAP_HEADERS_KEY)
        for i, secret in enumerate((first, second))
        if secret
    }
    assert soap == expected
    by_id = {bp["id"]: first, pets["id"]: second}
    assert _snapshot(mongo, inst["id"])["soap_headers_secrets"] == {
        k: v for k, v in by_id.items() if v
    }
