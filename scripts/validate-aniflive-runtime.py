"""验证受管 AnifLive（本地语音）服务、真实播放和进程资源；不更改播放设置。"""
from pathlib import Path
import argparse
import asyncio
import hashlib
import json
import sys
import time
import wave

PROGRAM = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROGRAM))
from voice_terminal.providers.aniflive_server import AnifLiveTtsServer
from voice_terminal.providers.aniflive_tts import AnifLiveTtsClient
from voice_terminal.tts.voices import VoiceLibrary
from voice_terminal.tts.aniflive_native import contained


def resources(server):
    import psutil
    from voice_terminal.gpu_memory import process_vram_snapshot, process_vram_bytes
    if not server.pid:
        return []
    process = psutil.Process(server.pid)
    if abs(process.create_time() - server.created_at) >= .01:
        raise RuntimeError('语音进程身份已变化，拒绝归属资源。')
    gpu = process_vram_snapshot()
    rows, seen = [], set()
    for child in [process, *process.children(recursive=True)]:
        try:
            identity = child.pid, child.create_time()
            if identity in seen:
                continue
            seen.add(identity)
            memory = child.memory_info()
            rows.append({'pid': child.pid, 'created_at': identity[1], 'rss_bytes': memory.rss,
                         'private_commit_bytes': getattr(memory, 'private', None),
                         'dedicated_vram_bytes': process_vram_bytes(child.pid, snapshot=gpu)})
        except psutil.NoSuchProcess:
            pass
    return rows


