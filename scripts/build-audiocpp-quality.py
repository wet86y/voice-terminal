"""原生质量补丁隔离构建：复用已验证的依赖对象，只编译补丁涉及的 C++ 文件。"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]
CHANGED = ("app/server/runtime.cpp", "app/server/parallel_runtime.cpp",
           "src/models/voxcpm2/session.cpp", "src/models/voxcpm2/minicpm.cpp",
           "src/models/voxcpm2/audiovae.cpp")


def build(source: Path, output: Path, build_dir: Path | None = None):
    source, output = source.resolve(), output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    build_dir = (build_dir or source / "build/windows-cuda-release").resolve()
    lines = (build_dir / "build.ninja").read_text("utf-8").splitlines()
    vc = Path("D:/Program Files/Microsoft Visual Studio/18/Community/VC/Tools/MSVC/14.51.36231")
    sdk = Path("C:/Program Files (x86)/Windows Kits/10")
    cuda = Path("D:/Program Files (x86)/NVIDIA CUDA")
    environment = os.environ.copy()
    environment["INCLUDE"] = ";".join([str(vc / "include")] + [str(sdk / "Include/10.0.26100.0" / p) for p in ("ucrt", "shared", "um", "winrt")])
    environment["LIB"] = ";".join([str(vc / "lib/x64"), str(sdk / "Lib/10.0.26100.0/ucrt/x64"), str(sdk / "Lib/10.0.26100.0/um/x64"), str(cuda / "lib/x64")])

    def rule(prefix):
        index = next(i for i, line in enumerate(lines) if line.startswith(prefix))
        values = {}
        for line in lines[index + 1:]:
            if not line.startswith("  "):
                break
            key, value = line.strip().split(" = ", 1)
            values[key] = value
        return lines[index], values

    replacements = {}
    commands = []
    with (output / "build.log").open("w", encoding="utf-8") as log:
        for relative in CHANGED:
            suffix = relative.replace("/", "\\") + ".obj"
            line = next(line for line in lines if line.startswith("build ") and line.split(": ")[0].endswith(suffix))
            target = line.split(": ", 1)[0][6:]
            _, values = rule("build " + target + ":")
            snapshot = output / "source" / relative
            snapshot.parent.mkdir(parents=True, exist_ok=True)
            snapshot.write_bytes((source / relative).read_bytes())
            obj = output / (relative.replace("/", "-") + ".obj")
            definitions = values["DEFINES"].replace('\\"', '"').split()
            includes = ["-I" + str(source / "include")]
            includes += ["-I" + part.rstrip() for part in values["INCLUDES"].split("-I")[1:]]
            args = [str(vc / "bin/Hostx64/x64/cl.exe"), "/nologo", "/TP", "/utf-8", "/EHsc", "/O2", "/Ob2", "/DNDEBUG", "-std:c++17", "-MD", "/permissive-", "/openmp:experimental"]
            args += definitions + includes + ["-I" + str((source / relative).parent), "/Fo" + str(obj), "/Fd" + str(output / "compile.pdb"), "/c", str(snapshot)]
            commands.append(args)
            subprocess.run(args, cwd=build_dir, env=environment, stdout=log, stderr=subprocess.STDOUT, check=True)
            replacements[target] = str(obj)
            print("compiled（已编译）", relative, flush=True)
        library_line, _ = rule("build engine_runtime.lib:")
        objects = library_line.split(" ", 3)[3].split(" ||")[0].split()
        response = output / "engine.rsp"
        response.write_text("\n".join('"' + replacements.get(p, str(build_dir / p)) + '"' for p in objects), encoding="utf-16")
        subprocess.run([str(vc / "bin/Hostx64/x64/lib.exe"), "/nologo", "/OUT:" + str(output / "engine_runtime.lib"), "@" + str(response)], cwd=build_dir, env=environment, stdout=log, stderr=subprocess.STDOUT, check=True)
        server_line, values = rule("build bin\\audiocpp_server.exe:")
        objects = server_line.split(" ", 3)[3].split(" |")[0].split()
        libraries = values["LINK_LIBRARIES"].replace("engine_runtime.lib", '"' + str(output / "engine_runtime.lib") + '"', 1)
        response = output / "server.rsp"
        response.write_text("\n".join('"' + replacements.get(p, str(build_dir / p)) + '"' for p in objects) + "\n" + libraries, encoding="utf-16")
        binary = output / "audiocpp_server.exe"
        subprocess.run([str(vc / "bin/Hostx64/x64/link.exe"), "/nologo", "/MACHINE:X64", "/INCREMENTAL:NO", "/SUBSYSTEM:CONSOLE", "/MANIFEST:EMBED", "/MANIFESTINPUT:" + str(source / "tools/cmake/audiocpp_utf8.manifest"), "/OUT:" + str(binary), "/PDB:" + str(output / "server.pdb"), "/IMPLIB:" + str(output / "server.lib"), "@" + str(response)], cwd=build_dir, env=environment, stdout=log, stderr=subprocess.STDOUT, check=True)
    report = {"compile_commands": commands, "server_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
              "fields": {"compile_commands": "补丁源码的完整编译参数", "server_sha256": "隔离构建程序完整摘要"}}
    (output / "build.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(report["server_sha256"], flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-repo", type=Path, required=True, help="source-repo（已应用质量补丁且依赖已构建的原生源码）")
    parser.add_argument("--output", type=Path, default=ROOT / ".local/cache/reports/tts-quality-build", help="output（隔离构建目录）")
    parser.add_argument("--build-dir", type=Path, help="build-dir（已验证的依赖构建目录，可与补丁源码分离）")
    args = parser.parse_args()
    build(args.source_repo, args.output, args.build_dir)
