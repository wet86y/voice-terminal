"""私有解释器工作进程：复用检查、离线 wheel（预编译包）组装、能力检测。仅使用标准库。"""
from __future__ import annotations
import argparse
import base64
import csv
import hashlib
from importlib import metadata
import importlib
import json
import os
from pathlib import Path
import platform
import shutil
import sys
import zipfile

def emit(value):
    print(json.dumps(value,ensure_ascii=False),flush=True)

def reparse(path):
    return path.is_symlink() or (getattr(path,'is_junction',lambda:False)())

def safe(root, text):
    path=Path(text)
    if path.is_absolute() or '..' in path.parts or ':' in text: raise ValueError('依赖记录包含外部路径')
    current=root
    for part in path.parts:
        current=current/part
        if reparse(current): raise ValueError('依赖记录不能包含外部链接')
    return current

def interpreter():
    return {'version':platform.python_version(),'architecture':platform.machine(),
            'prefix':sys.prefix,'base_prefix':sys.base_prefix,'executable':sys.executable,
            'implementation':sys.implementation.name,'official':sys.implementation.name=='cpython' and
            platform.python_version()=='3.12.10' and platform.architecture()[0]=='64bit' and
            sys.prefix==sys.base_prefix and not (Path(sys.prefix)/'conda-meta').exists()}

def reuse(lock, names, target):
    """RECORD（安装记录）每个文件均需可定位、大小和摘要可复核；不复用环境外脚本。"""
    reused=[]; rejected={}
    for item in lock['wheels']:
        if item['name'] not in names: continue
        try:
            distribution=metadata.distribution(item['name'])
            if distribution.version!=item['version']: raise ValueError('版本或构建类型不匹配')
            root=Path(distribution.locate_file('')).resolve()
            wheel=distribution.read_text('WHEEL') or ''
            if not any(tag in wheel for tag in ('win_amd64','none-any')): raise ValueError('架构不匹配')
            direct=distribution.read_text('direct_url.json')
            if direct and json.loads(direct).get('dir_info',{}).get('editable'):raise ValueError('可编辑安装无法复制')
            text=distribution.read_text('RECORD')
            if not text: raise ValueError('缺少完整 RECORD（安装文件记录）')
            rows=list(csv.reader(text.splitlines())); files=[]
            for relative,digest,size in rows:
                if relative.endswith('.pyc'): continue
                # 环境外 Scripts（脚本）与 .pth（搜索路径）不能直接复制。
                source=safe(root,relative)
                if not source.is_file(): raise ValueError('安装文件不完整')
                if relative.endswith('.pth') or relative.endswith('.egg-link'): raise ValueError('存在无法迁移的搜索路径或可编辑安装')
                if not digest:
                    if not relative.endswith('/RECORD'): raise ValueError('安装文件缺少摘要')
                else:
                    algorithm,encoded=digest.split('=',1)
                    if algorithm not in {'sha256','sha384','sha512'}: raise ValueError('摘要算法不受支持')
                    with source.open('rb') as stream: actual=base64.urlsafe_b64encode(hashlib.file_digest(stream,algorithm).digest()).decode().rstrip('=')
                    if actual!=encoded or not size or source.stat().st_size!=int(size): raise ValueError('安装文件摘要或大小错误')
                files.append((relative,source))
            for relative,source in files:
                destination=safe(target,relative);destination.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(source,destination)
            reused.append(item['name'])
        except Exception as error: rejected[item['name']]=str(error)
    return {'reused':reused,'rejected':rejected}

def install(wheels,site):
    records=[]
    for path in wheels:
        with zipfile.ZipFile(path) as archive:
            for item in archive.infolist():
                text=item.filename
                if item.is_dir(): continue
                parts=Path(text).parts
                if parts[0].endswith('.data'):
                    if len(parts)<3: raise ValueError('wheel（预编译包）数据路径无效')
                    if parts[1] not in {'purelib','platlib','data'}: continue
                    text='/'.join(parts[2:])
                destination=safe(site,text)
                if (item.external_attr>>16)&0o170000==0o120000: raise ValueError('预编译包包含链接')
                destination.parent.mkdir(parents=True,exist_ok=True)
                with archive.open(item) as source,destination.open('wb') as target: shutil.copyfileobj(source,target)
            records.append(path.name)
    return {'installed':records}

