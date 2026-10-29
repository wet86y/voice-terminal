"""构建锁定的语音音频处理预编译包；不安装或覆盖任何现用运行组件。"""
from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import urllib.request
import zipfile


ROOT = Path(__file__).resolve().parents[1]
VERSION = "2.4.0"
SOURCE_URL = f"https://www.surina.net/soundtouch/soundtouch-{VERSION}.tar.gz"
SOURCE_SHA256 = "3dda3c9ab1e287f15028c010a66ab7145fa855dfa62763538f341e70b4d10abd"
PACKAGE_VERSION = "0.1.0"


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build(*, update_lock=False, cmake=None, ninja=None):
    work = ROOT / ".local/build/speech-dsp" / VERSION
    cache = ROOT / ".local/cache/speech-dsp"
    work.mkdir(parents=True, exist_ok=True)
    cache.mkdir(parents=True, exist_ok=True)
    archive = cache / f"soundtouch-{VERSION}.tar.gz"
    if not archive.is_file() or digest(archive) != SOURCE_SHA256:
        with urllib.request.urlopen(SOURCE_URL, timeout=60) as response:
            content = response.read(16 * 1024 * 1024 + 1)
        if len(content) > 16 * 1024 * 1024 or hashlib.sha256(content).hexdigest() != SOURCE_SHA256:
            raise ValueError("SoundTouch（音频处理库）源包大小或摘要不匹配。")
        archive.write_bytes(content)
    source_root = work / "source"
    if not source_root.is_dir():
        source_root.mkdir()
        with tarfile.open(archive) as source:
            for entry in source.getmembers():
                target = (source_root / entry.name).resolve()
                if not target.is_relative_to(source_root.resolve()):
                    raise ValueError("源包存在越界路径。")
            source.extractall(source_root, filter="data")
    source = next(path.parent for path in source_root.rglob("CMakeLists.txt") if path.parent.name == "soundtouch")
    # 上游版本资源仅使用Windows SDK（系统开发包）常量，无需安装MFC（界面框架）。
    resource_path = source / 'source/SoundTouchDLL/SoundTouchDLL.rc'
    resource = resource_path.read_text(encoding='utf-8')
    patched_resource = resource.replace('#include "afxres.h"', '#include <winres.h>')
    if patched_resource != resource:
        resource_path.write_text(patched_resource, encoding='utf-8')
    vs_root = Path(os.environ.get("VSINSTALLDIR", "D:/Program Files/Microsoft Visual Studio/18/Community"))
    cmake = cmake or shutil.which("cmake") or str(vs_root / "Common7/IDE/CommonExtensions/Microsoft/CMake/CMake/bin/cmake.exe")
    ninja = ninja or shutil.which("ninja") or str(vs_root / "Common7/IDE/CommonExtensions/Microsoft/CMake/Ninja/ninja.exe")
    build_dir = work / "build"
    log_path = work / "build.log"
    options = ["-G", "Ninja", "-DCMAKE_BUILD_TYPE=Release", "-DSOUNDTOUCH_DLL=ON",
               "-DINTEGER_SAMPLES=OFF", "-DOPENMP=OFF", "-DSOUNDSTRETCH=OFF",
               "-DCMAKE_CXX_FLAGS=/DWIN32 /D_WINDOWS /EHsc /Brepro",
               "-DCMAKE_SHARED_LINKER_FLAGS=/Brepro",
               "-DCMAKE_MAKE_PROGRAM=" + str(ninja)]
    with log_path.open("w", encoding="utf-8") as log:
        subprocess.run([str(cmake), "-S", str(source), "-B", str(build_dir), *options],
                       stdout=log, stderr=subprocess.STDOUT, check=True)
        subprocess.run([str(cmake), "--build", str(build_dir), "--target", "SoundTouchDLL"],
                       stdout=log, stderr=subprocess.STDOUT, check=True)
    dlls = list(build_dir.rglob("SoundTouchDLL.dll"))
    if len(dlls) != 1:
        raise ValueError("没有唯一的SoundTouch（音频处理库）动态库。")
    wheelhouse = ROOT / ".local/build/runtime-lock"
    wheelhouse.mkdir(parents=True, exist_ok=True)
    filename = f"voice_audio_dsp-{PACKAGE_VERSION}-py3-none-win_amd64.whl"
    wheel = wheelhouse / filename
    info = f"voice_audio_dsp-{PACKAGE_VERSION}.dist-info"
    files = {
        "voice_audio_dsp/__init__.py": (ROOT / "packages/voice-audio-dsp/voice_audio_dsp/__init__.py").read_bytes(),
        "voice_audio_dsp/soundtouch.dll": dlls[0].read_bytes(),
        "voice_audio_dsp/source/soundtouch-2.4.0.tar.gz": archive.read_bytes(),
        "voice_audio_dsp/source/windows-sdk-resource.patch": b'--- a/source/SoundTouchDLL/SoundTouchDLL.rc\n+++ b/source/SoundTouchDLL/SoundTouchDLL.rc\n@@ -10 +10 @@\n-#include "afxres.h"\n+#include <winres.h>\n',
        f"{info}/METADATA": (f"Metadata-Version: 2.1\nName: voice-audio-dsp\nVersion: {PACKAGE_VERSION}\n"
                             "Summary: Locked SoundTouch streaming speech tempo bridge\nRequires-Python: >=3.12\n"
                             "License: LGPL-2.1\n\nIncludes corresponding SoundTouch source and license.\n").encode(),
        f"{info}/WHEEL": b"Wheel-Version: 1.0\nGenerator: voice-terminal-build\nRoot-Is-Purelib: false\nTag: py3-none-win_amd64\n",
        f"{info}/top_level.txt": b"voice_audio_dsp\n",
        f"{info}/SOURCE.json": json.dumps({"url": SOURCE_URL, "sha256": SOURCE_SHA256,
                                           "version": VERSION, "options": options}, indent=2).encode(),
    }
    license_path = next(path for path in source.rglob("COPYING*") if path.is_file())
    files[f"{info}/licenses/COPYING.txt"] = license_path.read_bytes()
    record = io.StringIO(newline="")
    writer = csv.writer(record, lineterminator="\n")
    for path, data in sorted(files.items()):
        fingerprint = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).decode().rstrip("=")
        writer.writerow([path, "sha256=" + fingerprint, len(data)])
    writer.writerow([f"{info}/RECORD", "", ""])
    files[f"{info}/RECORD"] = record.getvalue().encode()
    with zipfile.ZipFile(wheel, "w", zipfile.ZIP_DEFLATED) as output:
        for name, data in sorted(files.items()):
            entry = zipfile.ZipInfo(name, date_time=(2026, 10, 28, 0, 0, 0))
            entry.compress_type = zipfile.ZIP_DEFLATED
            output.writestr(entry, data)
    candidate = work / 'candidate'
    with zipfile.ZipFile(wheel) as output:
        output.extractall(candidate)
    probe = ('import sys;sys.path.insert(0,sys.argv[1]);'
             'import voice_audio_dsp;assert voice_audio_dsp.available();'
             'p=voice_audio_dsp.TempoProcessor(16000,speed=0.8);'
             'audio=p.push(bytes(32000))+p.finish();p.close();'
             'assert len(audio)==40000,(len(audio),40000)')
    subprocess.run([sys.executable, '-B', '-c', probe, str(candidate)], check=True)
    row = {"name": "voice-audio-dsp", "version": PACKAGE_VERSION, "file": filename, "url": "",
           "size": wheel.stat().st_size, "sha256": digest(wheel), "groups": ["voice-core"],
           "bundled": "tools/wheels/" + filename, "provenance": "verified-soundtouch-source-build"}
    metadata = {"source_sha256": SOURCE_SHA256, "dll_sha256": digest(dlls[0]), "wheel": row}
    (work / "build.json").write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    if update_lock:
        path = ROOT / "config/runtime/voice-runtime.lock.json"
        lock = json.loads(path.read_text(encoding="utf-8"))
        lock["groups"]["voice-core"] = sorted(set(lock["groups"]["voice-core"]) | {row["name"]})
        lock["wheels"] = [item for item in lock["wheels"] if item["name"] != row["name"]] + [row]
        lock["wheels"].sort(key=lambda item: item["name"])
        path.write_text(json.dumps(lock, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps(metadata, ensure_ascii=False))
    return wheel


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--update-lock", action="store_true", help="更新语音运行组件的依赖锁")
    parser.add_argument("--cmake", help="现有CMake（构建工具）路径")
    parser.add_argument("--ninja", help="现有Ninja（构建执行器）路径")
    args = parser.parse_args()
    build(update_lock=args.update_lock, cmake=args.cmake, ninja=args.ninja)
