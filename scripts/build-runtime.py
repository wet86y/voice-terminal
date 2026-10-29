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

ROOT = next(p for p in Path(__file__).resolve().parents if (p / 'pyproject.toml').is_file())
sys.path.insert(0, str(ROOT))
from voice_terminal.distribution import manifest, verify, digest, unpack, collect_vc_runtime
from voice_terminal.paths import atomic_json
from voice_terminal.local_files import remove_tree
from voice_terminal.dependencies import dependency_errors, ONNX_GPU_VERSION
from voice_terminal.download_sources import download, pip_run

PROBE = """
import importlib,json,sys
modules=['torch','torchaudio','funasr','funasr_onnx','kaldi_native_fbank','onnxruntime','sherpa_onnx','sounddevice','soundfile','webrtcvad','aec_audio_processing','psutil','fastapi','httpx','keyring']
for name in modules: importlib.import_module(name)
import torch,onnxruntime,sounddevice
value=torch.tensor([1.,2.],device='cpu').sum().item()
assert value==3.
print(json.dumps({'python':sys.version.split()[0],'torch':torch.__version__,'cpu_tensor':value,'onnx_providers':onnxruntime.get_available_providers(),'audio_devices':len(sounddevice.query_devices())}))
"""

CUDA_PROBE = """
import json,torch
assert torch.cuda.is_available(), 'CUDA（图形处理器加速）后端不可用'
value=torch.ones((64,64),device='cuda')
result=(value @ value).sum().item()
torch.cuda.synchronize()
assert result==262144., result
print('VOICE_CUDA_RESULT='+json.dumps({'verified':True,'torch':torch.__version__,'cuda':torch.version.cuda,
    'device':torch.cuda.get_device_name(0),'matrix_sum':result}))
"""  # CUDA_PROBE（真实图形处理器矩阵计算检查），包含结果回传与同步。

ONNX_CUDA_PROBE = """
import json,tempfile
from pathlib import Path
import numpy as np,onnx,onnxruntime as ort
from onnx import helper,TensorProto
assert 'CUDAExecutionProvider' in ort.get_available_providers(), 'ONNX（模型格式）显卡执行后端缺失'
ort.preload_dlls()
with tempfile.TemporaryDirectory(prefix='voice-onnx-cuda-') as folder:
    graph=helper.make_graph([helper.make_node('MatMul',['a','b'],['c'])], 'cuda-check',
        [helper.make_tensor_value_info(name,TensorProto.FLOAT,[64,64]) for name in ['a','b']],
        [helper.make_tensor_value_info('c',TensorProto.FLOAT,[64,64])])
    model=helper.make_model(graph,opset_imports=[helper.make_opsetid('',17)],ir_version=10)
    options=ort.SessionOptions()
    options.enable_profiling=True
    options.profile_file_prefix=str(Path(folder)/'profile')
    session=ort.InferenceSession(model.SerializeToString(),sess_options=options,
        providers=['CUDAExecutionProvider','CPUExecutionProvider'])
    session.disable_fallback()
    assert 'CUDAExecutionProvider' in session.get_providers(), 'CUDA（显卡）后端初始化失败'
    value=np.ones((64,64),dtype=np.float32)
    result=float(session.run(None,{'a':value,'b':value})[0].sum())
    trace=json.loads(Path(session.end_profiling()).read_text())
    kernels=[item for item in trace if item.get('args',{}).get('provider')=='CUDAExecutionProvider'
        and item.get('args',{}).get('op_name')=='MatMul']
    assert result==262144. and kernels, '没有实际在 CUDA（显卡）执行矩阵计算'
    print('VOICE_ONNX_CUDA_RESULT='+json.dumps({'verified':True,'version':ort.__version__,
        'providers':session.get_providers(),'matrix_sum':result,'cuda_matmul_calls':len(kernels)}))
"""  # ONNX_CUDA_PROBE（模型推理运行时的显卡计算及内核追踪验证）。


def verify_cuda(executable: Path, *, env: dict, cwd: Path) -> dict:
    """verify_cuda（验证加速）：失败时停止构建，不生成错误的就绪清单。"""
    result = subprocess.run([str(executable), "-B", "-X", "utf8", "-c", CUDA_PROBE],
                            env=env, cwd=cwd, capture_output=True, text=True, encoding="utf-8", timeout=180)
    lines = [line.removeprefix("VOICE_CUDA_RESULT=") for line in result.stdout.splitlines()
             if line.startswith("VOICE_CUDA_RESULT=")]
    if result.returncode != 0 or not lines:
        raise RuntimeError("CUDA（图形处理器加速）实际计算验证失败：" + (result.stderr[-3000:] or result.stdout[-3000:]))
    value = json.loads(lines[-1])
    if value.get("verified") is not True or value.get("matrix_sum") != 262144.0:
        raise RuntimeError("CUDA（图形处理器加速）计算结果无效")
    return value


