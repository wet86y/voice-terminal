/* voice_opus（Voice Terminal Opus 适配）：为 Rust 提供固定签名的 C ABI。 */
#include <stdint.h>
#include <opus/opus.h>

void *voice_opus_encoder_create(int32_t sample_rate, int32_t bitrate,
                                int32_t *error, int32_t *lookahead_samples) {
    if (error == 0 || lookahead_samples == 0 ||
        (sample_rate != 16000 && sample_rate != 48000) ||
        bitrate < 6000 || bitrate > 510000) {
        if (error != 0) *error = OPUS_BAD_ARG;
        return 0;
    }

    *lookahead_samples = 0;
    OpusEncoder *encoder = opus_encoder_create(sample_rate, 1, OPUS_APPLICATION_VOIP, error);
    if (encoder == 0) return 0;

    int result = opus_encoder_ctl(encoder, OPUS_SET_BITRATE(bitrate));
    if (result == OPUS_OK) result = opus_encoder_ctl(encoder, OPUS_SET_DTX(0));
    if (result == OPUS_OK) result = opus_encoder_ctl(encoder, OPUS_GET_LOOKAHEAD(lookahead_samples));
    if (result != OPUS_OK) {
        *error = result;
        opus_encoder_destroy(encoder);
        return 0;
    }
    *error = OPUS_OK;
    return encoder;
}

int32_t voice_opus_encode(void *state, const int16_t *pcm, int32_t frame_samples,
                          uint8_t *packet, int32_t packet_capacity) {
    if (state == 0 || pcm == 0 || packet == 0 || frame_samples <= 0 || packet_capacity <= 0)
        return OPUS_BAD_ARG;
    return opus_encode((OpusEncoder *)state, pcm, frame_samples, packet, packet_capacity);
}

void voice_opus_encoder_destroy(void *state) {
    if (state != 0) opus_encoder_destroy((OpusEncoder *)state);
}

void *voice_opus_decoder_create(int32_t sample_rate, int32_t *error) {
    if (error == 0 || (sample_rate != 16000 && sample_rate != 48000)) {
        if (error != 0) *error = OPUS_BAD_ARG;
        return 0;
    }
    return opus_decoder_create(sample_rate, 1, error);
}

int32_t voice_opus_decode(void *state, const uint8_t *packet, int32_t packet_length,
                          int16_t *pcm, int32_t frame_capacity) {
    if (state == 0 || packet == 0 || pcm == 0 || packet_length <= 0 ||
        packet_length > 1275 || frame_capacity <= 0)
        return OPUS_BAD_ARG;
    return opus_decode((OpusDecoder *)state, packet, packet_length, pcm, frame_capacity, 0);
}

void voice_opus_decoder_destroy(void *state) {
    if (state != 0) opus_decoder_destroy((OpusDecoder *)state);
}
