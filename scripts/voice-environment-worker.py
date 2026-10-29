"""私有解释器工作进程：复用检查、离线 wheel（预编译包）组装、能力检测。仅使用标准库。"""
from __future__ import annotations
import sys
# UTF-8（统一字符编码）显式应用于标准流，兼容中文路径及 ._pth（隔离搜索路径）解释器。
if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
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
import subprocess
import zipfile

# 私有环境检查同样可能在产品宿主中导入第三方框架；在导入依赖前启用后台进程策略。
if __name__ == '__main__' and os.environ.get('VOICE_PROCESS_HOST'):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from voice_terminal.process_host import configure_background_subprocesses
    configure_background_subprocesses()

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

def installed_relative(text):
    """installed_relative（实际安装路径）：安装与卸载使用同一 .data（包附加文件）映射。"""
    parts=Path(text).parts
    if not parts: raise ValueError('预编译包路径为空')
    if parts[0].endswith('.data'):
        if len(parts)<3: raise ValueError('wheel（预编译包）数据路径无效')
        if parts[1] not in {'purelib','platlib','data'}: return None
        return '/'.join(parts[2:])
    return text


def install(wheels,site):
    records=[]
    for path in wheels:
        with zipfile.ZipFile(path) as archive:
            for item in archive.infolist():
                text=item.filename
                if item.is_dir(): continue
                text=installed_relative(text)
                if text is None: continue
                destination=safe(site,text)
                if (item.external_attr>>16)&0o170000==0o120000: raise ValueError('预编译包包含链接')
                destination.parent.mkdir(parents=True,exist_ok=True)
                with archive.open(item) as source,destination.open('wb') as target: shutil.copyfileobj(source,target)
            records.append(path.name)
    return {'installed':records}


def runtime_lock(path, gpu_path=None):
    """runtime_lock（有效依赖锁）：可选显卡锁仅替换两个配套张量依赖。"""
    value = json.loads(Path(path).read_text('utf-8'))
    if gpu_path:
        gpu = json.loads(Path(gpu_path).read_text('utf-8'))
        rows = gpu.get('wheels', [])
        if (gpu.get('schema') != 1 or gpu.get('profile') != 'cu128' or len(rows) != 2
                or {row.get('name') for row in rows} != {'torch', 'torchaudio'}):
            raise ValueError('可选显卡依赖锁格式或包集合无效')
        replacements = {row['name']: row for row in rows}
        replaced = 0
        for row in value['wheels']:
            replacement = replacements.get(row['name'])
            if replacement:
                if replacement['version'] != row['version'].split('+')[0] + '+cu128':
                    raise ValueError('显卡依赖与基础依赖版本不匹配')
                row.update(replacement)
                replaced += 1
        if replaced != 2:
            raise ValueError('基础依赖锁缺少配套张量依赖')
    return value


def prune(site, names):
    """prune（清理候选旧包）：仅删除暂存环境内的记录文件，源环境保持完整。"""
    site = Path(site).resolve()
    # RECORD（安装文件记录）可能包含环境外脚本；这些入口没有复制/解包到候选中，不删除它们。
    selected = set(names)
    if not selected.issubset({'torch', 'torchaudio'}):
        raise ValueError('仅允许替换配套张量依赖')
    paths = set()
    for distribution in metadata.distributions(path=[str(site)]):
        if distribution.metadata['Name'].lower().replace('_', '-') not in selected:
            continue
        if not distribution.files:
            raise ValueError('候选旧依赖缺少完整安装文件记录')
        for relative in distribution.files:
            if '..' in Path(str(relative)).parts or Path(str(relative)).is_absolute():
                continue
            source = safe(site, str(relative))
            if source.is_file():
                paths.add(source)
    # 所有路径先验证，再执行删除，防止遇到链接时留下半清理的候选。
    parents = set()
    for path in paths:
        path.unlink()
        parents.update(parent for parent in path.parents if parent != site and parent.is_relative_to(site))
    for parent in sorted(parents, key=lambda path: len(path.parts), reverse=True):
        try:
            parent.rmdir()
        except OSError:
            pass
    return {'removed_files': len(paths)}  # removed_files（已清理的候选文件数）。

