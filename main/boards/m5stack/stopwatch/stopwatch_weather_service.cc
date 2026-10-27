#include "sdkconfig.h"

#if CONFIG_STOPWATCH_LOCAL_APPS

#include "stopwatch_weather_service.h"

#include <esp_random.h>
#include <esp_timer.h>
#include <nvs.h>
#include <wifi_manager.h>

#include <algorithm>
#include <array>
#include <atomic>
#include <cmath>
#include <cstdio>
#include <ctime>
#include <mutex>
#include <utility>

#include "board.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "stopwatch_weather_codec.h"

namespace {
using namespace stopwatch_weather;
constexpr uint64_t kMinute = 60000;
constexpr uint64_t kWeatherTtl = 15 * kMinute;
constexpr uint64_t kIpTtl = 6 * 60 * kMinute;
constexpr uint64_t kTokenTtl = 10 * kMinute;
constexpr uint64_t kMaxAge = 24 * 60 * kMinute;
constexpr uint64_t kWriteInterval = 60 * kMinute;
constexpr uint64_t kGlobalWriteInterval = 5 * kMinute;
constexpr const char* kNamespace = "sw_weather";
constexpr const char* kConfigKey = "config_v1";
constexpr const char* kCacheKey = "lastgood_v1";

class DevicePlatform final : public Platform {
public:
    uint64_t NowMs() const override { return static_cast<uint64_t>(esp_timer_get_time()) / 1000; }
    uint32_t Random32() const override { return esp_random(); }
    bool Connected() const override { return WifiManager::GetInstance().IsConnected(); }
    HttpReply Get(const std::string& url, size_t limit,
                  const std::function<bool()>& cancelled) override {
        HttpReply reply;
        const bool allowed = url == "https://ipwho.is/" ||
            url.rfind("https://geocoding-api.open-meteo.com/v1/search?", 0) == 0 ||
            url.rfind("https://api.open-meteo.com/v1/forecast?", 0) == 0;
        if (!allowed || cancelled()) { reply.error = "cancelled"; return reply; }
        auto* network = Board::GetInstance().GetNetwork();
        auto http = network ? network->CreateHttp(0) : nullptr;
        if (!http) { reply.error = "http_unavailable"; return reply; }
        http->SetTimeout(5000);
        http->SetKeepAlive(false);
        http->SetHeader("Accept", "application/json");
        http->SetHeader("Accept-Encoding", "identity");
        // Http::SetTimeout does not bound DNS/TLS connect in this component.
        // Only the isolated worker calls Open/Read/Close; never force-close it.
        if (!http->Open("GET", url)) {
            reply.error = "connect_failed";
            http->Close();
            return reply;
        }
        const uint64_t deadline = NowMs() + 15000;
        auto status = http->GetStatusCode();
        if (!status) { reply.error = "missing_status"; http->Close(); return reply; }
        reply.status = *status;
        reply.retry_after = http->GetResponseHeader("retry-after");
        if (reply.retry_after.empty()) reply.retry_after = http->GetResponseHeader("Retry-After");
        auto encoding = http->GetResponseHeader("content-encoding");
        if (encoding.empty()) encoding = http->GetResponseHeader("Content-Encoding");
        auto type = http->GetResponseHeader("content-type");
        if (type.empty()) type = http->GetResponseHeader("Content-Type");
        auto lower = [](std::string& text) {
            std::transform(text.begin(), text.end(), text.begin(), [](unsigned char c) {
                return c >= 'A' && c <= 'Z' ? static_cast<char>(c + 32) : static_cast<char>(c);
            });
        };
        lower(type);
        lower(encoding);
        const bool json_type = type == "application/json" || type.rfind("application/json;", 0) == 0;
        const auto declared_length = http->GetBodyLength();
        if (reply.status != 200) reply.error = "http_" + std::to_string(reply.status);
        else if (!encoding.empty() && encoding != "identity") reply.error = "unsupported_encoding";
        else if (!json_type) reply.error = "not_json";
        else if (declared_length > limit) reply.error = "response_too_large";
        if (!reply.error.empty()) { http->Close(); return reply; }
        char buffer[512];
        while (!cancelled() && NowMs() < deadline) {
            auto read = http->Read(buffer, sizeof(buffer));
            if (!read || *read < 0) { reply.error = "read_failed"; break; }
            if (cancelled()) { reply.error = "cancelled"; break; }
            if (NowMs() >= deadline) { reply.error = "read_timeout"; break; }
            if (*read == 0) {
                if (declared_length && reply.body.size() != declared_length) reply.error = "truncated_response";
                else reply.ok = true;
                break;
            }
            if (static_cast<size_t>(*read) > sizeof(buffer) ||
                reply.body.size() + static_cast<size_t>(*read) > limit) {
                reply.error = "response_too_large";
                break;
            }
            reply.body.append(buffer, static_cast<size_t>(*read));
        }
        if (!reply.ok && reply.error.empty()) reply.error = cancelled() ? "cancelled" : "read_timeout";
        http->Close();
        return reply;
    }
    bool ReadBlob(const char* key, std::string& bytes, size_t limit) override {
        nvs_handle_t handle;
        if (nvs_open(kNamespace, NVS_READONLY, &handle) != ESP_OK) return false;
        size_t size = 0;
        bool ok = nvs_get_blob(handle, key, nullptr, &size) == ESP_OK && size > 0 && size <= limit;
        if (ok) {
            bytes.resize(size);
            ok = nvs_get_blob(handle, key, bytes.data(), &size) == ESP_OK && size == bytes.size();
        }
        nvs_close(handle);
        if (!ok) bytes.clear();
        return ok;
    }
    bool WriteBlob(const char* key, const std::string& bytes) override {
        if (bytes.empty() || bytes.size() > codec::kBlobLimit) return false;
        nvs_handle_t handle;
        if (nvs_open(kNamespace, NVS_READWRITE, &handle) != ESP_OK) return false;
        bool ok = nvs_set_blob(handle, key, bytes.data(), bytes.size()) == ESP_OK;
        if (ok) ok = nvs_commit(handle) == ESP_OK;
        nvs_close(handle);
        return ok;
    }
    bool StartWorker(void (*entry)(void*), void* arg) override {
        return xTaskCreate(entry, "sw_weather", 12288, arg, 2, &task_) == pdPASS;
    }
    void WakeWorker() override { if (task_) xTaskNotifyGive(task_); }
    void WaitWorker(uint32_t milliseconds) override {
        ulTaskNotifyTake(pdTRUE, pdMS_TO_TICKS(milliseconds));
    }
    void JoinWorker() override {
        // Device lifetime owner normally never destroys this service. If it
        // does, let the worker finish its own HTTP rather than killing it.
        while (!finished_.load()) vTaskDelay(pdMS_TO_TICKS(10));
    }
    void WorkerFinished() override {
        finished_.store(true);
        vTaskDelete(nullptr);
    }
private:
    TaskHandle_t task_ = nullptr;
    std::atomic<bool> finished_{false};
};

bool ExactLocation(const Location& a, const Location& b) {
    return SameLocation(a, b) && a.city == b.city && a.display_name == b.display_name &&
           a.region == b.region && a.country == b.country;
}
}  // namespace

