#include "sdkconfig.h"

#if CONFIG_STOPWATCH_LOCAL_APPS

#include "stopwatch_weather_codec.h"

#include <cJSON.h>

#include <algorithm>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <ctime>
#include <memory>
#include <set>
#include <vector>

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
std::string DisplayName(const Location& location, const std::string& fallback) {
    for (int i = 0; i < kCityCount; ++i) {
        auto city = BuiltinLocation(i);
        if ((location.city == city.city || location.city == city.display_name) &&
            std::abs(city.latitude - location.latitude) < 0.5 &&
            std::abs(city.longitude - location.longitude) < 0.5 &&
            city.country_code == location.country_code) return city.display_name;
    }
    if (!location.city.empty() && Ascii(location.city)) return location.city;
    std::string result = Ascii(fallback) ? fallback.substr(0, 64) : "City";
    if (!location.country_code.empty()) result += " " + location.country_code;
    result += " " + location.id;
    if (result.size() > 96) result.resize(96);
    return result;
}
bool Timestamp(int64_t value) { return value >= 946684800 && value <= 4102444800LL; }
bool Optional(float value, float low, float high) {
    return std::isnan(value) || (std::isfinite(value) && value >= low && value <= high);
}
}  // namespace

namespace codec {
namespace {
using Json = std::unique_ptr<cJSON, decltype(&cJSON_Delete)>;

// Bounded preflight before recursive cJSON allocation. Escapes are recognized,
// so braces inside a string do not affect nesting. Actual grammar is cJSON's.
bool Preflight(const std::string& text) {
    if (text.empty() || text.size() > 16384 || text.find('\0') != std::string::npos)
        return false;
    int depth = 0;
    size_t nodes = 0;
    bool in_string = false, escaped = false;
    size_t string_bytes = 0;
    for (size_t i = 0; i < text.size(); ++i) {
        unsigned char c = text[i];
        if (in_string) {
            if (++string_bytes > 512) return false;
            if (escaped) {
                if (c == 'u' && i + 4 < text.size() && text.compare(i + 1, 4, "0000") == 0)
                    return false;
                escaped = false;
            } else if (c == '\\') escaped = true;
            else if (c == '"') in_string = false;
            else if (c < 0x20) return false;
        } else if (c == '"') {
            in_string = true;
            string_bytes = 0;
            if (++nodes > 1024) return false;
        } else if (c == '{' || c == '[') {
            if (++depth > 8 || ++nodes > 1024) return false;
        } else if (c == '}' || c == ']') {
            if (--depth < 0) return false;
        } else if (c == ',' || c == ':') {
            if (++nodes > 1024) return false;
        } else if (c >= 0x80 || (c < 0x20 && c != '\n' && c != '\r' && c != '\t')) {
            return false;
        }
    }
    return !in_string && depth == 0;
}
bool CheckTree(const cJSON* value) {
    if (cJSON_IsString(value)) {
        if (!value->valuestring) return false;
        std::string text(value->valuestring);
        if (text.size() > 512 || (!text.empty() && !Utf8(text))) return false;
    }
    if (cJSON_IsNumber(value) && !std::isfinite(value->valuedouble)) return false;
    std::set<std::string> keys;
    for (auto* child = value->child; child; child = child->next) {
        if (cJSON_IsObject(value)) {
            if (!child->string || !Utf8(child->string) || !keys.insert(child->string).second)
                return false;
        }
        if (!CheckTree(child)) return false;
    }
    return true;
}
Json Parse(const std::string& text) {
    if (!Preflight(text)) return Json(nullptr, cJSON_Delete);
    const char* end = nullptr;
    Json root(cJSON_ParseWithLengthOpts(text.c_str(), text.size() + 1, &end, true), cJSON_Delete);
    if (!root || !cJSON_IsObject(root.get()) || !CheckTree(root.get())) root.reset();
    return root;
}
const cJSON* Field(const cJSON* root, const char* key) {
    return cJSON_GetObjectItemCaseSensitive(root, key);
}
bool String(const cJSON* root, const char* key, std::string& out, size_t limit,
            bool required = true) {
    auto* field = Field(root, key);
    if (!field || cJSON_IsNull(field)) { out.clear(); return !required; }
    if (!cJSON_IsString(field) || !field->valuestring) return false;
    out = field->valuestring;
    return out.size() <= limit && (out.empty() ? !required : Utf8(out));
}
bool Number(const cJSON* item, double& result, double low, double high) {
    if (!cJSON_IsNumber(item) || !std::isfinite(item->valuedouble) ||
        item->valuedouble < low || item->valuedouble > high) return false;
    result = item->valuedouble;
    return true;
}
bool Integer(const cJSON* item, int64_t& result, int64_t low, int64_t high) {
    double value = 0;
    if (!Number(item, value, static_cast<double>(low), static_cast<double>(high)) ||
        value != std::floor(value)) return false;
    result = static_cast<int64_t>(value);
    return true;
}
bool OptionalNumber(const cJSON* item, float& result, float low, float high) {
    if (!item || cJSON_IsNull(item)) { result = kMissing; return true; }
    double value;
    if (!Number(item, value, low, high)) return false;
    result = static_cast<float>(value);
    return true;
}
bool Code(const cJSON* item, int& result) {
    if (!item || cJSON_IsNull(item)) { result = -1; return true; }
    int64_t value;
    if (!Integer(item, value, 0, 999)) return false;
    result = static_cast<int>(value);
    return true;
}
bool Unit(const cJSON* root, const char* key, const char* expected) {
    auto* item = Field(root, key);
    return cJSON_IsString(item) && item->valuestring &&
           std::strcmp(item->valuestring, expected) == 0;
}
bool OptionalUnit(const cJSON* units, const cJSON* values, const char* key, const char* expected) {
    auto* value = Field(values, key);
    return (!value || cJSON_IsNull(value)) || Unit(units, key, expected);
}
bool Array(const cJSON* root, const char* key, int count, const cJSON*& out,
           bool required = true) {
    out = Field(root, key);
    if (!out || cJSON_IsNull(out)) { out = nullptr; return !required; }
    return cJSON_IsArray(out) && cJSON_GetArraySize(out) == count;
}
const cJSON* At(const cJSON* array, int index) {
    return array ? cJSON_GetArrayItem(array, index) : nullptr;
}
bool ReadLocation(const cJSON* root, Location& value, bool ip, const std::string& fallback) {
    double lat, lon;
    if (!Number(Field(root, "latitude"), lat, -90, 90) ||
        !Number(Field(root, "longitude"), lon, -180, 180) ||
        !String(root, ip ? "city" : "name", value.city, 96) ||
        !String(root, ip ? "region" : "admin1", value.region, 64, false) ||
        !String(root, "country", value.country, 64, false) ||
        !String(root, "country_code", value.country_code, 2)) return false;
    value.latitude = lat;
    value.longitude = lon;
    if (ip) {
        char id[64];
        std::snprintf(id, sizeof(id), "ip:%.4f,%.4f", lat, lon);
        value.id = id;
    } else {
        int64_t id;
        if (!Integer(Field(root, "id"), id, 1, 9999999999LL)) return false;
        value.id = "geonames:" + std::to_string(id);
    }
    value.display_name = DisplayName(value, fallback);
    return ValidLocation(value);
}
bool ValidForecast(const Forecast& f) {
    if (!ValidLocation(f.location) || !Timestamp(f.observed_at) ||
        std::abs(static_cast<int64_t>(f.utc_offset)) > 50400 || f.utc_offset % 60 != 0 ||
        !std::isfinite(f.temperature) || f.temperature < -100 || f.temperature > 70 ||
        !Optional(f.feels_like, -120, 100) || !Optional(f.humidity, 0, 100) ||
        !Optional(f.wind, 0, 500) || f.code < -1 || f.code > 999) return false;
    int64_t previous = f.observed_at;
    for (const auto& h : f.hours) {
        if (!Timestamp(h.time) || h.time <= previous || h.time > f.observed_at + 36 * 3600 ||
            !Optional(h.temperature, -100, 70) || !Optional(h.precipitation, 0, 100) ||
            h.code < -1 || h.code > 999) return false;
        previous = h.time;
    }
    previous = 0;
    for (const auto& d : f.days) {
        if (!Timestamp(d.time) || d.time <= previous ||
            std::abs(d.time - f.observed_at) > 4 * 86400 ||
            !Optional(d.low, -100, 70) || !Optional(d.high, -100, 70) ||
            (std::isfinite(d.low) && std::isfinite(d.high) && d.low > d.high) ||
            !Optional(d.precipitation, 0, 100) || d.code < -1 || d.code > 999) return false;
        previous = d.time;
    }
    return true;
}
}  // namespace

bool ParseIp(const std::string& json, Location& result, std::string& error) {
    error = "invalid_ip_response";
    auto root = Parse(json);
    if (!root || !cJSON_IsTrue(Field(root.get(), "success"))) return false;
    Location value;
    if (!ReadLocation(root.get(), value, true, "City")) return false;
    result = std::move(value);
    error.clear();
    return true;
}
namespace {
struct SearchQuery {
    std::string name;
    std::array<std::string, 2> qualifiers{};
    size_t count = 0;
};
std::string Trim(std::string value) {
    const auto first = value.find_first_not_of(' ');
    if (first == std::string::npos) return {};
    return value.substr(first, value.find_last_not_of(' ') - first + 1);
}
std::string Fold(std::string value) {
    for (char& c : value) if (c >= 'A' && c <= 'Z') c = static_cast<char>(c + 32);
    return Trim(std::move(value));
}
bool SplitSearch(const std::string& query, SearchQuery& result) {
    size_t start = 0;
    auto comma = query.find(',');
    result.name = Trim(query.substr(0, comma));
    if (result.name.empty()) return false;
    while (comma != std::string::npos) {
        if (result.count == result.qualifiers.size()) return false;
        start = comma + 1;
        comma = query.find(',', start);
        auto qualifier = Fold(query.substr(start, comma == std::string::npos ? comma : comma - start));
        if (qualifier.empty()) return false;
        result.qualifiers[result.count++] = std::move(qualifier);
    }
    return true;
}
bool MatchCountry(const std::string& qualifier, const Location& location) {
    if (qualifier == Fold(location.country_code) || qualifier == Fold(location.country)) return true;
    // Common English aliases, never approximate/sub-string matching.
    if ((qualifier == "usa" || qualifier == "united states of america") && location.country_code == "US") return true;
    if ((qualifier == "uk" || qualifier == "great britain") && location.country_code == "GB") return true;
    return false;
}
bool MatchQuery(const SearchQuery& query, const Location& location, const std::string& country) {
    if (!country.empty() && Fold(country) != Fold(location.country_code)) return false;
    if (query.count == 2) {
        return query.qualifiers[0] == Fold(location.region) &&
               MatchCountry(query.qualifiers[1], location);
    }
    return !query.count || query.qualifiers[0] == Fold(location.region) ||
           MatchCountry(query.qualifiers[0], location);
}
}  // namespace
bool ParseSearch(const std::string& json, const std::string& query,
                 std::array<Candidate, 5>& result, size_t& count, std::string& error,
                 const std::string& country) {
    error = "invalid_search_response";
    auto root = Parse(json);
    if (!root || cJSON_IsTrue(Field(root.get(), "error"))) return false;
    const auto* list = Field(root.get(), "results");
    if (!list) { result = {}; count = 0; error.clear(); return true; }
    if (!cJSON_IsArray(list) || cJSON_GetArraySize(list) > 5) return false;
    SearchQuery parsed_query;
    if (!SplitSearch(query, parsed_query)) return false;
    std::array<Candidate, 5> values{};
    const int size = cJSON_GetArraySize(list);
    size_t selected = 0;
    std::set<std::string> identities;
    for (int i = 0; i < size; ++i) {
        Location location;
        if (!ReadLocation(At(list, i), location, false, parsed_query.name) ||
            !identities.insert(location.id).second) return false;
        if (MatchQuery(parsed_query, location, country)) values[selected++].location = std::move(location);
    }
    count = selected;
    result = std::move(values);
    error.clear();
    return true;
}
bool ParseForecast(const std::string& json, const Location& location, Forecast& result,
                   std::string& error) {
    error = "invalid_weather_response";
    auto root = Parse(json);
    if (!root || cJSON_IsTrue(Field(root.get(), "error")) || !ValidLocation(location)) return false;
    double root_latitude, root_longitude;
    if (!Number(Field(root.get(), "latitude"), root_latitude, -90, 90) ||
        !Number(Field(root.get(), "longitude"), root_longitude, -180, 180)) return false;
    // Open-Meteo gridpoint coordinates may differ slightly from the city.
    Forecast f;
    f.location = location;
    int64_t value;
    if (!Integer(Field(root.get(), "utc_offset_seconds"), value, -50400, 50400) || value % 60)
        return false;
    f.utc_offset = static_cast<int>(value);
    auto* current = Field(root.get(), "current");
    auto* units = Field(root.get(), "current_units");
    double temperature;
    if (!cJSON_IsObject(current) || !cJSON_IsObject(units) ||
        !Integer(Field(current, "time"), f.observed_at, 946684800, 4102444800LL) ||
        !Number(Field(current, "temperature_2m"), temperature, -100, 70) ||
        !Unit(units, "time", "unixtime") || !Unit(units, "temperature_2m", "°C") ||
        !OptionalNumber(Field(current, "apparent_temperature"), f.feels_like, -120, 100) ||
        !OptionalNumber(Field(current, "relative_humidity_2m"), f.humidity, 0, 100) ||
        !OptionalNumber(Field(current, "wind_speed_10m"), f.wind, 0, 500) ||
        !OptionalUnit(units, current, "apparent_temperature", "°C") ||
        !OptionalUnit(units, current, "relative_humidity_2m", "%") ||
        !OptionalUnit(units, current, "wind_speed_10m", "km/h") ||
        !OptionalUnit(units, current, "weather_code", "wmo code") ||
        !Code(Field(current, "weather_code"), f.code) ||
        !Integer(Field(current, "is_day"), value, 0, 1)) return false;
    f.temperature = static_cast<float>(temperature);
    f.is_day = value != 0;
    auto* hourly = Field(root.get(), "hourly");
    auto* hu = Field(root.get(), "hourly_units");
    const auto* ht = Field(hourly, "time");
    int length = cJSON_IsArray(ht) ? cJSON_GetArraySize(ht) : 0;
    const cJSON *temps, *rain, *codes;
    if (length < 4 || length > 24 || !Unit(hu, "time", "unixtime") ||
        !Array(hourly, "temperature_2m", length, temps, false) ||
        !Array(hourly, "precipitation_probability", length, rain, false) ||
        !Array(hourly, "weather_code", length, codes, false) ||
        !OptionalUnit(hu, hourly, "temperature_2m", "°C") ||
        !OptionalUnit(hu, hourly, "precipitation_probability", "%") ||
        !OptionalUnit(hu, hourly, "weather_code", "wmo code")) return false;
    int64_t previous = 0;
    size_t selected = 0;
    for (int i = 0; i < length; ++i) {
        Hour h;
        if (!Integer(At(ht, i), h.time, 946684800, 4102444800LL) || h.time <= previous ||
            std::abs(h.time - f.observed_at) > 36 * 3600 ||
            !OptionalNumber(At(temps, i), h.temperature, -100, 70) ||
            !OptionalNumber(At(rain, i), h.precipitation, 0, 100) ||
            !Code(At(codes, i), h.code)) return false;
        previous = h.time;
        if (h.time > f.observed_at && selected < f.hours.size()) f.hours[selected++] = h;
    }
    if (selected != f.hours.size()) return false;
    auto* daily = Field(root.get(), "daily");
    auto* du = Field(root.get(), "daily_units");
    const cJSON *dt, *lows, *highs;
    if (!Array(daily, "time", 3, dt) || !Array(daily, "temperature_2m_min", 3, lows, false) ||
        !Array(daily, "temperature_2m_max", 3, highs, false) ||
        !Array(daily, "precipitation_probability_max", 3, rain, false) ||
        !Array(daily, "weather_code", 3, codes, false) || !Unit(du, "time", "unixtime") ||
        !OptionalUnit(du, daily, "temperature_2m_min", "°C") ||
        !OptionalUnit(du, daily, "temperature_2m_max", "°C") ||
        !OptionalUnit(du, daily, "precipitation_probability_max", "%") ||
        !OptionalUnit(du, daily, "weather_code", "wmo code")) return false;
    for (int i = 0; i < 3; ++i) {
        auto& d = f.days[i];
        if (!Integer(At(dt, i), d.time, 946684800, 4102444800LL) ||
            !OptionalNumber(At(lows, i), d.low, -100, 70) ||
            !OptionalNumber(At(highs, i), d.high, -100, 70) ||
            !OptionalNumber(At(rain, i), d.precipitation, 0, 100) ||
            !Code(At(codes, i), d.code)) return false;
        if (i && (d.time - f.days[i - 1].time < 23 * 3600 ||
                  d.time - f.days[i - 1].time > 25 * 3600)) return false;
    }
    if (!ValidForecast(f)) return false;
    result = std::move(f);
    error.clear();
    return true;
}

bool ValidQuery(const std::string& query, const std::string& country) {
    SearchQuery parsed;
    if (query.empty() || query.size() > 96 || !Utf8(query) || !SplitSearch(query, parsed)) return false;
    if (country.empty()) return true;
    return country.size() == 2 && std::all_of(country.begin(), country.end(), [](unsigned char c) {
        return (c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z');
    });
}
std::string EncodeQuery(const std::string& query) {
    constexpr char hex[] = "0123456789ABCDEF";
    std::string result;
    for (unsigned char c : query) {
        if ((c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') ||
            (c >= '0' && c <= '9') || c == '-' || c == '_' || c == '.' || c == '~') result += c;
        else { result += '%'; result += hex[c >> 4]; result += hex[c & 15]; }
    }
    return result;
}
std::string SearchUrl(const std::string& query, const std::string& country) {
    SearchQuery parsed;
    if (!ValidQuery(query, country) || !SplitSearch(query, parsed)) return {};
    std::string url = "https://geocoding-api.open-meteo.com/v1/search?name=" +
        EncodeQuery(parsed.name) + "&count=5&language=en&format=json";
    std::string filter = country;
    if (filter.empty() && parsed.count && parsed.qualifiers[parsed.count - 1].size() == 2)
        filter = parsed.qualifiers[parsed.count - 1];
    for (char& c : filter) if (c >= 'a' && c <= 'z') c = static_cast<char>(c - 32);
    if (!filter.empty()) url += "&countryCode=" + EncodeQuery(filter);
    return url;
}
std::string ForecastUrl(const Location& location) {
    char coordinates[96];
    std::snprintf(coordinates, sizeof(coordinates), "latitude=%.6f&longitude=%.6f",
                  location.latitude, location.longitude);
    return std::string("https://api.open-meteo.com/v1/forecast?") + coordinates +
        "&temperature_unit=celsius&wind_speed_unit=kmh&timeformat=unixtime&timezone=auto"
        "&forecast_days=3&forecast_hours=8"
        "&current=temperature_2m,apparent_temperature,relative_humidity_2m,wind_speed_10m,weather_code,is_day"
        "&hourly=temperature_2m,precipitation_probability,weather_code"
        "&daily=temperature_2m_min,temperature_2m_max,precipitation_probability_max,weather_code";
}

namespace {
uint32_t Checksum(const std::string& value) {
    uint32_t crc = 0xffffffffU;
    for (unsigned char b : value) {
        crc ^= b;
        for (int i = 0; i < 8; ++i) crc = (crc >> 1) ^ (0xedb88320U & (0U - (crc & 1U)));
    }
    return ~crc;
}
struct Writer {
    std::string bytes;
    void U(uint64_t value, unsigned count) {
        while (count--) { bytes.push_back(static_cast<char>(value)); value >>= 8; }
    }
    void F(float value) {
        uint32_t bits;
        static_assert(sizeof(float) == sizeof(bits));
        std::memcpy(&bits, &value, sizeof(bits));
        U(bits, 4);
    }
    void D(double value) {
        uint64_t bits;
        static_assert(sizeof(double) == sizeof(bits));
        std::memcpy(&bits, &value, sizeof(bits));
        U(bits, 8);
    }
    void S(const std::string& text) { U(text.size(), 1); bytes += text; }
    void L(const Location& v) {
        S(v.id); S(v.city); S(v.display_name); S(v.region); S(v.country); S(v.country_code);
        D(v.latitude); D(v.longitude);
    }
    bool Finish(std::string& result) {
        U(Checksum(bytes), 4);
        if (bytes.size() > kBlobLimit) return false;
        result = std::move(bytes);
        return true;
    }
};
struct Reader {
    const std::string& bytes;
    size_t position = 0;
    bool good = true;
    uint64_t U(unsigned count) {
        if (position + count > bytes.size()) { good = false; return 0; }
        uint64_t result = 0;
        for (unsigned i = 0; i < count; ++i)
            result |= static_cast<uint64_t>(static_cast<unsigned char>(bytes[position++])) << (8 * i);
        return result;
    }
    float F() { uint32_t bits = static_cast<uint32_t>(U(4)); float f; std::memcpy(&f, &bits, 4); return f; }
    double D() { uint64_t bits = U(8); double d; std::memcpy(&d, &bits, 8); return d; }
    std::string S() {
        const size_t count = static_cast<size_t>(U(1));
        if (position + count > bytes.size()) { good = false; return {}; }
        auto result = bytes.substr(position, count);
        position += count;
        return result;
    }
    Location L() {
        Location v;
        v.id = S(); v.city = S(); v.display_name = S(); v.region = S(); v.country = S();
        v.country_code = S(); v.latitude = D(); v.longitude = D();
        if (!ValidLocation(v)) good = false;
        return v;
    }
    bool Header(uint8_t kind) {
        return bytes.size() >= 12 && bytes.size() <= kBlobLimit &&
               U(4) == 0x31575753 && U(1) == kind && U(1) == 1;
    }
    bool Finish() {
        if (!good || position + 4 != bytes.size()) return false;
        const uint32_t expected = Checksum(bytes.substr(0, position));
        return U(4) == expected && good;
    }
};
void Header(Writer& w, unsigned kind) { w.U(0x31575753, 4); w.U(kind, 1); w.U(1, 1); }
}  // namespace
bool EncodeConfig(const Config& config, std::string& bytes) {
    if (config.recent_count > 3 || (!config.automatic && !ValidLocation(config.location))) return false;
    Writer w;
    Header(w, 1);
    w.U(config.automatic ? 1 : 0, 1); w.U(config.revision, 4);
    if (!config.automatic) w.L(config.location);
    w.U(config.recent_count, 1);
    for (size_t i = 0; i < config.recent_count; ++i) {
        if (!ValidLocation(config.recent[i])) return false;
        w.L(config.recent[i]);
    }
    return w.Finish(bytes);
}
bool DecodeConfig(const std::string& bytes, Config& config) {
    Reader r{bytes};
    if (!r.Header(1)) return false;
    Config value;
    const auto mode = r.U(1);
    if (mode > 1) return false;
    value.automatic = mode != 0;
    value.revision = static_cast<uint32_t>(r.U(4));
    if (!value.automatic) value.location = r.L();
    value.recent_count = static_cast<size_t>(r.U(1));
    if (value.recent_count > 3) return false;
    for (size_t i = 0; i < value.recent_count; ++i) value.recent[i] = r.L();
    if (!r.Finish()) return false;
    config = std::move(value);
    return true;
}
bool EncodeCache(const Cache& cache, std::string& bytes) {
    const auto& f = cache.forecast;
    if (!ValidForecast(f)) return false;
    Writer w;
    Header(w, 2);
    w.U(cache.automatic ? 1 : 0, 1); w.U(cache.config_revision, 4); w.L(f.location);
    w.U(static_cast<uint64_t>(f.observed_at), 8); w.U(static_cast<uint32_t>(f.utc_offset), 4);
    w.F(f.temperature); w.F(f.feels_like); w.F(f.humidity); w.F(f.wind);
    w.U(static_cast<uint32_t>(f.code), 4); w.U(f.is_day ? 1 : 0, 1);
    for (const auto& h : f.hours) {
        w.U(static_cast<uint64_t>(h.time), 8); w.F(h.temperature); w.F(h.precipitation);
        w.U(static_cast<uint32_t>(h.code), 4);
    }
    for (const auto& d : f.days) {
        w.U(static_cast<uint64_t>(d.time), 8); w.F(d.low); w.F(d.high); w.F(d.precipitation);
        w.U(static_cast<uint32_t>(d.code), 4);
    }
    return w.Finish(bytes);
}
bool DecodeCache(const std::string& bytes, Cache& cache) {
    Reader r{bytes};
    if (!r.Header(2)) return false;
    Cache value;
    auto mode = r.U(1);
    if (mode > 1) return false;
    value.automatic = mode != 0;
    value.config_revision = static_cast<uint32_t>(r.U(4));
    auto& f = value.forecast;
    f.location = r.L(); f.observed_at = static_cast<int64_t>(r.U(8));
    f.utc_offset = static_cast<int32_t>(r.U(4));
    f.temperature = r.F(); f.feels_like = r.F(); f.humidity = r.F(); f.wind = r.F();
    f.code = static_cast<int32_t>(r.U(4));
    auto day = r.U(1);
    if (day > 1) return false;
    f.is_day = day != 0;
    for (auto& h : f.hours) {
        h.time = static_cast<int64_t>(r.U(8)); h.temperature = r.F(); h.precipitation = r.F();
        h.code = static_cast<int32_t>(r.U(4));
    }
    for (auto& d : f.days) {
        d.time = static_cast<int64_t>(r.U(8)); d.low = r.F(); d.high = r.F();
        d.precipitation = r.F(); d.code = static_cast<int32_t>(r.U(4));
    }
    if (!r.Finish() || !ValidForecast(f)) return false;
    cache = std::move(value);
    return true;
}
uint64_t RetryAfterMs(const std::string& value, int64_t wall_now) {
    if (value.empty() || value.size() > 96) return 0;
    if (std::all_of(value.begin(), value.end(), [](char c) { return c >= '0' && c <= '9'; })) {
        uint64_t seconds = 0;
        for (char c : value) {
            seconds = seconds * 10 + static_cast<unsigned>(c - '0');
            if (seconds > 7 * 86400) return 0;
        }
        return seconds ? seconds * 1000 : 1000;
    }
    if (!Timestamp(wall_now)) return 0;
    char weekday[4]{}, month[4]{}, zone[4]{}, extra = 0;
    int day, year, hour, minute, second;
    if (std::sscanf(value.c_str(), "%3s, %d %3s %d %d:%d:%d %3s%c", weekday, &day, month,
                    &year, &hour, &minute, &second, zone, &extra) != 8 ||
        std::strcmp(zone, "GMT") != 0 || year < 2000 || year > 2100 || day < 1 || day > 31 ||
        hour < 0 || hour > 23 || minute < 0 || minute > 59 || second < 0 || second > 59) return 0;
    constexpr const char* months[] = {"Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"};
    int m = 0;
    for (; m < 12 && std::strcmp(month, months[m]); ++m) {}
    if (m == 12) return 0;
    // Gregorian civil date to UTC; independent of process/global timezone.
    int y = year - (m < 2);
    const int era = y / 400;
    const unsigned yoe = static_cast<unsigned>(y - era * 400);
    const unsigned mp = static_cast<unsigned>(m + (m > 1 ? -2 : 10));
    const unsigned doy = (153 * mp + 2) / 5 + static_cast<unsigned>(day) - 1;
    const unsigned doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    const int64_t target = (static_cast<int64_t>(era) * 146097 + doe - 719468) * 86400 +
                           hour * 3600 + minute * 60 + second;
    const int64_t delta = target - wall_now;
    return delta > 0 && delta <= 7 * 86400 ? static_cast<uint64_t>(delta) * 1000 : 0;
}
}  // namespace codec
}  // namespace stopwatch_weather

#endif  // CONFIG_STOPWATCH_LOCAL_APPS
