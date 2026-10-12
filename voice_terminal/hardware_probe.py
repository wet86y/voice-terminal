"""一次性硬件探测：重型运行库随探测进程退出，不进入主服务。"""
import json
import argparse
from pathlib import Path
from .compute import _backend_hardware_status, onnx_model_cuda_probe

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", choices=("smart_turn", "campplus"))
    parser.add_argument("--model-path", type=Path)
    args = parser.parse_args()
    if args.model and not args.model_path:
        parser.error("模型检查需要 --model-path（模型文件位置）")
    result = onnx_model_cuda_probe(args.model, args.model_path) if args.model else _backend_hardware_status()
    print(json.dumps(result, ensure_ascii=False))
