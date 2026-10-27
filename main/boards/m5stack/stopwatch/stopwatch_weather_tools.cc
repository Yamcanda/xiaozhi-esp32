#include "sdkconfig.h"

#if CONFIG_STOPWATCH_LOCAL_APPS

#include "stopwatch_weather_tools.h"

#include <charconv>
#include <string>
#include <system_error>

#include "cjson_utils.h"
#include "mcp_server.h"
#include "stopwatch_weather_service.h"

namespace {
using namespace stopwatch_weather;

const char* StateName(State state) {
    switch (state) {
        case State::Starting:
            return "starting";
        case State::Locating:
            return "locating";
        case State::Updating:
            return "updating";
        case State::Ready:
            return "ready";
        case State::Offline:
            return "offline";
        case State::Error:
            return "error";
    }
    return "unknown";
}

const char* SearchName(SearchState state) {
    switch (state) {
        case SearchState::Unknown:
            return "unknown";
        case SearchState::Pending:
            return "pending";
        case SearchState::Ready:
            return "ready";
        case SearchState::Error:
            return "error";
        case SearchState::Superseded:
            return "superseded";
        case SearchState::Expired:
            return "expired";
    }
    return "unknown";
}

bool AddLocation(cJSON* parent, const char* key, const Location& location) {
    CJsonUniquePtr object(cJSON_CreateObject());
    if (!object || !cJSON_AddStringToObject(object.get(), "id", location.id.c_str()) ||
        !cJSON_AddStringToObject(object.get(), "city", location.city.c_str()) ||
        !cJSON_AddStringToObject(object.get(), "display_name", location.display_name.c_str()) ||
        !cJSON_AddStringToObject(object.get(), "region", location.region.c_str()) ||
        !cJSON_AddStringToObject(object.get(), "country", location.country.c_str()) ||
        !cJSON_AddStringToObject(object.get(), "country_code", location.country_code.c_str())) {
        return false;
    }
    if (!location.id.empty() &&
        (!cJSON_AddNumberToObject(object.get(), "latitude", location.latitude) ||
         !cJSON_AddNumberToObject(object.get(), "longitude", location.longitude))) {
        return false;
    }
    if (!cJSON_AddItemToObject(parent, key, object.get())) {
        return false;
    }
    object.release();
    return true;
}

bool AddSnapshot(cJSON* json, const Snapshot& snapshot) {
    return cJSON_AddStringToObject(json, "mode", snapshot.automatic ? "auto" : "manual") &&
           cJSON_AddNumberToObject(json, "config_revision", snapshot.config_revision) &&
           AddLocation(json, "location", snapshot.location) &&
           cJSON_AddStringToObject(json, "weather_state", StateName(snapshot.state)) &&
           cJSON_AddStringToObject(json, "weather_error", snapshot.error.c_str()) &&
           cJSON_AddBoolToObject(json, "weather_pending", snapshot.pending) &&
           cJSON_AddBoolToObject(json, "has_data", snapshot.has_data) &&
           cJSON_AddBoolToObject(json, "cached", snapshot.cached) &&
           cJSON_AddBoolToObject(json, "stale", snapshot.stale) &&
           cJSON_AddBoolToObject(json, "age_known", snapshot.age_known) &&
           (!snapshot.has_data ||
            (AddLocation(json, "data_location", snapshot.forecast.location) &&
             cJSON_AddNumberToObject(json, "observed_at_utc", snapshot.forecast.observed_at) &&
             cJSON_AddNumberToObject(json, "utc_offset_seconds", snapshot.forecast.utc_offset)));
}

bool AddSearch(cJSON* json, const SearchResult& search) {
    CJsonUniquePtr object(cJSON_CreateObject());
    CJsonUniquePtr candidates(cJSON_CreateArray());
    const std::string id = std::to_string(search.request_id);
    if (!object || !candidates ||
        !cJSON_AddStringToObject(object.get(), "request_id", id.c_str()) ||
        !cJSON_AddStringToObject(object.get(), "state", SearchName(search.state)) ||
        !cJSON_AddStringToObject(object.get(), "error", search.error.c_str()) ||
        !cJSON_AddNumberToObject(object.get(), "config_revision", search.config_revision)) {
        return false;
    }
    // Tokens are usable only while the service still owns a ready result.
    if (search.state == SearchState::Ready) {
        for (size_t i = 0; i < search.count && i < search.candidates.size(); ++i) {
            const auto& candidate = search.candidates[i];
            CJsonUniquePtr item(cJSON_CreateObject());
            if (!item ||
                !cJSON_AddStringToObject(item.get(), "selection_token", candidate.token.c_str()) ||
                !AddLocation(item.get(), "location", candidate.location) ||
                !cJSON_AddItemToArray(candidates.get(), item.get())) {
                return false;
            }
            item.release();
        }
    }
    if (!cJSON_AddItemToObject(object.get(), "candidates", candidates.get())) {
        return false;
    }
    candidates.release();
    if (!cJSON_AddItemToObject(json, "search", object.get())) {
        return false;
    }
    object.release();
    return true;
}

ToolResult AllocationError() { return std::unexpected("Unable to allocate weather tool result"); }
}  // namespace