def verify_onnx_cuda(executable: Path, *, env: dict, cwd: Path) -> dict:
    """verify_onnx_cuda（验证模型格式显卡后端）：同时校验输出与实际内核归属。"""
    result = subprocess.run([str(executable), "-B", "-X", "utf8", "-c", ONNX_CUDA_PROBE],
                            env=env, cwd=cwd, capture_output=True, text=True, encoding="utf-8", timeout=180)
    lines = [line.removeprefix("VOICE_ONNX_CUDA_RESULT=") for line in result.stdout.splitlines()
             if line.startswith("VOICE_ONNX_CUDA_RESULT=")]
    if result.returncode != 0 or not lines:
        raise RuntimeError("ONNX Runtime（模型推理运行时）实际显卡计算验证失败：" + (result.stderr[-3000:] or result.stdout[-3000:]))
    value = json.loads(lines[-1])
    if value.get("verified") is not True or value.get("matrix_sum") != 262144.0 or value.get("cuda_matmul_calls", 0) < 1:
        raise RuntimeError("ONNX Runtime（模型推理运行时）显卡计算或内核追踪结果无效")
    return value


def check_dependencies(directory):
    # 只读取候选目录，不能让构建机的已安装包掩盖候选环境缺少的依赖。
    failures = dependency_errors(directory)
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
    pip_run([executable, "-m", "pip", "install", "--disable-pip-version-check", "--timeout", "20",
             "pip==25.0.1", "setuptools==78.1.0", "wheel==0.45.1"])
    return executable


