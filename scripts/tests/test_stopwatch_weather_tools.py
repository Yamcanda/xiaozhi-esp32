"""Compile the complete board-local MCP adapter with real MCP/schema/JSON code.

The weather service is a fake here: service I/O/state have a separate harness.
No network, microphone, GUI, backend conversation, or device NVS is exercised.
"""

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
BOARD = ROOT / "main/boards/m5stack/stopwatch"
MINGW = Path("D:/sdk/mingw64/bin/g++.exe")
COMPILER = str(MINGW) if MINGW.is_file() else shutil.which("g++")
CJSON = ROOT / "managed_components/espressif__cjson/cJSON"


@unittest.skipUnless(COMPILER and (CJSON / "cJSON.c").is_file(),
                     "requires C++23 and the managed cJSON source")
class StopwatchWeatherToolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory(prefix="stopwatch-weather-tools-")
        cls.addClassCleanup(cls.directory.cleanup)
        build = Path(cls.directory.name)
        (build / "mbedtls").mkdir()
        (build / "sdkconfig.h").write_text(
            "#define CONFIG_STOPWATCH_LOCAL_APPS 1\n", encoding="utf-8")
        (build / "esp_system.h").write_text(
            '#pragma once\n#include <cstdlib>\n'
            '[[noreturn]] inline void esp_system_abort(const char*) { std::abort(); }\n',
            encoding="utf-8")
        (build / "mbedtls/base64.h").write_text(
            '#pragma once\n#include <cstddef>\n#include <cstdlib>\n'
            'inline int mbedtls_base64_encode(unsigned char*, size_t, size_t*, '
            'const unsigned char*, size_t) { std::abort(); }\n', encoding="utf-8")
        source = build / "driver.cc"
        source.write_text(DRIVER, encoding="utf-8")
        cls.binary = build / "weather_tools.exe"
        result = subprocess.run(
            [COMPILER, "-std=c++23", "-fno-exceptions", "-Wall", "-Wextra", "-Werror",
             f"-I{build}", f"-I{ROOT / 'main'}", f"-I{BOARD}", f"-I{CJSON}",
             str(source), str(BOARD / "stopwatch_weather_tools.cc"),
             "-x", "c++", str(CJSON / "cJSON.c"), "-o", str(cls.binary)],
            text=True, encoding="utf-8", errors="replace", capture_output=True, timeout=120)
        if result.returncode:
            raise AssertionError(result.stdout + result.stderr)
        disabled = build / "disabled"
        disabled.mkdir()
        (disabled / "sdkconfig.h").write_text(
            "#define CONFIG_STOPWATCH_LOCAL_APPS 0\n", encoding="utf-8")
        # Disabled adapter must not need MCP, LVGL, networking, service or fonts.
        result = subprocess.run(
            [COMPILER, "-std=c++23", "-Wall", "-Wextra", "-Werror", f"-I{disabled}",
             "-c", str(BOARD / "stopwatch_weather_tools.cc"),
             "-o", str(build / "disabled.o")],
            text=True, encoding="utf-8", errors="replace", capture_output=True, timeout=60)
        if result.returncode:
            raise AssertionError(result.stdout + result.stderr)

    def run_case(self, case):
        result = subprocess.run([str(self.binary), case], capture_output=True,
                                text=True, encoding="utf-8", errors="replace", timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_voice_visible_schema(self):
        self.run_case("schema")

    def test_get_configuration_and_cache_identity(self):
        self.run_case("status")

    def test_search_accepted_does_not_commit(self):
        self.run_case("accepted")

    def test_ready_candidates_retain_native_names(self):
        self.run_case("ready")

    def test_busy_and_worker_unavailable_are_errors(self):
        self.run_case("search_error")

    def test_request_id_is_strict_nonzero_decimal(self):
        self.run_case("request_id")

    def test_committed_configuration_is_not_weather_success(self):
        self.run_case("committed")

    def test_persistence_and_token_failures_are_errors(self):
        self.run_case("commit_error")

    def test_expired_results_do_not_export_tokens(self):
        self.run_case("expired")

    def test_board_registers_once_and_injects_shared_service(self):
        board = (BOARD / "m5stack_stopwatch.cc").read_text(encoding="utf-8")
        apps = (BOARD / "stopwatch_apps.cc").read_text(encoding="utf-8")
        self.assertEqual(board.count("RegisterStopwatchWeatherTools(weather_service_)"), 1)
        self.assertIn("StopwatchWeatherService weather_service_;", board)
        self.assertIn("display_->SetWeatherService(&weather_service_);", board)
        self.assertIn("configure_wifi_(); }, weather_service_, alarm_service_)", " ".join(board.split()))
        self.assertIn("std::move(configure_wifi), weather_service)", apps)
        self.assertIn("weather_(display, weather_service)", apps)


DRIVER = r'''
#include <cassert>
#include <iostream>
#include <string>
#include <utility>
#include <vector>
#include "mcp_server.h"
#include "stopwatch_weather_service.h"
#include "stopwatch_weather_tools.h"

using namespace stopwatch_weather;
std::vector<McpTool*> registered;
McpServer::McpServer() = default;
McpServer::~McpServer() = default;
void McpServer::AddTool(const std::string& name, const std::string& description,
                        const PropertyList& properties, ToolCallback callback) {
    auto tool = std::make_unique<McpTool>(name, description, properties, std::move(callback));
    registered.push_back(tool.get());
    tools_.push_back(std::move(tool));
}

Snapshot snapshot;
SearchResult search_result;
Result operation{true, 7, 11, "", true};
int search_calls = 0, commit_calls = 0;
std::string query_seen, country_seen, mode_seen, token_seen;
uint32_t request_seen = 0;
struct StopwatchWeatherService::Impl {};
StopwatchWeatherService::StopwatchWeatherService() : impl_(std::make_unique<Impl>()) {}
StopwatchWeatherService::~StopwatchWeatherService() = default;
Snapshot StopwatchWeatherService::GetSnapshot() const { return snapshot; }
SearchResult StopwatchWeatherService::GetSearch(uint32_t id) const {
    request_seen = id;
    return search_result;
}
Result StopwatchWeatherService::SearchCity(const std::string& query, const std::string& country) {
    ++search_calls; query_seen = query; country_seen = country;
    return operation;
}
Result StopwatchWeatherService::SetLocationToken(const std::string& mode,
                                                 const std::string& token) {
    ++commit_calls; mode_seen = mode; token_seen = token;
    return operation;
}
McpTool& Tool(const std::string& suffix) {
    for (auto* tool : registered) if (tool->name() == "self.weather." + suffix) return *tool;
    std::abort();
}
std::expected<std::string, std::string> Call(
        const std::string& name,
        std::initializer_list<std::pair<std::string, std::string>> values = {}) {
    auto& tool = Tool(name);
    auto props = tool.properties();
    for (const auto& [key, value] : values) {
        bool found = false;
        for (auto& property : props) {
            if (property.name() == key) {
                auto set = property.set_value(value);
                if (!set) return std::unexpected(set.error());
                found = true;
            }
        }
        assert(found);
    }
    return tool.Call(props);
}
CJsonUniquePtr Json(const std::string& name,
        std::initializer_list<std::pair<std::string, std::string>> values = {}) {
    auto reply = Call(name, values);
    assert(reply);
    CJsonUniquePtr envelope(cJSON_Parse(reply->c_str()));
    assert(envelope && cJSON_IsFalse(cJSON_GetObjectItemCaseSensitive(envelope.get(), "isError")));
    auto* content = cJSON_GetArrayItem(cJSON_GetObjectItemCaseSensitive(envelope.get(), "content"), 0);
    auto* text = cJSON_GetObjectItemCaseSensitive(content, "text");
    assert(cJSON_IsString(text));
    CJsonUniquePtr result(cJSON_Parse(text->valuestring));
    assert(result);
    return result;
}
cJSON* Field(cJSON* json, const char* key) {
    auto* field = cJSON_GetObjectItemCaseSensitive(json, key); assert(field); return field;
}
std::string Text(cJSON* json, const char* key) {
    auto* field = Field(json, key); assert(cJSON_IsString(field)); return field->valuestring;
}
int main(int argc, char** argv) {
    assert(argc == 2);
    StopwatchWeatherService service;
    RegisterStopwatchWeatherTools(service);
    const std::string test = argv[1];
    snapshot.automatic = false; snapshot.config_revision = 11;
    snapshot.location.id = "geonames:123";
    snapshot.location.city = "原生城市"; snapshot.location.display_name = "Example";
    snapshot.location.region = "Region"; snapshot.location.country = "Country";
    snapshot.location.country_code = "XY";
    snapshot.location.latitude = 12.5; snapshot.location.longitude = -45;
    snapshot.state = State::Offline; snapshot.error = "offline";
    search_result.request_id = 7; search_result.config_revision = 11;
    search_result.state = SearchState::Pending;
    if (test == "schema") {
        assert(registered.size() == 3);
        for (auto* tool : registered) assert(!tool->user_only());
        auto required = Tool("search_city").properties().GetRequired();
        assert(required.size() == 1 && required[0] == "query");
        assert(Tool("search_city").properties()["query"].max_length() == 96);
        assert(Tool("set_location").properties()["selection_token"].has_default_value());
        assert(Tool("set_location").properties().GetRequired()[0] == "mode");
        assert(!Call("search_city", {{"query", std::string(97, 'x')}}));
        assert(search_calls == 0);
    } else if (test == "status") {
        snapshot.has_data = true; snapshot.cached = true; snapshot.stale = true;
        snapshot.age_known = false; snapshot.forecast.location = snapshot.location;
        snapshot.forecast.observed_at = 1800000000; snapshot.forecast.utc_offset = 20700;
        auto json = Json("get_status");
        assert(Text(json.get(), "mode") == "manual");
        assert(Text(json.get(), "weather_state") == "offline");
        assert(cJSON_IsTrue(Field(json.get(), "stale")));
        assert(cJSON_IsFalse(Field(json.get(), "age_known")));
        assert(Text(Field(json.get(), "data_location"), "city") == "原生城市");
        assert(Field(json.get(), "utc_offset_seconds")->valueint == 20700);
        assert(search_calls == 0 && commit_calls == 0 && request_seen == 0);
    } else if (test == "accepted") {
        auto json = Json("search_city", {{"query", "Paris, France"}, {"country_code", "FR"}});
        assert(Text(json.get(), "status") == "accepted");
        assert(Text(json.get(), "request_id") == "7");
        assert(Text(Field(json.get(), "search"), "state") == "pending");
        assert(query_seen == "Paris, France" && country_seen == "FR");
        assert(commit_calls == 0 && request_seen == 7);
    } else if (test == "ready") {
        search_result.state = SearchState::Ready; search_result.count = 2;
        search_result.candidates[0] = {"token-a", snapshot.location};
        search_result.candidates[1] = {"token-b", snapshot.location};
        search_result.candidates[1].location.region = "Other region";
        auto json = Json("get_status", {{"request_id", "7"}});
        auto* candidates = Field(Field(json.get(), "search"), "candidates");
        assert(cJSON_GetArraySize(candidates) == 2);
        assert(Text(cJSON_GetArrayItem(candidates, 0), "selection_token") == "token-a");
        assert(Text(Field(cJSON_GetArrayItem(candidates, 0), "location"), "city") == "原生城市");
        assert(Text(Field(cJSON_GetArrayItem(candidates, 1), "location"), "region") == "Other region");
        assert(commit_calls == 0 && search_calls == 0);
    } else if (test == "search_error") {
        for (const auto* reason : {"busy", "worker_unavailable"}) {
            operation.ok = false; operation.error = reason;
            auto result = Call("search_city", {{"query", "London"}});
            assert(!result && result.error() == reason);
        }
        assert(search_calls == 2 && commit_calls == 0);
    } else if (test == "request_id") {
        for (const auto* id : {"0", "-1", "+1", "1x", " 7", "7 ", "4294967296"}) {
            assert(!Call("get_status", {{"request_id", id}}));
        }
        assert(request_seen == 0);
        auto json = Json("get_status", {{"request_id", "4294967295"}});
        assert(json && request_seen == UINT32_MAX);
    } else if (test == "committed") {
        snapshot.state = State::Error; snapshot.error = "worker_unavailable";
        auto json = Json("set_location", {{"mode", "manual"}, {"selection_token", "token-a"}});
        assert(Text(json.get(), "status") == "committed");
        assert(Text(json.get(), "weather_state") == "error");
        assert(Text(json.get(), "weather_error") == "worker_unavailable");
        assert(!cJSON_IsTrue(Field(json.get(), "weather_pending")));
        assert(mode_seen == "manual" && token_seen == "token-a" && commit_calls == 1);
        Json("set_location", {{"mode", "auto"}});
        assert(mode_seen == "auto" && token_seen.empty() && commit_calls == 2);
    } else if (test == "commit_error") {
        for (const auto* reason : {"storage_failed", "expired", "revision_conflict", "invalid_mode"}) {
            operation.ok = false; operation.error = reason;
            auto result = Call("set_location", {{"mode", "manual"}, {"selection_token", "old"}});
            assert(!result && result.error() == reason);
        }
    } else if (test == "expired") {
        for (auto state : {SearchState::Expired, SearchState::Superseded, SearchState::Unknown,
                           SearchState::Error}) {
            search_result.state = state;
            search_result.count = 1;
            search_result.candidates[0] = {"stale-token", snapshot.location};
            auto json = Json("get_status", {{"request_id", "7"}});
            assert(cJSON_GetArraySize(Field(Field(json.get(), "search"), "candidates")) == 0);
        }
    } else { std::abort(); }
}
'''


if __name__ == "__main__":
    unittest.main()
