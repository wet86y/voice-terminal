"""AnifLive（本地语音引擎）的共用非交互配置入口。"""
from pathlib import Path
import argparse
import json
import sys

PROGRAM = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROGRAM))
from voice_terminal.tts.aniflive_install import AnifLiveInstallTask
from voice_terminal.tts.aniflive_transactions import task_guard


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    default_root = PROGRAM / '.local' if (PROGRAM / 'apps/desktop/Cargo.toml').is_file() else PROGRAM.parent
    parser.add_argument('--root', type=Path, default=default_root, help='root（受管安装根目录；自动识别源码和已安装布局）')
    parser.add_argument('--program', type=Path, default=PROGRAM, help='program（本体程序目录）')
    parser.add_argument('--prepare-runtime', action='store_true', help='prepare-runtime（准备固定源码与隔离依赖）')
    parser.add_argument('--preset', help='preset（准备预设模型输入）')
    parser.add_argument('--registered-model', help='registered-model（从受管原始输入修复已登记导入模型）')
    parser.add_argument('--convert', action='store_true', help='convert（执行完整转换、验证与登记）')
    parser.add_argument('--input-spec', type=Path, help='input-spec（兼容权重导入规格文件）')
    parser.add_argument('--native-ack', action='store_true', help='native-ack（由原生宿主确认已释放显卡）')
    parser.add_argument('--cleanup-aliases', action='store_true', help='cleanup-aliases（子任务退出后回收已失活的本任务盘符）')
    args = parser.parse_args()
    try:
        if args.cleanup_aliases:
            from voice_terminal.tts.aniflive_aliases import cleanup_aliases
            print(json.dumps({'ok': True, 'result': cleanup_aliases(args.root)}, ensure_ascii=False))
            return
        with task_guard(args.root):
            task = AnifLiveInstallTask(args.program, args.root)
            task.native_ack = args.native_ack
            if args.prepare_runtime:
                task.prepare_source()
                task.prepare_dependencies()
            inputs = task.prepare_inputs(args.preset) if args.preset else None
            if args.registered_model:
                inputs = task.registered_inputs(args.registered_model)
            name = None
            if args.input_spec:
                inputs, name = task.import_inputs(args.input_spec)
            result = {'runtime_ready': args.prepare_runtime, 'inputs': str(inputs) if inputs else None}
            if args.convert:
                if inputs is None:
                    raise ValueError('转换任务缺少预设或兼容权重输入。')
                result = task.convert(inputs, native_ack=args.native_ack, name=name)
            print(json.dumps({'ok': True, 'result': result, 'installed': [args.preset] if args.convert and args.preset else []}, ensure_ascii=False))
    except Exception as error:
        print(json.dumps({'ok': False, 'error': {'message': str(error), 'installed_resource_ids': []}}, ensure_ascii=False))
        raise SystemExit(1)


if __name__ == '__main__':
    main()