def main():
    os.environ['PIP_CACHE_DIR'] = str(ROOT / '.local/cache/pip')  # PIP_CACHE_DIR（依赖安装缓存）。
    parser = argparse.ArgumentParser(description="构建可移动 Python（程序运行环境）组件")
    parser.add_argument("--profile", choices=("cu128", "cpu"), default="cu128", help="先验证现用组合，必要时改中央处理器组合")
    parser.add_argument("--onnx-profile", choices=("cpu", "cuda"), help="ONNX（模型格式）计算组合：加速张量组合默认同时包含显卡运行库")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--wheelhouse", type=Path, default=ROOT / ".local/build/runtime/wheels")
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--verify-cuda", action="store_true", help="在本机显卡上验证搬迁后实际 CUDA（图形处理器加速）计算；需要 cu128（加速依赖组合）")
    parser.add_argument("--constraints", type=Path, default=ROOT / "config/runtime/python-312.constraints.txt", help="来自已验证业务环境的依赖版本约束")
    parser.add_argument('--vc-redist', type=Path, required=True, help='已授权 MSVC（微软构建工具）的 Redist（可再分发）版本下 x64（64 位）目录')
    args = parser.parse_args()
    onnx_profile = args.onnx_profile or ("cuda" if args.profile == "cu128" else "cpu")
    if onnx_profile == "cuda" and args.profile != "cu128":
        parser.error("ONNX（模型格式）显卡组合需要 cu128（包含显卡动态库的张量组合）")
    if args.verify_cuda and args.profile != "cu128":
        parser.error("--verify-cuda（实际加速验证）只能用于 cu128（加速依赖组合）")
    if sys.version_info[:2] != (3,12) or os.name != "nt":
        raise RuntimeError("构建机需要 Windows x64 与 Python（程序运行环境）3.12。")
    output = args.output.resolve()
    if not output.is_relative_to(ROOT / ".local/tmp"):
        raise ValueError("新运行组件必须在项目临时区构建")
    if output.exists():
        raise FileExistsError("运行环境输出目录已经存在，请先检查并回收旧候选暂存。")
    wheels = args.wheelhouse.resolve() / (args.profile + "-onnx-cuda" if onnx_profile == "cuda" else args.profile)
    wheels.mkdir(parents=True, exist_ok=True)
    runtime_lock = json.loads((ROOT / 'config/runtime/voice-runtime.lock.json').read_text('utf-8'))
    for bundled_row in runtime_lock['wheels']:
        if bundled_row.get('provenance') != 'verified-soundtouch-source-build':
            continue
        internal = ROOT / '.local/build/runtime-lock' / bundled_row['file']
        if not internal.is_file() or digest(internal) != bundled_row['sha256']:
            raise ValueError('缺少锁定的语音处理预编译包，请先运行 scripts/build-speech-dsp.py。')
        shutil.copy2(internal, wheels / bundled_row['file'])
    cache = ROOT / ".local/build/runtime/downloads"
    cache.mkdir(parents=True, exist_ok=True)
    archive = cache / "python-3.12.10-embed-amd64.zip"
    python_record = json.loads((ROOT / "config/runtime/voice-runtime.lock.json").read_text("utf-8"))["python"]
    if not archive.is_file() or digest(archive) != python_record["sha256"]:
        if args.offline: raise FileNotFoundError("离线构建缺少官方可嵌入解释器压缩包。")
        download(python_record["url"], archive, sha256=python_record["sha256"], size=python_record["size"])
    if not args.offline:
        # 纯 Python（脚本语言）依赖允许在构建机生成 wheel（预编译依赖包），客户机仅消费轮子。
        producer = wheel_builder(cache)
        pip_run([producer, "-m", "pip", "wheel", "--no-build-isolation", "--prefer-binary", "--timeout", "20", "--find-links", wheels, "--wheel-dir", wheels,
                 "-c", args.constraints, "-r", ROOT / "apps/server/requirements-vad-onnx.txt", f"torch==2.11.0+{args.profile}", f"torchaudio==2.11.0+{args.profile}"], profile=args.profile)
        if onnx_profile == "cuda":
            pip_run([producer, "-m", "pip", "wheel", "--only-binary=:all:", "--no-deps", "--timeout", "20",
                     "--find-links", wheels, "--wheel-dir", wheels, f"onnxruntime-gpu=={ONNX_GPU_VERSION}"])
    lines, artifacts, versions = [], [], {}
    for wheel in sorted(wheels.glob("*.whl")):
        with zipfile.ZipFile(wheel) as source:
            metadata_paths = [name for name in source.namelist() if name.endswith(".dist-info/METADATA") and len(name.split("/")) == 2]
            if len(metadata_paths) != 1:
                raise ValueError(f"依赖包元数据数量异常：{wheel.name}")
            metadata = BytesParser().parsebytes(source.read(metadata_paths[0]))
        wheel_name, wheel_version, _, _ = parse_wheel_filename(wheel.name)
        # 依赖解析可能缓存两种发行包，但它们共用模块目录，只将选中的一个写入安装锁。
        if wheel_name == ("onnxruntime" if onnx_profile == "cuda" else "onnxruntime-gpu"):
            continue
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
    onnx_package = "onnxruntime-gpu" if onnx_profile == "cuda" else "onnxruntime"
    onnx_version = ONNX_GPU_VERSION if onnx_profile == "cuda" else "1.24.3"
    if versions.get(onnx_package) != onnx_version:
        raise ValueError(f"轮子仓库缺少所选 ONNX（模型格式）运行库：{onnx_package}=={onnx_version}")
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
    probe_env = {**os.environ,"CUDA_VISIBLE_DEVICES":"-1","PYTHONPYCACHEPREFIX":str(ROOT / ".local/build/runtime/bytecode"),"PYTHONNOUSERSITE":"1"}
    run(stage / "python.exe","-B","-X","utf8","-c",PROBE,env=probe_env,cwd=ROOT)
    # 移动后再次执行真实导入；不保留虚拟环境和开发机基础解释器路径。
    moved = output.with_name(output.name + " 中文搬迁验证")
    stage.rename(moved)
    run(moved / "python.exe","-B","-X","utf8","-c",PROBE,env=probe_env,cwd=output.parent)
    cuda_probe = None  # cuda_probe（本机真实显卡计算验证结果）；不代替无显卡设备验收。
    onnx_cuda_probe = None  # onnx_cuda_probe（模型格式推理运行时的真实显卡内核验证）。
    if args.verify_cuda:
        cuda_env = {key:value for key,value in probe_env.items() if key != "CUDA_VISIBLE_DEVICES"}
        # PATH（系统程序搜索路径）仅保留系统目录，不能依赖构建机外装的 CUDA（加速运行库）。
        system_root = Path(os.environ.get("SystemRoot", "C:/Windows"))
        cuda_env["PATH"] = os.pathsep.join(str(system_root / part) for part in ("System32", ""))
        cuda_probe = verify_cuda(moved / "python.exe", env=cuda_env, cwd=output.parent)
        if onnx_profile == "cuda":
            onnx_cuda_probe = verify_onnx_cuda(moved / "python.exe", env=cuda_env, cwd=output.parent)
    moved.rename(output)
    shutil.copyfile(lock, output / "wheels.lock.txt")
    atomic_json(output / "runtime.json",{"schema":1,"profile":args.profile,"onnx_profile":onnx_profile,"python":"3.12.10","embedded_sha256":digest(archive),"wheels":artifacts,"vc_runtime":vc_runtime,"relocated_imports":True,"physical_gpu_absence_verified":False,
                                        "cuda_probe":cuda_probe,"onnx_cuda_probe":onnx_cuda_probe})
    record = manifest(output,kind="python",build="pending")
    record["build"] = hashlib.sha256(json.dumps(record["files"], sort_keys=True).encode()).hexdigest()[:16]
    atomic_json(output / "manifest.json",record)
    verify(output,record)
    remove_tree(wheels, ROOT / ".local")
    archive.unlink()
    print(f"运行环境构建与搬迁导入检查通过：{output}；已回收离线安装资源。")

if __name__ == "__main__": main()