struct StopwatchWeatherService::Impl {
    explicit Impl(Platform& io) : io(io) {
        std::string bytes;
        if (io.ReadBlob(kConfigKey, bytes, codec::kBlobLimit)) codec::DecodeConfig(bytes, config);
        snapshot.automatic = config.automatic;
        snapshot.config_revision = config.revision;
        snapshot.location = config.location;
        snapshot.recent = config.recent;
        snapshot.recent_count = config.recent_count;
        if (io.ReadBlob(kCacheKey, bytes, codec::kBlobLimit) && codec::DecodeCache(bytes, disk_cache)) {
            disk_cache_valid = true;
            if (CacheMatches(disk_cache)) {
                snapshot.forecast = disk_cache.forecast;
                snapshot.location = disk_cache.forecast.location;
                snapshot.has_data = true;
                snapshot.cached = true;
                snapshot.stale = true;
                snapshot.age_known = false;
                snapshot.state = State::Ready;
            }
            last_cache_bytes = bytes;
        }
        snapshot.version = 1;
    }
    struct Record {
        SearchResult result;
        std::string query;
        std::string country;
        uint64_t created = 0;
        uint64_t expires = 0;
    };
    Platform& io;
    mutable std::mutex mutex;
    // Only NVS operations use this mutex. A setter uses try_lock so it never
    // waits for an HTTP worker's cache write. Snapshot lock is never held here.
    std::mutex storage_mutex;
    std::atomic<bool> stop{false};
    codec::Config config;
    Snapshot snapshot;
    std::array<Record, 4> records{};
    codec::Cache disk_cache;
    bool disk_cache_valid = false;
    std::string last_cache_bytes;
    bool worker_started = false;
    bool commit_in_flight = false;
    bool visible = false;
    bool refresh = false;
    bool explicit_refresh = false;
    bool active_refresh = false;
    uint32_t active_revision = 0;
    uint32_t active_epoch = 0;
    uint32_t epoch = 0;
    uint32_t next_request = 0;
    uint32_t pending_search = 0;
    uint32_t active_search = 0;
    uint64_t observed_ms = 0;
    bool fresh_this_boot = false;
    bool connected_seen = false;
    bool last_connected = false;
    bool force_ip = true;
    uint64_t connected_at = 0;
    uint64_t ip_at = 0;
    bool ip_known = false;
    uint64_t ip_network_gate = 0;
    uint64_t next_attempt = 0;
    uint64_t ip_cooldown = 0;
    uint64_t weather_cooldown = 0;
    uint64_t search_cooldown = 0;
    unsigned failures = 0;
    uint64_t last_write_at = 0;
    bool wrote_cache = false;
    std::array<std::pair<std::string, uint64_t>, 16> city_writes{};

