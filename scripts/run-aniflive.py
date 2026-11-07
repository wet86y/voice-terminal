"""在专用隔离依赖中执行固定 AnifLive（本地语音引擎）命令。"""
from pathlib import Path
import argparse
import runpy
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from voice_terminal.tts.aniflive_environment import configure


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT / ".local", help="root（受管安装目录）")
    parser.add_argument("--role", choices=("inference", "export"), required=True,
                        help="role（依赖角色）：推理或导出")
    parser.add_argument("--script", type=Path, help="script（内部导出子进程脚本）")
    parser.add_argument("--module", choices=("voice_terminal.tts.aniflive_worker", "voice_terminal.tts.aniflive_service"),
                        help="module（应用受管子进程入口）")
    parser.add_argument("arguments", nargs=argparse.REMAINDER, help="arguments（上游命令参数）")
    args = parser.parse_args()
    configure(args.root, ROOT, args.role)
    arguments = args.arguments[1:] if args.arguments[:1] == ["--"] else args.arguments
    if args.script is not None:
        source = (args.root / "runtimes/aniflive-tts/source/minimal_inference").resolve()
        script = args.script.resolve()
        if args.role != "export" or script.parent != source or script.name not in {"export_onnx.py", "onnx_to_fp16.py"}:
            raise ValueError("仅允许执行固定兼容源码中的导出脚本。")
        if script.name == 'export_onnx.py' and '--export-stage' not in arguments:
            from voice_terminal.tts.aniflive_export import export_stages
            export_stages(args.root, ROOT, script, arguments)
            return
        sys.argv = [str(script), *arguments]
        try:
            runpy.run_path(str(script), run_name="__main__")
        except Exception as error:
            from voice_terminal.tts.aniflive_export import memory_failure
            if script.name == 'export_onnx.py' and '--export-stage' in arguments and memory_failure(error):
                import json
                output = Path(arguments[arguments.index('--output_dir')+1])
                (output/'stage-result.json').write_text(json.dumps({
                    'kind':'cpu_memory', 'stage':arguments[arguments.index('--export-stage')+1],
                    'message':str(error), 'fields':{'kind':'CPU（中央处理器）内存分配失败',
                    'stage':'导出阶段','message':'原始错误'}}), encoding='utf-8')
            raise
    else:
        # 上游转换器使用 sys.executable（解释器）派生导出进程；必须再次进入同一隔离入口。
        if args.role == "export":
            from aniflive_tts import converter
            original = converter._run
            def isolated_run(command, *, cwd, env):
                if command[0] != sys.executable:
                    raise RuntimeError("上游导出命令的解释器身份不匹配。")
                command = [sys.executable, "-S", "-B", "-X", "utf8", str(Path(__file__).resolve()),
                           "--root", str(args.root), "--role", "export", "--script", command[1], "--", *command[2:]]
                return original(command, cwd=cwd, env=env)
            converter._run = isolated_run
        sys.argv = ["aniflive-tts", *arguments]
        runpy.run_module(args.module or "aniflive_tts.__main__", run_name="__main__")


if __name__ == "__main__":
    main()