void RegisterStopwatchWeatherTools(StopwatchWeatherService& service) {
    auto& server = McpServer::GetInstance();
    server.AddTool(
        "self.weather.get_status",
        "Get the configured weather location and actual data/cache status. Optional request_id "
        "queries a city search without starting a network request. A pending search is NOT a saved "
        "location. Use the returned candidates to resolve ambiguity with the user before setting "
        "a location; do not repeatedly poll faster than once per second.",
        PropertyList({Property("request_id", kPropertyTypeString, std::string()).SetMaxLength(10)}),
        [&service](const PropertyList& properties) -> ToolResult {
            const auto& text = properties["request_id"].value<std::string>();
            uint32_t request_id = 0;
            if (!text.empty()) {
                auto parsed = std::from_chars(text.data(), text.data() + text.size(), request_id);
                if (parsed.ec != std::errc() || parsed.ptr != text.data() + text.size() ||
                    request_id == 0) {
                    return std::unexpected("request_id must be a nonzero decimal search ID");
                }
            }
            CJsonUniquePtr json(cJSON_CreateObject());
            if (!json || !AddSnapshot(json.get(), service.GetSnapshot()) ||
                (request_id != 0 && !AddSearch(json.get(), service.GetSearch(request_id)))) {
                return AllocationError();
            }
            return json.release();
        });
    server.AddTool(
        "self.weather.search_city",
        "Search for a weather city by name, pinyin, English or postal code; optionally append a "
        "country or region after a comma. country_code is an optional ISO two-letter code. This "
        "is asynchronous: accepted means queued, NOT location saved. Call get_status with the "
        "returned request_id after at least one second. Ask the user which candidate they intend "
        "when ambiguous, then pass its selection_token to set_location. Never invent coordinates "
        "or pick the first ambiguous result. Queries are limited to 96 UTF-8 bytes.",
        PropertyList({Property("query", kPropertyTypeString).SetMaxLength(96),
                      Property("country_code", kPropertyTypeString, std::string()).SetMaxLength(2)}),
        [&service](const PropertyList& properties) -> ToolResult {
            const auto result = service.SearchCity(properties["query"].value<std::string>(),
                                                   properties["country_code"].value<std::string>());
            if (!result.ok) {
                return std::unexpected(result.error);
            }
            CJsonUniquePtr json(cJSON_CreateObject());
            const std::string id = std::to_string(result.request_id);
            if (!json || !cJSON_AddStringToObject(json.get(), "status", "accepted") ||
                !cJSON_AddStringToObject(json.get(), "request_id", id.c_str()) ||
                !cJSON_AddNumberToObject(json.get(), "poll_after_ms", 1000) ||
                !AddSearch(json.get(), service.GetSearch(result.request_id))) {
                return AllocationError();
            }
            return json.release();
        });
    server.AddTool(
        "self.weather.set_location",
        "Save the weather location shared with Settings. mode must be auto or manual. auto "
        "restores approximate public-IP location and requires an empty selection_token. manual "
        "requires an unexpired selection_token from search_city/get_status, after resolving any "
        "ambiguity with the user. committed means configuration was persisted, not that weather "
        "was fetched; report weather_state/weather_error separately. Does not navigate the "
        "screen or start a microphone session.",
        PropertyList({Property("mode", kPropertyTypeString).SetMaxLength(6),
                      Property("selection_token", kPropertyTypeString, std::string())
                          .SetMaxLength(64)}),
        [&service](const PropertyList& properties) -> ToolResult {
            const auto result = service.SetLocationToken(
                properties["mode"].value<std::string>(),
                properties["selection_token"].value<std::string>());
            if (!result.ok) {
                return std::unexpected(result.error);
            }
            CJsonUniquePtr json(cJSON_CreateObject());
            if (!json || !cJSON_AddStringToObject(json.get(), "status", "committed") ||
                !AddSnapshot(json.get(), service.GetSnapshot())) {
                return AllocationError();
            }
            return json.release();
        });
}

#endif
