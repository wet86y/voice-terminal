#pragma once

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef struct VoiceNativeAec VoiceNativeAec; // VoiceNativeAec（设备侧 WebRTC AEC 实例）。

VoiceNativeAec* voice_native_aec_create(int enable_aec, int enable_ns, int enable_agc);
void voice_native_aec_destroy(VoiceNativeAec* handle); // handle（原生 AEC 实例句柄）。
int voice_native_aec_set_formats(VoiceNativeAec* handle, int capture_rate, int render_rate);
int voice_native_aec_set_delay_ms(VoiceNativeAec* handle, int delay_ms);
int voice_native_aec_reset(VoiceNativeAec* handle);
int voice_native_aec_process_capture(
    VoiceNativeAec* handle,
    const int16_t* input,
    size_t sample_count,
    int16_t* output);
int voice_native_aec_process_reverse(
    VoiceNativeAec* handle,
    const int16_t* input,
    size_t sample_count,
    int16_t* output);

#ifdef __cplusplus
}
#endif
