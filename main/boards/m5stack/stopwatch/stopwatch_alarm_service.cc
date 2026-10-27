#include "sdkconfig.h"

#if CONFIG_STOPWATCH_LOCAL_APPS

#include "stopwatch_alarm_service.h"

#include <nvs.h>

#include <algorithm>
#include <array>
#include <atomic>
#include <cstdio>
#include <mutex>
#include <utility>

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

namespace stopwatch_alarm::detail {
struct Config {
    uint32_t revision = 0;
    uint32_t next_id = 1;  // Zero means exhausted; deleted IDs are never reused.
    std::vector<Alarm> alarms;
};
}  // namespace stopwatch_alarm::detail

namespace {
using namespace stopwatch_alarm;
using stopwatch_alarm::detail::Config;
constexpr const char* kNamespace = "sw_alarm";
constexpr const char* kStorageKey = "storage_v1";
constexpr uint32_t kMagic = 0x4c415753;  // SWAL, little endian.
constexpr uint16_t kStorageVersion = 1;

class NvsHandle {
public:
    ~NvsHandle() {
        if (open)
            nvs_close(value);
    }
    nvs_handle_t value = 0;
    bool open = false;
};

class DevicePlatform final : public Platform {
public:
    ReadStatus ReadBlob(std::string& bytes, size_t limit) override {
        bytes.clear();
        NvsHandle handle;
        auto error = nvs_open(kNamespace, NVS_READONLY, &handle.value);
        if (error == ESP_ERR_NVS_NOT_FOUND)
            return ReadStatus::Missing;
        if (error != ESP_OK)
            return ReadStatus::Error;
        handle.open = true;
        size_t size = 0;
        error = nvs_get_blob(handle.value, kStorageKey, nullptr, &size);
        if (error == ESP_ERR_NVS_NOT_FOUND)
            return ReadStatus::Missing;
        if (error != ESP_OK || size == 0 || size > limit)
            return ReadStatus::Error;
        bytes.resize(size);
        if (nvs_get_blob(handle.value, kStorageKey, bytes.data(), &size) != ESP_OK ||
            size != bytes.size()) {
            bytes.clear();
            return ReadStatus::Error;
        }
        return ReadStatus::Ok;
    }
    bool WriteBlob(const std::string& bytes) override {
        if (bytes.empty() || bytes.size() > kBlobLimit)
            return false;
        NvsHandle handle;
        if (nvs_open(kNamespace, NVS_READWRITE, &handle.value) != ESP_OK)
            return false;
        handle.open = true;
        return nvs_set_blob(handle.value, kStorageKey, bytes.data(), bytes.size()) == ESP_OK &&
               nvs_commit(handle.value) == ESP_OK;
    }
    bool StartWorker(void (*entry)(void*), void* argument) override {
        TaskHandle_t task = nullptr;
        if (xTaskCreate(entry, "sw_alarm", 6144, argument, 2, &task) != pdPASS)
            return false;
        std::lock_guard<std::mutex> lock(task_mutex_);
        task_ = task;
        return true;
    }
    void WakeWorker() override {
        std::lock_guard<std::mutex> lock(task_mutex_);
        if (task_)
            xTaskNotifyGive(task_);
    }
    void WaitWorker(uint32_t milliseconds) override {
        ulTaskNotifyTake(pdTRUE, pdMS_TO_TICKS(milliseconds));
    }
    void JoinWorker() override {
        while (!finished_.load())
            vTaskDelay(pdMS_TO_TICKS(10));
    }
    void WorkerFinished() override {
        {
            std::lock_guard<std::mutex> lock(task_mutex_);
            task_ = nullptr;
        }
        finished_.store(true);
        vTaskDelete(nullptr);
    }

private:
    std::mutex task_mutex_;
    TaskHandle_t task_ = nullptr;
    std::atomic<bool> finished_{false};
};

bool ValidReminder(const std::string& text) {
    if (text.size() > kMaxReminderBytes)
        return false;
    for (size_t i = 0; i < text.size();) {
        const auto first = static_cast<unsigned char>(text[i++]);
        uint32_t code = first;
        size_t remaining = 0;
        uint32_t minimum = 0;
        if (first >= 0xc2 && first <= 0xdf) {
            code = first & 0x1f;
            remaining = 1;
            minimum = 0x80;
        } else if (first >= 0xe0 && first <= 0xef) {
            code = first & 0x0f;
            remaining = 2;
            minimum = 0x800;
        } else if (first >= 0xf0 && first <= 0xf4) {
            code = first & 0x07;
            remaining = 3;
            minimum = 0x10000;
        } else if (first >= 0x80) {
            return false;
        }
        if (remaining > text.size() - i)
            return false;
        while (remaining--) {
            const auto next = static_cast<unsigned char>(text[i++]);
            if ((next & 0xc0) != 0x80)
                return false;
            code = (code << 6) | (next & 0x3f);
        }
        if (code < minimum || code > 0x10ffff || (code >= 0xd800 && code <= 0xdfff) ||
            code < 0x20 || (code >= 0x7f && code <= 0x9f)) {
            return false;
        }
    }
    return true;
}

bool ValidDay(int year, int month, int day) {
    constexpr int days[] = {31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31};
    if (year < 2020 || year > 9999 || month < 1 || month > 12 || day < 1)
        return false;
    const bool leap = year % 4 == 0 && (year % 100 != 0 || year % 400 == 0);
    return day <= days[month - 1] + (month == 2 && leap ? 1 : 0);
}

bool ParseDate(const std::string& text, uint32_t& value) {
    value = 0;
    if (text.empty())
        return true;
    if (text.size() != 10 || text[4] != '-' || text[7] != '-')
        return false;
    for (size_t i = 0; i < text.size(); ++i) {
        if (i != 4 && i != 7 && (text[i] < '0' || text[i] > '9'))
            return false;
    }
    const int year =
        (text[0] - '0') * 1000 + (text[1] - '0') * 100 + (text[2] - '0') * 10 + text[3] - '0';
    const int month = (text[5] - '0') * 10 + text[6] - '0';
    const int day = (text[8] - '0') * 10 + text[9] - '0';
    if (!ValidDay(year, month, day))
        return false;
    value = static_cast<uint32_t>(year * 10000 + month * 100 + day);
    return true;
}

std::string DateText(uint32_t value) {
    if (!value)
        return {};
    char text[16];
    std::snprintf(text, sizeof(text), "%04u-%02u-%02u", static_cast<unsigned>(value / 10000),
                  static_cast<unsigned>(value / 100 % 100), static_cast<unsigned>(value % 100));
    return text;
}

bool ValidDateValue(uint32_t value) {
    return value == 0 || ValidDay(value / 10000, value / 100 % 100, value % 100);
}

void Put(std::string& bytes, uint32_t value, size_t width) {
    for (size_t i = 0; i < width; ++i)
        bytes.push_back(static_cast<char>(value >> (i * 8)));
}

uint32_t Checksum(const std::string& bytes, size_t size) {
    uint32_t crc = 0xffffffff;
    for (size_t i = 0; i < size; ++i) {
        crc ^= static_cast<unsigned char>(bytes[i]);
        for (int bit = 0; bit < 8; ++bit)
            crc = (crc >> 1) ^ (0xedb88320 & (0u - (crc & 1u)));
    }
    return ~crc;
}

bool Encode(const Config& config, std::string& bytes) {
    if (config.alarms.size() > kMaxAlarms || config.revision == kAnyRevision)
        return false;
    bytes.clear();
    bytes.reserve(20 + config.alarms.size() * 32);
    Put(bytes, kMagic, 4);
    Put(bytes, kStorageVersion, 2);
    Put(bytes, config.alarms.size(), 2);
    Put(bytes, config.revision, 4);
    Put(bytes, config.next_id, 4);
    for (const auto& alarm : config.alarms) {
        uint32_t date = 0, last = 0;
        if (alarm.id == 0 || alarm.hour < 0 || alarm.hour > 23 || alarm.minute < 0 ||
            alarm.minute > 59 || !ValidReminder(alarm.reminder) || !ParseDate(alarm.date, date) ||
            !ParseDate(alarm.last_triggered_date, last) ||
            (date != 0 && last != 0 && date != last)) {
            return false;
        }
        Put(bytes, alarm.id, 4);
        Put(bytes, alarm.hour, 1);
        Put(bytes, alarm.minute, 1);
        Put(bytes, alarm.enabled ? 1 : 0, 1);
        Put(bytes, 0, 1);
        Put(bytes, date, 4);
        Put(bytes, last, 4);
        Put(bytes, alarm.reminder.size(), 2);
        bytes += alarm.reminder;
    }
    Put(bytes, Checksum(bytes, bytes.size()), 4);
    return bytes.size() <= kBlobLimit;
}

bool Decode(const std::string& bytes, Config& config) {
    if (bytes.size() < 20 || bytes.size() > kBlobLimit)
        return false;
    size_t at = 0;
    const auto get = [&](size_t width, uint32_t& value) {
        if (width > bytes.size() - at)
            return false;
        value = 0;
        for (size_t i = 0; i < width; ++i)
            value |= static_cast<uint32_t>(static_cast<unsigned char>(bytes[at++])) << (i * 8);
        return true;
    };
    uint32_t magic = 0, version = 0, count = 0;
    Config decoded;
    if (!get(4, magic) || magic != kMagic || !get(2, version) || version != kStorageVersion ||
        !get(2, count) || count > kMaxAlarms || !get(4, decoded.revision) ||
        decoded.revision == kAnyRevision || !get(4, decoded.next_id)) {
        return false;
    }
    decoded.alarms.reserve(count);
    for (uint32_t i = 0; i < count; ++i) {
        Alarm alarm;
        uint32_t hour = 0, minute = 0, enabled = 0, reserved = 0, date = 0, last = 0, length = 0;
        if (!get(4, alarm.id) || !alarm.id || !get(1, hour) || hour > 23 || !get(1, minute) ||
            minute > 59 || !get(1, enabled) || enabled > 1 || !get(1, reserved) || reserved != 0 ||
            !get(4, date) || !ValidDateValue(date) || !get(4, last) || !ValidDateValue(last) ||
            (date && last && date != last) || !get(2, length) || length > kMaxReminderBytes ||
            length > bytes.size() - at || (decoded.next_id && alarm.id >= decoded.next_id)) {
            return false;
        }
        for (const auto& other : decoded.alarms) {
            if (other.id == alarm.id)
                return false;
        }
        alarm.hour = hour;
        alarm.minute = minute;
        alarm.enabled = enabled != 0;
        alarm.date = DateText(date);
        alarm.last_triggered_date = DateText(last);
        alarm.reminder.assign(bytes, at, length);
        at += length;
        if (!ValidReminder(alarm.reminder))
            return false;
        decoded.alarms.push_back(std::move(alarm));
    }
    uint32_t checksum = 0;
    if (at != bytes.size() - 4 || !get(4, checksum) ||
        checksum != Checksum(bytes, bytes.size() - 4)) {
        return false;
    }
    config = std::move(decoded);
    return true;
}

bool NextRevision(Config& config) {
    if (config.revision >= kAnyRevision - 1)
        return false;
    ++config.revision;
    return true;
}
}  // namespace

