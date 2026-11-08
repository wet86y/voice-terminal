"""AnifLive（本地语音）参考资料的配对与路径边界。"""
from voice_terminal.tts.aniflive_reference_catalog import discover_references
from voice_terminal.tts.aniflive_import import AnifLiveImportService, _atomic_json, _file_id, _persist_reference_candidates
import json
import pytest


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


def test_root_relative_manifest_fills_missing_profile_without_inventing_language():
    paths = ['manifests/references.yaml', 'references/serious.wav', 'references/soft.wav', 'profiles/soft.yaml']
    manifest = b'''selected_final: main
references:
- file: references/serious.wav
  profile: profiles/main.yaml
  prompt_text: serious transcript
  recommendation: serious scene
- file: references/soft.wav
  profile: profiles/soft.yaml
  prompt_text: soft transcript
'''
    profile = b'prompt:\n  ref_audio_path: ../references/soft.wav\n  prompt_text: soft transcript\n  prompt_lang: zh\n'
    rows = discover_references(paths, {paths[0]: manifest, paths[3]: profile})
    assert len(rows) == 2
    serious = next(row for row in rows if row['tag'] == 'main')
    assert serious['name'] == 'serious scene'
    assert serious['reference_text'] == 'serious transcript'
    assert serious['is_default'] and serious['status'] == 'pending'
    soft = next(row for row in rows if row['tag'] == 'soft')
    assert soft['status'] == 'ready' and soft['source_config'] == 'profiles/soft.yaml'


def test_list_annotations_pair_by_full_relative_path_and_keep_ambiguity_pending():
    paths = ['references/a.wav', 'other/a.wav', 'labels.list']
    rows = discover_references(paths, {'labels.list': b'references/a.wav|speaker|ZH|correct text\n../outside.wav|s|zh|outside'})
    assert next(row for row in rows if row['path'] == 'references/a.wav')['reference_text'] == 'correct text'
    assert next(row for row in rows if row['path'] == 'other/a.wav')['status'] == 'pending'
    rows = discover_references(paths, {'labels.list': b'references/a.wav|s|zh|one\nreferences/a.wav|s|zh|two'})
    assert all(row['status'] == 'pending' for row in rows)


@pytest.mark.parametrize('document', [
    {'default_refer_path': 'references/a.wav', 'default_refer_text': 'hello', 'default_refer_language': '英文'},
    {'default_refer': {'path': 'references/a.wav', 'text': 'hello', 'language': 'en'}},
    {'refer_wav_path': 'references/a.wav', 'prompt_text': 'hello', 'prompt_language': 'en', 'is_default': True},
    {'ref_audio_path': 'references/a.wav', 'prompt_text': 'hello', 'prompt_lang': 'en', 'recommended': True},
    {'default_profile': 'soft', 'profiles': [{'id': 'soft', 'reference_audio': 'references/a.wav',
      'reference_text': 'hello', 'reference_language': 'en'}]},
    {'character_config': {'tts_config': {'gpt_sovits_tts': {'reference_audio': 'references/a.wav',
      'prompt_text': 'hello', 'prompt_language': 'en', 'is_default': True}}}},
])
def test_known_reference_field_combinations_preserve_explicit_recommendations(document):
    rows = discover_references(['config.json', 'references/a.wav'], {'config.json': json.dumps(document).encode()})
    assert len(rows) == 1
    assert rows[0]['reference_text'] == 'hello' and rows[0]['reference_language'] == 'en'
    assert rows[0]['is_default'] and rows[0]['status'] == 'ready'


def test_minimal_inference_voice_map_uses_explicit_default_key():
    document = {'default': {'gpt_path': 'a.ckpt', 'sovits_path': 'a.pth',
        'ref_audio': 'references/a.wav', 'ref_text': 'hello', 'ref_lang': 'en'}}
    rows = discover_references(['voices.json', 'references/a.wav'], {'voices.json': json.dumps(document).encode()})
    assert rows[0]['is_default'] and rows[0]['reference_text'] == 'hello'


def test_remote_paths_are_remapped_only_with_unique_source_paths_and_api_snapshots_are_not_combined():
    document = {'ref_audio_path': 'D:\\old-device\\references\\a.wav', 'prompt_text': 'hello', 'prompt_lang': 'en'}
    paths = ['config.json', 'references/a.wav']
    rows = discover_references(paths, {'config.json': json.dumps(document).encode()})
    assert rows[0]['status'] == 'ready' and not rows[0]['is_default']
    snapshots = {'vits': {'gpt_sovits': {'api_0322': document, 'v2_api_0821': document}}}
    rows = discover_references(paths, {'config.json': json.dumps(snapshots).encode()})
    assert rows[0]['status'] == 'pending' and not rows[0]['is_default']


