#pragma once

#include "stopwatch_weather_data.h"

namespace stopwatch_weather::codec {

inline constexpr size_t kBlobLimit = 2048;
struct Config {
    bool automatic = true;
    uint32_t revision = 0;
    Location location;
    std::array<Location, 3> recent{};
    size_t recent_count = 0;
};
struct Cache {
    bool automatic = true;
    uint32_t config_revision = 0;
    Forecast forecast;
};

// Every parser rejects malformed UTF-8, embedded NUL, duplicate object keys,
// trailing material, excessive complexity and out-of-range mandatory data.
bool ParseIp(const std::string& json, Location& result, std::string& error);
bool ParseSearch(const std::string& json, const std::string& query,
                 std::array<Candidate, 5>& result, size_t& count, std::string& error,
                 const std::string& country = "");
bool ParseForecast(const std::string& json, const Location& location, Forecast& result,
                   std::string& error);
bool EncodeConfig(const Config& config, std::string& bytes);
bool DecodeConfig(const std::string& bytes, Config& config);
bool EncodeCache(const Cache& cache, std::string& bytes);
bool DecodeCache(const std::string& bytes, Cache& cache);
bool ValidQuery(const std::string& query, const std::string& country);
std::string EncodeQuery(const std::string& query);
std::string SearchUrl(const std::string& query, const std::string& country);
std::string ForecastUrl(const Location& location);
// Delta-seconds and RFC 7231 IMF-fixdate, bounded to seven days. Invalid => 0.
uint64_t RetryAfterMs(const std::string& value, int64_t wall_now);

}  // namespace stopwatch_weather::codec