struct StopwatchAlarmService::Impl {
    explicit Impl(Platform& platform) : io(platform) {}
    enum class Kind { Add, SetEnabled, Remove };
    struct Request {
        Kind kind = Kind::Add;
        uint32_t request_id = 0;
        uint32_t id = 0;
        uint32_t expected_revision = kAnyRevision;
        int hour = 0;
        int minute = 0;
        bool enabled = false;
        std::string reminder;
        std::string date;
    };
    struct Due {
        uint32_t id = 0;
        uint32_t date = 0;
        int hour = 0;
        int minute = 0;
    };
    Platform& io;
    mutable std::mutex mutex;
    std::atomic<bool> stop{false};
    bool worker_started = false;
    bool load_requested = false;
    bool loading = false;
    bool initialized = false;
    bool time_valid = false;
    uint32_t version = 1;
    uint32_t today = 0;
    int hour = 0;
    int minute = 0;
    std::string error;
    Config config;
    std::array<Request, kQueueCapacity> queue{};
    size_t queue_head = 0;
    size_t queue_count = 0;
    bool request_active = false;
    uint32_t next_request = 1;
    std::array<Result, kResultCapacity> results{};
    size_t result_next = 0;
    Result last_result;
    std::array<Due, kMaxAlarms> due{};
    size_t due_count = 0;
    std::array<Due, kMaxAlarms> active_due{};
    size_t active_due_count = 0;
    std::array<Alarm, kMaxAlarms> outbox{};
    size_t outbox_count = 0;