async def run(args, report):
    import httpx
    import psutil
    library = VoiceLibrary(args.output / 'reference-library', model='aniflive')
    server = AnifLiveTtsServer(args.root, PROGRAM, local_model=args.model, library_root=library.root)
    client = AnifLiveTtsClient(server=server, model_id=args.model, voice_library=library,
                               prewarm_timeout_seconds=15)
    settings = args.root / 'data/config/product-settings.json'
    before = hashlib.sha256(settings.read_bytes()).hexdigest() if settings.exists() else None
    report.update({'model_id': args.model, 'cases': [], 'fields': {
        'model_id': '实际验证模型', 'cases': '真实合成场景', 'first_network_seconds': '首块网络音频到达秒数',
        'first_consumer_seconds': '等待后首块交付秒数', 'first_play_seconds': '播放器首次回调输出秒数',
        'resources': '按进程身份去重的实际资源', 'rss_bytes': '常驻内存字节',
        'private_commit_bytes': '私有提交内存字节', 'dedicated_vram_bytes': '系统专用显存字节，空值为未知'}})
    try:
        started = time.perf_counter()
        await client.ensure_ready()
        report['load_seconds'] = time.perf_counter() - started
        report['revision'] = server.revision
        async with httpx.AsyncClient(trust_env=False) as http:
            response = await http.get(server.url + '/health')
            assert response.status_code in (401, 403), '无认证请求未被拒绝'
        report['unauthenticated_rejected'] = True
        report['prewarm'] = await client.prewarm(turn_id='validation-prewarm')
        if report['prewarm'].get('status') != 'READY':
            raise RuntimeError('真实事件预热未就绪：' + str(report['prewarm']))
        for tokens, wait in ((48, 0), (96, 120)):
            await client.configure_optimization(gpu_max_performance=False, local_optimization=True,
                aniflive_chunk_optimization=tokens == 48, aniflive_first_audio_wait_ms=wait)
            for length, text in (('short', '你好，我们正在验证本地语音。'),
                                 ('long', '这是一段较长的中文语音验证。我们检查连续文本能否完整生成，也检查后续音频分块、服务端会话终态和取消后的资源释放是否正确。')):
                key = f'validation-{tokens}-{length}'
                player, first_play = None, []
                started = time.perf_counter()
                if args.play and tokens == 48 and length == 'short':
                    from voice_terminal.audio.player import PcmPlayer
                    player = PcmPlayer(sample_rate=32000, channels=1, output_device=None,
                        on_playback_started=lambda _key: first_play.append(time.perf_counter()))
                    player.start(); player.allow_user_initiated(key)
                session = client.session(turn_id=key, voice_snapshot=None)
                audio, first_consumer, chunks = bytearray(), None, 0
                try:
                    await session.start()
                    await session.send_text(text)
                    await session.finish()
                    async for chunk in session.audio_chunks():
                        if first_consumer is None:
                            first_consumer = time.perf_counter()
                        audio.extend(chunk); chunks += 1
                        if player:
                            player.enqueue(key, chunk)
                    assert session.server_finished and session.completed_segments > 0
                    assert len(audio) > 6400 and len(audio) % 2 == 0
                    if wait:
                        assert first_consumer - session.first_audio_perf_at >= wait / 1000
                    if player:
                        player.finish_stream(key)
                        await player.wait_until_output_complete(40)
                        assert player.has_started(key) and first_play, '播放器没有实际输出回调'
                    with wave.open(str(args.output / f'{key}.wav'), 'wb') as output:
                        output.setnchannels(1); output.setsampwidth(2); output.setframerate(32000)
                        output.writeframes(audio)
                    report['cases'].append({'tokens': tokens, 'length': length, 'wait_ms': wait,
                        'chunks': chunks, 'audio_bytes': len(audio),
                        'first_network_seconds': session.first_audio_perf_at - started,
                        'first_consumer_seconds': first_consumer - started,
                        'first_play_seconds': first_play[0] - started if first_play else None,
                        'elapsed_seconds': time.perf_counter() - started, 'resources': resources(server)})
                finally:
                    await session.close()
                    if player:
                        player.stop()
        await client.configure_optimization(gpu_max_performance=True, local_optimization=True)
        report['performance_prewarm'] = await client.prewarm()
        assert report['performance_prewarm']['status'] == 'DISABLED'
        await client.configure_optimization(gpu_max_performance=False, local_optimization=True)
        # 验证库与真实用户资料隔离；使用同一个正式参考桥接与高保真素材实现。
        asset = library.put_import((PROGRAM / 'config/resources/assets/aniflive/reference.wav').read_bytes(), 'reference.wav')
        text = '你好，这是一段用于测试流式语音生成的较长中文文本。'
        voice = library.create_voice({'name': '本机验证参考', 'source': 'import',
            'reference_asset_id': asset['asset_id'], 'reference_text': text, 'reference_language': 'zh'})
        selected = {'voice_id': voice['id'], 'revision': voice['revision']}
        library.activate(selected)
        snapshot = library.resolve('aniflive', selected)
        await client.prepare_reference()
        library.activate(None)
        await client.prepare_reference()
        report['reference_cases'] = []
        for mode in ('complete', 'cancel'):
            preview = client.session(turn_id='validation-reference-' + mode, voice_snapshot=snapshot)
            try:
                await preview.start(); await preview.send_text('这段试听使用临时参考，结束后应恢复正式参考。')
                await preview.finish()
                size = 0
                async for block in preview.audio_chunks():
                    size += len(block)
                    if mode == 'cancel':
                        await preview.cancel()
                        break
                assert size > 0
            finally:
                await preview.close()
                await client.prepare_reference()
            http = await client._client()
            restored = await http.post(client.base_url + '/internal/application/reference', json={
                'model': args.model, 'asset_id': None, 'reference_text': '',
                'reference_language': 'zh', 'stream_chunk_tokens': 48})
            assert restored.status_code == 200 and restored.json()['changed'] is False
            report['reference_cases'].append({'mode': mode, 'audio_bytes': size, 'formal_reference_restored': True})
        rejected = await http.post(client.base_url + '/internal/application/reference', json={
            'model': args.model, 'asset_id': asset['asset_id'], 'reference_text': '',
            'reference_language': 'zh', 'stream_chunk_tokens': 48})
        assert rejected.status_code == 422
        await client.prepare_reference()
        report['invalid_reference_rejected'] = True
        session = client.session(turn_id='validation-cancel', voice_snapshot=None)
        await session.start(); await session.send_text('这段文本将取消，以检查会话是否释放。')
        report['cancelled'] = await session.cancel()
        await session.close()
        assert not client._foreground_sessions
        try:
            await client.switch_model('minimax-female-shaonv-zh')
        except ValueError as error:
            report['unavailable_switch_rejected'] = str(error)
            assert server.loaded_model == args.model
        else:
            assert server.loaded_model == 'minimax-female-shaonv-zh'
            report['base_switch_ready'] = True
            await client.switch_model(args.model)
            assert server.loaded_model == args.model
        report['before_close_resources'] = resources(server)
        from voice_terminal.application.supervisor import ApplicationHost
        import os
        host = ApplicationHost(PROGRAM)
        try:
            host.state.component('voice', state='ready', pid=os.getpid())
            host.local_tts_pid, host.local_tts_created_at = server.pid, server.created_at
            host.sample_metrics()
            metrics = host.state.snapshot()
            ledger = metrics['resource_processes']
            assert len({(row['pid'], row['created_at']) for row in ledger}) == len(ledger)
            assert any(row['pid'] == server.pid and row['component_id'] == 'local-tts' for row in ledger)
            for total, field in (('total_memory_rss_bytes', 'memory_rss_bytes'),
                                 ('total_memory_private_commit_bytes', 'memory_private_commit_bytes'),
                                 ('total_gpu_dedicated_bytes', 'gpu_dedicated_bytes')):
                if metrics[total] is not None:
                    assert all(row[field] is not None for row in ledger)
                    assert sum(row[field] for row in ledger) == metrics[total]
            report['host_resource_processes'] = ledger
            report['host_resource_totals'] = {key: metrics[key] for key in (
                'total_memory_rss_bytes', 'total_memory_private_commit_bytes', 'total_gpu_dedicated_bytes')}
            report['model_status'] = server.model_status()
        finally:
            host.outer_job.close()
        pid, birth = server.pid, server.created_at
        await client.close()
        assert not server.pid
        assert not psutil.pid_exists(pid) or psutil.Process(pid).create_time() != birth
        report['process_released'] = True
        await client.ensure_ready()
        report['restart_resources'] = resources(server)
        report['restart_ready'] = server.loaded_model == args.model
        assert report['restart_ready']
    finally:
        await client.close()
        after = hashlib.sha256(settings.read_bytes()).hexdigest() if settings.exists() else None
        report['playback_settings_unchanged'] = before == after
        if before != after:
            raise RuntimeError('验证期间播放设置被更改。')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=PROGRAM / '.local', help='root（受管安装目录）')
    parser.add_argument('--model', default='tangyuge-v2pp-d78cf598', help='model（已登记模型编号）')
    parser.add_argument('--output', type=Path, default=PROGRAM / '.local/cache/reports/aniflive-runtime', help='output（验证报告目录）')
    parser.add_argument('--play', action='store_true', help='play（实际通过默认扬声器播放短句）')
    args = parser.parse_args()
    args.root = args.root.absolute()
    args.output = contained(args.root, args.output.absolute()); args.output.mkdir(parents=True, exist_ok=True)
    report = {'ok': False}
    try:
        asyncio.run(run(args, report))
        report['ok'] = True
    except Exception as error:
        report['error'] = f'{type(error).__name__}: {error}'
        raise
    finally:
        (args.output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')


if __name__ == '__main__':
    main()