def test_conflicting_transcripts_and_ambiguous_relative_paths_remain_pending():
    first = {'prompt': {'ref_audio_path': '../references/a.wav', 'prompt_text': 'one', 'prompt_lang': 'en'}, 'status': 'final_selected'}
    second = {'prompt': {'ref_audio_path': '../references/a.wav', 'prompt_text': 'two', 'prompt_lang': 'en'}}
    paths = ['profiles/one.json', 'profiles/two.json', 'references/a.wav']
    rows = discover_references(paths, {paths[0]: json.dumps(first).encode(), paths[1]: json.dumps(second).encode()})
    assert all(row['status'] == 'pending' and '冲突' in row['error'] for row in rows)
    paths = ['profiles/test.json', 'profiles/a.wav', 'a.wav']
    rows = discover_references(paths, {paths[0]: b'{"ref_audio_path":"a.wav","prompt_text":"hello","prompt_lang":"en"}'})
    assert all(row['status'] == 'pending' and not row['reference_text'] for row in rows)


def test_same_recording_in_multiple_profiles_is_registered_once():
    document = {'status': 'final_selected', 'prompt': {'ref_audio_path': '../references/a.wav',
        'prompt_text': 'hello', 'prompt_lang': 'en'}}
    paths = ['profiles/one.json', 'profiles/two.json', 'references/a.wav']
    rows = discover_references(paths, {paths[0]: json.dumps(document).encode(), paths[1]: json.dumps(document).encode()})
    assert len(rows) == 1 and rows[0]['is_default'] and rows[0]['status'] == 'ready'
    assert rows[0]['source_configs'] == paths[:2]


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


@pytest.mark.parametrize('failures', [1, 3])
def test_reference_bundle_publication_retries_transient_windows_locks_without_losing_source(tmp_path, monkeypatch, failures):
    from voice_terminal.tts import aniflive_import
    stage = tmp_path / 'stage'; stage.mkdir()
    data = tmp_path / 'data'; data.mkdir()
    task_id = 'a' * 32
    (stage / 'audio.wav').write_bytes(b'source')
    _atomic_json(stage / 'reference-candidates.json', {'task_id': task_id, 'files': {'audio.wav': 'audio.wav'}, 'fields': {}})
    replace = aniflive_import.os.replace
    attempts = []
    def locked(source, destination):
        if Path(source).is_dir():
            attempts.append(str(source))
            if len(attempts) <= failures:
                error = PermissionError('temporarily locked'); error.winerror = 5
                raise error
        return replace(source, destination)
    from pathlib import Path
    monkeypatch.setattr(aniflive_import.os, 'replace', locked)
    monkeypatch.setattr(aniflive_import.time, 'sleep', lambda _: None)
    if failures == 3:
        with pytest.raises(PermissionError):
            _persist_reference_candidates(stage, data, task_id)
        assert (stage / 'audio.wav').read_bytes() == b'source'
    else:
        bundle = _persist_reference_candidates(stage, data, task_id)
        assert (bundle / 'audio.wav').read_bytes() == b'source'
    assert len(attempts) == (3 if failures == 3 else 2)