    void BumpLocked() {
        if (!++version)
            ++version;
    }
    Result FailureLocked(const std::string& reason) const {
        Result result;
        result.revision = config.revision;
        result.error = reason;
        return result;
    }
    bool StartLocked() {
        if (worker_started)
            return true;
        load_requested = true;
        if (!io.StartWorker(Entry, this)) {
            load_requested = false;
            error = "worker_unavailable";
            BumpLocked();
            return false;
        }
        worker_started = true;
        BumpLocked();
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
        {
            std::lock_guard<std::mutex> lock(mutex);
            started = worker_started;
        }
        if (started) {
            io.WakeWorker();
            io.JoinWorker();
        }
    }
    Result Submit(Request request) {
        Result result;
        {
            std::lock_guard<std::mutex> lock(mutex);
            if (!StartLocked())
                return FailureLocked("worker_unavailable");
            if (!initialized)
                return FailureLocked("not_ready");
            if (queue_count + (request_active ? 1 : 0) >= kQueueCapacity)
                return FailureLocked("busy");
            if (!next_request)
                return FailureLocked("request_id_exhausted");
            request.request_id = next_request++;
            result.ok = true;
            result.pending = true;
            result.request_id = request.request_id;
            result.id = request.id;
            result.revision = config.revision;
            queue[(queue_head + queue_count) % queue.size()] = std::move(request);
            ++queue_count;
            results[result_next] = result;
            result_next = (result_next + 1) % results.size();
            BumpLocked();
        }
        io.WakeWorker();
        return result;
    }
    void Load() {
        std::string bytes;
        Config loaded;
        const auto status = io.ReadBlob(bytes, kBlobLimit);
        const bool valid =
            status == ReadStatus::Missing || (status == ReadStatus::Ok && Decode(bytes, loaded));
        std::lock_guard<std::mutex> lock(mutex);
        loading = false;
        if (valid) {
            config = std::move(loaded);
            initialized = true;
            error.clear();
        } else {
            error = status == ReadStatus::Error ? "storage_failed" : "storage_corrupt";
        }
        BumpLocked();
    }
    void PurgeLocked(uint32_t id) {
        size_t kept = 0;
        for (size_t i = 0; i < due_count; ++i) {
            if (due[i].id != id)
                due[kept++] = due[i];
        }
        due_count = kept;
        kept = 0;
        for (size_t i = 0; i < outbox_count; ++i) {
            if (outbox[i].id != id) {
                if (kept != i)
                    outbox[kept] = std::move(outbox[i]);
                ++kept;
            }
        }
        for (size_t i = kept; i < outbox_count; ++i)
            outbox[i] = {};
        outbox_count = kept;
    }
    void CompleteLocked(Result result) {
        request_active = false;
        for (auto& previous : results) {
            if (previous.request_id == result.request_id) {
                previous = result;
                break;
            }
        }
        last_result = std::move(result);
        error = last_result.error;
        BumpLocked();
    }
    void Execute(const Request& request) {
        Config proposed;
        Result result;
        bool changed = false;
        {
            std::lock_guard<std::mutex> lock(mutex);
            result.request_id = request.request_id;
            result.id = request.id;
            result.revision = config.revision;
            if (request.expected_revision != kAnyRevision &&
                request.expected_revision != config.revision) {
                result.error = "conflict";
            } else {
                proposed = config;
                if (request.kind == Kind::Add) {
                    if (proposed.alarms.size() == kMaxAlarms)
                        result.error = "full";
                    else if (!proposed.next_id)
                        result.error = "id_exhausted";
                    else {
                        Alarm alarm;
                        alarm.id = proposed.next_id++;
                        alarm.hour = request.hour;
                        alarm.minute = request.minute;
                        alarm.reminder = request.reminder;
                        alarm.date = request.date;
                        result.id = alarm.id;
                        proposed.alarms.push_back(std::move(alarm));
                        changed = true;
                    }
                } else {
                    auto found =
                        std::find_if(proposed.alarms.begin(), proposed.alarms.end(),
                                     [&](const Alarm& alarm) { return alarm.id == request.id; });
                    if (found == proposed.alarms.end())
                        result.error = "not_found";
                    else if (request.kind == Kind::Remove) {
                        proposed.alarms.erase(found);
                        changed = true;
                    } else if (found->enabled != request.enabled) {
                        found->enabled = request.enabled;
                        changed = true;
                    }
                }
                if (changed && !NextRevision(proposed))
                    result.error = "revision_exhausted";
            }
            if (!result.error.empty() || !changed) {
                result.ok = result.error.empty();
                if (!result.ok && request.kind == Kind::Add)
                    result.id = 0;
                CompleteLocked(std::move(result));
                return;
            }
        }
        // The worker is the only config writer. Never hold mutex during NVS.
        std::string bytes;
        const bool saved = Encode(proposed, bytes) && io.WriteBlob(bytes);
        std::lock_guard<std::mutex> lock(mutex);
        if (saved) {
            config = std::move(proposed);
            result.ok = true;
            result.revision = config.revision;
            if (request.kind == Kind::Remove ||
                (request.kind == Kind::SetEnabled && !request.enabled)) {
                PurgeLocked(request.id);
            }
        } else {
            result.error = "storage_failed";
            // A failed Add has no stable ID; its proposed ID was never published.
            if (request.kind == Kind::Add)
                result.id = 0;
        }
        CompleteLocked(std::move(result));
    }
    bool MatchesClockLocked(const Due& candidate) const {
        return time_valid && today == candidate.date && hour == candidate.hour &&
               minute == candidate.minute;
    }
    bool CommitDue() {
        Config proposed;
        std::array<Alarm, kMaxAlarms> delivery{};
        size_t delivery_count = 0;
        {
            std::lock_guard<std::mutex> lock(mutex);
            if (!due_count || outbox_count == outbox.size())
                return false;
            proposed = config;
            const size_t available = outbox.size() - outbox_count;
            size_t kept = 0;
            for (size_t i = 0; i < due_count; ++i) {
                const auto& candidate = due[i];
                if (!MatchesClockLocked(candidate))
                    continue;
                auto found =
                    std::find_if(proposed.alarms.begin(), proposed.alarms.end(),
                                 [&](const Alarm& alarm) { return alarm.id == candidate.id; });
                if (found == proposed.alarms.end() || !found->enabled ||
                    found->hour != candidate.hour || found->minute != candidate.minute)
                    continue;
                const std::string date = DateText(candidate.date);
                if ((!found->date.empty() && found->date != date) ||
                    (!found->last_triggered_date.empty() && found->last_triggered_date >= date))
                    continue;
                if (delivery_count == available) {
                    due[kept++] = candidate;
                    continue;
                }
                found->last_triggered_date = date;
                if (!found->date.empty())
                    found->enabled = false;
                delivery[delivery_count] = *found;
                active_due[delivery_count++] = candidate;
            }
            due_count = kept;
            active_due_count = delivery_count;
            if (!delivery_count)
                return true;
            if (!NextRevision(proposed)) {
                active_due_count = 0;
                error = "revision_exhausted";
                BumpLocked();
                return true;
            }
        }
        std::string bytes;
        const bool saved = Encode(proposed, bytes) && io.WriteBlob(bytes);
        std::lock_guard<std::mutex> lock(mutex);
        if (saved) {
            config = std::move(proposed);
            for (size_t i = 0; i < delivery_count; ++i) {
                // A sync/backwards correction while NVS was blocked must not
                // emit a stale reminder. Its durable high-water mark remains.
                if (MatchesClockLocked(active_due[i]))
                    outbox[outbox_count++] = std::move(delivery[i]);
            }
            error.clear();
        } else {
            error = "storage_failed";
        }
        active_due_count = 0;
        BumpLocked();
        return true;
    }
    void Run() {
        while (!stop.load()) {
            bool load = false, execute = false;
            Request request;
            {
                std::lock_guard<std::mutex> lock(mutex);
                if (load_requested) {
                    load_requested = false;
                    loading = true;
                    load = true;
                } else if (queue_count) {
                    request = std::move(queue[queue_head]);
                    queue[queue_head] = {};
                    queue_head = (queue_head + 1) % queue.size();
                    --queue_count;
                    request_active = true;
                    execute = true;
                }
            }
            if (load)
                Load();
            else if (execute) {
                Execute(request);
                if (!stop.load())
                    CommitDue();  // CRUD cannot starve a due minute.
            } else if (!CommitDue())
                io.WaitWorker(1000);
        }
    }
};

