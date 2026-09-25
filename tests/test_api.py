"""HTTP 端到端：状态码、错误响应结构、幂等头、冲突串行化。"""

import pytest
from fastapi.testclient import TestClient

from microvm.api import build_default_service


@pytest.fixture
def c():
    from microvm.api import create_app

    return TestClient(create_app(build_default_service()))


H_A = {"Authorization": "Bearer token-tenant-a"}
H_B = {"Authorization": "Bearer token-tenant-b"}
H_ADMIN = {"Authorization": "Bearer token-admin"}


def test_full_lifecycle_over_http(c):
    print("\n[HTTP] 输入: POST /v1/instances (1,128)")
    r = c.post("/v1/instances", headers=H_A, json={"name": "web", "cpus": 1, "memory_mb": 128})
    assert r.status_code == 201
    vm = r.json()
    iid = vm["instance_id"]
    print(f"[HTTP] 判定: 201 state={vm['state']} id={iid}")

    r = c.post(f"/v1/instances/{iid}/start", headers=H_A)
    print(f"[HTTP] start -> {r.status_code} state={r.json()['state']}")
    assert r.status_code == 200 and r.json()["state"] == "running"

    r = c.post(f"/v1/instances/{iid}/start", headers=H_A)
    print(f"[HTTP] 重复 start -> {r.status_code} code={r.json()['error']['code']}")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "invalid_state_transition"

    r = c.post(f"/v1/instances/{iid}/restart", headers=H_A)
    assert r.status_code == 200 and r.json()["state"] == "running"
    r = c.post(f"/v1/instances/{iid}/stop", headers=H_A)
    assert r.json()["state"] == "stopped"
    r = c.post(f"/v1/instances/{iid}/delete", headers=H_A)
    assert r.status_code == 200 and r.json()["state"] == "deleted"
    r = c.get(f"/v1/instances/{iid}", headers=H_A)
    print(f"[HTTP] 删除后 GET -> {r.status_code} code={r.json()['error']['code']}")
    assert r.status_code == 404


def test_idempotency_header_over_http(c):
    h = {**H_A, "X-Idempotency-Key": "http-once"}
    print("\n[HTTP] 输入: 两次相同 POST 携带相同 X-Idempotency-Key")
    r1 = c.post("/v1/instances", headers=h, json={"name": "once", "cpus": 1, "memory_mb": 64})
    r2 = c.post("/v1/instances", headers=h, json={"name": "once", "cpus": 1, "memory_mb": 64})
    assert r1.status_code == 201 and r2.status_code == 201
    print(f"[HTTP] 判定: 两个响应同一实例={r1.json()['instance_id'] == r2.json()['instance_id']}")
    assert r1.json()["instance_id"] == r2.json()["instance_id"]

    listing = c.get("/v1/instances", headers=H_A).json()["instances"]
    assert len(listing) == 1


def test_capacity_and_authz_over_http(c):
    print("\n[HTTP] 输入: 占满 4C 后再申请 1C")
    r = c.post("/v1/instances", headers=H_A, json={"name": "big", "cpus": 4, "memory_mb": 1000})
    assert r.status_code == 201
    r = c.post("/v1/instances", headers=H_B, json={"name": "x", "cpus": 1, "memory_mb": 100})
    print(f"[HTTP] 判定: 容量耗尽 http={r.status_code} code={r.json()['error']['code']}")
    assert r.status_code == 507
    assert r.json()["error"]["code"] == "capacity_exhausted"

    iid = c.get("/v1/instances", headers=H_A).json()["instances"][0]["instance_id"]
    r = c.get(f"/v1/instances/{iid}", headers=H_B)
    print(f"[HTTP] 判定: 跨租户 GET http={r.status_code}（不泄露存在性）")
    assert r.status_code == 404

    r = c.get("/v1/admin/capacity", headers=H_A)
    print(f"[HTTP] 判定: 普通租户访问管理接口 http={r.status_code}")
    assert r.status_code == 403
    r = c.get("/v1/admin/capacity", headers=H_ADMIN)
    assert r.status_code == 200
