"""Build runtime（构建运行环境）：官方可嵌入解释器＋锁定轮子，客户机不安装依赖。"""
from __future__ import annotations
import argparse
from email.parser import BytesParser
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from packaging.utils import canonicalize_name, parse_wheel_filename

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from voice_terminal.distribution import manifest, verify, digest, unpack, collect_vc_runtime
from voice_terminal.paths import atomic_json

PROBE = """
import importlib,json,sys
modules=['torch','torchaudio','funasr','funasr_onnx','kaldi_native_fbank','onnxruntime','sherpa_onnx','sounddevice','soundfile','webrtcvad','aec_audio_processing','psutil','fastapi','httpx','keyring']
for name in modules: importlib.import_module(name)
import torch,onnxruntime,sounddevice
value=torch.tensor([1.,2.],device='cpu').sum().item()
assert value==3.
print(json.dumps({'python':sys.version.split()[0],'torch':torch.__version__,'cpu_tensor':value,'onnx_providers':onnxruntime.get_available_providers(),'audio_devices':len(sounddevice.query_devices())}))
"""


def check_dependencies(directory):
    # 只读取候选目录，不能让构建机的已安装包掩盖候选环境缺少的依赖。
    from importlib.metadata import distributions
    from packaging.requirements import Requirement
    from packaging.utils import canonicalize_name
    installed = {canonicalize_name(item.metadata["Name"]): item for item in distributions(path=[str(directory)])}
    failures = []
    for package in installed.values():
        for raw in package.requires or []:
            requirement = Requirement(raw)
            if requirement.marker and not requirement.marker.evaluate({"extra": ""}):
                continue
            dependency = installed.get(canonicalize_name(requirement.name))
            if dependency is None or dependency.version not in requirement.specifier:
                failures.append(f"{package.metadata['Name']} 需要 {requirement}")
    if failures:
        raise RuntimeError("候选依赖不闭合：\n" + "\n".join(failures))

def run(*command, cwd=None, env=None):
    subprocess.run([str(v) for v in command], cwd=cwd, env=env, check=True)


def wheel_builder(cache: Path) -> Path:
    """锁定构建机工具；旧式 setup.py（依赖构建脚本）不能交给不断变化的隔离默认环境。"""
    tools = cache.parent / "build-tools"
    executable = tools / "Scripts/python.exe"
    if not executable.is_file():
        run(sys.executable, "-m", "venv", tools)
    run(executable, "-m", "pip", "install", "--disable-pip-version-check", "--timeout", "120",
        "pip==25.0.1", "setuptools==78.1.0", "wheel==0.45.1")
    return executable


