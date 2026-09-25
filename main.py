"""本地 microVM 生命周期控制面入口。"""

import uvicorn

from app.api import create_app
from app.models import Quota
from app.quota import QuotaManager
from app.runtime import LocalProcessRuntime
from app.service import InstanceService

QUOTAS = {
    "tenant-a": Quota(max_instances=4, max_vcpu=8, max_memory_mb=4096),
    "tenant-b": Quota(max_instances=2, max_vcpu=4, max_memory_mb=2048),
    "admin": Quota(max_instances=0, max_vcpu=0, max_memory_mb=0),
}

service = InstanceService(QuotaManager(QUOTAS), LocalProcessRuntime())
app = create_app(service)

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8080)
