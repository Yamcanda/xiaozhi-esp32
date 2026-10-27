#pragma once

#include "sdkconfig.h"

#if CONFIG_STOPWATCH_LOCAL_APPS

#include <cstddef>
#include <cstdint>
#include <ctime>
#include <memory>
#include <string>
#include <vector>

namespace stopwatch_alarm {

inline constexpr size_t kMaxAlarms = 16;
inline constexpr size_t kMaxReminderBytes = 512;
inline constexpr size_t kQueueCapacity = 4;
inline constexpr size_t kResultCapacity = 16;
inline constexpr size_t kBlobLimit = 9216;
inline constexpr uint32_t kAnyRevision = UINT32_MAX;

struct Alarm {
    uint32_t id = 0;
    int hour = 0;
    int minute = 0;
    bool enabled = true;
    std::string reminder;
    // Empty means daily; otherwise a validated device-local YYYY-MM-DD date.
    std::string date;
    // Persisted high-water date: moving the clock backwards cannot repeat it.
    std::string last_triggered_date;
};

struct Result {
    // CRUD returns ok + pending + request_id for acceptance, NOT a saved alarm.
    // GetRequest returns ok + !pending only after commit (id is then available).
    bool ok = false;
    bool pending = false;
    uint32_t request_id = 0;
    uint32_t id = 0;
    uint32_t revision = 0;
    std::string error;
};

struct Snapshot {
    uint32_t revision = 0;
    uint32_t version = 1;
    bool initialized = false;
    bool time_valid = false;
    bool pending = false;
    size_t pending_count = 0;
    std::string error;
    std::vector<Alarm> alarms;
    Result last_result;
};

enum class ReadStatus { Missing, Ok, Error };

// Only this I/O seam creates a task. All storage calls run on that task, never
// from construction, Initialize, CRUD, Poll, GetSnapshot or GetRequest.
class Platform {
public:
    virtual ~Platform() = default;
    virtual ReadStatus ReadBlob(std::string& bytes, size_t limit) = 0;
    virtual bool WriteBlob(const std::string& bytes) = 0;
    virtual bool StartWorker(void (*entry)(void*), void* argument) = 0;
    virtual void WakeWorker() = 0;
    virtual void WaitWorker(uint32_t milliseconds) = 0;
    virtual void JoinWorker() = 0;
    virtual void WorkerFinished() {}
};

}  // namespace stopwatch_alarm

class StopwatchAlarmService {
public:
    StopwatchAlarmService();
    explicit StopwatchAlarmService(stopwatch_alarm::Platform& platform);
    ~StopwatchAlarmService();
    StopwatchAlarmService(const StopwatchAlarmService&) = delete;
    StopwatchAlarmService& operator=(const StopwatchAlarmService&) = delete;

    // Nonblocking and idempotent: starts one bounded worker which loads NVS.
    // true means task started, NOT that loading or a request has completed.
    bool Initialize();
    bool Start() { return Initialize(); }
    stopwatch_alarm::Snapshot GetSnapshot() const;
    uint32_t Version() const;

    // Bounded asynchronous FIFO, safe from main/UI/MCP callbacks. No NVS I/O.
    // Add validates 24h time, UTF-8 text <=512 bytes (no control characters),
    // and optional Gregorian date in 2020..9999. Empty reminder is a plain ring.
    stopwatch_alarm::Result Add(int hour, int minute, const std::string& reminder = "",
                                const std::string& date = "");
    // kAnyRevision skips the optimistic check. UI callers should pass the
    // rendered Snapshot.revision; the worker rechecks it before any commit.
    stopwatch_alarm::Result SetEnabled(uint32_t id, bool enabled,
                                       uint32_t expected_revision = stopwatch_alarm::kAnyRevision);
    stopwatch_alarm::Result Remove(uint32_t id,
                                   uint32_t expected_revision = stopwatch_alarm::kAnyRevision);
    // Completed results are retained for the latest kResultCapacity requests;
    // an older/unknown ID returns ok=false, error="unknown_request".
    stopwatch_alarm::Result GetRequest(uint32_t request_id) const;

    // Board calls once per second with device-local time represented by time_t.
    // No catch-up: only the current local minute is eligible, even after sync.
    // First observes due alarms and wakes the worker; a later Poll returns at
    // most 16 previously committed due alarms. Commit failure emits nothing.
    // The worker persists date deduplication and disables date-specific alarms
    // before delivery; a crash may lose a reminder, but cannot ring it twice.
    // Returned reminders are instructions for the board, NOT proof of speech.
    std::vector<stopwatch_alarm::Alarm> Poll(time_t now);

private:
    struct Impl;
    std::unique_ptr<stopwatch_alarm::Platform> owned_platform_;
    std::unique_ptr<Impl> impl_;
};

#endif  // CONFIG_STOPWATCH_LOCAL_APPS