def main():
    parser = argparse.ArgumentParser(description="构建可移动 Python（程序运行环境）组件")
    parser.add_argument("--profile", choices=("cu128", "cpu"), default="cu128", help="先验证现用组合，必要时改中央处理器组合")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--wheelhouse", type=Path, default=ROOT / ".runtime/package-build/wheels")
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--constraints", type=Path, default=ROOT / "config/runtime/python-312.constraints.txt", help="来自已验证业务环境的依赖版本约束")
    parser.add_argument('--vc-redist', type=Path, required=True, help='已授权 MSVC（微软构建工具）的 Redist（可再分发）版本下 x64（64 位）目录')
    args = parser.parse_args()
    if sys.version_info[:2] != (3,12) or os.name != "nt":
        raise RuntimeError("构建机需要 Windows x64 与 Python（程序运行环境）3.12。")
    output = args.output.resolve()
    if output.exists():
        raise FileExistsError("运行环境输出目录已经存在，请选择新的构建标识。")
    wheels = args.wheelhouse.resolve() / args.profile
    wheels.mkdir(parents=True, exist_ok=True)
    cache = ROOT / ".runtime/package-build/downloads"
    cache.mkdir(parents=True, exist_ok=True)
    archive = cache / "python-3.12.10-embed-amd64.zip"
    if not archive.is_file():
        if args.offline: raise FileNotFoundError("离线构建缺少官方可嵌入解释器压缩包。")
        urllib.request.urlretrieve("https://www.python.org/ftp/python/3.12.10/python-3.12.10-embed-amd64.zip", archive)
    if not args.offline:
        # 纯 Python（脚本语言）依赖允许在构建机生成 wheel（预编译依赖包），客户机仅消费轮子。
        producer = wheel_builder(cache)
        run(producer, "-m", "pip", "wheel", "--no-build-isolation", "--prefer-binary", "--timeout", "120", "--find-links", wheels, "--wheel-dir", wheels,
            "--extra-index-url", f"https://download.pytorch.org/whl/{args.profile}",
            "-c", args.constraints, "-r", ROOT / "apps/server/requirements-vad-onnx.txt", f"torch==2.11.0+{args.profile}", f"torchaudio==2.11.0+{args.profile}")
    lines, artifacts, versions = [], [], {}
    for wheel in sorted(wheels.glob("*.whl")):
        with zipfile.ZipFile(wheel) as source:
            metadata_paths = [name for name in source.namelist() if name.endswith(".dist-info/METADATA") and len(name.split("/")) == 2]
            if len(metadata_paths) != 1:
                raise ValueError(f"依赖包元数据数量异常：{wheel.name}")
            metadata = BytesParser().parsebytes(source.read(metadata_paths[0]))
        wheel_name, wheel_version, _, _ = parse_wheel_filename(wheel.name)
        if wheel_name != canonicalize_name(metadata['Name']) or str(wheel_version) != metadata['Version']:
            raise ValueError(f"依赖包名称或版本与内部元数据不符：{wheel.name}")
        fingerprint = digest(wheel)
        name = canonicalize_name(metadata['Name'])
        if name in versions:
            raise ValueError(f"轮子目录含同名依赖的多个候选：{name}；请为每次环境构建使用独立目录。")
        versions[name] = metadata['Version']
        lines.append(f"{metadata['Name']}=={metadata['Version']} --hash=sha256:{fingerprint}")
        artifacts.append({"file":wheel.name,"size":wheel.stat().st_size,"sha256":fingerprint})
    if not lines: raise RuntimeError("轮子仓库为空。")
    for name in ('torch', 'torchaudio'):
        if versions.get(name) != f'2.11.0+{args.profile}':
            raise ValueError(f"轮子仓库与所选计算组合不一致：{name}")
    lock = wheels / "wheels.lock.txt"
    lock.write_text("\n".join(lines)+"\n", encoding="utf-8")
    stage = output.with_name(output.name + ".staging")
    if stage.exists(): raise FileExistsError("存在未完成的暂存环境；保留诊断后选择新输出目录。")
    stage.mkdir(parents=True)
    unpack(archive, stage)
    (stage / "python312._pth").write_text("python312.zip\n.\nLib\nLib/site-packages\nimport site\n", encoding="utf-8")
    run(sys.executable,"-m","pip","install","--no-index","--find-links",wheels,"--require-hashes","--no-deps","--no-compile","--target",stage / "Lib/site-packages","-r",lock)
    # pip（依赖安装工具）生成的控制台脚本绑定构建解释器；发行入口统一显式导入模块。
    # 这些目录只容纳安装器生成的脚本，不属于依赖包内部代码。
    for name in ("bin", "Scripts"):
        generated = stage / "Lib/site-packages" / name
        if generated.is_dir():
            generated.resolve().relative_to(stage.resolve())
            shutil.rmtree(generated)
    check_dependencies(stage / "Lib/site-packages")
    vc_runtime = collect_vc_runtime(stage, args.vc_redist.resolve(strict=True))
    probe_env = {**os.environ,"CUDA_VISIBLE_DEVICES":"-1","PYTHONPYCACHEPREFIX":str(ROOT / ".runtime/package-build/bytecode"),"PYTHONNOUSERSITE":"1"}
    run(stage / "python.exe","-B","-X","utf8","-c",PROBE,env=probe_env,cwd=ROOT)
    # 移动后再次执行真实导入；不保留虚拟环境和开发机基础解释器路径。
    moved = output.with_name(output.name + " 中文搬迁验证")
    stage.rename(moved)
    run(moved / "python.exe","-B","-X","utf8","-c",PROBE,env=probe_env,cwd=output.parent)
    moved.rename(output)
    shutil.copyfile(lock, output / "wheels.lock.txt")
    atomic_json(output / "runtime.json",{"schema":1,"profile":args.profile,"python":"3.12.10","embedded_sha256":digest(archive),"wheels":artifacts,"vc_runtime":vc_runtime,"relocated_imports":True,"physical_gpu_absence_verified":False})
    record = manifest(output,kind="python",build="pending")
    record["build"] = hashlib.sha256(json.dumps(record["files"], sort_keys=True).encode()).hexdigest()[:16]
    atomic_json(output / "manifest.json",record)
    verify(output,record)
    print(f"运行环境构建与搬迁导入检查通过：{output}")

if __name__ == "__main__": main()
