#include "sdkconfig.h"

#if CONFIG_STOPWATCH_LOCAL_APPS

#include "stopwatch_weather_data.h"

#include <algorithm>
#include <cmath>
#include <cstdio>
#include <ctime>

namespace stopwatch_weather {
namespace {
bool Utf8(const std::string& text) {
    const auto* p = reinterpret_cast<const unsigned char*>(text.data());
    size_t i = 0;
    while (i < text.size()) {
        unsigned c = p[i++];
        if (!c || c < 0x20 || c == 0x7f) return false;
        if (c < 0x80) continue;
        unsigned n = 0, value = 0, minimum = 0;
        if (c >= 0xc2 && c <= 0xdf) { n = 1; value = c & 31; minimum = 0x80; }
        else if (c >= 0xe0 && c <= 0xef) { n = 2; value = c & 15; minimum = 0x800; }
        else if (c >= 0xf0 && c <= 0xf4) { n = 3; value = c & 7; minimum = 0x10000; }
        else return false;
        if (i + n > text.size()) return false;
        while (n--) {
            c = p[i++];
            if ((c & 0xc0) != 0x80) return false;
            value = (value << 6) | (c & 63);
        }
        if (value < minimum || value > 0x10ffff || (value >= 0xd800 && value <= 0xdfff))
            return false;
    }
    return true;
}
bool Ascii(const std::string& value) {
    return std::all_of(value.begin(), value.end(), [](unsigned char c) {
        return c >= 0x20 && c < 0x7f;
    });
}
bool DisplaySafe(const std::string& value) {
    if (value.empty()) return false;
    if (Ascii(value)) return true;
    for (const auto& city : kCities) if (value == city.chinese) return true;
    return false;
}
bool Timestamp(int64_t value) { return value >= 946684800 && value <= 4102444800LL; }
}  // namespace

Location BuiltinLocation(int index) {
    if (index < 0 || index >= kCityCount) return {};
    const auto& city = kCities[index];
    Location result;
    result.id = std::string("builtin:") + city.name;
    result.city = city.name;
    result.display_name = city.chinese;
    result.latitude = city.latitude;
    result.longitude = city.longitude;
    result.country_code = index == 10 ? "HK" : (index == 11 ? "TW" : "CN");
    result.country = index == 11 ? "Taiwan" : "China";
    return result;
}
bool ValidLocation(const Location& location) {
    return !location.id.empty() && location.id.size() <= 64 && Ascii(location.id) &&
           !location.city.empty() && location.city.size() <= 96 && Utf8(location.city) &&
           location.display_name.size() <= 96 && DisplaySafe(location.display_name) &&
           location.region.size() <= 64 && (location.region.empty() || Utf8(location.region)) &&
           location.country.size() <= 64 && (location.country.empty() || Utf8(location.country)) &&
           location.country_code.size() == 2 &&
           location.country_code[0] >= 'A' && location.country_code[0] <= 'Z' &&
           location.country_code[1] >= 'A' && location.country_code[1] <= 'Z' &&
           std::isfinite(location.latitude) && std::abs(location.latitude) <= 90 &&
           std::isfinite(location.longitude) && std::abs(location.longitude) <= 180;
}
bool SameLocation(const Location& left, const Location& right) {
    return !left.id.empty() && left.id == right.id &&
           left.country_code == right.country_code &&
           std::abs(left.latitude - right.latitude) < 0.000001 &&
           std::abs(left.longitude - right.longitude) < 0.000001;
}
Condition WeatherCondition(int code) {
    if (code == 0) return Condition::Clear;
    if (code >= 1 && code <= 3) return Condition::Cloudy;
    if (code == 45 || code == 48) return Condition::Fog;
    if (code == 51 || code == 53 || code == 55 || code == 56 || code == 57 ||
        code == 61 || code == 63 || code == 65 || code == 66 || code == 67 ||
        code == 80 || code == 81 || code == 82) return Condition::Rain;
    if (code == 71 || code == 73 || code == 75 || code == 77 || code == 85 || code == 86)
        return Condition::Snow;
    if (code == 95 || code == 96 || code == 97 || code == 99) return Condition::Thunderstorm;
    return Condition::Unknown;
}
const char* ConditionText(int code, bool is_day) {
    switch (WeatherCondition(code)) {
        case Condition::Clear: return is_day ? "晴" : "晴夜";
        case Condition::Cloudy: return "多云";
        case Condition::Fog: return "雾";
        case Condition::Rain: return "雨";
        case Condition::Snow: return "雪";
        case Condition::Thunderstorm: return "雷雨";
        default: return "未知";
    }
}
std::string FormatLocalTime(int64_t utc, int offset, const char* format) {
    if (!Timestamp(utc) || std::abs(static_cast<int64_t>(offset)) > 14 * 3600 || !format) return "--";
    time_t local = static_cast<time_t>(utc + offset);
    std::tm tm{};
#if defined(_WIN32)
    if (gmtime_s(&tm, &local) != 0) return "--";
#else
    if (!gmtime_r(&local, &tm)) return "--";
#endif
    char buffer[96];
    if (!std::strftime(buffer, sizeof(buffer), format, &tm)) return "--";
    return buffer;
}
std::string FormatUtcOffset(int offset) {
    if (std::abs(static_cast<int64_t>(offset)) > 14 * 3600) return "UTC?";
    char buffer[20];
    int minutes = std::abs(offset) / 60;
    std::snprintf(buffer, sizeof(buffer), "UTC%c%02d:%02d", offset < 0 ? '-' : '+',
                  minutes / 60, minutes % 60);
    return buffer;
}

}  // namespace stopwatch_weather

#endif  // CONFIG_STOPWATCH_LOCAL_APPS