    bool CacheMatches(const codec::Cache& value) const {
        return value.automatic == config.automatic && value.config_revision == config.revision &&
               (config.automatic || SameLocation(value.forecast.location, config.location));
    }
    void BumpLocked() { ++snapshot.version; if (!snapshot.version) ++snapshot.version; }
    void SearchBumpLocked(Record& record) {
        BumpLocked();
        record.result.version = snapshot.version;
    }
    Record* FindLocked(uint32_t id) {
        for (auto& r : records) if (r.result.request_id == id && id) return &r;
        return nullptr;
    }
    void ExpireLocked(uint64_t now) {
        for (auto& r : records) {
            if (r.result.request_id && r.expires && now >= r.expires &&
                r.result.state != SearchState::Expired && r.result.state != SearchState::Superseded) {
                r.result.state = SearchState::Expired;
                r.result.error = "expired";
                r.result.count = 0;
                SearchBumpLocked(r);
                if (pending_search == r.result.request_id) pending_search = 0;
            }
        }
    }
    void AgeLocked(uint64_t now) {
        if (!snapshot.has_data || !fresh_this_boot) return;
        const auto age = now >= observed_ms ? now - observed_ms : 0;
        if (age >= kWeatherTtl && !snapshot.stale) { snapshot.stale = true; BumpLocked(); }
        if (age > kMaxAge) {
            snapshot.has_data = false;
            snapshot.stale = true;
            BumpLocked();
        }
    }
    Result FailureLocked(const std::string& error) const {
        Result result;
        result.config_revision = config.revision;
        result.error = error;
        return result;
    }
    bool StartLocked() {
        if (worker_started) return true;
        // entry first takes mutex, so all flags publish before it can do work.
        if (!io.StartWorker(Entry, this)) return false;
        worker_started = true;
        return true;
    }
    static void Entry(void* argument) {
        auto* self = static_cast<Impl*>(argument);
        self->Run();
        self->io.WorkerFinished();
    }
    void Shutdown() {
        stop.store(true);
        bool started;
        { std::lock_guard<std::mutex> lock(mutex); started = worker_started; }
        if (started) { io.WakeWorker(); io.JoinWorker(); }
    }
    bool Current(uint32_t revision, uint32_t location_epoch) {
        std::lock_guard<std::mutex> lock(mutex);
        return !stop.load() && config.revision == revision && epoch == location_epoch &&
               !commit_in_flight;
    }
    uint64_t Cooldown(const HttpReply& reply, bool ip) {
        if (reply.status == 429) {
            auto parsed = codec::RetryAfterMs(reply.retry_after, static_cast<int64_t>(std::time(nullptr)));
            return parsed ? parsed : (ip ? 24 * 60 * kMinute : 60 * kMinute);
        }
        if (reply.status >= 400 && reply.status < 500) return 60 * kMinute;
        return 0;
    }
    uint64_t BackoffLocked() {
        constexpr unsigned minutes[] = {1, 5, 15, 30};
        const auto base = minutes[std::min<unsigned>(failures, 3)] * kMinute;
        if (failures < 4) ++failures;
        return base + io.Random32() % 5001;
    }
    void RefreshFailure(uint32_t revision, uint32_t location_epoch, const HttpReply& reply,
                        const std::string& error, bool locating) {
        const auto now = io.NowMs();
        const auto cooldown = Cooldown(reply, locating);
        std::lock_guard<std::mutex> lock(mutex);
        if (cooldown) {
            auto& gate = locating ? ip_cooldown : weather_cooldown;
            gate = std::max(gate, now + cooldown);
        }
        active_refresh = false;
        if (config.revision != revision || epoch != location_epoch || commit_in_flight) return;
        next_attempt = now + BackoffLocked();
        refresh = visible;
        explicit_refresh = false;
        snapshot.pending = false;
        snapshot.state = reply.error == "offline" ? State::Offline : State::Error;
        snapshot.error = error.empty() ? "request_failed" : error;
        BumpLocked();
    }
    void Search(uint32_t id) {
        std::string query, country;
        uint32_t revision;
        {
            std::lock_guard<std::mutex> lock(mutex);
            auto* record = FindLocked(id);
            if (!record || record->result.state != SearchState::Pending) { active_search = 0; return; }
            query = record->query; country = record->country;
            revision = record->result.config_revision;
        }
        auto cancelled = [this, id, revision] {
            if (stop.load()) return true;
            std::lock_guard<std::mutex> lock(mutex);
            auto* record = FindLocked(id);
            return !record || record->result.state != SearchState::Pending ||
                   config.revision != revision || commit_in_flight;
        };
        std::string url = codec::SearchUrl(query, country);
        HttpReply reply;
        if (!io.Connected()) reply.error = "offline";
        else reply = io.Get(url, 16384, cancelled);
        std::array<Candidate, 5> candidates{};
        size_t count = 0;
        std::string error = reply.error;
        const bool ok = reply.ok && reply.status == 200 && reply.body.size() <= 16384 &&
                        codec::ParseSearch(reply.body, query, candidates, count, error, country);
        const auto now = io.NowMs();
        const auto cooldown = Cooldown(reply, false);
        std::lock_guard<std::mutex> lock(mutex);
        active_search = 0;
        if (cooldown) search_cooldown = std::max(search_cooldown, now + cooldown);
        auto* record = FindLocked(id);
        if (!record || record->result.state != SearchState::Pending) return;
        if (config.revision != revision || commit_in_flight) {
            record->result.state = SearchState::Superseded;
            record->result.error = "superseded";
        } else if (ok) {
            record->result.state = SearchState::Ready;
            record->result.error.clear();
            record->result.count = count;
            for (size_t i = 0; i < count; ++i) {
                char token[64];
                std::snprintf(token, sizeof(token), "%08lx-%08lx-%08lx-%u",
                    static_cast<unsigned long>(io.Random32()),
                    static_cast<unsigned long>(io.Random32()),
                    static_cast<unsigned long>(id), static_cast<unsigned>(i));
                candidates[i].token = token;
            }
            record->result.candidates = std::move(candidates);
        } else {
            record->result.state = SearchState::Error;
            record->result.error = error.empty() ? "search_failed" : error;
        }
        record->expires = now + kTokenTtl;
        SearchBumpLocked(*record);
    }
    void Fetch(uint32_t revision, uint32_t location_epoch, bool locate, Location location) {
        std::function<bool()> cancelled = [this, revision, location_epoch] {
            return !Current(revision, location_epoch);
        };
        if (locate) {
            HttpReply reply;
            if (!io.Connected()) reply.error = "offline";
            else reply = io.Get("https://ipwho.is/", 4096, cancelled);
            Location resolved;
            std::string error = reply.error;
            if (!reply.ok || reply.status != 200 || reply.body.size() > 4096 ||
                !codec::ParseIp(reply.body, resolved, error)) {
                RefreshFailure(revision, location_epoch, reply, error, true);
                return;
            }
            {
                std::lock_guard<std::mutex> lock(mutex);
                if (config.revision != revision || epoch != location_epoch || commit_in_flight) {
                    active_refresh = false; return;
                }
                if (!SameLocation(snapshot.location, resolved)) {
                    ++epoch;
                    location_epoch = epoch;
                    snapshot.forecast = {};
                    snapshot.has_data = false;
                    snapshot.cached = false;
                    snapshot.stale = false;
                    snapshot.age_known = false;
                    fresh_this_boot = false;
                }
                snapshot.location = resolved;
                location = std::move(resolved);
                ip_at = io.NowMs(); ip_known = true; force_ip = false;
                active_epoch = location_epoch;
                snapshot.state = State::Updating;
                BumpLocked();
            }
            cancelled = [this, revision, location_epoch] { return !Current(revision, location_epoch); };
        }
        HttpReply reply;
        if (!io.Connected()) reply.error = "offline";
        else reply = io.Get(codec::ForecastUrl(location), 16384, cancelled);
        Forecast forecast;
        std::string error = reply.error;
        if (!reply.ok || reply.status != 200 || reply.body.size() > 16384 ||
            !codec::ParseForecast(reply.body, location, forecast, error)) {
            RefreshFailure(revision, location_epoch, reply, error, false);
            return;
        }
        codec::Cache cache;
        {
            std::lock_guard<std::mutex> lock(mutex);
            active_refresh = false;
            if (config.revision != revision || epoch != location_epoch || commit_in_flight ||
                !SameLocation(snapshot.location, location)) return;
            snapshot.forecast = forecast;
            snapshot.has_data = true;
            snapshot.cached = false;
            snapshot.stale = false;
            snapshot.age_known = true;
            snapshot.pending = false;
            snapshot.state = State::Ready;
            snapshot.error.clear();
            observed_ms = io.NowMs(); fresh_this_boot = true;
            refresh = false; explicit_refresh = false; failures = 0; next_attempt = 0;
            cache.automatic = config.automatic;
            cache.config_revision = config.revision;
            cache.forecast = std::move(forecast);
            BumpLocked();
        }
        PersistCache(cache, revision, location_epoch);
    }
    void PersistCache(const codec::Cache& cache, uint32_t revision, uint32_t location_epoch) {
        std::string bytes;
        if (!codec::EncodeCache(cache, bytes)) return;
        const auto now = io.NowMs();
        bool content_changed;
        {
            std::lock_guard<std::mutex> lock(mutex);
            if (disk_cache_valid && disk_cache.automatic == cache.automatic &&
                disk_cache.config_revision == cache.config_revision &&
                SameLocation(disk_cache.forecast.location, cache.forecast.location)) {
                auto comparison = disk_cache;
                comparison.forecast.observed_at = cache.forecast.observed_at;
                std::string old_bytes;
                content_changed = !codec::EncodeCache(comparison, old_bytes) || old_bytes != bytes;
            } else content_changed = true;
            if (!content_changed || bytes == last_cache_bytes ||
                (wrote_cache && now - last_write_at < kGlobalWriteInterval)) return;
            for (const auto& write : city_writes) {
                if (write.first == cache.forecast.location.id && now - write.second < kWriteInterval) return;
            }
        }
        std::unique_lock<std::mutex> storage(storage_mutex, std::try_to_lock);
        if (!storage.owns_lock() || !Current(revision, location_epoch)) return;
        const bool ok = io.WriteBlob(kCacheKey, bytes);
        if (!ok) return;  // Cache write failure never invalidates a RAM forecast.
        std::lock_guard<std::mutex> lock(mutex);
        wrote_cache = true;
        last_write_at = now;
        for (auto& write : city_writes) {
            if (write.first.empty() || write.first == cache.forecast.location.id ||
                now - write.second >= kWriteInterval) {
                write = {cache.forecast.location.id, now};
                break;
            }
        }
        last_cache_bytes = std::move(bytes);
        disk_cache = cache; disk_cache_valid = true;
    }
    void TickNetworkLocked(uint64_t now, bool connected) {
        if (!connected_seen || connected != last_connected) {
            if (connected) {
                connected_at = now;
                if (config.automatic && connected_seen) force_ip = true;
            }
            connected_seen = true;
            last_connected = connected;
        }
        AgeLocked(now);
        ExpireLocked(now);
        if (!connected && (visible || refresh) && !active_refresh) {
            if (snapshot.state != State::Offline || snapshot.pending) {
                snapshot.state = State::Offline;
                snapshot.pending = false;
                snapshot.error = "offline";
                BumpLocked();
            }
        }
        if (visible && !active_refresh &&
            (!fresh_this_boot || now - observed_ms >= kWeatherTtl ||
             (config.automatic && force_ip))) refresh = true;
    }
    void Run() {
        while (!stop.load()) {
            const bool connected = io.Connected();
            const uint64_t now = io.NowMs();
            uint32_t search = 0, revision = 0, location_epoch = 0;
            bool fetch = false, locate = false;
            Location location;
            {
                std::lock_guard<std::mutex> lock(mutex);
                TickNetworkLocked(now, connected);
                if (!commit_in_flight) {
                    if (pending_search) {
                        search = pending_search;
                        pending_search = 0;
                        if (now < search_cooldown) {
                            auto* record = FindLocked(search);
                            if (record && record->result.state == SearchState::Pending) {
                                record->result.state = SearchState::Error;
                                record->result.error = "rate_limited";
                                record->expires = now + kTokenTtl;
                                SearchBumpLocked(*record);
                            }
                            search = 0;
                        } else {
                            active_search = search;
                        }
                    } else if (refresh && (visible || explicit_refresh) && connected &&
                               now >= connected_at + 2000 && now >= next_attempt) {
                        locate = config.automatic && (force_ip || !ip_known || now - ip_at >= kIpTtl);
                        const uint64_t gate = std::max(weather_cooldown,
                            locate ? std::max(ip_cooldown, ip_network_gate) : uint64_t{0});
                        if (now >= gate) {
                            location = snapshot.location;
                            if (locate || ValidLocation(location)) {
                                fetch = true; active_refresh = true;
                                revision = active_revision = config.revision;
                                location_epoch = active_epoch = epoch;
                                refresh = false;
                                if (locate) ip_network_gate = now + kMinute;
                                snapshot.pending = true;
                                snapshot.state = locate ? State::Locating : State::Updating;
                                snapshot.error.clear();
                                BumpLocked();
                            }
                        }
                    }
                }
            }
            if (search) Search(search);
            else if (fetch) Fetch(revision, location_epoch, locate, std::move(location));
            else io.WaitWorker(1000);
        }
    }

