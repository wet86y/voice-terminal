"""AnifLive（本地语音）参考桥接的原子替换。"""
import asyncio
import logging
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import numpy as np
import soundfile as sf
from fastapi import FastAPI

from voice_terminal.tts.aniflive_service import install_bridge


def test_reference_bridge_prepares_catalog_and_retains_previous_on_failure(tmp_path, monkeypatch):
    source = Path(__file__).resolve().parents[1] / '.local/runtimes/aniflive-tts/source/src'
    monkeypatch.syspath_prepend(str(source))
    class Bank:
        def __init__(self, *, identity_id='default'):
            self.identity_id = identity_id
            self.values = {}
        def add(self, value):
            self.values[value.id] = value
    class Streamer:
        reference_wav = reference_text = reference_language = None
        _preview_token_hint = None
        def __init__(self):
            self.reference_bank = Bank()
        def _prepare_reference(self, *, reference_id, reference_wav, reference_text, reference_language):
            if reference_text == 'fail':
                raise RuntimeError('准备失败')
            return SimpleNamespace(id=reference_id)
    class Lease:
        def ensure_valid(self):
            pass
    class Service:
        ready = True
        _expression_catalog = None
        def __init__(self):
            self._inference_lock = threading.RLock()
            self._streamer = Streamer()
        def _is_busy(self):
            return False
        def _begin_request(self):
            return Lease()
        def _end_request(self, lease):
            return None
    class Manager:
        def __init__(self):
            self._switch_lock = threading.RLock()
            self._service = Service()
        def activate(self, model_id):
            return model_id
    manager = Manager()
    upstream = SimpleNamespace(SERVICE=manager, MODEL_ID='model', SESSION_MANAGER=SimpleNamespace(has_active=AsyncMock(return_value=False)),
                               RequestError=ValueError, ModelSwitchConflict=ValueError, LOGGER=logging.getLogger(__name__))
    library = tmp_path / 'library'
    assets = library / 'assets'
    assets.mkdir(parents=True)
    for item in ('a' * 32, 'b' * 32):
        sf.write(assets / (item + '.wav'), np.ones(16000 * 3, dtype=np.float32) * .1, 16000)
    app = FastAPI()
    app.state.session_model_lock = asyncio.Lock()
    install_bridge(app, upstream, library, 'secret')
    body = {'model': 'model', 'asset_id': 'a' * 32, 'reference_text': 'base', 'reference_language': 'zh',
            'references': [{'id': 'c' * 32, 'asset_id': 'b' * 32, 'tag': 'soft', 'name': '温柔',
                            'intensity': .5, 'reference_language': 'zh', 'reference_text': 'soft'}]}
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            response = await client.post('/internal/application/reference', json=body, headers={'authorization': 'Bearer secret'})
            assert response.status_code == 200, response.text
            service = manager._service
            bank, catalog = service._streamer.reference_bank, service._expression_catalog
            assert set(bank.values) == {'default', 'c' * 32}
            assert catalog.select(profile='soft', intensity=.5, language='zh').id == 'c' * 32
            body['references'][0]['reference_text'] = 'fail'
            response = await client.post('/internal/application/reference', json=body, headers={'authorization': 'Bearer secret'})
            assert response.status_code == 500
            assert service._streamer.reference_bank is bank
            assert service._expression_catalog is catalog
    asyncio.run(run())