@pytest.mark.parametrize('base_selection', ['user', 'source_config', 'unregistered', 'conflict'])
def test_import_stage_preserves_all_scene_references(tmp_path, monkeypatch, base_selection):
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
            f'status: {"final_selected" if base_selection == "conflict" or (name == "neutral" and base_selection != "unregistered") else "scene_reference"}\nprompt:\n  ref_audio_path: ../references/{name}.wav\n  prompt_text: hello\n  prompt_lang: zh\n',
            encoding='utf-8')
    paths = ['gpt.ckpt', 'sovits.pth', 'references/neutral.wav', 'references/soft.wav',
             'profiles/neutral.yaml', 'profiles/soft.yaml']
    files = [{'path': name, 'size': (source / name).stat().st_size} for name in paths]
    task_id = 'b' * 32
    record = {'source_type': 'local', 'source': str(source), 'revision': None, 'files': files}
    task = {'task_id': task_id, 'model_id': 'test-voice', 'name': '测试音色',
            'selected_gpt': _file_id('gpt.ckpt'), 'selected_sovits': _file_id('sovits.pth'),
            'reference_audio': _file_id('references/neutral.wav') if base_selection == 'user' else None, 'reference_text_file': None,
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
    recommended = base_selection in {'user', 'source_config'}
    assert receipt['default_audio'] is None
    assert receipt['validation_audio'] == 'references/neutral.wav'
    assert receipt['recommendation_audio'] == ('references/neutral.wav' if recommended else None)
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
    assert result['ready'] == 2 and result['pending'] == 0
    assert result['default_registered'] is recommended
    voice = library.get_voice(result['voice_id'])
    assert {row['tag'] for row in voice['references']} == {'neutral', 'soft'}
    if recommended:
        snapshot = library.resolve('test-voice', library.active_selection)
        assert [row['tag'] for row in snapshot.references] == ['soft']
    else:
        from voice_terminal.tts.voices import VoiceLibraryError
        with pytest.raises(VoiceLibraryError, match='默认合成参考未登记'):
            library.resolve('test-voice', library.active_selection)
    assert library.asset_path(voice['references'][0]['asset_id']).is_file()
    assert register_reference_bundle(library, 'test-voice', package, bundle) == result
    assert len(library.get_voice(result['voice_id'])['references']) == 2
    # 用户选择优先：后续同步不能把作者的一次性推荐重新套用。
    soft = next(row for row in voice['references'] if row['tag'] == 'soft')
    selected = library.set_default_reference(voice['id'], voice['revision'], soft['id'])
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
    assert finished['reference_registration']['ready'] == 2
    current = library.get_voice(result['voice_id'])
    assert len(current['references']) == 2
    assert current['default_reference_id'] == soft['id'] and current['revision'] == selected['revision']
    assert not (service.staging_root / second_id).exists()


@pytest.mark.parametrize('select_base', [True, False])
def test_long_base_reference_is_preserved_before_conversion_is_blocked(tmp_path, select_base):
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
            'reference_audio': _file_id('base.wav') if select_base else None, 'reference_text_file': None,
            'reference_text': '参考文字', 'reference_language': 'zh', 'status': 'staging'}
    service = AnifLiveImportService(local, data)
    _atomic_json(service._job_path(task_id), task)
    service._stage_worker(task_id, record, task)
    result = json.loads(service._job_path(task_id).read_text(encoding='utf-8'))
    assert result['status'] == 'pending_reference' and not result['reference_audio_staged']
    if select_base:
        assert '3–10 秒' in result['phase']
    else:
        assert '等待参考录音' in result['phase']
    bundle = data / 'tts/aniflive/reference-imports' / task_id
    manifest = json.loads((bundle / 'reference-candidates.json').read_text(encoding='utf-8'))
    assert manifest['default_audio'] is None
    assert manifest['validation_audio'] == ('base.wav' if select_base else None)
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
    library = owner.for_model(task['model_id'])
    voice = library.get_voice(result['reference_registration']['voice_id'])
    assert len(voice['references']) == 1 and voice['default_reference_registered'] is False
    assert result['reference_registration']['default_registered'] is False


def test_independent_policy_survives_conversion_input_preparation_and_lazy_library_creation(tmp_path):
    from voice_terminal.tts.aniflive_install import AnifLiveInstallTask
    from voice_terminal.tts.voices import ModelVoiceLibraries
    from voice_terminal.tts.aniflive_reference_registration import register_model_default
    from tests.test_tts_voices import _wav_bytes
    local = tmp_path / 'local'; local.mkdir()
    source = tmp_path / 'source'; source.mkdir()
    for name in ('gpt.ckpt', 'sovits.pth'):
        (source / name).write_bytes(b'fixture')
    (source / 'reference.wav').write_bytes(_wav_bytes())
    spec = {'model_id': 'test-model', 'name': '测试模型', 'gpt': 'gpt.ckpt', 'sovits': 'sovits.pth',
        'reference': 'reference.wav', 'reference_text': '验收转写', 'reference_language': 'zh',
        'reference_policy': 'independent-v1', 'import_id': 'f' * 32}
    _atomic_json(source / 'input.json', spec)
    installer = AnifLiveInstallTask.__new__(AnifLiveInstallTask); installer.local = local
    prepared, _ = installer.import_inputs(source / 'input.json')
    assert json.loads(prepared.read_text(encoding='utf-8'))['reference_policy'] == 'independent-v1'
    package = tmp_path / 'package'; profile = package / 'voices/default'; profile.mkdir(parents=True)
    (profile / 'reference.wav').write_bytes(_wav_bytes())
    _atomic_json(profile / 'profile.json', {'reference_audio': 'reference.wav', 'reference_text': '验收转写', 'reference_language': 'zh'})
    _atomic_json(package / 'application-receipt.json', {'reference_policy': 'independent-v1'})
    library = ModelVoiceLibraries(local / 'data/tts').for_model('test-model')
    voice = register_model_default(library, package)
    assert voice['default_reference_registered'] is False and not library.list_voices()[0]['enabled']