    bool AllowedLocked(const Location& location, uint64_t now) {
        if (!ValidLocation(location)) return false;
        if (ExactLocation(snapshot.location, location)) return true;
        for (int i = 0; i < kCityCount; ++i) if (ExactLocation(BuiltinLocation(i), location)) return true;
        for (size_t i = 0; i < config.recent_count; ++i)
            if (ExactLocation(config.recent[i], location)) return true;
        for (const auto& r : records) {
            if (r.result.state != SearchState::Ready || r.result.config_revision != config.revision ||
                now >= r.expires) continue;
            for (size_t i = 0; i < r.result.count; ++i)
                if (ExactLocation(r.result.candidates[i].location, location)) return true;
        }
        return false;
    }
    Result Set(bool automatic, const Location& location, uint32_t expected) {
        codec::Config proposed;
        {
            std::lock_guard<std::mutex> lock(mutex);
            if (expected != kAnyRevision && expected != config.revision) return FailureLocked("conflict");
            if (commit_in_flight) return FailureLocked("busy");
            ExpireLocked(io.NowMs());
            if (!automatic && !AllowedLocked(location, io.NowMs())) return FailureLocked("invalid_location");
            if (config.automatic == automatic && (automatic || ExactLocation(config.location, location))) {
                Result result;
                result.ok = true; result.config_revision = config.revision;
                result.pending = snapshot.pending;
                return result;
            }
            proposed = config;
            proposed.automatic = automatic;
            proposed.location = automatic ? Location{} : location;
            ++proposed.revision;
            if (proposed.revision == kAnyRevision) ++proposed.revision;
            if (!automatic) {
                std::array<Location, 3> recent{};
                size_t count = 0;
                recent[count++] = location;
                for (size_t i = 0; i < config.recent_count && count < recent.size(); ++i)
                    if (!SameLocation(config.recent[i], location)) recent[count++] = config.recent[i];
                proposed.recent = std::move(recent); proposed.recent_count = count;
            }
            commit_in_flight = true;
        }
        std::string bytes;
        const bool encoded = codec::EncodeConfig(proposed, bytes);
        std::unique_lock<std::mutex> storage(storage_mutex, std::try_to_lock);
        const bool written = encoded && storage.owns_lock() && io.WriteBlob(kConfigKey, bytes);
        std::lock_guard<std::mutex> lock(mutex);
        commit_in_flight = false;
        if (!written) {
            // The in-flight request observed commit_in_flight and may already
            // have cancelled. Restore the old configuration's demand rather
            // than leaving its stale pending/Locating state stuck forever.
            ++epoch;
            active_refresh = false;
            refresh = visible || explicit_refresh;
            snapshot.pending = false;
            snapshot.state = snapshot.has_data ? State::Ready : State::Starting;
            snapshot.error = !storage.owns_lock() ? "busy" : "storage_failed";
            BumpLocked();
            io.WakeWorker();
            return FailureLocked(snapshot.error);
        }
        config = std::move(proposed);
        snapshot.automatic = config.automatic;
        snapshot.config_revision = config.revision;
        snapshot.location = config.location;
        snapshot.recent = config.recent; snapshot.recent_count = config.recent_count;
        snapshot.forecast = {}; snapshot.has_data = false; snapshot.cached = false;
        snapshot.stale = false; snapshot.age_known = false; fresh_this_boot = false;
        snapshot.state = State::Starting; snapshot.error.clear();
        ++epoch;
        ip_known = false; force_ip = true;
        next_attempt = 0; failures = 0;
        active_refresh = false;
        for (auto& r : records) {
            if (r.result.request_id && r.result.state != SearchState::Expired &&
                r.result.state != SearchState::Superseded) {
                r.result.state = SearchState::Superseded; r.result.error = "superseded";
                r.result.count = 0; SearchBumpLocked(r);
            }
        }
        pending_search = 0;
        if (disk_cache_valid && CacheMatches(disk_cache)) {
            snapshot.forecast = disk_cache.forecast; snapshot.location = disk_cache.forecast.location;
            snapshot.has_data = true; snapshot.cached = true; snapshot.stale = true;
        }
        refresh = true; explicit_refresh = true;
        const bool started = StartLocked();
        snapshot.pending = started;
        if (!started) { snapshot.state = State::Error; snapshot.error = "worker_unavailable"; }
        BumpLocked();
        Result result;
        result.ok = true; result.config_revision = config.revision; result.pending = started;
        if (!started) result.error = "worker_unavailable";
        io.WakeWorker();
        return result;
    }
};

