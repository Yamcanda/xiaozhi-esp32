#pragma once

#include <cstddef>
#include <cstdint>
#include <functional>
#include <memory>
#include <string>

#include "stopwatch_weather_data.h"

namespace stopwatch_weather {

// Injectable board-local I/O for native tests. Production default uses Board,
// WifiManager, checked NVS and one FreeRTOS task. HTTP is worker-only.
struct HttpReply {
    bool ok = false;
    int status = 0;
    std::string body;
    std::string retry_after;
    std::string error;
};
class Platform {
public:
    virtual ~Platform() = default;
    virtual uint64_t NowMs() const = 0;
    virtual uint32_t Random32() const = 0;
    virtual bool Connected() const = 0;
    virtual HttpReply Get(const std::string& url, size_t limit,
                          const std::function<bool()>& cancelled) = 0;
    virtual bool ReadBlob(const char* key, std::string& bytes, size_t limit) = 0;
    virtual bool WriteBlob(const char* key, const std::string& bytes) = 0;
    virtual bool StartWorker(void (*entry)(void*), void* argument) = 0;
    virtual void WakeWorker() = 0;
    virtual void WaitWorker(uint32_t milliseconds) = 0;
    // No forced task deletion / cross-thread HTTP Close. Device owns service
    // for its lifetime; native test platforms join their thread here.
    virtual void JoinWorker() = 0;
    virtual void WorkerFinished() {}
};

}  // namespace stopwatch_weather

class StopwatchWeatherService {
public:
    // Construction reads only two small NVS blobs, never Board or network.
    StopwatchWeatherService();
    explicit StopwatchWeatherService(stopwatch_weather::Platform& platform);
    ~StopwatchWeatherService();
    StopwatchWeatherService(const StopwatchWeatherService&) = delete;
    StopwatchWeatherService& operator=(const StopwatchWeatherService&) = delete;

    stopwatch_weather::Snapshot GetSnapshot() const;
    uint32_t Version() const;
    void SetVisible(bool visible);
    void RequestRefresh();
    stopwatch_weather::Result SearchCity(const std::string& query,
                                        const std::string& country_code = "");
    stopwatch_weather::SearchResult GetSearch(uint32_t request_id) const;
    uint32_t SearchVersion(uint32_t request_id) const;
    void CancelSearch(uint32_t request_id);
    // Commit methods must run on Application's main task, outside LVGL lock.
    // Direct manual locations must exactly match a BuiltinLocation, recent city,
    // live search candidate or the effective-current Snapshot.location.
    // Arbitrary coordinates are NOT accepted; MCP manual selection is token-only.
    stopwatch_weather::Result SetLocation(
        bool automatic, const stopwatch_weather::Location& location = {},
        uint32_t expected_revision = stopwatch_weather::kAnyRevision);
    // mode is exactly "auto" or "manual". Manual requires a live token bound
    // to the current config_revision. Auto accepts only an empty token.
    stopwatch_weather::Result SetLocationToken(const std::string& mode,
                                              const std::string& token = "");

private:
    struct Impl;
    std::unique_ptr<stopwatch_weather::Platform> owned_platform_;
    std::unique_ptr<Impl> impl_;
};
