#pragma once

#include <array>
#include <cstdint>
#include <limits>
#include <string>

// Plain board-local values. No LVGL, application, network or SDK dependencies.
namespace stopwatch_weather {

inline constexpr float kMissing = std::numeric_limits<float>::quiet_NaN();
inline constexpr uint32_t kAnyRevision = UINT32_MAX;

struct Location {
    std::string id;  // Stable "geonames:<id>", "builtin:<name>" or "ip:<coordinates>".
    std::string city;  // Provider's original UTF-8, also used by MCP.
    std::string display_name;  // Controlled Chinese alias or printable ASCII fallback.
    std::string region;
    std::string country;
    std::string country_code;
    double latitude = 0;
    double longitude = 0;
};

struct City {
    const char* name;
    const char* chinese;
    double latitude;
    double longitude;
};

// Offline shortcuts, not the universe of supported cities.
inline constexpr City kCities[] = {
    {"Beijing", "北京", 39.9042, 116.4074},
    {"Shanghai", "上海", 31.2304, 121.4737},
    {"Guangzhou", "广州", 23.1291, 113.2644},
    {"Shenzhen", "深圳", 22.5431, 114.0579},
    {"Hangzhou", "杭州", 30.2741, 120.1551},
    {"Chengdu", "成都", 30.5728, 104.0668},
    {"Chongqing", "重庆", 29.5630, 106.5516},
    {"Wuhan", "武汉", 30.5928, 114.3055},
    {"Nanjing", "南京", 32.0603, 118.7969},
    {"Xi'an", "西安", 34.3416, 108.9398},
    {"Hong Kong", "香港", 22.3193, 114.1694},
    {"Taipei", "台北", 25.0330, 121.5654},
};
inline constexpr int kCityCount = sizeof(kCities) / sizeof(kCities[0]);
Location BuiltinLocation(int index);
bool ValidLocation(const Location& location);
bool SameLocation(const Location& left, const Location& right);

// Missing optional numeric measurements are NaN, never an invented zero.
// Missing/unknown WMO codes are -1; timestamps are provider UTC seconds.
struct Hour {
    int64_t time = 0;
    float temperature = kMissing;
    float precipitation = kMissing;
    int code = -1;
};
struct Day {
    int64_t time = 0;
    float low = kMissing;
    float high = kMissing;
    float precipitation = kMissing;
    int code = -1;
};
struct Forecast {
    Location location;
    int64_t observed_at = 0;
    int utc_offset = 0;  // Provider fixed offset, not a global timezone/DST rule.
    float temperature = kMissing;
    float feels_like = kMissing;
    float humidity = kMissing;
    float wind = kMissing;
    int code = -1;
    bool is_day = true;
    std::array<Hour, 4> hours{};
    std::array<Day, 3> days{};
};

enum class State { Starting, Locating, Updating, Ready, Offline, Error };
enum class SearchState { Unknown, Pending, Ready, Error, Superseded, Expired };
struct Candidate {
    std::string token;
    Location location;
};
struct SearchResult {
    uint32_t request_id = 0;
    uint32_t config_revision = 0;
    uint32_t version = 0;
    SearchState state = SearchState::Unknown;
    std::string error;
    std::array<Candidate, 5> candidates{};
    size_t count = 0;
};
struct Result {
    bool ok = false;  // accepted search / committed config; NOT weather success.
    uint32_t request_id = 0;
    uint32_t config_revision = 0;
    std::string error;  // Stable ASCII reason, empty on success.
    bool pending = false;
};
struct Snapshot {
    bool automatic = true;
    uint32_t config_revision = 0;
    uint32_t version = 0;
    Location location;  // Configured manual or effective auto location.
    Forecast forecast;
    bool has_data = false;  // False also when known age exceeds 24 hours.
    bool cached = false;
    bool stale = false;
    bool age_known = false;  // Reboot cache is historical, age unknown.
    bool pending = false;
    State state = State::Starting;
    std::string error;
    std::array<Location, 3> recent{};
    size_t recent_count = 0;
};

enum class Condition { Clear, Cloudy, Fog, Rain, Snow, Thunderstorm, Unknown };
Condition WeatherCondition(int code);
const char* ConditionText(int code, bool is_day = true);
// Format provider timestamps without reading/changing device time or timezone.
std::string FormatLocalTime(int64_t utc, int utc_offset, const char* format = "%H:%M");
std::string FormatUtcOffset(int utc_offset);

}  // namespace stopwatch_weather
