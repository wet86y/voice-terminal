"""锁定语音环境：构建时解析 Windows wheel（预编译包），目标设备只下载和解包。"""
from __future__ import annotations
import concurrent.futures
import hashlib
from importlib import metadata
import json
from pathlib import Path
import re
import urllib.request
import zipfile
from packaging.requirements import Requirement
from packaging.tags import sys_tags
from packaging.utils import canonicalize_name, parse_wheel_filename

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'config/runtime/voice-runtime.lock.json'
CACHE = ROOT / '.local/build/runtime-lock'
GROUPS = {
    'voice-core': ['fastapi', 'uvicorn', 'httpx', 'pyyaml', 'numpy', 'psutil', 'websockets', 'keyring',
                   'sounddevice', 'soundfile', 'webrtcvad-wheels', 'aec-audio-processing', 'pypinyin'],
    'torch-cpu': ['torch', 'torchaudio', 'funasr'],
    'onnx-cpu': ['onnxruntime', 'transformers', 'librosa', 'kaldi-native-fbank'],
    'onnx-vad': ['funasr-onnx'],
    'sherpa-cpu': ['sherpa-onnx', 'sherpa-onnx-core'],
    'modelscope-download': ['modelscope', 'modelscope-hub'],
}

def request(url: str, *, head=False):
    with urllib.request.urlopen(urllib.request.Request(url, method='HEAD' if head else 'GET'), timeout=30) as response:
        return dict(response.headers) if head else response.read()

def closure(roots):
    pending=list(roots); seen=set()
    while pending:
        name=canonicalize_name(pending.pop())
        if name in seen: continue
        seen.add(name)
        distribution=metadata.distribution('onnxruntime-gpu' if name=='onnxruntime' else name)
        for text in distribution.requires or ():
            requirement=Requirement(text)
            if requirement.marker and not requirement.marker.evaluate({'extra': ''}): continue
            pending.append(requirement.name)
    return sorted(seen)

def bundled(distribution, name, version):
    """无官方 wheel 时从已校验开发基线组装；保留原许可证与 RECORD（安装文件记录）。"""
    site=Path(distribution.locate_file('')).resolve()
    manifest=json.loads((ROOT / '.local/runtimes/python/manifest.json').read_text('utf-8'))
    trusted={row['path']: row for row in manifest['files']}
    wheel=CACHE / f'{name.replace("-", "_")}-{version}-cp312-cp312-win_amd64.whl'
    with zipfile.ZipFile(wheel, 'w', zipfile.ZIP_DEFLATED) as output:
        for row in distribution.files or ():
            relative=Path(str(row))
            if relative.is_absolute() or '..' in relative.parts or relative.suffix=='.pyc': continue
            source=site/relative
            if not source.is_file(): continue
            record=trusted.get('Lib/site-packages/'+relative.as_posix())
            if not record or hashlib.sha256(source.read_bytes()).hexdigest()!=record['sha256']:
                raise ValueError(f'开发依赖文件不匹配已校验清单：{name}/{relative}')
            output.write(source, relative.as_posix())
    return {'file': wheel.name, 'url': '', 'size': wheel.stat().st_size,
            'sha256': hashlib.sha256(wheel.read_bytes()).hexdigest(), 'bundled': f'tools/wheels/{wheel.name}',
            'provenance': 'verified-development-manifest'}

def resolve(name):
    distribution=metadata.distribution('onnxruntime-gpu' if name=='onnxruntime' else name)
    version={'torch':'2.11.0+cpu','torchaudio':'2.11.0+cpu','onnxruntime':'1.24.3'}.get(name,distribution.version)
    if name in {'torch','torchaudio'}:
        index=request(f'https://download.pytorch.org/whl/cpu/{name}/').decode()
        from urllib.parse import unquote
        wanted=f'{name}-{version}-cp312-cp312-win_amd64.whl'
        matches=[url for url in re.findall(r'href="([^"]+)"',index) if unquote(url.split('#')[0]).endswith(wanted)]
        if len(matches)!=1: raise ValueError(f'没有唯一的官方中央处理器包：{wanted}')
        url=matches[0].replace('https://download-r2.pytorch.org/', 'https://download.pytorch.org/'); digest=url.split('#sha256=')[-1]
        size=int(next(v for k,v in request(url.split('#')[0],head=True).items() if k.lower()=='content-length'))
        row={'file':wanted,'url':url.split('#')[0],'size':size,'sha256':digest}
    else:
        try:
            data=json.loads(request(f'https://pypi.org/pypi/{name}/{version}/json'))
            tags=set(sys_tags()); candidates=[]
            for item in data['urls']:
                if item['packagetype']!='bdist_wheel' or item.get('yanked'): continue
                _,_,_,wheel_tags=parse_wheel_filename(item['filename'])
                if tags & wheel_tags: candidates.append(item)
            candidates.sort(key=lambda item: (not item['filename'].endswith('win_amd64.whl'), item['filename']))
            if not candidates: raise LookupError('缺少官方 Windows 预编译包')
            item=candidates[0]
            row={'file':item['filename'],'url':item['url'],'size':item['size'],'sha256':item['digests']['sha256']}
        except (urllib.error.HTTPError, LookupError):
            if name in {'torch','torchaudio'}: raise
            row=bundled(distribution,name,version)
    row.update({'name':name,'version':version,'groups':[group for group,items in resolved_groups.items() if name in items]})
    return row

if __name__=='__main__':
    CACHE.mkdir(parents=True,exist_ok=True)
    resolved_groups={group:closure(roots) for group,roots in GROUPS.items()}
    names=sorted(set().union(*map(set,resolved_groups.values())))
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        wheels=list(pool.map(resolve,names))
    url='https://www.python.org/ftp/python/3.12.10/python-3.12.10-embed-amd64.zip'
    size=int(next(v for k,v in request(url,head=True).items() if k.lower()=='content-length'))
    record={'schema':1,'python':{'version':'3.12.10','architecture':'amd64','url':url,'size':size,
            'sha256':'4acbed6dd1c744b0376e3b1cf57ce906f9dc9e95e68824584c8099a63025a3c3'},
            'groups':resolved_groups,'wheels':wheels,
            'fields':{'schema':'运行环境锁定格式','python':'官方可嵌入解释器','architecture':'64 位架构',
            'groups':'按组件共享的依赖组；onnx-vad（轻量语音活动检测）为独立运行组','wheels':'固定版本的预编译包','name':'依赖名称','version':'锁定版本',
            'file':'固定文件名','url':'原始下载地址','size':'预期字节数','sha256':'SHA-256 文件摘要',
            'bundled':'随本体携带的小型预编译包','provenance':'构建来源','group':'所需共享依赖组'}}
    OUT.write_text(json.dumps(record,ensure_ascii=False,indent=2)+'\n','utf-8')
    print(f'已锁定 {len(wheels)} 个预编译包；无需下载大型推理包。')
