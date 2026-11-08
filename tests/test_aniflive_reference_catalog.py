"""AnifLive（本地语音）参考资料的配对与路径边界。"""
from voice_terminal.tts.aniflive_reference_catalog import discover_references
from voice_terminal.tts.aniflive_import import AnifLiveImportService, _atomic_json, _file_id, _persist_reference_candidates
import json


def test_scene_configuration_pairs_audio_without_executing_source():
    paths = ['profiles/romantic_soft.yaml', 'references/romantic.wav', 'references/other.wav']
    source = b'''prompt:
  ref_audio_path: ../references/romantic.wav
  prompt_text: hello
  prompt_lang: zh
scene:
  usage: soft scene
'''
    records = discover_references(paths, {paths[0]: source})
    paired = next(row for row in records if row['path'].endswith('romantic.wav'))
    assert paired['tag'] == 'romantic_soft'
    assert paired['reference_text'] == 'hello'
    assert paired['reference_language'] == 'zh'
    assert paired['status'] == 'ready'
    assert next(row for row in records if row['path'].endswith('other.wav'))['status'] == 'pending'


def test_outside_reference_and_ambiguous_text_remain_pending():
    paths = ['profiles/test.yaml', 'references/a.wav', 'references/a.txt', 'references/a.lab']
    source = b'prompt:\n  ref_audio_path: ../../outside.wav\n  prompt_text: hello\n  prompt_lang: zh\n'
    records = discover_references(paths, {paths[0]: source, paths[2]: b'one', paths[3]: b'two'})
    assert len(records) == 1
    assert records[0]['path'] == 'references/a.wav'
    assert records[0]['reference_text'] == ''
    assert records[0]['status'] == 'pending'


def test_reference_bundle_survives_removal_of_staging(tmp_path):
    import shutil
    task_id = 'a' * 32
    stage = tmp_path / 'stage'
    data = tmp_path / 'data'
    (stage / 'candidates' / 'item').mkdir(parents=True)
    data.mkdir()
    (stage / 'candidates' / 'item' / 'voice.wav').write_bytes(b'voice data')
    (stage / 'reference-candidates.json').write_text(json.dumps({
        'task_id': task_id, 'files': {'references/voice.wav': 'candidates/item/voice.wav'},
        'fields': {}, 'references': [],
    }), encoding='utf-8')
    bundle = _persist_reference_candidates(stage, data, task_id)
    shutil.rmtree(stage)
    assert (bundle / 'candidates' / 'item' / 'voice.wav').read_bytes() == b'voice data'
    receipt = json.loads((bundle / 'reference-candidates.json').read_text(encoding='utf-8'))
    assert receipt['persisted_files'][0]['source_path'] == 'references/voice.wav'


