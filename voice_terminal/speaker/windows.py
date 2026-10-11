from __future__ import annotations


def speaker_window_ranges(total_samples: int, window_samples: int,
                          step_samples: int) -> tuple[tuple[int, int], ...]:
    """Return half-open speaker-window sample ranges for a completed clip.

    ``speaker window``（声纹分析窗） starts on the regular step grid. A final
    off-grid tail is included, matching the live worker's ``finish`` behavior;
    clips shorter than one full window produce one partial window.
    """
    total = max(0, int(total_samples))
    window = max(1, int(window_samples))
    step = max(1, int(step_samples))
    if total == 0:
        return ()
    if total < window:
        return ((0, total),)

    ranges = [
        (start, start + window)
        for start in range(0, total - window + 1, step)
    ]
    if ranges[-1][1] < total:
        tail = (total - window, total)
        if tail != ranges[-1]:
            ranges.append(tail)
    return tuple(ranges)
