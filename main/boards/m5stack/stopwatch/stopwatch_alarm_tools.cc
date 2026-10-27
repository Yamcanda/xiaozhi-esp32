#include "sdkconfig.h"

#if CONFIG_STOPWATCH_LOCAL_APPS

#include "stopwatch_alarm_tools.h"

#include <charconv>
#include <string>
#include <system_error>

#include "cjson_utils.h"
#include "mcp_server.h"
#include "stopwatch_alarm_service.h"

namespace {
using namespace stopwatch_alarm;

bool ParseId(const std::string& text, uint32_t& id) {
    if (text.empty() || text.size() > 10)
        return false;
    for (char character : text) {
        if (character < '0' || character > '9')
            return false;
    }
    auto parsed = std::from_chars(text.data(), text.data() + text.size(), id);
    return parsed.ec == std::errc() && parsed.ptr == text.data() + text.size() && id != 0;
}

bool AddResult(cJSON* parent, const char* key, const Result& result) {
    CJsonUniquePtr object(cJSON_CreateObject());
    const auto request_id = std::to_string(result.request_id);
    const char* state = result.pending                                   ? "pending"
                        : result.ok                                      ? "committed"
                        : result.request_id == 0 && result.error.empty() ? "none"
                                                                         : "error";
    if (!object || !cJSON_AddStringToObject(object.get(), "state", state) ||
        !cJSON_AddBoolToObject(object.get(), "ok", result.ok) ||
        !cJSON_AddBoolToObject(object.get(), "pending", result.pending) ||
        !cJSON_AddStringToObject(object.get(), "request_id", request_id.c_str()) ||
        !cJSON_AddNumberToObject(object.get(), "id", result.id) ||
        !cJSON_AddNumberToObject(object.get(), "revision", result.revision) ||
        !cJSON_AddStringToObject(object.get(), "error", result.error.c_str()) ||
        !cJSON_AddItemToObject(parent, key, object.get())) {
        return false;
    }
    object.release();
    return true;
}

bool AddSnapshot(cJSON* json, const Snapshot& snapshot) {
    CJsonUniquePtr alarms(cJSON_CreateArray());
    if (!alarms || !cJSON_AddNumberToObject(json, "revision", snapshot.revision) ||
        !cJSON_AddBoolToObject(json, "initialized", snapshot.initialized) ||
        !cJSON_AddBoolToObject(json, "time_valid", snapshot.time_valid) ||
        !cJSON_AddBoolToObject(json, "pending", snapshot.pending) ||
        !cJSON_AddNumberToObject(json, "pending_count", snapshot.pending_count) ||
        !cJSON_AddStringToObject(json, "error", snapshot.error.c_str()) ||
        !cJSON_AddStringToObject(json, "time_basis", "device_local") ||
        !cJSON_AddNumberToObject(json, "capacity", kMaxAlarms) ||
        !AddResult(json, "last_result", snapshot.last_result)) {
        return false;
    }
    for (const auto& alarm : snapshot.alarms) {
        CJsonUniquePtr object(cJSON_CreateObject());
        if (!object || !cJSON_AddNumberToObject(object.get(), "id", alarm.id) ||
            !cJSON_AddNumberToObject(object.get(), "hour", alarm.hour) ||
            !cJSON_AddNumberToObject(object.get(), "minute", alarm.minute) ||
            !cJSON_AddBoolToObject(object.get(), "enabled", alarm.enabled) ||
            !cJSON_AddStringToObject(object.get(), "repeat",
                                     alarm.date.empty() ? "daily" : "once") ||
            !cJSON_AddStringToObject(object.get(), "date", alarm.date.c_str()) ||
            !cJSON_AddStringToObject(object.get(), "reminder", alarm.reminder.c_str()) ||
            !cJSON_AddStringToObject(object.get(), "last_triggered_date",
                                     alarm.last_triggered_date.c_str()) ||
            !cJSON_AddItemToArray(alarms.get(), object.get())) {
            return false;
        }
        object.release();
    }
    if (!cJSON_AddItemToObject(json, "alarms", alarms.get()))
        return false;
    alarms.release();
    return true;
}

ToolResult AllocationError() { return std::unexpected("Unable to allocate alarm tool result"); }

ToolResult Accepted(const Result& result) {
    if (!result.ok)
        return std::unexpected(result.error);
    CJsonUniquePtr json(cJSON_CreateObject());
    const auto request_id = std::to_string(result.request_id);
    if (!json || !cJSON_AddStringToObject(json.get(), "status", "accepted") ||
        !cJSON_AddStringToObject(json.get(), "request_id", request_id.c_str()) ||
        !cJSON_AddNumberToObject(json.get(), "poll_after_ms", 1000) ||
        !cJSON_AddStringToObject(json.get(), "time_basis", "device_local") ||
        !cJSON_AddStringToObject(json.get(), "meaning",
                                 "Queued only; query get_status request before claiming saved. "
                                 "This is not proof the user has been reminded.")) {
        return AllocationError();
    }
    return json.release();
}
}  // namespace