StopwatchAlarmService::StopwatchAlarmService()
    : owned_platform_(std::make_unique<DevicePlatform>()),
      impl_(std::make_unique<Impl>(*owned_platform_)) {}
StopwatchAlarmService::StopwatchAlarmService(Platform& platform)
    : impl_(std::make_unique<Impl>(platform)) {}
StopwatchAlarmService::~StopwatchAlarmService() { impl_->Shutdown(); }

bool StopwatchAlarmService::Initialize() {
    bool started;
    {
        std::lock_guard<std::mutex> lock(impl_->mutex);
        started = impl_->StartLocked();
        if (started && !impl_->initialized && !impl_->loading)
            impl_->load_requested = true;
    }
    if (started)
        impl_->io.WakeWorker();
    return started;
}

Snapshot StopwatchAlarmService::GetSnapshot() const {
    std::lock_guard<std::mutex> lock(impl_->mutex);
    Snapshot snapshot;
    snapshot.revision = impl_->config.revision;
    snapshot.version = impl_->version;
    snapshot.initialized = impl_->initialized;
    snapshot.time_valid = impl_->time_valid;
    snapshot.pending_count = impl_->queue_count + (impl_->request_active ? 1 : 0);
    snapshot.pending = snapshot.pending_count || impl_->load_requested || impl_->loading ||
                       impl_->due_count || impl_->active_due_count;
    snapshot.error = impl_->error;
    snapshot.alarms = impl_->config.alarms;
    snapshot.last_result = impl_->last_result;
    return snapshot;
}