StopwatchWeatherService::StopwatchWeatherService()
    : owned_platform_(std::make_unique<DevicePlatform>()), impl_(std::make_unique<Impl>(*owned_platform_)) {}
StopwatchWeatherService::StopwatchWeatherService(Platform& platform)
    : impl_(std::make_unique<Impl>(platform)) {}
StopwatchWeatherService::~StopwatchWeatherService() { impl_->Shutdown(); }
Snapshot StopwatchWeatherService::GetSnapshot() const {
    std::lock_guard<std::mutex> lock(impl_->mutex);
    impl_->AgeLocked(impl_->io.NowMs());
    return impl_->snapshot;
}
uint32_t StopwatchWeatherService::Version() const {
    std::lock_guard<std::mutex> lock(impl_->mutex);
    impl_->AgeLocked(impl_->io.NowMs());
    impl_->ExpireLocked(impl_->io.NowMs());
    return impl_->snapshot.version;
}
void StopwatchWeatherService::SetVisible(bool visible) {
    {
        std::lock_guard<std::mutex> lock(impl_->mutex);
        impl_->visible = visible;
        if (!visible) {
            if (!impl_->explicit_refresh) impl_->refresh = false;
        } else if (!impl_->StartLocked()) {
            impl_->snapshot.pending = false; impl_->snapshot.state = State::Error;
            impl_->snapshot.error = "worker_unavailable"; impl_->BumpLocked();
        }
    }
    impl_->io.WakeWorker();
}
void StopwatchWeatherService::RequestRefresh() {
    {
        std::lock_guard<std::mutex> lock(impl_->mutex);
        if (!impl_->StartLocked()) {
            impl_->snapshot.pending = false; impl_->snapshot.state = State::Error;
            impl_->snapshot.error = "worker_unavailable"; impl_->BumpLocked();
            return;
        }
        const auto now = impl_->io.NowMs();
        impl_->AgeLocked(now);
        if (impl_->fresh_this_boot && impl_->snapshot.has_data &&
            now - impl_->observed_ms < kWeatherTtl &&
            !(impl_->config.automatic && impl_->force_ip)) return;
        if (impl_->active_refresh && impl_->active_revision == impl_->config.revision &&
            impl_->active_epoch == impl_->epoch) return;
        impl_->refresh = true;
        // An Open request belongs to visible demand. Closing the page must
        // not turn it into a persistent hidden retry. Explicit hidden calls
        // and successful location commits are allowed one attempt.
        if (!impl_->visible) impl_->explicit_refresh = true;
    }
    impl_->io.WakeWorker();
}
Result StopwatchWeatherService::SearchCity(const std::string& query, const std::string& country_code) {
    if (!codec::ValidQuery(query, country_code)) {
        std::lock_guard<std::mutex> lock(impl_->mutex);
        return impl_->FailureLocked("invalid_query");
    }
    std::string country = country_code;
    for (char& c : country) if (c >= 'a' && c <= 'z') c = static_cast<char>(c - 32);
    std::lock_guard<std::mutex> lock(impl_->mutex);
    const auto now = impl_->io.NowMs();
    impl_->ExpireLocked(now);
    if (impl_->commit_in_flight) return impl_->FailureLocked("busy");
    for (const auto& r : impl_->records) {
        if (r.query == query && r.country == country && r.result.config_revision == impl_->config.revision &&
            (r.result.state == SearchState::Ready || r.result.state == SearchState::Pending) &&
            now < r.expires) {
            Result result;
            result.ok = true; result.request_id = r.result.request_id;
            result.config_revision = impl_->config.revision;
            result.pending = r.result.state == SearchState::Pending;
            return result;
        }
    }
    if (now < impl_->search_cooldown) return impl_->FailureLocked("rate_limited");
    if (impl_->pending_search) return impl_->FailureLocked("busy");
    Impl::Record* slot = nullptr;
    for (auto& r : impl_->records) {
        if (!r.result.request_id || r.result.state == SearchState::Expired ||
            r.result.state == SearchState::Superseded) { slot = &r; break; }
    }
    if (!slot) return impl_->FailureLocked("busy");
    if (!impl_->StartLocked()) return impl_->FailureLocked("worker_unavailable");
    *slot = {};
    if (!++impl_->next_request) ++impl_->next_request;
    slot->result.request_id = impl_->next_request;
    slot->result.config_revision = impl_->config.revision;
    slot->result.state = SearchState::Pending;
    slot->query = query; slot->country = country;
    slot->created = now; slot->expires = now + kTokenTtl;
    impl_->pending_search = slot->result.request_id;
    impl_->SearchBumpLocked(*slot);
    Result result;
    result.ok = true; result.pending = true; result.request_id = slot->result.request_id;
    result.config_revision = impl_->config.revision;
    impl_->io.WakeWorker();
    return result;
}
SearchResult StopwatchWeatherService::GetSearch(uint32_t request_id) const {
    std::lock_guard<std::mutex> lock(impl_->mutex);
    impl_->ExpireLocked(impl_->io.NowMs());
    auto* record = impl_->FindLocked(request_id);
    if (record) return record->result;
    SearchResult result;
    result.request_id = request_id; result.error = "unknown_request";
    return result;
}
uint32_t StopwatchWeatherService::SearchVersion(uint32_t request_id) const {
    std::lock_guard<std::mutex> lock(impl_->mutex);
    impl_->ExpireLocked(impl_->io.NowMs());
    auto* record = impl_->FindLocked(request_id);
    return record ? record->result.version : 0;
}
void StopwatchWeatherService::CancelSearch(uint32_t request_id) {
    std::lock_guard<std::mutex> lock(impl_->mutex);
    auto* record = impl_->FindLocked(request_id);
    if (!record || record->result.state == SearchState::Expired ||
        record->result.state == SearchState::Superseded) return;
    record->result.state = SearchState::Superseded; record->result.error = "cancelled";
    record->result.count = 0; impl_->SearchBumpLocked(*record);
    if (impl_->pending_search == request_id) impl_->pending_search = 0;
    impl_->io.WakeWorker();
}
Result StopwatchWeatherService::SetLocation(bool automatic, const Location& location,
                                            uint32_t expected_revision) {
    return impl_->Set(automatic, location, expected_revision);
}
Result StopwatchWeatherService::SetLocationToken(const std::string& mode, const std::string& token) {
    if (mode == "auto" && token.empty()) return impl_->Set(true, {}, kAnyRevision);
    Location location;
    uint32_t revision = 0;
    {
        std::lock_guard<std::mutex> lock(impl_->mutex);
        impl_->ExpireLocked(impl_->io.NowMs());
        if (mode != "manual" || token.empty() || token.size() > 64)
            return impl_->FailureLocked("invalid_selection");
        for (const auto& r : impl_->records) {
            if (r.result.state != SearchState::Ready || r.result.config_revision != impl_->config.revision)
                continue;
            for (size_t i = 0; i < r.result.count; ++i) {
                if (r.result.candidates[i].token == token) {
                    location = r.result.candidates[i].location;
                    revision = r.result.config_revision;
                }
            }
        }
        if (location.id.empty()) return impl_->FailureLocked("invalid_selection");
    }
    return impl_->Set(false, location, revision);
}

#endif  // CONFIG_STOPWATCH_LOCAL_APPS
