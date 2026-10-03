"""一次性硬件探测：重型运行库随探测进程退出，不进入主服务。"""
import json
from .compute import _backend_hardware_status

if __name__ == "__main__":
    print(json.dumps(_backend_hardware_status(), ensure_ascii=False))
