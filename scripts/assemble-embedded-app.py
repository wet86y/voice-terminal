"""内嵌本体组装：排除入口自身、Python（后端环境）、模型与 DSH（代理宿主）。"""
from __future__ import annotations
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import sys
import zipfile

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
spec=importlib.util.spec_from_file_location('assembler',ROOT/'scripts/assemble-desktop.py')
assembler=importlib.util.module_from_spec(spec);spec.loader.exec_module(assembler)
from voice_terminal.distribution import manifest,verify
from voice_terminal.paths import atomic_json

if __name__=='__main__':
    cache=ROOT/'.local/build/embedded-app';cache.mkdir(parents=True,exist_ok=True)
    stage=cache/'payload'
    if stage.exists():
        if stage.is_symlink() or stage.is_junction() or stage.resolve().parent!=cache.resolve():raise ValueError('本体暂存路径越界')
        shutil.rmtree(stage)
    assembler.collect_program(stage,ROOT/'.local/build/desktop/distribution/release',include_desktop=False)
    (stage/'docs').mkdir()
    shutil.copy2(ROOT/'docs/模型运行环境配置指南.md',stage/'docs/模型运行环境配置指南.md')
    lock=json.loads((stage/'config/runtime/voice-runtime.lock.json').read_text('utf-8'))
    for row in lock['wheels']:
        if row.get('bundled'):
            destination=stage/row['bundled'];destination.parent.mkdir(parents=True,exist_ok=True)
            shutil.copy2(ROOT/'.local/build/runtime-lock'/row['file'],destination)
            if hashlib.sha256(destination.read_bytes()).hexdigest()!=row['sha256']:raise ValueError('内置依赖摘要不一致')
    record=manifest(stage,kind='app',build='embedded-'+json.loads((ROOT/'package.json').read_text())['version'])
    record['launch_channel']='distribution'
    record['required_components']=[] # required_components（必要组件）：桌面不依赖后端或模型。
    record['optional_components']=['python','models','dsh','webview2'] # optional_components（按需组件）。
    record['components']=[] # components（可选组件绑定）：原生安装后补记运行环境与资源摘要。
    record['fields']={'launch_channel':'分发渠道；内嵌清单排除程序自身，启用后补齐实际入口','files':'本体固定文件','path':'相对路径','size':'字节数','sha256':'SHA-256 文件摘要'}
    atomic_json(stage/'release.json',record);verify(stage,record)
    archive=cache/'app.zip'
    with zipfile.ZipFile(archive,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=6,allowZip64=True) as output:
        for path in sorted(stage.rglob('*')):
            if path.is_file():output.write(path,path.relative_to(stage).as_posix())
    atomic_json(cache/'embedded-app.json',{'schema':1,'size':archive.stat().st_size,'sha256':hashlib.sha256(archive.read_bytes()).hexdigest(),
        'files':len(record['files']),'fields':{'schema':'内嵌包记录格式','size':'压缩本体大小','sha256':'内嵌归档摘要','files':'排除入口的文件数'}})
    print(f'内嵌本体：{archive}，{archive.stat().st_size/1024**2:.1f} MiB（兆二进制字节）。')
