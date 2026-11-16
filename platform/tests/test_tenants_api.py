from tests.helpers.clusters import register_cluster
from tests.helpers.http import detail_code


def test_patch_validation(client):
    cid = register_cluster(client)
    assert client.patch("/tenants/acme/settings", json={}).json()["detail"]["code"] == "empty_patch"
    r = client.patch("/tenants/acme/settings", json={"default_cluster_id": "nope"})
    assert detail_code(r) == (422, "unknown_cluster")
    r = client.patch("/tenants/acme/settings", json={"allowed_cluster_ids": ["nope"]})
    assert detail_code(r) == (422, "unknown_cluster")
    r = client.patch(
        "/tenants/acme/settings",
        json={"default_bindings": [{"data_service_id": "d", "env_prefix": "bad-prefix"}]},
    )
    assert r.status_code == 422
    r = client.patch(
        "/tenants/acme/settings", json={"default_cluster_id": cid, "allowed_cluster_ids": []}
    )
    assert detail_code(r) == (422, "invalid_value")
    assert client.get("/tenants/bad tenant!/settings").status_code == 400