uint32_t StopwatchAlarmService::Version() const {
    std::lock_guard<std::mutex> lock(impl_->mutex);
    return impl_->version;
}

Result StopwatchAlarmService::Add(int hour, int minute, const std::string& reminder,
                                  const std::string& date) {
    uint32_t parsed = 0;
    const char* invalid = hour < 0 || hour > 23 || minute < 0 || minute > 59 ? "invalid_time"
                          : !ValidReminder(reminder)                         ? "invalid_reminder"
                          : !ParseDate(date, parsed)                         ? "invalid_date"
                                                                             : nullptr;
    if (invalid) {
        std::lock_guard<std::mutex> lock(impl_->mutex);
        return impl_->FailureLocked(invalid);
    }
    Impl::Request request;
    request.kind = Impl::Kind::Add;
    request.hour = hour;
    request.minute = minute;
    request.reminder = reminder;
    request.date = date;
    return impl_->Submit(std::move(request));
}

Result StopwatchAlarmService::SetEnabled(uint32_t id, bool enabled, uint32_t expected_revision) {
    if (!id) {
        std::lock_guard<std::mutex> lock(impl_->mutex);
        return impl_->FailureLocked("invalid_id");
    }
    Impl::Request request;
    request.kind = Impl::Kind::SetEnabled;
    request.id = id;
    request.enabled = enabled;
    request.expected_revision = expected_revision;
    return impl_->Submit(std::move(request));
}

