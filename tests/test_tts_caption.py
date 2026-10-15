"""Floating caption（悬浮字幕）：只按实际播放时钟和可信文字边界定位。"""
from voice_terminal.tts.alignment import DeliveryResolver


def _snapshot(position, *, started=True, callback=None):
    return {"started": started, "output_estimated_ms": position, "played_ms": callback or position}


def _words():
    resolver = DeliveryResolver()
    resolver.add_provider_alignment([
        {"word": "hello", "start_ms": 0, "end_ms": 1000},
        {"word": "world", "start_ms": 1000, "end_ms": 2000},
    ])
    return resolver


def test_caption_follows_device_clock_and_preserves_original_spacing():
    resolver = _words()
    first = resolver.caption_progress(_snapshot(500, callback=1500), "hello world")
    assert first == {"text": "hello world", "start": 0, "end": 5, "precision": "word"}
    second = resolver.caption_progress(_snapshot(1500), "hello world")
    assert second == {"text": "hello world", "start": 5, "end": 11, "precision": "word"}


def test_caption_does_not_advance_before_audio_starts():
    resolver = _words()
    assert resolver.caption_progress(_snapshot(500, started=False), "hello world") is None
    assert resolver.caption_progress(_snapshot(0), "hello world") is None


def test_caption_uses_sentence_boundaries_without_word_timestamps():
    resolver = DeliveryResolver()
    resolver.add_text("第一句。", 0)
    resolver.mark_boundary(1000)
    resolver.add_text("第二句。", 1000)
    resolver.mark_boundary(2000)
    caption = resolver.caption_progress(_snapshot(1500), "第一句。第二句。还有未播放的文字")
    assert caption == {"text": "第一句。第二句。还有未播放的文字", "start": 4, "end": 8, "precision": "sentence"}


def test_caption_keeps_open_sentence_without_jumping_to_unsounded_tail():
    resolver = DeliveryResolver()
    resolver.add_text("当前句。", 0)
    resolver.add_text("还未播放。", 0)
    caption = resolver.caption_progress(_snapshot(100), "当前句。还未播放。")
    assert (caption["start"], caption["end"], caption["precision"]) == (0, 4, "sentence")


def test_caption_does_not_jump_to_pending_sentence_while_audio_waits_at_boundary():
    resolver = DeliveryResolver()
    resolver.add_text("上一句。", 0)
    resolver.mark_boundary(1000)
    resolver.add_text("待合成。", 1000)
    paused = resolver.caption_progress(_snapshot(1000), "上一句。待合成。")
    assert (paused["start"], paused["end"]) == (0, 4)
    resumed = resolver.caption_progress(_snapshot(1001), "上一句。待合成。")
    assert (resumed["start"], resumed["end"]) == (4, 8)


def test_caption_rejects_mismatching_alignment_and_empty_text():
    resolver = _words()
    assert resolver.caption_progress(_snapshot(500), "different screenshot explanation") is None
    assert resolver.caption_progress(_snapshot(500), " ") is None


def test_caption_unicode_offsets_count_characters_including_emoji():
    resolver = DeliveryResolver()
    resolver.add_provider_alignment([
        {"word": "你", "start_ms": 0, "end_ms": 100},
        {"word": "😊", "start_ms": 100, "end_ms": 200},
        {"word": "好", "start_ms": 200, "end_ms": 300},
    ])
    caption = resolver.caption_progress(_snapshot(150), "你😊好")
    assert (caption["start"], caption["end"]) == (1, 2)
