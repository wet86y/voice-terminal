"""预设模型完成转换后，独占登记来源参考并返回组件任务结果。"""
from pathlib import Path
import argparse
import json
import sys
import time

PROGRAM = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROGRAM))
from voice_terminal.tts.aniflive_import import AnifLiveImportService
from voice_terminal.tts.voices import ModelVoiceLibraries


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True, help='root（受管本机资源目录）')
    parser.add_argument('--data', type=Path, required=True, help='data（持久数据目录）')
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--model', help='model（预设基础模型编号）')
    source.add_argument('--task', help='task（已转换导入任务编号）')
    args = parser.parse_args()
    try:
        owner = ModelVoiceLibraries(args.data / 'tts')
        service = AnifLiveImportService(args.root, args.data, voice_libraries=owner)
        task = service.task(args.task) if args.task else service.start_existing_references(args.model, {})
        previous = None
        while True:
            task = service.task(task['task_id'])
            status = (task.get('phase'), task.get('progress'))
            if status != previous:
                print(json.dumps({'phase': status[0] or '正在登记预设音色参考',
                                  'completed_bytes': status[1] or 0, 'total_bytes': 100}, ensure_ascii=False), flush=True)
                previous = status
            if task['status'] in {'complete', 'failed', 'reference_failed', 'cancelled'} or (
                args.task and task.get('reference_registration')
            ):
                break
            time.sleep(.5)
        if task['status'] != 'complete' and not (
            args.task and task['status'] == 'registering_references' and task.get('reference_registration')
        ):
            raise RuntimeError(task.get('error') or '参考资料登记未完成。')
        if args.task:
            receipt = json.loads((args.data / 'tts/aniflive/reference-imports' / args.task /
                                  'reference-registration.json').read_text(encoding='utf-8'))
            if receipt.get('task_id') != args.task or receipt.get('model_id') != task.get('model_id') or (
                receipt.get('registration') != task.get('reference_registration')
            ):
                raise RuntimeError('参考资料登记收据与当前任务不匹配。')
        print(json.dumps({'ok': True, 'result': task['reference_registration'], 'installed': [],
                          'fields': {'result': '参考就绪、待完善数量与音色版本'}}, ensure_ascii=False), flush=True)
    except Exception as error:
        print(json.dumps({'ok': False, 'error': {'message': str(error), 'installed_resource_ids': []}}, ensure_ascii=False), flush=True)
        raise SystemExit(1)


if __name__ == '__main__':
    main()
