"""应用托管的 AnifLive（本地语音）服务桥接，仅绑定当前已验证模型版本。"""
from __future__ import annotations
import argparse
import asyncio
from contextlib import asynccontextmanager
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import re
import socket

from .aniflive_native import contained


def install_bridge(app, upstream, library: Path, token: str):
    """固定版本私有接口集中在此桥接层；参考提取成功后才替换正式参考。"""
    from fastapi import Request
    from fastapi.responses import JSONResponse
    from starlette.concurrency import run_in_threadpool

    @app.middleware('http')
    async def authenticate(request: Request, call_next):
        if not hmac.compare_digest(request.headers.get('authorization', '').encode(), ('Bearer ' + token).encode()):
            return JSONResponse({'detail': '本地语音服务身份校验失败。'}, status_code=403)
        return await call_next(request)

    manager = upstream.SERVICE
    original_activate = manager.activate
    def activate_current(model_id):
        if model_id != upstream.MODEL_ID:
            raise upstream.RequestError('模型切换只能由应用托管进程执行。')
        return original_activate(model_id)
    manager.activate = activate_current
    state = {'service': None, 'default': None, 'key': None}

    def apply(values):
        with manager._switch_lock:
            service = manager._service
            if values.get('model') != upstream.MODEL_ID or not service.ready:
                raise ValueError('当前模型版本与参考请求不一致。')
            tokens = values.get('stream_chunk_tokens', 48)
            if type(tokens) is not int or tokens not in {48, 96}:
                raise ValueError('后续分块只接受 48 或 96 个标记。')
            text, language, asset = values.get('reference_text', ''), values.get('reference_language', 'zh'), values.get('asset_id')
            if language not in {'zh', 'yue', 'en', 'ja', 'ko'} or not isinstance(text, str) or len(text) > 2000:
                raise ValueError('参考录音语言或转写无效。')
            references = values.get('references', [])
            if not isinstance(references, list) or len(references) > 100 or (references and asset is None):
                raise ValueError('情绪参考集合无效。')
            path = None
            if asset is not None:
                if not isinstance(asset, str) or not re.fullmatch('[a-f0-9]{32}', asset) or not text.strip():
                    raise ValueError('参考素材编号或转写无效。')
                path = contained(library, library / 'assets' / (asset + '.hifi.wav'))
                if not path.is_file():
                    path = contained(library, library / 'assets' / (asset + '.wav'))
                import soundfile
                info = soundfile.info(str(path))
                if info.channels != 1 or not 3 <= info.duration <= 10:
                    raise ValueError('参考录音必须为 3–10 秒单声道音频。')
            prepared_rows = []
            ids, keys = set(), set()
            for row in references:
                if not isinstance(row, dict) or set(row) != {'id', 'asset_id', 'tag', 'name', 'intensity', 'reference_language', 'reference_text'}:
                    raise ValueError('情绪参考字段无效。')
                ref_id, ref_asset, tag = row['id'], row['asset_id'], row['tag']
                ref_text, ref_lang, intensity = row['reference_text'], row['reference_language'], row['intensity']
                if (not isinstance(ref_id, str) or not re.fullmatch('[a-f0-9]{32}', ref_id) or ref_id in ids
                        or not isinstance(ref_asset, str) or not re.fullmatch('[a-f0-9]{32}', ref_asset)
                        or not isinstance(tag, str) or tag in {'default', 'base_internal'} or not re.fullmatch('[a-z0-9][a-z0-9_-]{0,63}', tag)
                        or not isinstance(row['name'], str) or not 1 <= len(row['name']) <= 80
                        or not isinstance(ref_text, str) or not 1 <= len(ref_text.strip()) <= 2000
                        or ref_lang not in {'zh', 'yue', 'en', 'ja', 'ko'}
                        or isinstance(intensity, bool) or not isinstance(intensity, (int, float))
                        or not math.isfinite(intensity) or not 0 <= intensity <= 1):
                    raise ValueError('情绪参考编号、标记、转写或强度无效。')
                if (tag, ref_lang, intensity) in keys:
                    raise ValueError('情绪参考标记、语言与强度重复。')
                ids.add(ref_id)
                keys.add((tag, ref_lang, intensity))
                ref_path = contained(library, library / 'assets' / (ref_asset + '.hifi.wav'))
                if not ref_path.is_file():
                    ref_path = contained(library, library / 'assets' / (ref_asset + '.wav'))
                import soundfile
                info = soundfile.info(str(ref_path))
                if info.channels != 1 or not 3 <= info.duration <= 10:
                    raise ValueError('情绪参考必须为 3–10 秒单声道音频。')
                prepared_rows.append((row, ref_path, hashlib.sha256(ref_path.read_bytes()).hexdigest()))
            key = hashlib.sha256(json.dumps([hashlib.sha256(path.read_bytes()).hexdigest() if path else None,
                                             text.strip() if path else '', language if path else '', tokens,
                                             [(row, digest) for row, _, digest in prepared_rows]],
                                            sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            if state['service'] is service and state['key'] == key:
                return {'changed': False}
            if service._is_busy():
                raise upstream.ModelSwitchConflict('正在合成语音，请等待结束。')
            lease = service._begin_request()
            try:
                with service._inference_lock:
                    streamer = service._streamer
                    if state['service'] is not service:
                        state.update(service=service, key=None, default=(streamer.reference_bank, streamer.reference_wav,
                            streamer.reference_text, streamer.reference_language, service._expression_catalog))
                    if path is None:
                        bank, wav, transcript, lang, catalog = state['default']
                    else:
                        prepared = streamer._prepare_reference(reference_id='default', reference_wav=path,
                                                                reference_text=text.strip(), reference_language=language)
                        bank = type(streamer.reference_bank)(identity_id='default')
                        bank.add(prepared)
                        wav, transcript, lang, catalog = path, text.strip(), language, None
                        if prepared_rows:
                            from aniflive_tts.expression import ExpressionCatalog, ReferenceDescriptor, ConditioningPolicy
                            descriptors = [ReferenceDescriptor(id='default', emotion='base_internal', intensity=0.0,
                                reference_audio=path, reference_text=transcript, reference_language=lang,
                                manual_verified=False)]
                            for row, ref_path, _ in prepared_rows:
                                ref = streamer._prepare_reference(reference_id=row['id'], reference_wav=ref_path,
                                    reference_text=row['reference_text'].strip(), reference_language=row['reference_language'])
                                bank.add(ref)
                                descriptors.append(ReferenceDescriptor(id=row['id'], emotion=row['tag'],
                                    intensity=float(row['intensity']), reference_audio=ref_path,
                                    reference_text=row['reference_text'].strip(), reference_language=row['reference_language'],
                                    manual_verified=False))
                            catalog = ExpressionCatalog(default_profile='base_internal', profiles=tuple(descriptors),
                                                        preferred_policy=ConditioningPolicy.SEMANTIC_STYLE)
                    lease.ensure_valid()
                    streamer._application_chunk_tokens = tokens
                    streamer.reference_bank = bank
                    streamer.reference_wav, streamer.reference_text, streamer.reference_language = wav, transcript, lang
                    streamer._preview_token_hint = None
                    service._expression_catalog = catalog
                    state['key'] = key
            finally:
                cleanup_error = service._end_request(lease)
                if cleanup_error is not None:
                    raise cleanup_error
            return {'changed': True}

    async def reference(request: Request):
        body = bytearray()
        async for block in request.stream():
            body.extend(block)
            if len(body) > 256 * 1024:
                return JSONResponse({'detail': '参考请求超出大小限制。'}, status_code=413)
        try:
            values = json.loads(body)
            if not isinstance(values, dict) or set(values) - {'model', 'asset_id', 'reference_text', 'reference_language', 'stream_chunk_tokens', 'references'}:
                raise ValueError('参考请求字段无效。')
            async with app.state.session_model_lock:
                if await upstream.SESSION_MANAGER.has_active():
                    return JSONResponse({'detail': '当前语音会话尚未结束。'}, status_code=409)
                task = asyncio.create_task(run_in_threadpool(apply, values))
                try:
                    return await asyncio.shield(task)
                except asyncio.CancelledError:
                    await task
                    raise
        except upstream.RequestError as error:
            return JSONResponse({'detail': str(error)}, status_code=error.status_code)
        except (ValueError, OSError) as error:
            return JSONResponse({'detail': '参考素材无效：' + type(error).__name__}, status_code=422)
        except Exception:
            upstream.LOGGER.exception('应用参考准备失败，保留旧参考。')
            return JSONResponse({'detail': '参考准备失败，原参考已保留。'}, status_code=500)
    reference.__annotations__['request'] = Request
    app.add_api_route('/internal/application/reference', reference, methods=['POST'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--package', type=Path, required=True)
    parser.add_argument('--library', type=Path, required=True)
    parser.add_argument('--nonce', required=True)
    parser.add_argument('--diagnostic-step-candidate', type=Path,
                        help='diagnostic-step-candidate（显式研发候选收据；不写入正式模型设置）')
    args = parser.parse_args()
    local = args.root.absolute()
    package = contained(local, args.package.absolute())
    library = contained(local, args.library.absolute())
    token = (library / 'reference-control.token').read_text(encoding='ascii').strip()
    if not re.fullmatch('[a-f0-9]{64}', token):
        raise ValueError('本机语音服务凭据格式无效。')
    os.environ['ANIFLIVE_TTS_MODEL_PACKAGE'] = str(package)
    os.environ['ANIFLIVE_TTS_CACHE_DIR'] = str(local / 'cache/aniflive-tts')
    import aniflive_tts
    if aniflive_tts.__version__ != '1.5.0b1':
        raise RuntimeError('参考桥接只支持已审阅的 AnifLive 1.5.0b1 固定版本。')
    import aniflive_tts.api as package_api
    from .aniflive_models import validate_registered_package
    # 仅受认证应用子进程使用受管登记校验；外部包导入、转换验收及主动维修仍走上游完整摘要。
    package_api.validate_checksums = lambda requested: validate_registered_package(local, Path(requested))
    create_app = package_api.create_app
    import aniflive_tts.streaming as streaming
    original = streaming.TensorRTFixedReferenceStreamer
    class ApplicationStreamer(original):
        def streaming_chunk_length(self):
            return min(getattr(self, '_application_chunk_tokens', 48), super().streaming_chunk_length())
    streaming.TensorRTFixedReferenceStreamer = ApplicationStreamer
    app = create_app()
    import aniflive_tts.service as upstream
    if args.diagnostic_step_candidate:
        from .aniflive_diagnostics import install_step_candidate
        install_step_candidate(streaming, upstream, args.diagnostic_step_candidate, package, local)
    install_bridge(app, upstream, library, token)
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(('127.0.0.1', 0))
    listener.listen(128)
    original_lifespan = app.router.lifespan_context
    @asynccontextmanager
    async def lifespan(app):
        async with original_lifespan(app):
            print('ANIFLIVE_READY=' + json.dumps({'port': listener.getsockname()[1], 'nonce': args.nonce, 'pid': os.getpid()}), flush=True)
            yield
    app.router.lifespan_context = lifespan
    import uvicorn
    try:
        uvicorn.Server(uvicorn.Config(app, workers=1, access_log=False)).run(sockets=[listener])
    finally:
        listener.close()


if __name__ == '__main__':
    main()
