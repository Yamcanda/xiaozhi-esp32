#pragma once

#include "sdkconfig.h"

#if CONFIG_STOPWATCH_LOCAL_APPS

#include <cstdint>
#include <ctime>
#include <functional>
#include <memory>
#include <string>
#include <vector>

#include "stopwatch_alarm_service.h"

class StopwatchAlarmPlayer;

// Board adapters marshal every call, including request_text completion, onto the
// application task. The runtime itself has no UI, microphone, storage or task API.
class StopwatchAlarmRuntime {
public:
    struct Hooks {
        std::function<bool()> ready;
        std::function<void(const std::vector<stopwatch_alarm::Alarm>&, const char* status)> present;
        std::function<void()> hide;
        std::function<void()> suspend_voice;
        std::function<void()> restore_voice;
        std::function<bool()> activate_voice;
        std::function<bool()> voice_foreground;
        std::function<bool()> audio_quiet;
        // true completion means TTS started, not that playback finished. The
        // return value means accepted; false or a false completion falls back.
        std::function<bool(const std::string&, std::function<void(bool)>)> request_text;
    };

    StopwatchAlarmRuntime(StopwatchAlarmService& service, StopwatchAlarmPlayer& player,
                          Hooks hooks);
    ~StopwatchAlarmRuntime();
    StopwatchAlarmRuntime(const StopwatchAlarmRuntime&) = delete;
    StopwatchAlarmRuntime& operator=(const StopwatchAlarmRuntime&) = delete;

    // Call at about 10 Hz after application/audio startup. Poll is throttled to
    // once per monotonic second, even while the alarm UI is hidden/unavailable.
    void Tick(time_t now, int64_t monotonic_us);
    void Acknowledge();
    // Home/wake own their next foreground/conversation policy; do not restore it.
    void CancelForUser();
    // Includes deferred alarms which cannot yet show an overlay, not audio tails.
    bool IsActive() const;
    // Owner-task query only; no audio calls. False from a successful ring start
    // until Tick observes stop acknowledgement and >=120 ms of continuous quiet,
    // even after the overlay/batch has been dismissed. Initially true.
    bool IsAudioReleased() const;

private:
    enum class Phase { Idle, Draining, WaitingVoice, Ringing };
    struct PendingAlarm {
        stopwatch_alarm::Alarm alarm;
        bool local_only = false;
    };
    struct CallbackState {
        StopwatchAlarmRuntime* owner = nullptr;
        uint64_t generation = 0;
    };

    void Enqueue(const stopwatch_alarm::Alarm& alarm);
    void BeginBatch();
    void DeferBatch();
    void BeginFallback();
    void StartVoice();
    void OnVoiceResult(uint64_t generation, bool started);
    void Clear(bool restore);
    void StopPlayer();
    void UpdateAudioReleased(int64_t monotonic_us);
    void Invalidate();
    void Present(const char* status);
    std::string BuildPrompt() const;

    StopwatchAlarmService& service_;
    StopwatchAlarmPlayer& player_;
    Hooks hooks_;
    std::shared_ptr<CallbackState> callback_state_;
    std::vector<PendingAlarm> pending_;
    std::vector<stopwatch_alarm::Alarm> active_;
    Phase phase_ = Phase::Idle;
    bool local_only_ = false;
    bool voice_attempted_ = false;
    bool polled_ = false;
    bool quiet_started_ = false;
    bool audio_released_ = true;
    bool release_quiet_started_ = false;
    bool restore_pending_ = false;
    int64_t last_poll_us_ = 0;
    int64_t last_tick_us_ = 0;
    int64_t quiet_since_us_ = 0;
    int64_t release_quiet_since_us_ = 0;
    int64_t voice_since_us_ = 0;
};

#endif  // CONFIG_STOPWATCH_LOCAL_APPS