def test_import_stage_preserves_all_scene_references(tmp_path, monkeypatch):
    import math
    import shutil
    import struct
    import wave
    local = tmp_path / 'local'
    data = local / 'data'
    source = tmp_path / 'source'
    data.mkdir(parents=True)
    (source / 'references').mkdir(parents=True)
    (source / 'profiles').mkdir()
    (source / 'gpt.ckpt').write_bytes(b'gpt')
    (source / 'sovits.pth').write_bytes(b'sovits')
    samples = b''.join(struct.pack('<h', round(5000 * math.sin(i * 0.05))) for i in range(16000 * 4))
    for name in ('neutral', 'soft'):
        with wave.open(str(source / 'references' / (name + '.wav')), 'wb') as writer:
            writer.setnchannels(1)
            writer.setsampwidth(2)
            writer.setframerate(16000)
            writer.writeframes(samples)
        (source / 'profiles' / (name + '.yaml')).write_text(
            f'prompt:\n  ref_audio_path: ../references/{name}.wav\n  prompt_text: hello\n  prompt_lang: zh\n',
            encoding='utf-8')
    paths = ['gpt.ckpt', 'sovits.pth', 'references/neutral.wav', 'references/soft.wav',
             'profiles/neutral.yaml', 'profiles/soft.yaml']
    files = [{'path': name, 'size': (source / name).stat().st_size} for name in paths]
    task_id = 'b' * 32
    record = {'source_type': 'local', 'source': str(source), 'revision': None, 'files': files}
    task = {'task_id': task_id, 'model_id': 'test-voice', 'name': '测试音色',
            'selected_gpt': _file_id('gpt.ckpt'), 'selected_sovits': _file_id('sovits.pth'),
            'reference_audio': _file_id('references/neutral.wav'), 'reference_text_file': None,
            'reference_text': '', 'reference_language': 'zh', 'status': 'staging'}
    service = AnifLiveImportService(local, data)
    _atomic_json(service._job_path(task_id), task)
    service._stage_worker(task_id, record, task)
    result = json.loads(service._job_path(task_id).read_text(encoding='utf-8'))
    assert result['status'] == 'ready_to_convert', result.get('error')
    staged = json.loads((service.staging_root / task_id / 'import-manifest.json').read_text(encoding='utf-8'))
    assert staged['reference_text'] == 'hello'
    bundle = data / 'tts' / 'aniflive' / 'reference-imports' / task_id
    receipt = json.loads((bundle / 'reference-candidates.json').read_text(encoding='utf-8'))
    assert {row['tag'] for row in receipt['references']} == {'neutral', 'soft'}
    assert all(row['status'] == 'ready' for row in receipt['references'])
    from voice_terminal.tts.voices import ModelVoiceLibraries
    from voice_terminal.tts.aniflive_reference_registration import register_reference_bundle
    package = tmp_path / 'package'
    profile = package / 'voices' / 'default'
    profile.mkdir(parents=True)
    shutil.copyfile(source / 'references' / 'neutral.wav', profile / 'reference.wav')
    (profile / 'profile.json').write_text(json.dumps({'reference_audio': 'reference.wav',
        'reference_text': 'hello', 'reference_language': 'zh'}), encoding='utf-8')
    owner = ModelVoiceLibraries(data / 'tts')
    library = owner.for_model('test-voice')
    from voice_terminal.tts.aniflive_models import AnifLiveModelRegistry
    monkeypatch.setattr(AnifLiveModelRegistry, 'resolve', lambda self, model, verify_hashes=False: (package, 'revision', {}))
    service.voice_libraries = owner
    (data / 'state').mkdir(exist_ok=True)
    jobs = data / 'state' / 'resource-jobs.json'
    jobs.write_text(json.dumps({'jobs': [{'job_id': 'aniflive-import-' + task_id,
        'state': 'running', 'phase': '正在登记音色参考'}]}), encoding='utf-8')
    pending = service.task(task_id)
    assert pending['status'] == 'registering_references'
    result = pending['reference_registration']
    assert result['ready'] == 1 and result['pending'] == 0
    voice = library.get_voice(result['voice_id'])
    assert voice['references'][0]['tag'] == 'soft'
    assert library.asset_path(voice['references'][0]['asset_id']).is_file()
    assert register_reference_bundle(library, 'test-voice', package, bundle) == result
    assert len(library.get_voice(result['voice_id'])['references']) == 1
    assert json.loads((bundle / 'reference-registration.json').read_text(encoding='utf-8'))['task_id'] == task_id
    jobs.write_text(json.dumps({'jobs': [{'job_id': 'aniflive-import-' + task_id, 'state': 'complete'}]}), encoding='utf-8')
    assert service.task(task_id)['status'] == 'complete'
    second_id = 'c' * 32
    second = {'task_id': second_id, 'model_id': 'test-voice', 'job_kind': 'reference_import',
              'name': '测试音色', 'status': 'staging', 'phase': '准备中', 'progress': 0,
              'created_at': 1, 'selected_reference_files': None}
    _atomic_json(service._job_path(second_id), second)
    service._existing_reference_worker(second_id, second, record, 'references/neutral.wav')
    finished = service.task(second_id)
    assert finished['status'] == 'complete', finished.get('error')
    assert finished['reference_registration']['ready'] == 1
    assert len(library.get_voice(result['voice_id'])['references']) == 1
    assert not (service.staging_root / second_id).exists()


