"""Test context（测试上下文）：可嵌入解释器的子进程显式加载源码，无环境副本。"""
from pathlib import Path
import json
import subprocess
import asyncio
import sys
import pytest

ROOT = next(p for p in Path(__file__).resolve().parents if (p / 'pyproject.toml').is_file())


@pytest.fixture(autouse=True)
def embedded_subprocess_imports(monkeypatch):
    """与受管后端入口相同：显式模块路径，不能依赖被隔离的 PYTHONPATH（模块搜索变量）。"""
    original = subprocess.Popen
    prefix = 'import sys;sys.dont_write_bytecode=True;sys.path[:0]=' + repr([str(ROOT), str(ROOT / '.local/tools/testing')]) + ';'
    def bootstrap(command):
        if isinstance(command, (list, tuple)) and command and Path(str(command[0])).resolve() == Path(sys.executable).resolve():
            command = list(command)
            if '-B' not in command:
                command.insert(1, '-B')
            if '-c' in command:
                index = command.index('-c') + 1
                command[index] = prefix + command[index]
            elif '-m' in command:
                index = command.index('-m')
                module, arguments = command[index + 1], command[index + 2:]
                command = command[:index] + ['-c', prefix + 'import runpy;sys.argv=' + repr([module, *arguments]) + ';runpy.run_module(' + repr(module) + ',run_name="__main__")']
            else:
                scripts = [i for i, value in enumerate(command[1:], 1) if str(value).endswith('.py')]
                if scripts:
                    index = scripts[0]
                    file, arguments = command[index], command[index + 1:]
                    command = command[:index] + ['-c', prefix + 'import runpy;sys.argv=' + repr([file, *arguments]) + ';runpy.run_path(' + repr(str(file)) + ',run_name="__main__")']
        return command
    def spawn(command, *args, **kwargs):
        return original(bootstrap(command), *args, **kwargs)
    monkeypatch.setattr(subprocess, 'Popen', spawn)
    asynchronous = asyncio.create_subprocess_exec
    async def spawn_async(*command, **kwargs):
        return await asynchronous(*bootstrap(command), **kwargs)
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', spawn_async)
    from voice_terminal.application.windows import ManagedProcess
    managed = ManagedProcess.__init__
    def spawn_managed(self, command, *args, **kwargs):
        managed(self, bootstrap(command), *args, **kwargs)
    monkeypatch.setattr(ManagedProcess, '__init__', spawn_managed)