def dynamic_library_closure():
    """DLL closure（动态库闭包）：运行库只接受私有目录或 Windows（系统）目录。"""
    if os.name!='nt': return
    import ctypes
    from ctypes import wintypes
    modules=(wintypes.HMODULE*2048)();needed=wintypes.DWORD()
    psapi=ctypes.WinDLL('psapi',use_last_error=True);kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.GetCurrentProcess.restype=wintypes.HANDLE
    psapi.EnumProcessModules.argtypes=[wintypes.HANDLE,ctypes.POINTER(wintypes.HMODULE),wintypes.DWORD,ctypes.POINTER(wintypes.DWORD)]
    kernel.GetModuleFileNameW.argtypes=[wintypes.HMODULE,wintypes.LPWSTR,wintypes.DWORD]
    process=kernel.GetCurrentProcess()
    if not psapi.EnumProcessModules(process,modules,ctypes.sizeof(modules),ctypes.byref(needed)): raise OSError('无法检查动态库来源')
    roots=[Path(sys.executable).parent.resolve(),Path(os.environ['SystemRoot']).resolve()]
    for module in list(modules)[:min(len(modules),needed.value//ctypes.sizeof(wintypes.HMODULE))]:
        text=ctypes.create_unicode_buffer(32768)
        if not kernel.GetModuleFileNameW(module,text,len(text)):raise OSError('无法定位运行动态库')
        path=Path(text.value).resolve()
        if not any(path.is_relative_to(root) for root in roots):raise ValueError('运行依赖仍引用私有环境之外的动态库：'+path.name)

def check(names, lock=None):
    errors={}
    modules={'fastapi':'fastapi','pyyaml':'yaml','pydantic':'pydantic','numpy':'numpy','psutil':'psutil',
             'httpx':'httpx','uvicorn':'uvicorn','sounddevice':'sounddevice','soundfile':'soundfile',
             'webrtcvad-wheels':'webrtcvad','aec-audio-processing':'aec_audio_processing',
             'torch':'torch','torchaudio':'torchaudio','funasr':'funasr','onnxruntime':'onnxruntime','sherpa-onnx':'sherpa_onnx'}
    for name in names:
        try:
            try:
                version=metadata.version(name)
                if lock:
                    expected=next(item['version'] for item in lock['wheels'] if item['name']==name)
                    if version!=expected: raise ValueError('固定版本或中央处理器构建类型不匹配')
            except metadata.PackageNotFoundError:
                if name == 'onnxruntime' and not lock: metadata.version('onnxruntime-gpu')
                else: raise
            if name in modules: importlib.import_module(modules[name])
        except Exception as error: errors[name]=f'{type(error).__name__}: {error}'[:300]
    if lock and not errors:
        try: dynamic_library_closure()
        except Exception as error: errors['dynamic_libraries']=str(error)[:300]
    return {'ready':not errors,'errors':errors}

def main():
    parser=argparse.ArgumentParser();parser.add_argument('operation',choices=['interpreter','reuse','install','check','gpu'])
    parser.add_argument('--lock');parser.add_argument('--names',default='[]');parser.add_argument('--target');parser.add_argument('--wheels',default='[]');parser.add_argument('--model');parser.add_argument('--program');parser.add_argument('--install-root');args=parser.parse_args()
    names=json.loads(args.names)
    if args.operation=='interpreter': result=interpreter()
    elif args.operation=='reuse': result=reuse(json.loads(Path(args.lock).read_text('utf-8')),names,Path(args.target))
    elif args.operation=='install': result=install([Path(path) for path in json.loads(args.wheels)],Path(args.target))
    elif args.operation=='gpu':
        sys.path.insert(0,args.program)
        os.environ['VOICE_GPU_PROBE']='1'
        from voice_terminal.gpu_environment import probe_model,fingerprint
        root=Path(args.install_root)
        result=check(names)
        if result['ready']:
            try: result.update(probe_model(args.model,Path(args.program),root))
            except Exception as error: result.update(gpu_available=False,reason=f'{type(error).__name__}: {error}'[:400])
        else:result.update(gpu_available=False,reason='共享工作进程所需依赖缺失：'+str(result['errors']))
        result.update(environment_fingerprint=fingerprint(Path(sys.executable).parent),models_fingerprint=fingerprint(root/'models'))
    else: result=check(names,json.loads(Path(args.lock).read_text('utf-8')) if args.lock else None)
    emit({'ok':True,'result':result})

if __name__=='__main__':
    try: main()
    except Exception as error: emit({'ok':False,'error':str(error)[:500]});sys.exit(1)