def test_long_base_reference_is_preserved_before_conversion_is_blocked(tmp_path):
    import wave
    local, source = tmp_path / 'local', tmp_path / 'source'
    data = local / 'data'
    data.mkdir(parents=True)
    source.mkdir()
    (source / 'gpt.ckpt').write_bytes(b'gpt')
    (source / 'sovits.pth').write_bytes(b'sovits')
    with wave.open(str(source / 'base.wav'), 'wb') as writer:
        writer.setnchannels(1); writer.setsampwidth(2); writer.setframerate(16000)
        writer.writeframes(b'\x01\x00' * 16000 * 12)
    paths = ['gpt.ckpt', 'sovits.pth', 'base.wav']
    record = {'source_type': 'local', 'source': str(source), 'revision': None,
              'files': [{'path': name, 'size': (source / name).stat().st_size} for name in paths]}
    task_id = 'd' * 32
    task = {'task_id': task_id, 'model_id': 'test-voice', 'name': '测试音色',
            'selected_gpt': _file_id('gpt.ckpt'), 'selected_sovits': _file_id('sovits.pth'),
            'reference_audio': _file_id('base.wav'), 'reference_text_file': None,
            'reference_text': '参考文字', 'reference_language': 'zh', 'status': 'staging'}
    service = AnifLiveImportService(local, data)
    _atomic_json(service._job_path(task_id), task)
    service._stage_worker(task_id, record, task)
    result = json.loads(service._job_path(task_id).read_text(encoding='utf-8'))
    assert result['status'] == 'pending_reference' and not result['reference_audio_staged']
    assert '3–10 秒' in result['phase']
    bundle = data / 'tts/aniflive/reference-imports' / task_id
    manifest = json.loads((bundle / 'reference-candidates.json').read_text(encoding='utf-8'))
    assert (bundle / manifest['persisted_files'][0]['path']).read_bytes() == (source / 'base.wav').read_bytes()


def test_bundled_preset_reference_can_be_registered_without_remote_scene_files(tmp_path, monkeypatch):
    from tests.test_tts_voices import _wav_bytes
    from voice_terminal.tts.voices import ModelVoiceLibraries
    from voice_terminal.tts.aniflive_models import AnifLiveModelRegistry
    local = tmp_path / 'local'; data = local / 'data'; data.mkdir(parents=True)
    package = tmp_path / 'package'; profile = package / 'voices/default'; profile.mkdir(parents=True)
    (profile / 'reference.wav').write_bytes(_wav_bytes())
    (profile / 'profile.json').write_text(json.dumps({'reference_audio': 'reference.wav',
        'reference_text': '默认参考。', 'reference_language': 'zh'}), encoding='utf-8')
    monkeypatch.setattr(AnifLiveModelRegistry, 'resolve', lambda self, model, verify_hashes=False: (package, 'revision', {}))
    owner = ModelVoiceLibraries(data / 'tts')
    service = AnifLiveImportService(local, data, voice_libraries=owner)
    task_id = 'e' * 32
    task = {'task_id': task_id, 'model_id': 'minimax-female-shaonv-zh', 'bundled_reference_only': True,
            'status': 'staging', 'created_at': 1, 'phase': '准备中'}
    _atomic_json(service._job_path(task_id), task)
    service._existing_reference_worker(task_id, task, None, None)
    result = service.task(task_id)
    assert result['status'] == 'complete' and result['reference_registration']['ready'] == 1
    assert owner.for_model(task['model_id']).active_selection is not None
