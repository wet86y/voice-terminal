// 静默时限统一存储整数秒；滑条前后各占一半，分别选择秒数和分钟数。
export const MIN_SILENCE_SECONDS = 5; // MIN_SILENCE_SECONDS（最短静默秒数）。
export const SECONDS_SEGMENT_END = 180; // SECONDS_SEGMENT_END（秒数区间上限）。
export const MAX_SILENCE_SECONDS = 30 * 60; // MAX_SILENCE_SECONDS（最长静默秒数）：30 分钟。

export type SilenceUnit = "seconds" | "minutes"; // SilenceUnit（数值框当前单位）。

export function normalizeSilenceSeconds(value: number): number {
  // normalizeSilenceSeconds（将时限限制为允许范围内的整数秒）。
  if (!Number.isFinite(value)) return MIN_SILENCE_SECONDS;
  return Math.max(MIN_SILENCE_SECONDS, Math.min(MAX_SILENCE_SECONDS, Math.round(value)));
}

export function stepSilenceSeconds(value: number, direction: -1 | 1): number {
  // stepSilenceSeconds（统一步进）：秒区每次 1 秒，分钟区每次 1 分钟，并可双向跨过分界。
  const seconds = normalizeSilenceSeconds(value);
  if (direction === 1) {
    return normalizeSilenceSeconds(seconds < SECONDS_SEGMENT_END ? seconds + 1 : (Math.floor(seconds / 60) + 1) * 60);
  }
  return normalizeSilenceSeconds(seconds <= SECONDS_SEGMENT_END ? seconds - 1 : (Math.ceil(seconds / 60) - 1) * 60);
}

export function silenceSecondsToSlider(value: number): number {
  // silenceSecondsToSlider（将整数秒换算为滑条百分比）。
  const seconds = normalizeSilenceSeconds(value);
  if (seconds <= SECONDS_SEGMENT_END) {
    return ((seconds - MIN_SILENCE_SECONDS) / (SECONDS_SEGMENT_END - MIN_SILENCE_SECONDS)) * 50;
  }
  return 50 + ((seconds - SECONDS_SEGMENT_END) / (MAX_SILENCE_SECONDS - SECONDS_SEGMENT_END)) * 50;
}

export function sliderToSilenceSeconds(value: number): number {
  // sliderToSilenceSeconds（前半段按秒、后半段按整分钟选择时限）。
  const position = Math.max(0, Math.min(100, Number.isFinite(value) ? value : 0));
  if (position <= 50) {
    return Math.round(MIN_SILENCE_SECONDS + (position / 50) * (SECONDS_SEGMENT_END - MIN_SILENCE_SECONDS));
  }
  const minutes = Math.max(4, Math.round(3 + ((position - 50) / 50) * 27));
  return minutes * 60;
}

export function silenceDurationDisplay(value: number) {
  // silenceDurationDisplay（数值和中文单位）：单位在 180 秒之后切换为分钟。
  const seconds = normalizeSilenceSeconds(value);
  const unit: SilenceUnit = seconds <= SECONDS_SEGMENT_END ? "seconds" : "minutes";
  return {
    value: unit === "seconds" ? seconds : Number((seconds / 60).toFixed(2)), // value（显示数值）。
    unit, // unit（单位标识）。
    label: unit === "seconds" ? "秒" : "分钟", // label（中文单位）。
  };
}
