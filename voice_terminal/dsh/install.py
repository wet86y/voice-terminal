"""Plugin install（插件安装）：共用生产运行目录和用户状态，不创建源码宿主。"""
import argparse
import asyncio
import os
from ..paths import AppPaths
from ..config import load_config
from .runtime import DshRuntimeManager


async def main():
    parser = argparse.ArgumentParser(description='验证生产宿主并同步受管插件')
    parser.add_argument('--validate-only', action='store_true', help='validate-only（仅校验版本）')
    args = parser.parse_args()
    paths = AppPaths.resolve()
    os.environ.update(paths.environment())
    paths.prepare()
    config = load_config(paths.default_config)
    manager = DshRuntimeManager(config.dsh)
    if args.validate_only:
        from ..distribution import verify
        import json
        runtime = manager.release_runtime
        verify(runtime, json.loads((runtime / 'manifest.json').read_text(encoding='utf-8-sig')))
    else:
        await manager._validate_installation()
    print('生产宿主与统一插件路径验证通过。')


if __name__ == '__main__':
    asyncio.run(main())
