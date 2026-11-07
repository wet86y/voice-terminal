"""AnifLive（本地合成）服务的受管进程、就绪身份和卸载生命周期。"""
from __future__ import annotations
import asyncio
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys

from ..process_host import interpreter_command
from ..tts.aniflive_models import AnifLiveModelRegistry
from ..tts.aniflive_native import contained


class AnifLiveTtsServer:
    def __init__(self, local: Path, program: Path, *, local_model: str, library_root: Path,
                 revision: str | None = None, data: Path | None = None):
        self.local, self.program = Path(local).absolute(), Path(program).absolute()
        self.registry = AnifLiveModelRegistry(self.local, data)
        self.local_model, self.revision = local_model, revision
        self.library = contained(self.local, Path(library_root).absolute())
        self._process = None
        self.created_at = None  # created_at（进程实际创建时间），与编号共同确认资源归属。
        self._reader = None
        self._lock = asyncio.Lock()
        self._state = 'unloaded'
        self.error = None
        self.url = ''
        self.token = ''

    @property
    def pid(self):
        return self._process.pid if self._process and self._process.returncode is None else None

    @property
    def loaded_model(self):
        return self.local_model if self.pid and self._state == 'ready' else None

    def model_status(self):
        rows = self.registry.models()
        for row in rows:
            row['loaded'] = row['id'] == self.loaded_model
        return {'local_model': self.local_model, 'model_id': self.local_model, 'revision': self.revision,
                'loaded_model': self.loaded_model, 'models': rows, 'sample_rate': 32000,
                'voice_profile': 'default', 'service_version': '1.5.0b1',
                'load_state': self._state if self.pid or self._state != 'ready' else 'error', 'error': self.error}

    async def ensure_ready(self):
        async with self._lock:
            if self.loaded_model:
                return
            await self._stop()
            package, self.revision, _ = self.registry.resolve(self.local_model, self.revision, verify_hashes=True)
            self.library.mkdir(parents=True, exist_ok=True)
            token_path = contained(self.local, self.library / 'reference-control.token')
            try:
                with token_path.open('x', encoding='ascii') as stream:
                    stream.write(secrets.token_hex(32))
            except FileExistsError:
                pass
            self.token = token_path.read_text(encoding='ascii').strip()
            if len(self.token) != 64:
                raise ValueError('现有本机语音凭据无效。')
            nonce = secrets.token_hex(16)
            command = interpreter_command(sys.executable, '-S', '-B', '-X', 'utf8',
                str(self.program / 'scripts/run-aniflive.py'), '--root', str(self.local), '--role', 'inference',
                '--module', 'voice_terminal.tts.aniflive_service', '--', '--root', str(self.local),
                '--package', str(package), '--library', str(self.library), '--nonce', nonce)
            self._state, self.error = 'loading', None
            try:
                self._process = await asyncio.create_subprocess_exec(*command, cwd=self.program,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                    env={**os.environ, 'VOICE_RESOURCE_ROLE': 'voice'},
                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                import psutil
                self.created_at = psutil.Process(self._process.pid).create_time()
                ready = asyncio.get_running_loop().create_future()
                self._reader = asyncio.create_task(self._read(ready, nonce), name='aniflive-service-log')
                async with asyncio.timeout(240):
                    self.url = await ready
                self._state = 'ready'
            except BaseException as error:
                self.error = str(error)[:300]
                await self._stop()
                self._state = 'error'
                raise

    async def _read(self, ready, nonce):
        # 每次进程启动保留独立诊断，切换失败后恢复旧模型不能覆盖失败现场。
        log = self.local / 'cache/logs' / f'aniflive-service-{nonce}.log'
        log.parent.mkdir(parents=True, exist_ok=True)
        try:
            with log.open('w', encoding='utf-8') as stream:
                async for line in self._process.stdout:
                    text = line.decode('utf-8', errors='replace')
                    stream.write(text)
                    stream.flush()
                    if text.startswith('ANIFLIVE_READY=') and not ready.done():
                        value = json.loads(text.removeprefix('ANIFLIVE_READY='))
                        if value['nonce'] != nonce or value['pid'] != self._process.pid or type(value['port']) is not int or not 1 <= value['port'] <= 65535:
                            raise RuntimeError('本地语音服务就绪身份不匹配。')
                        ready.set_result(f'http://127.0.0.1:{value["port"]}')
            if not ready.done():
                ready.set_exception(RuntimeError('本地语音服务在就绪前退出，详见服务日志。'))
            elif self._state == 'ready':
                self._state, self.error = 'error', '本地语音服务异常退出。'
        except BaseException as error:
            if not ready.done():
                ready.set_exception(error)
            raise

    async def switch_model(self, model_id, revision=None):
        previous, previous_revision = self.local_model, self.revision
        self.registry.resolve(model_id, revision)
        await self.close()
        self.local_model, self.revision = model_id, revision
        try:
            await self.ensure_ready()
        except BaseException:
            self.local_model, self.revision = previous, previous_revision
            await self.ensure_ready()
            raise

    async def _stop(self):
        cleanup = asyncio.create_task(self._stop_owned(), name='aniflive-process-cleanup')
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            await cleanup
            raise

    async def _stop_owned(self):
        process, reader = self._process, self._reader
        if process is not None and process.returncode is None:
            try:
                process.terminate()
            except ProcessLookupError:
                pass
            try:
                async with asyncio.timeout(10):
                    await process.wait()
            except TimeoutError:
                process.kill()
                await process.wait()
        if reader is not None:
            await asyncio.gather(reader, return_exceptions=True)
        self._process = self._reader = None
        self.created_at = None
        self.url = ''

    async def close(self):
        async with self._lock:
            await self._stop()
            self._state = 'unloaded'