Result StopwatchAlarmService::Remove(uint32_t id, uint32_t expected_revision) {
    if (!id) {
        std::lock_guard<std::mutex> lock(impl_->mutex);
        return impl_->FailureLocked("invalid_id");
    }
    Impl::Request request;
    request.kind = Impl::Kind::Remove;
    request.id = id;
    request.expected_revision = expected_revision;
    return impl_->Submit(std::move(request));
}

Result StopwatchAlarmService::GetRequest(uint32_t request_id) const {
    std::lock_guard<std::mutex> lock(impl_->mutex);
    for (const auto& result : impl_->results) {
        if (request_id && result.request_id == request_id)
            return result;
    }
    auto result = impl_->FailureLocked("unknown_request");
    result.request_id = request_id;
    return result;
}

std::vector<Alarm> StopwatchAlarmService::Poll(time_t now) {
    std::tm local{};
#ifdef _WIN32
    const bool converted = localtime_s(&local, &now) == 0;
#else
    const bool converted = localtime_r(&now, &local) != nullptr;
#endif
    const bool valid = now >= 0 && converted && local.tm_year >= 120 && local.tm_year <= 8099 &&
                       ValidDay(local.tm_year + 1900, local.tm_mon + 1, local.tm_mday) &&
                       local.tm_hour >= 0 && local.tm_hour <= 23 && local.tm_min >= 0 &&
                       local.tm_min <= 59;
    const uint32_t today = valid ? static_cast<uint32_t>((local.tm_year + 1900) * 10000 +
                                                         (local.tm_mon + 1) * 100 + local.tm_mday)
                                 : 0;
    const std::string date = DateText(today);
    std::vector<Alarm> delivery;
    bool wake = false;
    {
        std::lock_guard<std::mutex> lock(impl_->mutex);
        const bool clock_changed =
            impl_->time_valid != valid || impl_->today != today ||
            (valid && (impl_->hour != local.tm_hour || impl_->minute != local.tm_min));
        impl_->time_valid = valid;
        impl_->today = today;
        impl_->hour = local.tm_hour;
        impl_->minute = local.tm_min;
        if (clock_changed) {
            impl_->due_count = 0;
            impl_->BumpLocked();
        }
        delivery.reserve(impl_->outbox_count);
        for (size_t i = 0; i < impl_->outbox_count; ++i) {
            auto& alarm = impl_->outbox[i];
            if (valid && alarm.last_triggered_date == date && alarm.hour == local.tm_hour &&
                alarm.minute == local.tm_min) {
                delivery.push_back(std::move(alarm));
            }
            alarm = {};
        }
        impl_->outbox_count = 0;
        if (valid && impl_->initialized) {
            for (const auto& alarm : impl_->config.alarms) {
                if (!alarm.enabled || alarm.hour != local.tm_hour || alarm.minute != local.tm_min ||
                    (!alarm.date.empty() && alarm.date != date) ||
                    (!alarm.last_triggered_date.empty() && alarm.last_triggered_date >= date))
                    continue;
                const auto same = [&](const Impl::Due& due) {
                    return due.id == alarm.id && due.date == today;
                };
                bool queued = false;
                for (size_t i = 0; i < impl_->due_count; ++i)
                    queued |= same(impl_->due[i]);
                for (size_t i = 0; i < impl_->active_due_count; ++i)
                    queued |= same(impl_->active_due[i]);
                if (!queued && impl_->due_count < impl_->due.size()) {
                    impl_->due[impl_->due_count++] = {alarm.id, today, local.tm_hour, local.tm_min};
                    wake = true;
                }
            }
        }
        if (wake)
            impl_->BumpLocked();
        // Draining an outbox can release space for a previously queued batch.
        wake |= impl_->due_count != 0;
    }
    if (wake)
        impl_->io.WakeWorker();
    return delivery;
}

#endif  // CONFIG_STOPWATCH_LOCAL_APPS
