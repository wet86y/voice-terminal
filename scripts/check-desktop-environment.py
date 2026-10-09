"""Environment check（环境检测）：供用户与其他代理执行的可重复诊断。"""
from pathlib import Path
import argparse
import json
import os
import platform
import subprocess
import sys

ROOT = next(p for p in Path(__file__).resolve().parents if (p / 'pyproject.toml').is_file())
sys.path.insert(0, str(ROOT))
from voice_terminal.local_layout import local_root, read_portable
INSTALL = local_root()
SELECTED = read_portable(INSTALL)

PROBE = """
import importlib,json,platform,sys
failures={}
names=['torch','torchaudio','onnxruntime','sherpa_onnx','funasr','funasr_onnx','kaldi_native_fbank','sounddevice','soundfile','webrtcvad','aec_audio_processing','psutil','fastapi','httpx','keyring']
for name in names:
    try: importlib.import_module(name)
    except Exception as error: failures[name]=type(error).__name__+': '+str(error)
value={'python':platform.python_version(),'architecture':platform.machine(),'import_errors':failures,'cpu_tensor':None,'onnx_providers':[],'audio_devices':None}
for name in ['torch','onnxruntime','sounddevice']:
    if name in failures: continue
    try:
        module=importlib.import_module(name)
        if name=='torch': value['cpu_tensor']=module.tensor([1.,2.],device='cpu').sum().item()
        if name=='onnxruntime': value['onnx_providers']=module.get_available_providers()
        if name=='sounddevice': value['audio_devices']=len(module.query_devices())
    except Exception as error: failures[name]=type(error).__name__+': '+str(error)
print('VOICE_ENVIRONMENT_RESULT='+json.dumps(value))
"""

parser = argparse.ArgumentParser(description="桌面应用环境检测：实际导入、原生库、音频枚举和模型文件，不安装依赖")
parser.add_argument('--python', type=Path, default=INSTALL / SELECTED['python'], help='python（便携配置指定的解释器）')
parser.add_argument('--models', type=Path, default=INSTALL / SELECTED['models'], help='models（便携配置指定的模型）')
parser.add_argument('--webview2', type=Path, default=None, help='webview2（已统一为系统网页引擎，参数仅供旧诊断兼容）')
parser.add_argument('--dsh', type=Path, default=INSTALL / SELECTED['dsh'], help='dsh（闭合代理宿主）')
args = parser.parse_args()
for name in ('python', 'models', 'dsh'):
    if getattr(args, name).resolve() != (INSTALL / SELECTED[name]).resolve():
        raise ValueError('环境检测组件必须符合唯一便携配置：' + name)
result = subprocess.run([str(args.python.resolve()), '-B', '-X', 'utf8', '-c', PROBE],
                        env={**os.environ, 'CUDA_VISIBLE_DEVICES': '-1', 'PYTHONNOUSERSITE': '1'},
                        cwd=args.python.resolve().parent, capture_output=True, text=True, encoding='utf-8', timeout=180)
lines = [line.removeprefix('VOICE_ENVIRONMENT_RESULT=') for line in result.stdout.splitlines() if line.startswith('VOICE_ENVIRONMENT_RESULT=')]
value = json.loads(lines[-1]) if lines else {'import_errors': {'interpreter': result.stderr[-3000:] or '解释器未输出检测结果'}}
required = ['kws/sherpa-onnx-kws-zipformer-zh-en-3M/tokens.txt',
            'funasr/paraformer-zh-streaming/model.pt', 'funasr/paraformer-zh-streaming/config.yaml',
            'funasr/fsmn-vad/model.pt', 'analysis/sensevoice/model.pt',
            'analysis/smart-turn/smart-turn-v3.2-cpu.onnx', 'speaker/wespeaker-cnceleb-resnet34/cnceleb_resnet34.onnx']
value['missing_models'] = [path for path in required if not (args.models / path).is_file()]
value['system'] = platform.platform()
value['fixed_webview2'] = False
value['system_webview2'] = True # system_webview2（运行策略：系统网页引擎），可用性由原生宿主实际启动核验。
value['dsh_cli'] = None
if args.dsh:
    try:
        metadata = json.loads((args.dsh / 'runtime.json').read_text(encoding='utf-8-sig'))
        command = subprocess.run([str((args.dsh / metadata['node']).resolve()), str((args.dsh / metadata['entry']).resolve()), '--version'],
                                 cwd=args.dsh.resolve(), capture_output=True, text=True, encoding='utf-8', timeout=30, check=True)
        value['dsh_cli'] = command.stdout.strip()
    except (OSError, subprocess.SubprocessError) as error:
        value['import_errors']['dsh'] = str(error)
value['verified'] = (result.returncode == 0 and value.get('python', '').startswith('3.12.')
                     and value.get('architecture', '').lower() in {'amd64', 'x86_64'}
                     and value.get('cpu_tensor') == 3.0 and not value['import_errors'] and not value['missing_models'])
value['fields'] = {'import_errors': '实际导入或原生依赖错误', 'cpu_tensor': '中央处理器张量计算结果',
                   'onnx_providers': '推理后端', 'audio_devices': '可枚举音频设备数量', 'missing_models': '缺少的核心模型文件',
                   'fixed_webview2': '固定网页运行时入口存在', 'dsh_cli': '代理宿主入口版本', 'verified': '本次环境检测通过'}
value['notes'] = '本检测不替代真实模型加载、搬迁、无显卡机器、Windows 10 或实际语音验收；网页运行时还需桌面启动验证。'
print(json.dumps(value, ensure_ascii=False, indent=2))
raise SystemExit(0 if value['verified'] else 1)
