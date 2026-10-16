"""迁入已验证的本地 TTS（语音合成）资源，不覆盖工作区配置和凭据。"""
from pathlib import Path
import argparse
import hashlib
import json
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from voice_terminal.tts.voices import ModelVoiceLibraries
NATIVE_REVISION = "b4459b3bfa8d6cf4c579cc264dd3f4dfb57dd8ec"  # 已验证原生框架修订。
CUDA_SERVER_SHA256 = "4082389c40c0208dbafe4468370c30e0be5057e729b7188c82a784a2d97c2fcb"  # 取消、续接及高保真完整配方的运行程序摘要。


def digest(path):
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def migrate(source: Path, destination: Path):
    source, destination = source.resolve(), destination.resolve()
    if source == destination or not (source / "data/tts/voices/index.json").is_file():
        raise ValueError("迁移源必须为已有共享音色库的独立本机资源目录。")
    if digest(source / "runtimes/audiocpp/cuda/audiocpp_server.exe") != CUDA_SERVER_SHA256:
        raise ValueError("原生程序与已验证的取消、续接版本不一致，请先重新审阅并实测。")
    records = []
    for relative in ("runtimes/audiocpp/cpu", "runtimes/audiocpp/cuda", "models/audiocpp/tts/VoxCPM1-GGUF", "models/audiocpp/tts/VoxCPM2-GGUF",
                     "sources/audio.cpp/model_specs", "sources/audio.cpp/assets"):
        origin = source / relative
        if not origin.is_dir():
            raise FileNotFoundError(f"已验证资源目录缺失：{relative}")
        for asset in sorted(origin.rglob("*")):
            if not asset.is_file():
                continue
            name = asset.relative_to(source)
            target = destination / name
            target.parent.mkdir(parents=True, exist_ok=True)
            expected = digest(asset)
            if not target.is_file() or digest(target) != expected:
                shutil.copy2(asset, target)
            if digest(target) != expected:
                raise ValueError(f"迁入文件校验失败：{name}")
            records.append({"path": name.as_posix(), "sha256": expected, "size_bytes": target.stat().st_size})
        print(f"资源迁入并校验：{relative}", flush=True)
    index = json.loads((source / "data/tts/voices/index.json").read_text(encoding="utf-8"))
    default = index["migrations"]["default_voice"]
    item = index["voices"][default["voice_id"]]["revisions"][str(default["revision"])]
    audio = (source / "data/tts/voices/assets" / (item["reference_asset_id"] + ".wav")).read_bytes()
    seed = destination / "models/audiocpp/voice-seed"
    seed.mkdir(parents=True, exist_ok=True)
    (seed / "default.wav").write_bytes(audio)
    (seed / "default.json").write_text(json.dumps({"reference_text": item["reference_text"]}, ensure_ascii=False), encoding="utf-8")
    libraries = ModelVoiceLibraries(destination / "data/tts")
    settings_path = source / "data/config/product-settings.json"
    old_choices = (json.loads(settings_path.read_text(encoding="utf-8")).get("tts", {}).get("active_voice_by_model", {})
                   if settings_path.is_file() else {})
    for model, library in libraries.libraries.items():
        had_selection = library.active_selection is not None
        library.register_default(audio, item["reference_text"])
        mapping = dict(library._index["migrations"].get("experimental_voices", {}))
        mapping[default["voice_id"]] = library.active_selection if not had_selection else library._index["default_voice"]
        for voice_id, voice in index["voices"].items():
            if voice_id == default["voice_id"] or voice_id in mapping:
                continue
            payload = dict(voice["revisions"][str(voice["latest_revision"])])
            if model == "voxcpm1" and not payload.get("reference_text"):
                continue
            for field in ("reference_asset_id", "source_reference_asset_id"):
                if payload.get(field):
                    imported = library.put_import((source / "data/tts/voices/assets" / (payload[field] + ".wav")).read_bytes(), "migration.wav")
                    payload[field] = imported["asset_id"]
            if model == "voxcpm1":
                payload["source"] = "import"
                for field in ("design_description", "source_reference_asset_id", "design_model", "design_text"):
                    payload.pop(field, None)
            elif payload.get("source") == "design":
                payload.setdefault("source_reference_text", "")
                payload.setdefault("inference_steps", 20)
                library.mark_design(payload["reference_asset_id"], payload)
            imported = library.create_voice(payload)
            mapping[voice_id] = {"voice_id": imported["id"], "revision": imported["revision"]}
            updated = json.loads(json.dumps(library._index))
            updated["migrations"]["experimental_voices"] = mapping
            library._save_index(updated)
        if not library._index["migrations"].get("experimental_selection"):
            old = old_choices.get(model) or {}
            if not had_selection and old.get("voice_id") in mapping:
                library.activate(mapping[old["voice_id"]])
            updated = json.loads(json.dumps(library._index))
            updated["migrations"]["experimental_voices"] = mapping
            updated["migrations"]["experimental_selection"] = True
            library._save_index(updated)
        print(f"独立音色库已初始化：{model}", flush=True)
    report = destination / "config/audiocpp/resource-provenance.json"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(json.dumps({"schema": 1, "native_revision": NATIVE_REVISION, "files": records,
        "fields": {"schema": "资源来源记录版本", "native_revision": "已验证原生框架修订", "files": "逐文件校验记录", "path": "本机资源相对路径", "sha256": "SHA-256 文件摘要", "size_bytes": "文件字节数"}}, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True, help="source（来源本机资源目录）")
    parser.add_argument("--destination", type=Path, default=ROOT / ".local", help="destination（目标本机资源目录）")
    args = parser.parse_args()
    migrate(args.source, args.destination)
