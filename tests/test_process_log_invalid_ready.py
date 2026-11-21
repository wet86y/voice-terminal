"""Invalid readiness（非法就绪消息）仍保留故障诊断，不被当作服务就绪。"""
import asyncio
import json
from types import SimpleNamespace

import pytest

from voice_terminal.providers.aniflive_server import AnifLiveTtsServer


def test_invalid_ready_identity_is_logged_before_failure(tmp_path):
    async def scenario():
        line = 'ANIFLIVE_READY=' + json.dumps({"nonce": "wrong", "pid": 42, "port": 1234}) + '\n'
        async def stdout():
            yield line.encode()
        server = object.__new__(AnifLiveTtsServer)
        server.local, server._state = tmp_path, 'loading'
        server._process = SimpleNamespace(pid=42, stdout=stdout())
        ready = asyncio.get_running_loop().create_future()
        with pytest.raises(RuntimeError, match="身份不匹配"):
            await server._read(ready, 'expected')
        with pytest.raises(RuntimeError, match="身份不匹配"):
            ready.result()
        assert line.strip() in (tmp_path / 'cache/logs/aniflive-service-expected.log').read_text('utf-8')
    asyncio.run(scenario())
