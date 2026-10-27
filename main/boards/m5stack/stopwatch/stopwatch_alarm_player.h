#pragma once

#include "sdkconfig.h"

#if CONFIG_STOPWATCH_LOCAL_APPS

#include <atomic>
#include <cstdint>
#include <functional>

class AudioService;

// Board-lifetime owner. Start/destruction are owner-task operations; requests
// and acknowledgements may be used from other tasks (including UI callbacks).
class StopwatchAlarmPlayer {
public:
    StopwatchAlarmPlayer(AudioService& audio, std::function<void(bool)> motor);
    // Teardown joins the worker; do not destroy from an audio/UI callback or
    // from the motor callback. The motor and AudioService must outlive us.
    ~StopwatchAlarmPlayer();
    StopwatchAlarmPlayer(const StopwatchAlarmPlayer&) = delete;
    StopwatchAlarmPlayer& operator=(const StopwatchAlarmPlayer&) = delete;

    // Creates one worker, without waiting for it or touching audio/hardware.
    // Idempotent after success; a failed task creation may be retried.
    bool Start();
    // Atomic request only: no allocation, I/O, waiting, or task notifications.
    void SetRinging(bool ringing);
    // Stop acknowledgement: motor off, no further submissions for this request.
    // Does not flush the shared decoder. Up to 120 ms of our already-submitted
    // audio may remain; the owner must gate voice entry until playback drains.
    // Before successful Start(), a stopped request is trivially acknowledged.
    bool IsStopped() const;

private:
    static void WorkerEntry(void* argument);
    void WorkerLoop();
    void SubmitPulse(uint32_t request);
    bool IsCurrent(uint32_t request) const;

    AudioService& audio_;
    std::function<void(bool)> motor_;
    // Low bit is the desired state; revisions prevent a stale stop ack from
    // authorizing voice after a quick stop/restart/stop between worker ticks.
    std::atomic<uint32_t> request_{0};
    std::atomic<uint32_t> stopped_request_{1};
    std::atomic<bool> started_{false};
    std::atomic<bool> shutdown_{false};
    std::atomic<bool> finished_{false};
};

#endif  // CONFIG_STOPWATCH_LOCAL_APPS