void RegisterStopwatchAlarmTools(StopwatchAlarmService& service) {
    auto& server = McpServer::GetInstance();
    server.AddTool(
        "self.alarm.get_status",
        "Read StopWatch alarms and device-local clock validity without changing anything. "
        "With request_id, also get that accepted operation's pending/committed/error result. "
        "Only committed means persisted; accepted/pending is not success. Results retain the "
        "latest 16 requests; last_result is the most recently completed operation. Alarm IDs are "
        "stable integers, not list positions. Empty date means daily; a date means once. "
        "last_triggered_date records scheduling, NOT proof that speech or user delivery succeeded. "
        "If time_valid is false, explain that the local clock must synchronize. Do not poll faster "
        "than once per second and never claim a reminder was spoken from this tool alone.",
        PropertyList({Property("request_id", kPropertyTypeString, std::string()).SetMaxLength(10)}),
        [&service](const PropertyList& properties) -> ToolResult {
            const auto& text = properties["request_id"].value<std::string>();
            uint32_t request_id = 0;
            if (!text.empty() && !ParseId(text, request_id))
                return std::unexpected("request_id must be a nonzero decimal ID");
            CJsonUniquePtr json(cJSON_CreateObject());
            if (!json || !AddSnapshot(json.get(), service.GetSnapshot()) ||
                (request_id && !AddResult(json.get(), "request", service.GetRequest(request_id)))) {
                return AllocationError();
            }
            return json.release();
        });
    server.AddTool(
        "self.alarm.add",
        "Queue a StopWatch alarm using the DEVICE LOCAL time, not UTC. hour is 0..23 and minute "
        "0..59. Maximum 16 alarms. Empty/omitted date repeats every day; YYYY-MM-DD (2020..9999, "
        "Gregorian) runs once on that local date and is disabled after scheduling. Empty/omitted "
        "reminder is a normal ringing alarm. A nonempty reminder (at most 512 UTF-8 bytes, no "
        "control characters) asks the board to open XiaoZhi interaction with that text when due; "
        "it does not speak now. Confirm the intended time/date/text with the user. The reply is "
        "accepted, NOT saved: query get_status with request_id after at least one second and "
        "only report saved on committed. Unsynchronized time never triggers; missed minutes are "
        "not replayed. Never claim the user has already been reminded.",
        PropertyList({Property("hour", kPropertyTypeInteger, 0, 23),
                      Property("minute", kPropertyTypeInteger, 0, 59),
                      Property("reminder", kPropertyTypeString, std::string())
                          .SetMaxLength(kMaxReminderBytes),
                      Property("date", kPropertyTypeString, std::string()).SetMaxLength(10)}),
        [&service](const PropertyList& properties) -> ToolResult {
            return Accepted(service.Add(properties["hour"].value<int>(),
                                        properties["minute"].value<int>(),
                                        properties["reminder"].value<std::string>(),
                                        properties["date"].value<std::string>()));
        });
    server.AddTool(
        "self.alarm.set_enabled",
        "Queue enabling or disabling one StopWatch alarm by stable id from get_status (never a "
        "list index). Enabling preserves the reminder/date and today's deduplication; it does not "
        "replay a passed minute or a completed one-time alarm. DEVICE LOCAL time applies. "
        "accepted is NOT persisted; query get_status(request_id) for committed/error. "
        "Does not start speech, navigate the screen, or prove the user was reminded.",
        PropertyList({Property("id", kPropertyTypeString).SetMaxLength(10),
                      Property("enabled", kPropertyTypeBoolean)}),
        [&service](const PropertyList& properties) -> ToolResult {
            uint32_t id = 0;
            if (!ParseId(properties["id"].value<std::string>(), id))
                return std::unexpected("id must be a nonzero decimal alarm ID");
            return Accepted(service.SetEnabled(id, properties["enabled"].value<bool>()));
        });
    server.AddTool(
        "self.alarm.remove",
        "Queue permanent removal of one StopWatch alarm by stable id from get_status, not its "
        "list position. Confirm the intended alarm with the user before deletion. accepted only "
        "means queued; query get_status(request_id) after at least one second and report removal "
        "only on committed. Does not stop an already handed-off voice session and never proves "
        "that a reminder was spoken. All alarm times/dates use DEVICE LOCAL time.",
        PropertyList({Property("id", kPropertyTypeString).SetMaxLength(10)}),
        [&service](const PropertyList& properties) -> ToolResult {
            uint32_t id = 0;
            if (!ParseId(properties["id"].value<std::string>(), id))
                return std::unexpected("id must be a nonzero decimal alarm ID");
            return Accepted(service.Remove(id));
        });
}

#endif  // CONFIG_STOPWATCH_LOCAL_APPS
