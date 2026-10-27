#ifndef AUDIO_ENGINE_H
#define AUDIO_ENGINE_H

#include <cstddef>
#include <cstdint>
#include <functional>
#include <string>
#include <vector>

#include <model_path.h>

#include "audio_codec.h"

class AudioEngine {
public:
    virtual ~AudioEngine() = default;

    virtual bool Initialize(AudioCodec* codec, int frame_duration_ms, srmodel_list_t* models_list) = 0;
    virtual void Feed(std::vector<int16_t>&& data, uint32_t voice_policy = 1) = 0;

    // A detection retains this policy token from the start of processing, not callback delivery.
    virtual void EnableWakeWordDetection(bool enable, uint32_t policy = 1) = 0;
    virtual void EnableVoiceProcessing(bool enable, uint32_t policy = 1) = 0;
    virtual void EnableDeviceAec(bool enable) = 0;

    virtual bool HasWakeWord() const = 0;
    virtual bool IsWakeWordDetectionEnabled() const = 0;
    virtual bool IsVoiceProcessingEnabled() const = 0;
    virtual bool IsAfeWakeWord() const = 0;
    virtual std::vector<std::string> GetWakeWords() const = 0;
    virtual size_t GetFeedSize() const = 0;

    virtual void OnWakeWordDetected(
        std::function<void(const std::string& wake_word, uint32_t policy)> callback) = 0;
    virtual void OnOutput(
        std::function<void(std::vector<int16_t>&& data, uint32_t policy)> callback) = 0;
    virtual void OnVadStateChange(std::function<void(bool speaking)> callback) = 0;

    virtual void EncodeWakeWordData() = 0;
    virtual bool GetWakeWordOpus(std::vector<uint8_t>& opus) = 0;
    virtual const std::string& GetLastDetectedWakeWord() const = 0;
};

#endif
