"""GPU environment（显卡环境）：相对路径登记、环境变更失效与按进程选择解释器。"""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path

MODEL_RESOURCES={'paraformer':'paraformer-streaming','voice_vad':'fsmn-vad','sensevoice':'sensevoice-small',
                 'smart_turn':'smart-turn','wespeaker':'wespeaker-cnceleb-resnet34','kws':'kws-sherpa-onnx'}
ROLES={'asr':('paraformer',),'analysis':('sensevoice','smart_turn'),'voice':('voice_vad','kws','wespeaker')}

def fingerprint(root:Path)->str:
    digest=hashlib.sha256()
    for path in sorted(root.rglob('*'),key=lambda p:p.relative_to(root).as_posix()):
        if path.is_symlink() or getattr(path,'is_junction',lambda:False)(): raise ValueError('显卡环境不能使用外部链接')
        if not path.is_file() or '__pycache__' in path.parts or path.suffix=='.pyc': continue
        stat=path.stat();digest.update(f'{path.relative_to(root).as_posix()}:{stat.st_size}:{stat.st_mtime_ns}\n'.encode())
    return digest.hexdigest()

def registered(install:Path,data:Path)->dict|None:
    try:
        value=json.loads((data/'config/gpu-environment.json').read_text('utf-8'))
        relative=Path(value['path'])
        if relative.is_absolute() or relative.as_posix() not in {'runtimes/python-gpu','runtimes/python'}: return None
        root=install/relative
        if not root.resolve().is_relative_to(install.resolve()) or not (root/'python.exe').is_file():return None
        if value.get('environment_fingerprint')!=fingerprint(root):return None
        # 模型文件变更后也必须重新检测。
        if value.get('models_fingerprint')!=fingerprint(install/'models'):return None
        return value
    except (OSError,ValueError,KeyError):return None

def worker_python(install:Path,data:Path,role:str,cpu_python:str,devices:dict)->str:
    value=registered(install,data)
    if not value:return cpu_python
    selected=ROLES.get(role,())
    if any(devices.get(name,'auto')!='cpu' and value.get('models',{}).get(name,{}).get('gpu_available') for name in selected):
        return str(install/value['path']/'python.exe')
    return cpu_python

def probe_model(name:str,program:Path,install:Path)->dict:
    """实际 GPU（显卡）推理：CPU（中央处理器）回退不能作为显卡检查成功。"""
    import sys
    sys.path.insert(0,str(program))
    from .compute import _backend_hardware_status,loaded_funasr_device
    hardware=_backend_hardware_status()
    if not hardware.get('models',{}).get(name,{}).get('gpu_available'):
        return {'gpu_available':False,'reason':hardware.get('models',{}).get(name,{}).get('reason','显卡后端不可用')}
    import numpy as np
    models=install/'models';pcm=b'\0\0'*32000
    if name in {'paraformer','voice_vad'}:
        from funasr import AutoModel
        folder=models/('funasr/paraformer-zh-streaming' if name=='paraformer' else 'funasr/fsmn-vad')
        model=AutoModel(model=str(folder),device='cuda',disable_update=True,disable_pbar=True)
        options={'chunk_size':[0,10,5]} if name=='paraformer' else {'chunk_size':200}
        model.generate(input=np.zeros(32000,dtype=np.float32),cache={},is_final=True,**options)
        if loaded_funasr_device(model,'cuda')!='cuda':raise RuntimeError('模型自动回退到中央处理器')
        import torch
        torch.cuda.synchronize()
    elif name=='sensevoice':
        from .analysis.sensevoice import SenseVoice
        model=SenseVoice(models/'analysis/sensevoice',models/'funasr/fsmn-vad',device='cuda')
        model(pcm)
        if model.device!='cuda' or model.internal_vad_device!='cuda':raise RuntimeError('精修或内部活动检测回退到中央处理器')
    elif name=='smart_turn':
        from .analysis.smart_turn import SmartTurn
        model=SmartTurn(models/'analysis/smart-turn',device='cuda')
        model(pcm)
        if model.device!='cuda':raise RuntimeError(model.load_error or '模型自动回退')
    elif name=='wespeaker':
        from .speaker.engine import WeSpeakerOnnxEngine
        model=WeSpeakerOnnxEngine(models/'speaker/wespeaker-cnceleb-resnet34/cnceleb_resnet34.onnx',device='cuda')
        model.embed_pcm(pcm)
        if model.device!='cuda':raise RuntimeError(model.load_error or '模型自动回退')
    elif name=='kws':
        from .kws.detector import KwsDetector
        from .kws.keywords import generate_keywords_for
        folder=models/'kws/sherpa-onnx-kws-zipformer-zh-en-3M'
        raw=install/'tmp/gpu-check/keywords.raw.txt';keywords=raw.with_name('keywords.txt')
        raw.parent.mkdir(parents=True,exist_ok=True)
        generate_keywords_for(model_dir=folder,raw_path=raw,output_path=keywords,entries=[('你好','NIHAO')])
        model=KwsDetector(model_dir=folder,keywords_file=keywords,keyword_label='NIHAO',provider='cuda')
        model.accept_pcm(pcm)
    else:raise ValueError('未定义的模型')
    return {'gpu_available':True,'reason':None,'actual_inference':True,'hardware':hardware}
