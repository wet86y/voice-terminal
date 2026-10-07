"""完整历史增长对照：保留正文与迟到精修，分别采样提交与物理驻留。"""
import argparse
import gc
import hashlib
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(next(p for p in Path(__file__).resolve().parents if (p / 'pyproject.toml').is_file())))


def main():
    parser = argparse.ArgumentParser(description="完整历史落盘对照（不缩短有效上下文）")
    parser.add_argument("--disk", type=Path, help="候选会话存储目录；不指定则使用原内存路径")
    parser.add_argument("--count", type=int, default=500)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    import psutil
    from voice_terminal.session.ledger import SessionLedger
    ledger = SessionLedger(storage_dir=args.disk)
    process = psutil.Process(); samples = []; latency = []
    for index in range(args.count):
        started = time.perf_counter()
        ledger.commit_input(str(index), f"输入{index}：" + "完整历史正文" * 1171)
        latency.append((time.perf_counter()-started)*1000)
        # 模拟持续到达，避免把不真实的全量瞬时写队列当作长会话收益。
        time.sleep(.004)
        if index % 50 == 49:
            if ledger.journal:
                assert ledger.journal.flush()
            memory = process.memory_info()
            samples.append({"turns": index+1, "private_commit_bytes": memory.private, "rss_bytes": memory.rss,
                            "read_cache_bytes": ledger.journal.cache_size if ledger.journal else None})
    ledger.update_input_refinement("0", "迟到精修保留，主模型正文不改写")
    before = ledger.context_messages("")
    digest = hashlib.sha256(json.dumps(before, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    del before; gc.collect()
    if ledger.journal:
        assert ledger.journal.flush()
    memory = process.memory_info()
    result = {"count": args.count, "disk": bool(args.disk), "history_digest": digest,
              "late_refinement": ledger.input_refinement("0"), "commit_samples_ms": latency,
              "memory_samples": samples, "settled_private_commit_bytes": memory.private,
              "settled_rss_bytes": memory.rss, "read_cache_bytes": ledger.journal.cache_size if ledger.journal else None}
    assert ledger.close_storage()
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"disk": result["disk"], "history_digest": digest,
                      "private_commit_bytes": memory.private, "rss_bytes": memory.rss}))


if __name__ == "__main__":
    main()
