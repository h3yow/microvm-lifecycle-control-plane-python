"""越权访问与管理级操作：明确区分并拒绝，且不泄露其他租户实例信息。"""

import pytest

from microvm.errors import Forbidden, InstanceNotFound, Unauthorized
from microvm.models import InstanceSpec


def test_tenant_cannot_access_other_tenants_instance(service, tenant_a, tenant_b):
    vm = service.create_instance(tenant_a, InstanceSpec("secret", 1, 128), None)
    print(f"\n[鉴权-越权读] 请求输入: tenant-b 尝试 GET tenant-a 的 id={vm.instance_id}")

    with pytest.raises(InstanceNotFound) as exc:
        service.get_instance(tenant_b, vm.instance_id)
    print(f"[鉴权-越权读] 判定: code={exc.value.code}（与“不存在”不可区分，不泄露归属）")
    assert exc.value.code == "instance_not_found"


def test_tenant_cannot_mutate_other_tenants_instance(service, tenant_a, tenant_b):
    vm = service.create_instance(tenant_a, InstanceSpec("secret", 1, 128), None)
    print(f"\n[鉴权-越权操作] 请求输入: tenant-b 对 tenant-a 的 id={vm.instance_id} "
          f"执行 start/stop/delete")

    for op in ("start", "stop", "delete"):
        with pytest.raises(InstanceNotFound) as exc:
            getattr(service, op)(tenant_b, vm.instance_id, None)
        print(f"[鉴权-越权操作] {op}: 拒绝 code={exc.value.code}")
        assert exc.value.code == "instance_not_found"

    # tenant-a 的实例完全未受影响，仍是 STOPPED
    untouched = service.get_instance(tenant_a, vm.instance_id)
    assert untouched.state.value == "stopped"
    print("[鉴权-越权操作] 判定: tenant-a 实例未受任何影响")


def test_list_is_tenant_scoped(service, tenant_a, tenant_b):
    print("\n[鉴权-列表隔离] 请求输入: tenant-a 建2个、tenant-b 建1个，各自列举")
    service.create_instance(tenant_a, InstanceSpec("a1", 1, 64), None)
    service.create_instance(tenant_a, InstanceSpec("a2", 1, 64), None)
    service.create_instance(tenant_b, InstanceSpec("b1", 1, 64), None)

    a_ids = {i.tenant_id for i in service.list_instances(tenant_a)}
    b_list = service.list_instances(tenant_b)
    print(f"[鉴权-列表隔离] 判定: tenant-a 只见自身2条={a_ids == {'tenant-a'}}，"
          f"tenant-b 只见自身1条={len(b_list) == 1}")
    assert a_ids == {"tenant-a"}
    assert len(b_list) == 1
    assert b_list[0].tenant_id == "tenant-b"


def test_admin_capacity_requires_admin_role(service, tenant_a, admin):
    print("\n[鉴权-管理操作] 请求输入: 普通租户访问 /admin/capacity")
    with pytest.raises(Forbidden) as exc:
        service.admin_capacity(tenant_a)
    print(f"[鉴权-管理操作] 判定: 普通租户被拒 code={exc.value.code}")
    assert exc.value.code == "forbidden"

    cap = service.admin_capacity(admin)
    print(f"[鉴权-管理操作] 判定: 管理员允许，容量视图={cap}")
    assert cap["total_cpus"] == 4


def test_admin_can_cross_tenant_operate(service, tenant_a, admin):
    vm = service.create_instance(tenant_a, InstanceSpec("admins-eyes", 1, 128), None)
    print(f"\n[鉴权-管理员跨租户] 请求输入: admin 操作 tenant-a 的 id={vm.instance_id}")
    seen = service.get_instance(admin, vm.instance_id)
    started = service.start(admin, vm.instance_id, None)
    print(f"[鉴权-管理员跨租户] 判定: 管理员可读({seen.instance_id == vm.instance_id})"
          f"且可 start -> {started.state.value}")
    assert started.state.value == "running"
    service.stop(admin, vm.instance_id, None)
    service.delete(admin, vm.instance_id, None)


def test_missing_or_bad_credentials_rejected_at_api(service):
    print("\n[鉴权-凭据] 通过 HTTP 层验证: 无凭据401、坏凭据401")
    from fastapi.testclient import TestClient
    from microvm.api import create_app

    client = TestClient(create_app(service))
    r1 = client.get("/v1/instances")
    r2 = client.get("/v1/instances", headers={"Authorization": "Bearer nope"})
    print(f"[鉴权-凭据] 判定: 无凭据 http={r1.status_code} code={r1.json()['error']['code']}；"
          f"坏凭据 http={r2.status_code} code={r2.json()['error']['code']}")
    assert r1.status_code == 401
    assert r1.json()["error"]["code"] == "unauthorized"
    assert r2.status_code == 401
    assert r2.json()["error"]["code"] == "unauthorized"
