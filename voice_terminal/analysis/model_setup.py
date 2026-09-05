"""Pinned analysis assets. Downloads resume in HF's cache; no whole-run deadline."""
import argparse
import json
import os
from pathlib import Path
import shutil

from ..model_setup import _sha256

MODELS = {
    "smart-turn": ("pipecat-ai/smart-turn-v3", "f766f81d3cfdf7737ac64aad813d91bbfd56bf93",
                   ["smart-turn-v3.2-cpu.onnx"]),
    "sensevoice": ("FunAudioLLM/SenseVoiceSmall", "3847d57b6bdf2dd8875cb1508d2af43d80a16bf7",
                  ["model.pt", "config.yaml", "configuration.json", "am.mvn", "chn_jpn_yue_eng_ko_spectok.bpe.model"]),
}


def validate(root: Path) -> list[str]:
    errors = []
    for name, (_, revision, files) in MODELS.items():
        folder = root / "analysis" / name
        try:
            manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
            required = files + (["preprocessor_config.json"] if name == "smart-turn" else [])
            if manifest["revision"] != revision:
                raise ValueError("revision mismatch")
            for filename in required:
                if _sha256(folder / filename) != manifest["sha256"][filename]:
                    raise ValueError(f"checksum mismatch: {filename}")
        except Exception as exc:
            errors.append(f"{name}: {exc}")
    return errors


def install(root: Path) -> None:
    from huggingface_hub import hf_hub_download
    from transformers import WhisperFeatureExtractor
    for name, (repo, revision, files) in MODELS.items():
        folder = root / "analysis" / name
        folder.mkdir(parents=True, exist_ok=True)
        checksums = {}
        for filename in files:
            print(f"Downloading {name}/{filename} revision={revision}", flush=True)
            cached = hf_hub_download(repo, filename, revision=revision, cache_dir=str(root / "cache/huggingface"))
            destination = folder / filename
            staging = destination.with_suffix(destination.suffix + ".part")
            shutil.copyfile(cached, staging)
            staging.replace(destination)
            checksums[filename] = _sha256(destination)
        if name == "smart-turn":
            WhisperFeatureExtractor(chunk_length=8).save_pretrained(folder)
            checksums["preprocessor_config.json"] = _sha256(folder / "preprocessor_config.json")
        (folder / "manifest.json").write_text(json.dumps({"repository": repo, "revision": revision,
                                                          "sha256": checksums}, indent=2), encoding="utf-8")
        print(f"Ready: {name}", flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-dir", type=Path, default=Path(r"D:\模型\voice-terminal"))
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--no-proxy", action="store_true", help="Disable proxy only inside this download process")
    args = parser.parse_args()
    if args.no_proxy:
        for key in list(os.environ):
            if key.lower() in {"http_proxy", "https_proxy", "all_proxy"}:
                os.environ.pop(key)
        os.environ["NO_PROXY"] = "*"
    if not args.offline:
        install(args.model_dir)
    errors = validate(args.model_dir)
    print("\n".join(errors) if errors else "PASS: pinned analysis models validated", flush=True)
    raise SystemExit(bool(errors))


if __name__ == "__main__":
    main()
