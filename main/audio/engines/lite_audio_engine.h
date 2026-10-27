#ifndef LITE_AUDIO_ENGINE_H
#define LITE_AUDIO_ENGINE_H

#include <atomic>
#include <functional>
#include <memory>
#include <mutex>
#include <string>
#include <vector>

#include "audio_engine.h"

class EspWakeWord;

class LiteAudioEngine : public AudioEngine {
public:
    LiteAudioEngine();
    ~LiteAudioEngine() override;

    bool Initialize(AudioCodec* codec, int frame_duration_ms, srmodel_list_t* models_list) override;
    void Feed(std::vector<int16_t>&& data, uint32_t voice_policy = 1) override;

    void EnableWakeWordDetection(bool enable, uint32_t policy = 1) override;
    void EnableVoiceProcessing(bool enable, uint32_t policy = 1) override;
    void EnableDeviceAec(bool enable) override;

    bool HasWakeWord() const override;
    bool IsWakeWordDetectionEnabled() const override;
    bool IsVoiceProcessingEnabled() const override;
    bool IsAfeWakeWord() const override { return false; }
    std::vector<std::string> GetWakeWords() const override;
    size_t GetFeedSize() const override;

    // Release the standalone WakeNet allocation while it cannot be used, then
    // recreate it before wake word detection is enabled again.
    void ReleaseWakeWordResources();
    bool RestoreWakeWordResources();

    void OnWakeWordDetected(
        std::function<void(const std::string& wake_word, uint32_t policy)> callback) override;
    void OnOutput(
        std::function<void(std::vector<int16_t>&& data, uint32_t policy)> callback) override;
    void OnVadStateChange(std::function<void(bool speaking)> callback) override;

    void EncodeWakeWordData() override;
    bool GetWakeWordOpus(std::vector<uint8_t>& opus) override;
    const std::string& GetLastDetectedWakeWord() const override;

private:
    AudioCodec* codec_ = nullptr;
    std::unique_ptr<EspWakeWord> wake_word_;
    srmodel_list_t* models_list_ = nullptr;
    bool should_have_wake_word_ = false;
    std::atomic<bool> wake_word_enabled_ = false;
    uint32_t wake_word_policy_ = 1;  // Protected by wake_word_mutex_, including synchronous Feed.
    std::atomic<bool> voice_processing_enabled_ = false;
    int frame_samples_ = 0;
    std::vector<int16_t> output_buffer_;
    std::mutex output_mutex_;
    mutable std::mutex wake_word_mutex_;

    std::function<void(const std::string&, uint32_t)> wake_word_detected_callback_;
    std::function<void(std::vector<int16_t>&&, uint32_t)> output_callback_;
    std::atomic<uint32_t> voice_policy_{1};
    uint32_t output_buffer_policy_ = 0;
    std::function<void(bool)> vad_state_change_callback_;
    std::string empty_wake_word_;

    bool CreateWakeWordLocked();
    void OutputRawAudio(const std::vector<int16_t>& data, uint32_t policy);
};

#endif
