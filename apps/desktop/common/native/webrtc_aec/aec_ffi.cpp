#include "aec_ffi.h"

#include <algorithm>
#include <cmath>
#include <memory>
#include <mutex>
#include <new>
#include <vector>

#include "webrtc/api/audio/audio_processing.h"
#include "webrtc/api/scoped_refptr.h"

struct VoiceNativeAec {
  webrtc::scoped_refptr<webrtc::AudioProcessing> apm;
  std::unique_ptr<webrtc::StreamConfig> capture_input;
  std::unique_ptr<webrtc::StreamConfig> capture_output;
  std::unique_ptr<webrtc::StreamConfig> render_input;
  std::unique_ptr<webrtc::StreamConfig> render_output;
  std::mutex mutex; // mutex（串行化处理与重置，防止代次切换和音频帧并发交错）。
};

namespace {
constexpr int kOk = 0;
constexpr int kInvalidArgument = -1;
constexpr int kProcessingFailure = -2;
constexpr int kOutOfMemory = -3;

bool valid_rate(int sample_rate) {
  return sample_rate == 16000 || sample_rate == 24000 || sample_rate == 32000 ||
         sample_rate == 48000;
}

int configure(VoiceNativeAec* state, int capture_rate, int render_rate) {
  if (state == nullptr || !valid_rate(capture_rate) || !valid_rate(render_rate)) {
    return kInvalidArgument;
  }
  try {
    state->capture_input =
        std::make_unique<webrtc::StreamConfig>(capture_rate, 1);
    state->capture_output =
        std::make_unique<webrtc::StreamConfig>(capture_rate, 1);
    state->render_input =
        std::make_unique<webrtc::StreamConfig>(render_rate, 1);
    state->render_output =
        std::make_unique<webrtc::StreamConfig>(render_rate, 1);
  } catch (...) {
    return kOutOfMemory;
  }
  return state->apm->Initialize() == webrtc::AudioProcessing::kNoError
             ? kOk
             : kProcessingFailure;
}

int process_frame(VoiceNativeAec* state,
                  const int16_t* input,
                  size_t sample_count,
                  int16_t* output,
                  bool reverse) {
  if (state == nullptr || input == nullptr || output == nullptr) {
    return kInvalidArgument;
  }
  auto& input_config = reverse ? state->render_input : state->capture_input;
  auto& output_config = reverse ? state->render_output : state->capture_output;
  if (!input_config || !output_config ||
      sample_count != static_cast<size_t>(input_config->num_frames())) {
    return kInvalidArgument;
  }
  try {
    std::vector<float> input_float(sample_count);
    std::vector<float> output_float(sample_count);
    for (size_t index = 0; index < sample_count; ++index) {
      input_float[index] = static_cast<float>(input[index]) / 32768.0f;
    }
    const float* input_channels[] = {input_float.data()};
    float* output_channels[] = {output_float.data()};
    const int result = reverse
                           ? state->apm->ProcessReverseStream(
                                 input_channels, *input_config, *output_config,
                                 output_channels)
                           : state->apm->ProcessStream(
                                 input_channels, *input_config, *output_config,
                                 output_channels);
    if (result != webrtc::AudioProcessing::kNoError) {
      return kProcessingFailure;
    }
    for (size_t index = 0; index < sample_count; ++index) {
      const float value = std::clamp(output_float[index], -1.0f, 1.0f);
      output[index] = static_cast<int16_t>(std::clamp(
          std::lround(value * 32768.0f), -32768L, 32767L));
    }
    return kOk;
  } catch (...) {
    return kOutOfMemory;
  }
}
}  // namespace

extern "C" VoiceNativeAec* voice_native_aec_create(int enable_aec,
                                                   int enable_ns,
                                                   int enable_agc) {
  if ((enable_aec & ~1) != 0 || (enable_ns & ~1) != 0 ||
      (enable_agc & ~1) != 0) {
    return nullptr;
  }
  try {
    auto state = std::make_unique<VoiceNativeAec>();
    state->apm = webrtc::AudioProcessingBuilder().Create();
    if (!state->apm) {
      return nullptr;
    }
    webrtc::AudioProcessing::Config config;
    config.echo_canceller.enabled = enable_aec != 0;
    config.echo_canceller.mobile_mode = false;
    config.noise_suppression.enabled = enable_ns != 0;
    config.noise_suppression.level =
        webrtc::AudioProcessing::Config::NoiseSuppression::Level::kHigh;
    config.gain_controller1.enabled = enable_agc != 0;
    config.gain_controller1.mode =
        webrtc::AudioProcessing::Config::GainController1::Mode::kAdaptiveDigital;
    state->apm->ApplyConfig(config);
    if (state->apm->Initialize() != webrtc::AudioProcessing::kNoError) {
      return nullptr;
    }
    return state.release();
  } catch (...) {
    return nullptr;
  }
}

extern "C" void voice_native_aec_destroy(VoiceNativeAec* handle) {
  delete handle;
}

extern "C" int voice_native_aec_set_formats(VoiceNativeAec* handle,
                                            int capture_rate,
                                            int render_rate) {
  if (handle == nullptr) {
    return kInvalidArgument;
  }
  std::lock_guard<std::mutex> lock(handle->mutex);
  return configure(handle, capture_rate, render_rate);
}

extern "C" int voice_native_aec_set_delay_ms(VoiceNativeAec* handle,
                                             int delay_ms) {
  if (handle == nullptr || delay_ms < 0 || delay_ms > 500) {
    return kInvalidArgument;
  }
  std::lock_guard<std::mutex> lock(handle->mutex);
  return handle->apm->set_stream_delay_ms(delay_ms) ==
                 webrtc::AudioProcessing::kNoError
             ? kOk
             : kProcessingFailure;
}

extern "C" int voice_native_aec_reset(VoiceNativeAec* handle) {
  if (handle == nullptr) {
    return kInvalidArgument;
  }
  std::lock_guard<std::mutex> lock(handle->mutex);
  return handle->apm->Initialize() == webrtc::AudioProcessing::kNoError
             ? kOk
             : kProcessingFailure;
}

extern "C" int voice_native_aec_process_capture(VoiceNativeAec* handle,
                                                const int16_t* input,
                                                size_t sample_count,
                                                int16_t* output) {
  if (handle == nullptr) {
    return kInvalidArgument;
  }
  std::lock_guard<std::mutex> lock(handle->mutex);
  return process_frame(handle, input, sample_count, output, false);
}

extern "C" int voice_native_aec_process_reverse(VoiceNativeAec* handle,
                                                const int16_t* input,
                                                size_t sample_count,
                                                int16_t* output) {
  if (handle == nullptr) {
    return kInvalidArgument;
  }
  std::lock_guard<std::mutex> lock(handle->mutex);
  return process_frame(handle, input, sample_count, output, true);
}