def defender_injected_module(path):
    """Defender module（系统防护注入模块）：核验固定系统位置和微软有效签名。"""
    program_data=os.environ.get('ProgramData')
    if not program_data or path.name.lower()!='mpoav.dll': return False
    platform_root=(Path(program_data)/'Microsoft/Windows Defender/Platform').resolve()
    if not path.is_relative_to(platform_root): return False
    powershell_root=Path(os.environ['SystemRoot'])/'System32/WindowsPowerShell/v1.0'
    script="Import-Module (Join-Path $PSHOME 'Modules/Microsoft.PowerShell.Security/Microsoft.PowerShell.Security.psd1') -ErrorAction Stop; $signature=Get-AuthenticodeSignature -LiteralPath $env:VOICE_SYSTEM_MODULE_PATH; if ($signature.Status -eq 'Valid' -and $signature.SignerCertificate.Subject -match '^CN=Microsoft Windows, O=Microsoft Corporation(?:,|$)') { exit 0 }; exit 1"
    try:
        result=subprocess.run([str(powershell_root/'powershell.exe'),'-NoProfile','-NonInteractive','-Command',script],
            env={**os.environ,'PSModulePath':str(powershell_root/'Modules'),'VOICE_SYSTEM_MODULE_PATH':str(path)},stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=20,
            creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        return result.returncode==0
    except (OSError,subprocess.TimeoutExpired): return False

def verified_product_host_files(image):
    """仅认可当前真实产品宿主及其清单登记的三项 CRT（C++运行库）文件。"""
    image=Path(image).resolve()
    if image.name.casefold()!='voiceterminaldesktop.exe': return set()
    try:
        release=json.loads((image.parent/'release.json').read_text('utf-8'))
        if not isinstance(release,dict) or release.get('role')!='product': return set()
        rows={item['path']:item for item in release['files'] if isinstance(item,dict) and isinstance(item.get('path'),str)}
        def matches(path):
            row=rows.get(path.name)
            return (row is not None and path.is_file() and path.stat().st_size==row.get('size')
                    and hashlib.sha256(path.read_bytes()).hexdigest()==row.get('sha256'))
        if not matches(image): return set()
        allowed={image}
        for name in ('msvcp140.dll','vcruntime140.dll','vcruntime140_1.dll'):
            path=image.parent/name
            if matches(path): allowed.add(path.resolve())
        return allowed
    except (OSError,ValueError,KeyError,TypeError): return set()


def dynamic_library_closure():
    """DLL closure（动态库闭包）：私有库、系统库和精确核验的产品宿主文件。"""
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
    image=ctypes.create_unicode_buffer(32768)
    if not kernel.GetModuleFileNameW(None,image,len(image)):raise OSError('无法定位实际工作进程宿主')
    host_files=verified_product_host_files(image.value)
    for module in list(modules)[:min(len(modules),needed.value//ctypes.sizeof(wintypes.HMODULE))]:
        text=ctypes.create_unicode_buffer(32768)
        if not kernel.GetModuleFileNameW(module,text,len(text)):raise OSError('无法定位运行动态库')
        path=Path(text.value).resolve()
        if path not in host_files and not any(path.is_relative_to(root) for root in roots) and not defender_injected_module(path):raise ValueError('运行依赖仍引用私有环境之外的动态库：'+path.name)

def installed_version(name, lock=None, compatible_gpu=False):
    """installed_version（已安装版本）：复用现用环境时接受项目已验证的显卡组合。"""
    distribution=name
    try:
        version=metadata.version(name)
    except metadata.PackageNotFoundError:
        if name!='onnxruntime' or (lock and not compatible_gpu): raise
        distribution='onnxruntime-gpu'
        version=metadata.version(distribution)
    if lock:
        expected=next(item['version'] for item in lock['wheels'] if item['name']==name)
        gpu_match=compatible_gpu and (
            (name in {'torch','torchaudio'} and version==expected.replace('+cpu','+cu128')) or
            (name=='onnxruntime' and distribution=='onnxruntime-gpu' and version=='1.24.4')
        )
        if (version!=expected or distribution!=name) and not gpu_match: raise ValueError('固定版本或兼容构建类型不匹配')
    return version

def inventory(names, lock, compatible_gpu=False):
    """inventory（依赖复用清单）：只复用文件清单已验证环境中的兼容依赖。"""
    reused=[]
    rejected={}
    for name in names:
        try:
            installed_version(name,lock,compatible_gpu)
            reused.append(name)
        except Exception as error: rejected[name]=str(error)[:300]
    return {'reused':reused,'rejected':rejected}


def package_identity(row):
    """package_identity（包内容身份）：地址、依赖组及排版变化不要求重新安装。"""
    return {key: row.get(key) for key in ('version', 'file', 'size', 'sha256')}


def delta_plan(root, lock, names, compatible_gpu=False, legacy=None, want_overlay=False, repair_local=False):
    """delta_plan（增量计划）：从受管清单及旧安装记录恢复包所有权，不修改现用环境。"""
    root = Path(root).resolve()
    record = json.loads(safe(root, 'runtime.json').read_text('utf-8'))
    manifest = json.loads(safe(root, 'manifest.json').read_text('utf-8'))
    tracked = {row['path'].casefold(): row['path'] for row in manifest['files']}
    tracked_rows = {row['path']: row for row in manifest['files']}
    if record.get('schema') not in (1, 2):
        raise ValueError('无法识别旧环境记录，保留现用环境，请先修复记录')
    canonical = lambda text: text.lower().replace('_', '-').replace('.', '-')
    legacy = legacy or {}
    broken=set()
    if repair_local:
        for row in manifest['files']:
            path=safe(root,row['path'])
            try:
                if not path.is_file() or path.stat().st_size!=row['size']:
                    broken.add(row['path'])
                else:
                    with path.open('rb') as stream:
                        if hashlib.file_digest(stream,'sha256').hexdigest()!=row['sha256']: broken.add(row['path'])
            except FileNotFoundError: broken.add(row['path'])
    unclaimed=set(broken)

    def scope(relative, wanted, receipts, old_names, allow_gpu, historical):
        site = safe(root, relative)
        distributions = {}
        recorded_entries = {}
        owners = {}
        paths = {}
        for distribution in metadata.distributions(path=[str(site)]):
            declared_name=distribution.metadata['Name']
            if not declared_name: raise ValueError('旧环境存在缺少名称的发行元数据，保留原环境')
            name = canonical(declared_name)
            if name in distributions:
                raise ValueError('旧环境存在重复包记录：' + name)
            distributions[name] = distribution
            raw_record=distribution.read_text('RECORD')
            # importlib.metadata（发行元数据库）会过滤无法按原 RECORD 路径找到的项，
            # .data（附加文件）已映射到实际位置时必须读取原记录，才能正确卸载。
            entries=[row[0] for row in csv.reader(raw_record.splitlines()) if row] if raw_record else None
            recorded_entries[name]=entries
            owned = set()
            for entry in entries or ():
                text = str(entry).replace('\\', '/')
                # 旧安装器没有释放环境外的命令脚本；不得把它们列入卸载。
                if '..' in Path(text).parts or Path(text).is_absolute():
                    continue
                text=installed_relative(text)
                if text is None: continue
                path = safe(site, text).relative_to(root).as_posix()
                if path.casefold() in tracked:
                    path = tracked[path.casefold()]
                    owned.add(path)
                    owners.setdefault(path, set()).add(name)
            # 标准发行元数据目录整体属于该发行包，包括旧安装器追加但未写回 RECORD（安装记录）的回执。
            metadata_folders={Path(str(entry)).parts[0] for entry in entries or ()
                              if Path(str(entry)).parts and Path(str(entry)).parts[0].endswith(('.dist-info','.egg-info'))}
            for folder in metadata_folders:
                prefix=safe(site,folder).relative_to(root).as_posix()+'/'
                for path in tracked.values():
                    if path.casefold().startswith(prefix.casefold()):
                        owned.add(path)
                        owners.setdefault(path,set()).add(name)
            paths[name] = owned
        desired = {row['name']: row for row in wanted}
        changed, reused, identities = [], [], {}
        for name, row in desired.items():
            distribution = distributions.get(name)
            if not allow_gpu and ((name in {'torch','torchaudio'} and distribution is not None and '+cu' in distribution.version and '+cpu' in row['version']) or
                                  (name=='onnxruntime' and distribution is None and 'onnxruntime-gpu' in distributions)):
                raise ValueError('不能以处理器依赖覆盖现用显卡包，需要完整性校验确认修复方向：'+name)
            if distribution is None and name == 'onnxruntime' and allow_gpu:
                distribution = distributions.get('onnxruntime-gpu')
            actual = distribution.version if distribution else None
            accepted = actual == row['version'] and (name != 'onnxruntime' or 'onnxruntime' in distributions)
            accepted |= bool(allow_gpu and ((name in {'torch', 'torchaudio'} and actual == row['version'].replace('+cpu', '+cu128')) or
                                          (name == 'onnxruntime' and 'onnxruntime-gpu' in distributions and actual == '1.24.4')))
            identity = package_identity(row)
            evidence = receipts.get(name, historical.get(name))
            if accepted and evidence is None:
                raise ValueError('旧依赖缺少可核对的内容身份，保留原环境，不能自动重装全部依赖：' + name)
            # 历史兼容显卡包没有原 wheel（预编译包）摘要：明确登记已验证安装文件身份，不能冒充 CPU（中央处理器）包摘要。
            variant = accepted and distribution is not None and (actual != row['version'] or canonical(distribution.metadata['Name']) != name)
            installed_identity = None
            if variant:
                owned=paths[canonical(distribution.metadata['Name'])]
                if not recorded_entries[canonical(distribution.metadata['Name'])] or not any(path.endswith('/RECORD') for path in owned):
                    raise ValueError('历史兼容包缺少受清单覆盖的完整安装记录，不能迁移内容身份：' + name)
                content=[tracked_rows[path] for path in sorted(owned)]
                installed_identity={'contract':identity,'installed_version':actual,
                                    'installed_files_sha256':hashlib.sha256(json.dumps(content,sort_keys=True,separators=(',',':')).encode()).hexdigest(),
                                    'provenance':'verified-legacy-installed-files'}
            artifact=legacy.get('artifacts',{}).get(name)
            artifact_matches=variant and artifact is not None and artifact==evidence and artifact['version']==actual
            matches = evidence == identity or artifact_matches or (installed_identity is not None and evidence == installed_identity)
            if accepted and matches:
                reused.append(name)
            else:
                changed.append(name)
            identities[name] = installed_identity if variant else identity
        for path in broken & owners.keys():
            affected={('onnxruntime' if name=='onnxruntime-gpu' else name) for name in owners[path]}
            if not affected<=desired.keys(): raise ValueError('损坏文件包含无法局部确认的共享所有者，需要完整性校验')
            for name in affected:
                if name not in changed: changed.append(name)
                if name in reused: reused.remove(name)
            unclaimed.discard(path)
        removed = (set(old_names) | set(receipts)) - set(desired)
        # 只卸载旧受管包。未登记的外来包及其它包共用的命名空间文件保持不变。
        retiring = set(changed) | removed
        if 'onnxruntime' in retiring and 'onnxruntime-gpu' in distributions:
            retiring.add('onnxruntime-gpu')
        for name in retiring & set(old_names):
            if name not in distributions and not (name=='onnxruntime' and 'onnxruntime-gpu' in distributions):
                raise ValueError('旧受管包缺少发行元数据，不能安全替换或卸载：' + name)
        deletes = set()
        if retiring and any(not entries for entries in recorded_entries.values()):
            raise ValueError('安装记录不完整，无法确认共享文件所有权；保留原环境')
        for name in retiring:
            if name in distributions and not recorded_entries[name]:
                raise ValueError('旧受管包缺少安装文件记录，不能安全卸载：' + name)
            for path in paths.get(name, ()):
                if owners[path] <= retiring:
                    deletes.add(path)
        # 删除已退役源码的派生字节码；避免旧缓存残留使后续完整性检查误报。
        caches=set()
        for path in deletes:
            source=safe(root,path)
            if source.suffix=='.py':
                folder=source.parent/'__pycache__'
                if reparse(folder): raise ValueError('字节码目录不能包含链接')
                for cached in folder.glob(source.stem+'.*.pyc'):
                    safe(root,cached.relative_to(root).as_posix())
                    caches.add(cached.relative_to(root).as_posix())
        deletes.update(caches)
        return {'install': changed, 'reused': reused, 'removed': sorted(removed),
                'delete': sorted(deletes), 'receipts': identities}

    wanted = [row for row in lock['wheels'] if row['name'] in names]
    main = scope('Lib/site-packages', wanted, record.get('package_receipts', {}), record.get('packages', legacy.get('main', {}).keys()), compatible_gpu, legacy.get('main', {}))
    overlay = None
    if want_overlay or record.get('gpu_profile') or record.get('compatible_gpu') or record.get('profile') == 'cu128':
        base = lock.get('cpu_wheels', lock['wheels'])
        overlay_names = {'torch', 'torchaudio'}
        if any(safe(root, 'Lib/site-packages').glob('onnxruntime_gpu-*.dist-info')):
            overlay_names.add('onnxruntime')
        old_overlay = set(record.get('cpu_overlay_receipts', {}))
        if not old_overlay:
            old_overlay = {canonical(d.metadata['Name']) for d in metadata.distributions(path=[str(safe(root, 'Lib/cpu-packages'))])}
        overlay = scope('Lib/cpu-packages', [r for r in base if r['name'] in overlay_names],
                        record.get('cpu_overlay_receipts', {}), old_overlay, False, legacy.get('overlay', {}))
    if unclaimed: raise ValueError('解释器或结构性文件损坏，无法安全局部重建，需要完整性校验：'+next(iter(sorted(unclaimed))))
    return {'main': main, 'overlay': overlay}


def legacy_identities(root, lock_path, gpu_path=None):
    """legacy_identities（旧锁身份）：按完整旧锁摘要匹配随本体登记的历史包身份。"""
    record = json.loads(safe(Path(root), 'runtime.json').read_text('utf-8'))
    path = Path(lock_path)
    history_path = path.with_name('voice-runtime-history.json')
    history = json.loads(history_path.read_text('utf-8')) if history_path.is_file() else {'locks': {}}
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    current = json.loads(path.read_text('utf-8'))
    identities = {row['name']: package_identity(row) for row in current['wheels']}
    old = identities if record.get('lock_sha256') == digest else history['locks'].get(record.get('lock_sha256'), {})
    result = {'main': dict(old), 'overlay': dict(old), 'artifacts':{}}
    # 最早的离线环境只有 wheels（原始包回执），没有整锁摘要或 packages（包名列表）。
    for artifact in record.get('wheels',[]):
        filename=artifact.get('file','');parts=filename.split('-')
        if Path(filename).name!=filename or not filename.endswith('.whl') or len(parts)<5:
            raise ValueError('旧离线环境的包文件身份无效')
        name=parts[0].lower().replace('_','-');name='onnxruntime' if name=='onnxruntime-gpu' else name
        identity=package_identity({**artifact,'version':parts[1]})
        if not isinstance(identity['sha256'],str) or len(identity['sha256'])!=64 or not isinstance(identity['size'],int):
            raise ValueError('旧离线环境缺少完整包摘要或大小')
        result['main'][name]=identity;result['artifacts'][name]=identity
    if gpu_path and record.get('gpu_lock_sha256') == hashlib.sha256(Path(gpu_path).read_bytes()).hexdigest():
        for row in json.loads(Path(gpu_path).read_text('utf-8'))['wheels']:
            result['main'][row['name']] = package_identity(row)
    return result

def check(names, lock=None, compatible_gpu=False, tensor_device=None):
    errors={}
    modules={'fastapi':'fastapi','pyyaml':'yaml','pydantic':'pydantic','numpy':'numpy','psutil':'psutil',
             'httpx':'httpx','uvicorn':'uvicorn','sounddevice':'sounddevice','soundfile':'soundfile',
             'webrtcvad-wheels':'webrtcvad','aec-audio-processing':'aec_audio_processing',
             'torch':'torch','torchaudio':'torchaudio','funasr':'funasr','onnxruntime':'onnxruntime','sherpa-onnx':'sherpa_onnx',
             'voice-audio-dsp':'voice_audio_dsp'}
    for name in names:
        try:
            installed_version(name,lock,compatible_gpu)
            if name in modules:
                module = importlib.import_module(modules[name])
                if name=='voice-audio-dsp' and not module.available():
                    raise ValueError('保音高音频处理动态库未就绪')
                if name == 'torch' and module.tensor([1., 2.], device='cpu').sum().item() != 3.:
                    raise ValueError('张量运行库的 CPU（中央处理器）计算验证失败')
                if tensor_device == 'cpu' and name == 'torch' and module.version.cuda is not None:
                    raise ValueError('处理器依赖不得使用显卡张量构建')
                if tensor_device == 'cpu' and name in {'torch', 'torchaudio', 'onnxruntime'}:
                    overlay = Path(sys.executable).parent / 'Lib/cpu-packages'
                    if (overlay / name).is_dir() and not Path(module.__file__).resolve().is_relative_to(overlay.resolve()):
                        raise ValueError('处理器依赖未从隔离目录加载')
        except Exception as error: errors[name]=f'{type(error).__name__}: {error}'[:300]
    if lock and not errors:
        try: dynamic_library_closure()
        except Exception as error: errors['dynamic_libraries']=str(error)[:300]
    return {'ready':not errors,'errors':errors}

def main():
    parser=argparse.ArgumentParser();parser.add_argument('operation',choices=['interpreter','reuse','inventory','delta-plan','install','prune','check','gpu','tts-selection','compatibility-repair'])
    parser.add_argument('--reset-devices', action='store_true') # reset_devices（重建环境后回退计算设备）。
    parser.add_argument('--repair-local',action='store_true') # repair_local（只重建内容损坏的已登记依赖）。
    parser.add_argument('--data') # data（当前工作区数据目录）。
    parser.add_argument('--selection') # selection（显式选择的语音后端与模型）。
    parser.add_argument('--compatible-gpu',action='store_true') # compatible_gpu（允许复用项目已验证的显卡组合）。
    parser.add_argument('--gpu-lock')  # gpu_lock（可选显卡依赖锁文件）。
    parser.add_argument('--tensor-device', choices=('cpu', 'cuda'), help='进程使用的处理器或显卡张量依赖')
    parser.add_argument('--lock');parser.add_argument('--names',default='[]');parser.add_argument('--target');parser.add_argument('--wheels',default='[]');parser.add_argument('--model');parser.add_argument('--program');parser.add_argument('--install-root');args=parser.parse_args()
    names=json.loads(args.names)
    if args.operation in {'check', 'inventory', 'reuse', 'gpu'} and (
            set(names) & {'torch', 'torchaudio', 'funasr', 'onnxruntime'} or args.operation == 'gpu'):
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from voice_terminal.tensor_runtime import activate_tensor_runtime
        activate_tensor_runtime(args.tensor_device or ('cuda' if args.gpu_lock or args.operation == 'gpu' or args.compatible_gpu else 'cpu'))
    if args.operation=='compatibility-repair':
        sys.path.insert(0,args.program)
        from voice_terminal.compatibility import repair
        result=repair(Path(args.program),Path(args.data),reset_devices=args.reset_devices)
    elif args.operation=='tts-selection':
        sys.path.insert(0,args.program)
        from voice_terminal.config import load_config
        from voice_terminal.server.product import ProductSettingsStore
        data=Path(args.data)
        local=data/'config/local.yaml'
        config=load_config(Path(args.program)/'config/default.yaml',local if local.is_file() else None)
        store=ProductSettingsStore(path=data/'config/product-settings.json',config=config)
        result=store.save_tts_local_model(json.loads(args.selection))
    elif args.operation=='interpreter': result=interpreter()
    elif args.operation=='reuse': result=reuse(runtime_lock(args.lock,args.gpu_lock),names,Path(args.target))
    elif args.operation=='inventory': result=inventory(names,runtime_lock(args.lock,args.gpu_lock),args.compatible_gpu)
    elif args.operation=='delta-plan':
        effective=runtime_lock(args.lock,args.gpu_lock)
        effective['cpu_wheels']=runtime_lock(args.lock)['wheels']
        result=delta_plan(Path(args.target),effective,names,args.compatible_gpu,legacy_identities(args.target,args.lock,args.gpu_lock),bool(args.gpu_lock),args.repair_local)
    elif args.operation=='install': result=install([Path(path) for path in json.loads(args.wheels)],Path(args.target))
    elif args.operation=='prune': result=prune(Path(args.target),names)
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
    else: result=check(names,runtime_lock(args.lock,args.gpu_lock) if args.lock else None,args.compatible_gpu,args.tensor_device)
    emit({'ok':True,'result':result})

if __name__=='__main__':
    try: main()
    except Exception as error: emit({'ok':False,'error':str(error)[:500]});sys.exit(1)
