"""Exercise real MCP discovery/replies and LCD/StopWatch theme application on a host."""

import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import textwrap
import unittest


ROOT = Path(__file__).resolve().parents[2]


def definition(source, signature):
    start = source.index(signature)
    brace = source.index("{", start)
    depth, end = 1, brace + 1
    while depth:
        depth += (source[end] == "{") - (source[end] == "}")
        end += 1
    return source[start:end]


class McpForegroundThemeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        compiler = shlex.split(os.environ.get("CXX", "c++"))
        if not compiler or shutil.which(compiler[0]) is None:
            raise unittest.SkipTest("requires a host C++23 compiler")
        cls.directory = tempfile.TemporaryDirectory(prefix="mcp-foreground-theme-")
        cls.addClassCleanup(cls.directory.cleanup)
        build = Path(cls.directory.name)
        # Reuse installed cJSON, or compile the managed dependency read-only.
        # No component download or development-package installation is needed.
        cjson = ROOT / "managed_components/espressif__cjson/cJSON"
        if (cjson / "cJSON.c").is_file():
            cjson_flags = [f"-I{cjson}"]
            cjson_inputs = ["-x", "c++", str(cjson / "cJSON.c")]
        else:
            cjson_flags = shlex.split(os.environ.get("MCP_CJSON_CFLAGS", "-I/usr/include/cjson"))
            cjson_inputs = shlex.split(os.environ.get("MCP_CJSON_LIBS", "-lcjson"))
        for name, contents in STUBS.items():
            path = build / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(textwrap.dedent(contents), encoding="utf-8")
        app = (ROOT / "main/application.cc").read_text(encoding="utf-8")
        lcd = (ROOT / "main/display/lcd_display.cc").read_text(encoding="utf-8")
        display = (ROOT / "main/display/display.cc").read_text(encoding="utf-8")
        board = (ROOT / "main/boards/m5stack/stopwatch/m5stack_stopwatch.cc").read_text(encoding="utf-8")
        apps = (ROOT / "main/boards/m5stack/stopwatch/stopwatch_apps.cc").read_text(encoding="utf-8")
        # Compile the dispatcher unchanged. Extract complete production methods
        # only where hardware-heavy translation units prevent host compilation.
        methods = [definition(app, signature) for signature in (
            "void Application::SendMcpMessage(",
            "bool Application::IsConversationCurrent(",
            "bool Application::IsConversationAvailable(",
        )]
        methods += [definition(lcd, "void LcdDisplay::InitializeLcdThemes("),
                    definition(lcd, "void LcdDisplay::SetTheme("),
                    definition(display, "void Display::SetTheme("),
                    definition(apps, "void StopwatchApps::ApplyTheme("),
                    definition(apps, "void StopwatchApps::LogThemeState("),
                    definition(board, "void SetTheme(Theme* theme) override").replace(
                        "void SetTheme(Theme* theme) override", "void RoundLcdDisplay::SetTheme(Theme* theme)", 1)]
        driver = build / "driver.cc"
        driver.write_text('#include "mcp_server.h"\n#include "lvgl_theme.h"\n#include "esp_log.h"\n'
                          + FIXTURE + "\n" + "\n".join(methods) + DRIVER, encoding="utf-8")
        probe = subprocess.run(
            compiler + ["-std=c++23", *cjson_flags, "-x", "c++", "-", *cjson_inputs,
                        "-o", str(build / "probe")],
            input="#include <expected>\n#include <cJSON.h>\n"
                  "int main() { std::expected<int, int> v; cJSON_Delete(cJSON_CreateObject()); }",
            text=True, capture_output=True,
        )
        if probe.returncode:
            raise unittest.SkipTest("requires C++23 and cJSON: " + probe.stderr)
        cls.executable = build / "mcp_foreground_theme"
        result = subprocess.run(
            compiler + ["-std=c++23", "-fno-exceptions", *cjson_flags, f"-I{build}",
                        f"-I{ROOT / 'main'}", f"-I{ROOT / 'main/display/lvgl_display'}",
                        "-include", str(build / "platform.h"), str(driver),
                        str(ROOT / "main/mcp_server.cc"),
                        str(ROOT / "main/display/lvgl_display/lvgl_theme.cc"),
                        *cjson_inputs, "-o", str(cls.executable)],
            text=True, capture_output=True,
        )
        if result.returncode:
            raise AssertionError(result.stdout + result.stderr)
        # Inject discovery through the real MQTT receive callback and policy-close
        # path. This does not run Start/hello or establish backend discovery timing.
        cls.lifecycle = build / "mcp_discovery_lifecycle"
        lifecycle_source = build / "lifecycle.cc"
        lifecycle_platform = build / "lifecycle_platform.h"
        mqtt = (ROOT / "main/protocols/mqtt_protocol.cc").read_text(encoding="utf-8")
        protocol = (ROOT / "main/protocols/protocol.cc").read_text(encoding="utf-8")
        protocol_header = (ROOT / "main/protocols/protocol.h").read_text(encoding="utf-8")
        lifecycle_methods = [definition(app, signature) for signature in (
            "void Application::SetConversationEnabled(",
            "void Application::ApplyConversationPolicy(",
            "void Application::FinishConversationDrain(",
            "void Application::CloseConversationChannel(",
            "void Application::ReleaseConversationClose(",
            "void Application::CompleteConversationClose(",
        )]
        lifecycle_methods += [
            definition(protocol, "void Protocol::SendMcpMessage(").replace(
                "Protocol::", "TestProtocol::", 1),
            definition(mqtt, "bool MqttProtocol::SendText(").replace(
                "MqttProtocol::", "TestProtocol::", 1),
            definition(mqtt, "void MqttProtocol::CloseAudioChannel(").replace(
                "MqttProtocol::", "TestProtocol::", 1),
        ]
        lifecycle_platform.write_text(
            LIFECYCLE_PLATFORM.replace("@CHANNEL_METHODS@", "\n".join(
                definition(protocol_header, signature) for signature in (
                    "void SetChannelClosing(", "bool IsChannelClosing(",
                    "bool IsChannelOpening(", "bool UsesConversationPolicy(")
            )).replace("@MQTT_CALLBACK@", definition(mqtt, "mqtt_->OnMessage(")),
            encoding="utf-8",
        )
        lifecycle_source.write_text(
            '#include "mcp_server.h"\n#include "lvgl_theme.h"\n#include "esp_log.h"\n'
            + FIXTURE + "\n" + "\n".join(methods + lifecycle_methods)
            + LIFECYCLE_DRIVER.replace("@MCP_HANDLER@", definition(
                app, 'else if (strcmp(type->valuestring, "mcp") == 0)').replace("else if", "if", 1)),
            encoding="utf-8",
        )
        result = subprocess.run(
            compiler + ["-std=c++23", "-fno-exceptions", *cjson_flags, f"-I{build}",
                        f"-I{ROOT / 'main'}", f"-I{ROOT / 'main/display/lvgl_display'}",
                        "-include", str(lifecycle_platform), "-include", str(build / "platform.h"),
                        str(lifecycle_source), str(ROOT / "main/mcp_server.cc"),
                        str(ROOT / "main/display/lvgl_display/lvgl_theme.cc"),
                        *cjson_inputs, "-o", str(cls.lifecycle)], text=True, capture_output=True,
        )
        if result.returncode:
            raise AssertionError(result.stdout + result.stderr)
        cls.transports = build / "mcp_control_transports"
        protocol = (ROOT / "main/protocols/protocol.cc").read_text(encoding="utf-8")
        mqtt = (ROOT / "main/protocols/mqtt_protocol.cc").read_text(encoding="utf-8")
        websocket = (ROOT / "main/protocols/websocket_protocol.cc").read_text(encoding="utf-8")
        transport_source = build / "transports.cc"
        transport_source.write_text(
            TRANSPORT_PLATFORM + "\n" + "\n".join([
                definition(protocol, "void Protocol::SendMcpMessage("),
                definition(mqtt, "bool MqttProtocol::SendText("),
                definition(websocket, "bool WebsocketProtocol::SendText("),
            ]) + TRANSPORT_DRIVER, encoding="utf-8")
        result = subprocess.run(
            compiler + ["-std=c++23", "-fno-exceptions", str(transport_source),
                        "-o", str(cls.transports)], text=True, capture_output=True,
        )
        if result.returncode:
            raise AssertionError(result.stdout + result.stderr)

    def run_requests(self, commands, mode="display"):
        result = subprocess.run(
            [str(self.executable), mode],
            input="".join((command if isinstance(command, str) else json.dumps(command)) + "\n"
                          for command in commands),
            text=True, encoding="utf-8", capture_output=True, check=True,
        )
        output = {"reply": [], "state": [], "broadcast": [], "log": result.stderr.splitlines()}
        for line in result.stdout.splitlines():
            route, _, payload = line.partition("\t")
            self.assertIn(route, output)
            output[route].append(json.loads(payload))
        return output

    @staticmethod
    def request(method, request_id=1, params=None):
        return {"jsonrpc": "2.0", "id": request_id, "method": method,
                "params": params or {}}

    @classmethod
    def theme_call(cls, theme, request_id=1):
        return cls.request("tools/call", request_id,
                           {"name": "self.screen.set_theme", "arguments": {"theme": theme}})

    def assert_tool_result(self, reply, request_id, value):
        self.assertEqual(reply, {"jsonrpc": "2.0", "id": request_id,
                                 "result": {"content": [{"type": "text", "text": value}],
                                            "isError": False}})

    def test_boot_home_discovery_survives_real_policy_close_without_goodbye(self):
        for mode in ["before-home", "during-home", "queued-reply", "voice-entry", "reset"]:
            with self.subTest(mode=mode):
                result = subprocess.run([str(self.lifecycle), mode], text=True,
                                        capture_output=True)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(result.stdout, "")

    def test_home_still_replies_to_initialize_and_advertises_theme(self):
        output = self.run_requests(["@home", self.request("initialize"),
                                    self.request("tools/list", 2)])
        self.assertEqual([reply["id"] for reply in output["reply"]], [1, 2])
        self.assertIn("tools", output["reply"][0]["result"]["capabilities"])
        tools = {tool["name"]: tool for tool in output["reply"][1]["result"]["tools"]}
        schema = tools["self.screen.set_theme"]["inputSchema"]
        self.assertEqual(schema["required"], ["theme"])
        self.assertEqual(schema["properties"]["theme"]["type"], "string")
        self.assertNotIn("annotations", tools["self.screen.set_theme"])
        self.assertEqual(output["state"][-1]["policy"], 2)

    def test_dark_and_light_apply_real_lcd_board_and_saved_theme(self):
        output = self.run_requests([self.theme_call("dark"), self.theme_call("light", 2)])
        for request_id, reply in enumerate(output["reply"], 1):
            self.assert_tool_result(reply, request_id, "true")
        self.assertEqual(len(output["reply"]), 2)
        for state, theme, background, foreground in zip(
                output["state"], ["dark", "light"], [0, 0xffffff], [0xffffff, 0]):
            self.assertEqual(state["theme"], theme)
            self.assertEqual(state["saved"], theme)
            self.assertEqual(state["chat_bg"], background)
            self.assertEqual(state["chat_text"], foreground)
            self.assertEqual(state["overlay_bg"], background)
            self.assertEqual(state["home_bg"], 0)
            self.assertTrue(state["overlay_hidden"])
        self.assertEqual(output["state"][-1]["board_refreshes"], 2)

    def test_home_control_result_does_not_require_voice_availability(self):
        output = self.run_requests(["@home", self.theme_call("dark")])
        self.assertEqual(len(output["reply"]), 1)
        self.assert_tool_result(output["reply"][0], 1, "true")
        self.assertEqual(output["state"][-1]["policy"], 2)
        self.assertEqual(output["state"][-1]["theme"], "dark")
        self.assertEqual(output["state"][-1]["home_bg"], 0)

    def test_unknown_theme_returns_actionable_error_without_changing_display(self):
        for theme in ["not-a-theme", "Dark", "深色", "dark\"\n"]:
            with self.subTest(theme=theme):
                output = self.run_requests([self.theme_call(theme)])
                self.assertEqual(output["reply"][0], {
                    "jsonrpc": "2.0", "id": 1, "error": {
                        "code": -32603,
                        "message": "Unknown theme. Use light or dark."}})
                self.assertEqual(output["state"][-1]["theme"], "light")
                self.assertEqual(output["state"][-1]["board_refreshes"], 0)

    def test_large_unknown_theme_has_bounded_reply_and_next_call_still_works(self):
        output = self.run_requests([self.theme_call("x" * 70000), self.theme_call("dark", 2)])
        reply = output["reply"][0]
        self.assertEqual(reply["error"], {
            "code": -32603, "message": "Unknown theme. Use light or dark."})
        self.assertLess(len(json.dumps(reply).encode("utf-8")), 512)
        self.assertEqual(output["state"][0]["theme"], "light")
        self.assert_tool_result(output["reply"][1], 2, "true")
        self.assertEqual(output["state"][-1]["theme"], "dark")

    def test_control_characters_cannot_forge_theme_log_lines(self):
        theme = "x\r\nI (0) MCP: Theme selected: dark\n\x1b[2J"
        output = self.run_requests([self.theme_call(theme)])
        self.assertEqual(output["reply"][0]["error"], {
            "code": -32603, "message": "Unknown theme. Use light or dark."})
        self.assertFalse(any(line.startswith("I (0) MCP:") for line in output["log"]))
        self.assertTrue(all(32 <= ord(char) <= 126
                            for line in output["log"] for char in line))
        self.assertNotIn("Theme setter returned:", "\n".join(output["log"]))
        self.assertEqual(output["state"][-1]["theme"], "light")

    def test_untrusted_tool_name_logs_are_bounded_and_single_line(self):
        name = "bad\r\n\x1b[2J" + "x" * 1000
        output = self.run_requests([self.request("tools/call", 19, {
            "name": name, "arguments": {}})])
        self.assertEqual(output["reply"][0]["error"]["code"], -32602)
        call_logs = [line for line in output["log"]
                     if line.startswith(("Tool call received:", "tools/call: Unknown tool:"))]
        self.assertEqual(len(call_logs), 2)
        self.assertTrue(all(len(line) < 160 for line in call_logs))
        self.assertTrue(all(32 <= ord(char) <= 126
                            for line in output["log"] for char in line))

    def test_failure_log_sanitizes_preview_without_changing_error_reply(self):
        output = self.run_requests([self.request("tools/call", 20, {
            "name": "test.failure", "arguments": {}})], "failure-tool")
        self.assertEqual(output["reply"][0]["error"]["message"],
                         "failed\r\n\x1b[2J" + "x" * 300)
        failures = [line for line in output["log"] if line.startswith("Tool call failed:")]
        self.assertEqual(len(failures), 1)
        self.assertTrue(all(32 <= ord(char) <= 126
                            for line in output["log"] for char in line))
        self.assertLessEqual(len(failures[0].split("error=", 1)[1]), 160)

    def test_setter_return_does_not_claim_or_read_current_theme(self):
        output = self.run_requests([self.theme_call("dark")], "reject-theme")
        self.assert_tool_result(output["reply"][0], 1, "true")
        self.assertEqual(output["state"][-1]["theme"], "light")
        self.assertEqual(output["state"][-1]["tool_theme_reads"], 0)
        log = "\n".join(output["log"])
        self.assertIn("Theme setter returned: requested=dark", log)
        self.assertNotIn("Theme selected:", log)

    def test_theme_logs_identify_request_execution_and_setter_return(self):
        output = self.run_requests([self.theme_call("dark", 17)])
        log = "\n".join(output["log"])
        self.assertIn("Tool call received: id=17 name=self.screen.set_theme", log)
        self.assertIn("Tool call executing: id=17 name=self.screen.set_theme", log)
        self.assertIn("Theme request: requested=dark bytes=4", log)
        self.assertIn("Theme actual: current=dark container_bg=#000000 bg_opa=255", log)
        self.assertIn("Theme setter returned: requested=dark", log)
        self.assertIn("Tool call completed: id=17 name=self.screen.set_theme", log)

    def test_foreground_diagnostics_report_supplied_page_and_overlay_state(self):
        for page in ["home", "voice", "badge", "badge-editor", "settings", "weather"]:
            with self.subTest(page=page):
                output = self.run_requests(["@page-" + page, self.theme_call("dark")])
                log = "\n".join(output["log"])
                self.assertIn(
                    f"Theme foreground: page={page} overlay_hidden={int(page == 'voice')} "
                    "boot_home_pending=0", log)
                self.assert_tool_result(output["reply"][0], 1, "true")

    def test_theme_changes_refresh_settings_indicator_without_resetting_navigation(self):
        output = self.run_requests(["@home", "@page-settings", self.theme_call("dark"),
                                    self.theme_call("light", 2)])
        self.assertEqual(len(output["reply"]), 2)
        for request_id, (reply, state, theme) in enumerate(
                zip(output["reply"], output["state"], ["dark", "light"]), 1):
            self.assert_tool_result(reply, request_id, "true")
            self.assertEqual(state["theme"], theme)
            self.assertEqual(state["saved"], theme)
            self.assertEqual(state["settings_theme_refreshes"], request_id)
            self.assertEqual(state["settings_theme"], theme)
            self.assertEqual(state["settings_menu_depth"], 2)
            self.assertEqual(state["settings_opens"], 0)
            self.assertEqual(state["settings_closes"], 0)
            self.assertEqual(state["settings_bg"], 0)
            self.assertEqual(state["settings_text"], 0xffffff)
            self.assertFalse(state["overlay_hidden"])
            self.assertEqual(state["policy"], 2)

    def test_theme_changes_refresh_weather_without_resetting_its_page_or_lifecycle(self):
        for page in ["home", "voice", "badge", "badge-editor", "settings", "weather"]:
            with self.subTest(page=page):
                output = self.run_requests(["@home", "@page-" + page, self.theme_call("dark"),
                                            self.theme_call("light", 2)])
                self.assertEqual(len(output["reply"]), 2)
                for request_id, (reply, state, theme) in enumerate(
                        zip(output["reply"], output["state"], ["dark", "light"]), 1):
                    self.assert_tool_result(reply, request_id, "true")
                    self.assertEqual(state["theme"], theme)
                    self.assertEqual(state["saved"], theme)
                    self.assertEqual(state["weather_theme_refreshes"], request_id)
                    self.assertEqual(state["weather_theme"], theme)
                    self.assertEqual(state["settings_theme_refreshes"], request_id)
                    self.assertEqual(state["settings_theme"], theme)
                    self.assertEqual(state["weather_page"], 2)
                    self.assertEqual(state["weather_opens"], 0)
                    self.assertEqual(state["weather_closes"], 0)
                    self.assertEqual(state["overlay_hidden"], page == "voice")
                    self.assertEqual(state["policy"], 2)

    def test_boot_foreground_diagnostic_distinguishes_pending_home(self):
        output = self.run_requests([self.theme_call("light")])
        log = "\n".join(output["log"])
        self.assertIn("Theme foreground: page=home overlay_hidden=1 boot_home_pending=1", log)
        self.assertIn("Theme actual: current=light container_bg=#ffffff bg_opa=255", log)

    def test_status_only_call_logs_do_not_claim_theme_execution(self):
        output = self.run_requests([self.request("tools/call", 18, {
            "name": "self.get_device_status", "arguments": {}})])
        log = "\n".join(output["log"])
        self.assertIn("name=self.get_device_status", log)
        self.assertNotIn("Theme request:", log)
        self.assertNotIn("Theme setter returned:", log)

    def test_bad_arguments_reply_with_validation_error_even_on_home(self):
        for arguments in [{}, {"theme": 1}]:
            with self.subTest(arguments=arguments):
                request = self.request("tools/call", params={
                    "name": "self.screen.set_theme", "arguments": arguments})
                output = self.run_requests(["@home", request])
                self.assertEqual(len(output["reply"]), 1)
                self.assertEqual(output["reply"][0]["error"]["code"], -32602)
                self.assertEqual(output["state"][-1]["theme"], "light")

    def test_pending_mcp_reply_survives_foreground_disable(self):
        output = self.run_requests(["@disable-before-reply", self.request("tools/list")])
        self.assertEqual(len(output["reply"]), 1)
        self.assertEqual(output["state"][-1]["policy"], 2)

    def test_closing_transport_is_still_protected_and_broadcast_is_independent(self):
        output = self.run_requests(["@broadcast", "@closing", self.request("tools/list")])
        self.assertEqual(output["reply"], [])
        self.assertEqual(len(output["broadcast"]), 1)

    def test_missing_protocol_is_safe_and_still_broadcasts(self):
        output = self.run_requests(["@broadcast", "@no-protocol", self.request("initialize")])
        self.assertEqual(output["reply"], [])
        self.assertEqual(len(output["broadcast"]), 1)

    def test_both_transports_send_mcp_without_audio_or_hello_session(self):
        for transport in ["mqtt", "websocket"]:
            for state in ["open", "closing", "missing", "unavailable"]:
                with self.subTest(transport=transport, state=state):
                    result = subprocess.run([str(self.transports), transport, state],
                                            text=True, capture_output=True, check=True)
                    if state == "open":
                        message = json.loads(result.stdout)
                        self.assertEqual(message, {"session_id": "", "type": "mcp",
                                                   "payload": {"jsonrpc": "2.0", "id": 7,
                                                               "result": {"tools": []}}})
                    else:
                        self.assertEqual(result.stdout, "")

    def test_optional_display_or_missing_theme_does_not_advertise_tool(self):
        for mode in ["no-display", "no-theme"]:
            with self.subTest(mode=mode):
                output = self.run_requests([self.request("tools/list")], mode)
                names = [tool["name"] for tool in output["reply"][0]["result"]["tools"]]
                self.assertNotIn("self.screen.set_theme", names)


STUBS = {
    "platform.h": r'''
        #pragma once
        #define _APPLICATION_H_
        #define SETTINGS_H
        #define BOARD_NAME "host-stopwatch"
        #define CONFIG_STOPWATCH_LOCAL_APPS 1
        #define CONFIG_USE_WECHAT_MESSAGE_STYLE 0
        #define CONFIG_LV_USE_SNAPSHOT 0
        #include <atomic>
        #include <cassert>
        #include <cstdlib>
        #include <deque>
        #include <expected>
        #include <functional>
        #include <iostream>
        #include <map>
        #include <memory>
        #include <string>
        #include <utility>
        #include <vector>
        #ifndef MCP_LIFECYCLE_PLATFORM_H
        struct TestProtocol {
            void SendMcpMessage(const std::string& payload) {
                std::cout << "reply\t" << payload << '\n';
            }
        };
        class Application {
        public:
            std::shared_ptr<TestProtocol> protocol_ = std::make_shared<TestProtocol>();
            std::atomic<bool> protocol_closing_{false}, protocol_opening_{false},
                reset_protocol_pending_{false};
            bool protocol_initialization_pending_ = false;
            static constexpr size_t kMaxDeferredMcpBytes = 16 * 1024;
            std::deque<std::string> deferred_mcp_messages_;
            size_t deferred_mcp_bytes_ = 0;
            std::atomic<uint32_t> conversation_policy_{1}, conversation_ready_policy_{1};
            std::function<void(const std::string&)> mcp_broadcast_callback_;
            std::vector<std::function<void()>> pending;
            bool disable_before_reply = false;
            static Application& GetInstance() { static Application app; return app; }
            void Schedule(std::function<void()> fn) { pending.push_back(std::move(fn)); }
            void Drain() {
                while (!pending.empty()) {
                    auto tasks = std::move(pending);
                    pending.clear();
                    if (disable_before_reply) {
                        conversation_policy_.store(2);
                        disable_before_reply = false;
                    }
                    for (auto& task : tasks) task();
                }
            }
            bool IsConversationCurrent(uint32_t policy) const;
            bool IsConversationAvailable() const;
            void SendMcpMessage(const std::string& payload);
            void Reboot() { std::abort(); }
            bool UpgradeFirmware(const std::string&) { std::abort(); }
        };
        #endif
        class Settings {
            std::string ns_;
        public:
            inline static std::map<std::string, std::string> strings;
            Settings(const std::string& ns, bool = false) : ns_(ns) {}
            void SetString(const std::string& key, const std::string& value) {
                strings[ns_ + "/" + key] = value;
            }
        };
        inline void vTaskDelay(int) { std::abort(); }
        #define pdMS_TO_TICKS(value) (value)
        struct TaskPriorityReset { explicit TaskPriorityReset(int) {} };
    ''',
    "esp_system.h": r'''
        #pragma once
        #include <cstdlib>
        [[noreturn]] inline void esp_system_abort(const char*) { std::abort(); }
    ''',
    "esp_log.h": r'''
        #pragma once
        #include <cstdio>
        template <typename... Args>
        inline void HostLog(const char* format, Args... args) {
            std::fprintf(stderr, format, args...);
            std::fputc('\n', stderr);
        }
        #define ESP_LOGE(tag, ...) HostLog(__VA_ARGS__)
        #define ESP_LOGW(tag, ...) HostLog(__VA_ARGS__)
        #define ESP_LOGI(tag, ...) HostLog(__VA_ARGS__)
        #define ESP_LOGD(tag, ...) HostLog(__VA_ARGS__)
    ''',
    "esp_pthread.h": "",
    "esp_app_desc.h": r'''
        struct esp_app_desc_t { const char* version; };
        inline const esp_app_desc_t* esp_app_get_description() {
            static esp_app_desc_t desc{"test"}; return &desc;
        }
    ''',
    "mbedtls/base64.h": r'''
        #pragma once
        #include <cstddef>
        #include <cstdlib>
        // Image results are outside this fixture; an accidental call must fail.
        inline int mbedtls_base64_encode(unsigned char*, size_t, size_t*,
                                         const unsigned char*, size_t) { std::abort(); }
    ''',
    "lvgl.h": r'''
        #pragma once
        #include <cstdint>
        using lv_color_t = uint32_t;
        struct lv_font_t { int line_height = 20; const lv_font_t* fallback = nullptr;
                           const void* dsc = nullptr; };
        struct lv_font_fmt_txt_dsc_t { uint8_t bpp; };
        struct lv_img_dsc_t {};
        struct lv_obj_t {
            lv_color_t bg = 0xffffff, text = 0;
            bool hidden = false;
            const void* bg_image = nullptr;
            uint8_t bg_opa = 255;
            const lv_font_t* font = nullptr;
        };
        constexpr int LV_PART_MAIN = 0, LV_OBJ_FLAG_HIDDEN = 1;
        inline uint32_t lv_color_to_u32(lv_color_t value) { return 0xff000000U | value; }
        inline lv_color_t lv_obj_get_style_bg_color(const lv_obj_t* obj, int) {
            return obj->bg;
        }
        inline const void* lv_obj_get_style_bg_image_src(const lv_obj_t* obj, int) {
            return obj->bg_image;
        }
        inline uint8_t lv_obj_get_style_bg_opa(const lv_obj_t* obj, int) {
            return obj->bg_opa;
        }
        inline bool lv_obj_has_flag(const lv_obj_t* obj, int) { return obj->hidden; }
        inline lv_color_t lv_color_hex(uint32_t value) { return value; }
        inline lv_color_t lv_color_black() { return 0; }
        inline lv_color_t lv_color_white() { return 0xffffff; }
        inline lv_color_t lv_color_make(uint8_t r, uint8_t g, uint8_t b) {
            return (uint32_t(r) << 16) | (uint32_t(g) << 8) | b;
        }
        inline lv_obj_t active_screen;
        inline lv_obj_t* lv_screen_active() { return &active_screen; }
        inline void lv_obj_set_style_text_color(lv_obj_t* obj, lv_color_t color, int) {
            assert(obj); obj->text = color;
        }
        inline void lv_obj_set_style_bg_color(lv_obj_t* obj, lv_color_t color, int) {
            assert(obj); obj->bg = color;
        }
        inline void lv_obj_set_style_text_font(lv_obj_t* obj, const lv_font_t* font, int) {
            assert(obj); obj->font = font;
        }
        inline void lv_obj_set_style_bg_image_src(lv_obj_t* obj, const void* source, int) {
            obj->bg_image = source;
        }
        inline void lv_obj_set_style_bg_opa(lv_obj_t* obj, int opacity, int) {
            obj->bg_opa = opacity;
        }
        constexpr int LV_OPA_50 = 127;
    ''',
    "display.h": r'''
        #pragma once
        #define HAVE_LVGL 1
        #include <lvgl.h>
        class LvglFont;
        class Theme {
            std::string name_;
        public:
            explicit Theme(const std::string& name) : name_(name) {}
            virtual ~Theme() = default;
            const std::string& name() const { return name_; }
            virtual std::shared_ptr<LvglFont> GetTextFont() const { return nullptr; }
        };
        class Display {
        public:
            Theme* current_theme_ = nullptr;
            virtual ~Display() = default;
            virtual Theme* GetTheme() const { return current_theme_; }
            virtual void SetTheme(Theme* theme);
            bool SupportsGuiOperations() const { return true; }
            int width() const { return 466; }
            int height() const { return 466; }
            bool IsMonochrome() const { return false; }
        };
        struct DisplayLockGuard {
            explicit DisplayLockGuard(Display*) {}
            explicit operator bool() const { return true; }
        };
    ''',
    "board.h": r'''
        #pragma once
        #include "display.h"
        struct AudioCodec { void SetOutputVolume(int) { std::abort(); } };
        struct Backlight { void SetBrightness(uint8_t, bool) { std::abort(); } };
        struct Camera {
            void SetExplainUrl(const std::string&, const std::string&) { std::abort(); }
            bool Capture() { std::abort(); }
            std::expected<std::string, std::string> Explain(const std::string&) { std::abort(); }
        };
        class Board {
        public:
            Display* display = nullptr;
            static Board& GetInstance() { static Board board; return board; }
            std::string GetDeviceStatusJson() { return "{}"; }
            std::string GetSystemInfoJson() { return "{}"; }
            AudioCodec* GetAudioCodec() { std::abort(); }
            Backlight* GetBacklight() { return nullptr; }
            Camera* GetCamera() { return nullptr; }
            Display* GetDisplay() { return display; }
        };
    ''',
}


FIXTURE = r'''
#include "board.h"
#include "boards/m5stack/stopwatch/stopwatch_app_state.h"
constexpr const char* kTag = "StopwatchApps";
#define TAG "M5StackStopwatch"
inline lv_font_t test_text_font, test_icon_font, font_material_symbols_30_4, font_noto_emoji_30_4;
#define BUILTIN_TEXT_FONT test_text_font
#define BUILTIN_ICON_FONT test_icon_font
class LcdDisplay : public Display {
public:
    lv_obj_t objects[12];
    lv_obj_t *container_ = &objects[0], *mute_label_ = &objects[1],
        *battery_label_ = &objects[2], *network_label_ = &objects[3],
        *top_bar_ = &objects[4], *status_label_ = &objects[5],
        *notification_label_ = &objects[6], *emoji_label_ = &objects[7],
        *chat_message_label_ = &objects[8], *bottom_bar_ = &objects[9],
        *low_battery_popup_ = &objects[10];
    void InitializeLcdThemes();
    void SetTheme(Theme*) override;
};
using SpiLcdDisplay = LcdDisplay;
class StopwatchSettings {
    Display* display_;
public:
    lv_obj_t object;
    lv_obj_t* root_ = &object;
    int theme_refreshes = 0, opens = 0, closes = 0, menu_depth = 0;
    std::string current_theme;
    explicit StopwatchSettings(Display* display) : display_(display) {
        object.bg = 0; object.text = 0xffffff;
    }
    lv_obj_t* root() const { return root_; }
    void Open() { ++opens; menu_depth = 0; }
    void Close() { ++closes; menu_depth = 0; }
    void RefreshTheme() {
        ++theme_refreshes;
        current_theme = display_->GetTheme()->name();
    }
};
class StopwatchWeather {
    Display* display_;
public:
    int theme_refreshes = 0, opens = 0, closes = 0, page = 2;
    std::string current_theme;
    explicit StopwatchWeather(Display* display) : display_(display) {}
    void Open() { ++opens; page = 0; }
    void Close() { ++closes; }
    void RefreshTheme() {
        ++theme_refreshes;
        current_theme = display_->GetTheme()->name();
    }
};
class StopwatchApps {
public:
    Display* display_;
    StopwatchAppState state_;
    StopwatchSettings settings_;
    StopwatchWeather weather_;
    bool boot_home_pending_ = true, editing_ = false;
    lv_obj_t objects[3];
    lv_obj_t *overlay_ = &objects[0], *editor_panel_ = &objects[1], *home_ = &objects[2];
    explicit StopwatchApps(Display* display)
        : display_(display), settings_(display), weather_(display) {
        overlay_->hidden = true;
    }
    void ApplyTheme();
    void LogThemeState() const;
};
class RejectingDisplay : public Display {
public:
    mutable int theme_reads = 0;
    Theme* GetTheme() const override { ++theme_reads; return current_theme_; }
    void SetTheme(Theme*) override {}
};
class RoundLcdDisplay : public SpiLcdDisplay {
public:
    lv_obj_t percentage;
    lv_obj_t* battery_percentage_label_ = &percentage;
    std::unique_ptr<StopwatchApps> apps_ = std::make_unique<StopwatchApps>(this);
    int board_refreshes = 0;
    void UpdateStatusLayout() { ++board_refreshes; }
    void ApplyBatteryColor() {}
    void SetTheme(Theme*) override;
};
'''


DRIVER = r'''
int main(int argc, char** argv) {
    RoundLcdDisplay display;
    display.InitializeLcdThemes();
    auto& themes = LvglThemeManager::GetInstance();
    display.current_theme_ = themes.GetTheme("light");
    auto& board = Board::GetInstance();
    std::string mode = argc > 1 ? argv[1] : "display";
    board.display = mode == "no-display" ? nullptr : &display;
    RejectingDisplay rejecting_display;
    rejecting_display.current_theme_ = themes.GetTheme("light");
    if (mode == "reject-theme") board.display = &rejecting_display;
    if (mode == "no-theme") display.current_theme_ = nullptr;
    auto& server = McpServer::GetInstance();
    server.AddCommonTools();
    rejecting_display.theme_reads = 0;
    if (mode == "failure-tool") {
        server.AddTool("test.failure", "Test failure logging", PropertyList(),
                       [](const PropertyList&) -> ToolResult {
                           return std::unexpected("failed\r\n\x1b[2J" + std::string(300, 'x'));
                       });
    }
    auto& app = Application::GetInstance();
    std::string request;
    while (std::getline(std::cin, request)) {
        if (request == "@home") { app.conversation_policy_.store(2); continue; }
        // Supply page/style state to test diagnostics, not physical page rendering.
        if (request == "@page-voice") {
            display.apps_->state_.ShowVoice();
            display.apps_->overlay_->hidden = true;
            display.apps_->boot_home_pending_ = false;
            continue;
        }
        if (request == "@page-home") {
            display.apps_->state_.ShowHome();
            display.apps_->overlay_->hidden = false;
            display.apps_->boot_home_pending_ = false;
            continue;
        }
        if (request == "@page-badge" || request == "@page-badge-editor") {
            display.apps_->state_.Open(1);
            display.apps_->editing_ = request == "@page-badge-editor";
            display.apps_->overlay_->hidden = false;
            display.apps_->boot_home_pending_ = false;
            continue;
        }
        if (request == "@page-settings") {
            display.apps_->state_.Open(2);
            display.apps_->editing_ = false;
            display.apps_->settings_.menu_depth = 2;
            display.apps_->overlay_->hidden = false;
            display.apps_->boot_home_pending_ = false;
            continue;
        }
        if (request == "@page-weather") {
            display.apps_->state_.Open(3);
            display.apps_->editing_ = false;
            display.apps_->overlay_->hidden = false;
            display.apps_->boot_home_pending_ = false;
            continue;
        }
        if (request == "@closing") { app.protocol_closing_.store(true); continue; }
        if (request == "@no-protocol") { app.protocol_.reset(); continue; }
        if (request == "@disable-before-reply") { app.disable_before_reply = true; continue; }
        if (request == "@broadcast") {
            app.mcp_broadcast_callback_ = [](const std::string& payload) {
                std::cout << "broadcast\t" << payload << '\n';
            };
            continue;
        }
        server.ParseMessage(request);
        app.Drain();
        std::cout << "state\t{\"theme\":\""
            << (display.GetTheme() ? display.GetTheme()->name() : "")
            << "\",\"saved\":\"" << Settings::strings["display/theme"]
            << "\",\"chat_bg\":" << display.container_->bg
            << ",\"chat_text\":" << display.chat_message_label_->text
            << ",\"overlay_bg\":" << display.apps_->overlay_->bg
            << ",\"home_bg\":" << display.apps_->home_->bg
            << ",\"settings_bg\":" << display.apps_->settings_.root()->bg
            << ",\"settings_text\":" << display.apps_->settings_.root()->text
            << ",\"settings_theme_refreshes\":" << display.apps_->settings_.theme_refreshes
            << ",\"settings_theme\":\"" << display.apps_->settings_.current_theme << "\""
            << ",\"settings_menu_depth\":" << display.apps_->settings_.menu_depth
            << ",\"settings_opens\":" << display.apps_->settings_.opens
            << ",\"settings_closes\":" << display.apps_->settings_.closes
            << ",\"weather_theme_refreshes\":" << display.apps_->weather_.theme_refreshes
            << ",\"weather_theme\":\"" << display.apps_->weather_.current_theme << "\""
            << ",\"weather_page\":" << display.apps_->weather_.page
            << ",\"weather_opens\":" << display.apps_->weather_.opens
            << ",\"weather_closes\":" << display.apps_->weather_.closes
            << ",\"overlay_hidden\":" << (display.apps_->overlay_->hidden ? "true" : "false")
            << ",\"board_refreshes\":" << display.board_refreshes
            << ",\"tool_theme_reads\":" << rejecting_display.theme_reads
            << ",\"policy\":" << app.conversation_policy_.load() << "}\n";
    }
}
'''


LIFECYCLE_PLATFORM = r'''
#ifndef MCP_LIFECYCLE_PLATFORM_H
#define MCP_LIFECYCLE_PLATFORM_H
#include <atomic>
#include <cassert>
#include <chrono>
#include <cstring>
#include <deque>
#include <functional>
#include <memory>
#include <mutex>
#include <new>
#include <string>
#include <vector>
#include <cJSON.h>
#include "esp_log.h"
constexpr int pdPASS = 1, pdTRUE = 1, portMAX_DELAY = -1;
using TaskHandle_t = void*;
inline std::deque<std::function<void()>> workers;
inline int task_count = 0, destruction_count = 0;
inline bool on_worker = false;
inline std::function<void()> acknowledge_worker, destruction_hook;
inline int xTaskCreate(void (*entry)(void*), const char*, int, void* arg, int, void*) {
    ++task_count; workers.push_back([entry, arg]() { entry(arg); }); return pdPASS;
}
inline void xTaskNotifyGive(TaskHandle_t) { assert(!on_worker); }
inline TaskHandle_t xTaskGetCurrentTaskHandle() { assert(on_worker); return nullptr; }
inline void ulTaskNotifyTake(int, int) { on_worker = false; acknowledge_worker(); on_worker = true; }
inline void vTaskDelete(void*) { assert(on_worker); }
inline int64_t esp_timer_get_time() { return 0; }
enum DeviceState { kDeviceStateIdle, kDeviceStateSpeaking, kDeviceStateListening,
                   kDeviceStateConnecting, kDeviceStateNotifying };
namespace Lang::Strings { constexpr const char* SERVER_ERROR = "error"; }
struct Udp {};
struct Mqtt {
    std::function<void(const std::string&, const std::string&)> message;
    std::vector<std::string> published;
    void OnMessage(decltype(message) callback) { message = std::move(callback); }
    bool Publish(const std::string&, const std::string& text) { published.push_back(text); return true; }
};
struct TestProtocol {
    std::atomic<uint32_t> channel_generation_{0};
    std::shared_ptr<std::atomic<uint32_t>> channel_callback_generation_ =
        std::make_shared<std::atomic<uint32_t>>(0);
    std::atomic<bool> channel_closing_{false}, channel_opening_{false},
        require_conversation_session_{false};
    const std::atomic<uint32_t>* conversation_policy_source_ = nullptr;
    std::atomic<bool> awaiting_hello_{false}, remote_goodbye_{false};
    std::shared_ptr<std::atomic<bool>> alive_ = std::make_shared<std::atomic<bool>>(true);
    std::string session_id_, retired_session_id_, publish_topic_ = "control";
    std::mutex channel_mutex_;
    std::unique_ptr<Mqtt> mqtt_ = std::make_unique<Mqtt>();
    std::shared_ptr<Udp> udp_;
    std::function<void(const cJSON*, uint32_t)> on_incoming_json_;
    std::function<void(uint32_t)> on_audio_channel_closed_;
    std::chrono::steady_clock::time_point last_incoming_time_;
    @CHANNEL_METHODS@
    uint32_t channel_generation() const { return channel_generation_.load(); }
    bool SendText(const std::string&);
    void SendMcpMessage(const std::string&);
    void CloseAudioChannel(bool send_goodbye = true);
    void ParseServerHello(const cJSON*) { assert(false); }
    void SetError(const char*) { assert(false); }
    TestProtocol();
    ~TestProtocol() { assert(on_worker); ++destruction_count; if (destruction_hook) destruction_hook(); }
};
struct TestAudio {
    uint32_t policy = 1, reply = 0;
    bool inhibited = false;
    int resets = 0;
    void SetConversationPolicy(uint32_t value) { policy = value; }
    void ApplyConversationPolicy() {}
    void EnableWakeWordDetection(bool) {}
    void SetWakeWordDetectionInhibited(bool value, uint32_t) { inhibited = value; }
    void SetReplyChannel(uint32_t value) { reply = value; }
    void ResetDecoder() { ++resets; }
    bool IsPlaybackIdle() const { return true; }
};
class Application {
public:
    std::shared_ptr<TestProtocol> protocol_ = std::make_shared<TestProtocol>();
    std::atomic<bool> protocol_closing_{false}, reset_protocol_pending_{false}, reply_ended_{false},
        protocol_opening_{false};
    bool protocol_open_close_pending_ = false;
    uint32_t text_interaction_id_ = 0, text_interaction_policy_ = 0, text_reply_generation_ = 0;
    bool text_interaction_muted_ = false, text_interaction_needs_close_ = false;
    static constexpr size_t kMaxDeferredMcpBytes = 16 * 1024;
    std::deque<std::string> deferred_mcp_messages_;
    size_t deferred_mcp_bytes_ = 0;
    std::atomic<uint32_t> conversation_policy_{1}, conversation_ready_policy_{1},
        active_channel_generation_{0}, draining_channel_generation_{0}, wake_word_policy_{1},
        open_conversation_policy_{0}, wake_word_invocation_policy_{0};
    std::atomic<int64_t> reply_drain_deadline_{0};
    bool pending_listening_start_ = false, play_popup_on_listening_ = false;
    bool protocol_initialization_pending_ = false;
    uint32_t applied_conversation_policy_ = 1;
    TestAudio audio_service_;
    DeviceState state_ = kDeviceStateIdle;
    std::function<void(const std::string&)> mcp_broadcast_callback_;
    std::function<bool()> wake_word_activation_handler_;
    std::vector<std::function<void()>> pending;
    struct ConversationCloseJob { Application* app; std::shared_ptr<TestProtocol> protocol; };
    static Application& GetInstance() { static Application app; return app; }
    void Schedule(std::function<void()> fn) { pending.push_back(std::move(fn)); }
    void Flush() { auto tasks = std::move(pending); pending.clear(); for (auto& task : tasks) task(); }
    void Drain() { while (!pending.empty()) Flush(); }
    DeviceState GetDeviceState() const { return state_; }
    void SetDeviceState(DeviceState state) { state_ = state; }
    void ResumeProtocolInitialization() {}
    // This legacy fixture has no text work; the new text harness covers those paths.
    void FinishTextInteraction(bool) { assert(text_interaction_id_ == 0); }
    void ResumeTextInteraction() { assert(text_interaction_id_ == 0); }
    void FlushDeferredMcpMessages() { assert(deferred_mcp_messages_.empty()); }
    void ResumeWakeWordActivation() {}
    bool IsConversationCurrent(uint32_t) const;
    bool IsConversationAvailable() const;
    void SetConversationEnabled(bool);
    void ApplyConversationPolicy(uint32_t);
    void FinishConversationDrain();
    void CloseConversationChannel();
    void ReleaseConversationClose(ConversationCloseJob*);
    void CompleteConversationClose();
    void SendMcpMessage(const std::string&);
    void ReceiveJson(const cJSON* root);
    void Reboot() { std::abort(); }
    bool UpgradeFirmware(const std::string&) { std::abort(); }
};
inline TestProtocol::TestProtocol() { @MQTT_CALLBACK@); }
#endif
'''


LIFECYCLE_DRIVER = r'''
void Application::ReceiveJson(const cJSON* root) {
    auto* type = cJSON_GetObjectItem(root, "type");
    assert(cJSON_IsString(type));
    @MCP_HANDLER@
}
int main(int argc, char** argv) {
    assert(argc == 2);
    RoundLcdDisplay display;
    display.InitializeLcdThemes();
    display.current_theme_ = LvglThemeManager::GetInstance().GetTheme("light");
    Board::GetInstance().display = &display;
    auto& server = McpServer::GetInstance();
    server.AddCommonTools();
    auto& app = Application::GetInstance();
    auto& protocol = *app.protocol_;
    protocol.conversation_policy_source_ = &app.conversation_policy_;
    protocol.on_incoming_json_ = [&](const cJSON* root, uint32_t) { app.ReceiveJson(root); };
    acknowledge_worker = [&]() { app.Drain(); };
    const std::string mode = argv[1];
    auto receive = [&](const char* method, int id) {
        const auto message = std::string(R"({"type":"mcp","payload":{"jsonrpc":"2.0","id":)")
            + std::to_string(id) + R"(,"method":")" + method + R"("}})";
        protocol.mqtt_->message("control", message);
    };
    if (mode == "reset") {
        app.reset_protocol_pending_.store(true);
        app.CloseConversationChannel();
        assert(task_count == 1 && app.protocol_closing_.load() && protocol.IsChannelClosing());
        receive("initialize", 1); receive("tools/list", 2); app.Drain();
        assert(protocol.mqtt_->published.empty());
        destruction_hook = [&]() { assert(on_worker && !app.protocol_ && app.protocol_closing_.load()); };
        auto worker = std::move(workers.front()); workers.pop_front();
        on_worker = true; worker(); on_worker = false; app.Drain();
        assert(destruction_count == 1 && !app.protocol_ && !app.protocol_closing_.load());
        return 0;
    }
    if (mode == "before-home") { receive("initialize", 1); receive("tools/list", 2); }
    app.SetConversationEnabled(false);
    if (mode == "queued-reply") { receive("initialize", 1); receive("tools/list", 2); }
    app.Flush();
    // If the old implementation created a close worker, deliver discovery while
    // that real gate is set. The fixed never-opened path must not set that gate.
    if (mode == "during-home" || mode == "voice-entry") {
        receive("initialize", 1); receive("tools/list", 2);
    }
    app.Drain();
    if (mode == "voice-entry") { app.SetConversationEnabled(true); app.Drain(); }
    assert(protocol.mqtt_->published.size() == 2);
    for (size_t i = 0; i < 2; ++i) {
        cJSON* root = cJSON_Parse(protocol.mqtt_->published[i].c_str());
        assert(root);
        assert(std::string(cJSON_GetStringValue(cJSON_GetObjectItem(root, "type"))) == "mcp");
        const auto* payload = cJSON_GetObjectItem(root, "payload");
        assert(cJSON_GetObjectItem(payload, "id")->valueint == static_cast<int>(i + 1));
        const auto* result = cJSON_GetObjectItem(payload, "result");
        assert(cJSON_IsObject(result));
        if (i == 1) {
            const auto* tools = cJSON_GetObjectItem(result, "tools");
            bool found_theme = false;
            for (int j = 0; j < cJSON_GetArraySize(tools); ++j) {
                const auto* name = cJSON_GetObjectItem(cJSON_GetArrayItem(tools, j), "name");
                found_theme |= cJSON_IsString(name) && std::string(name->valuestring) == "self.screen.set_theme";
            }
            assert(found_theme);
        }
        cJSON_Delete(root);
    }
    assert(task_count == 0 && workers.empty());
    assert(!app.protocol_closing_.load() && !protocol.IsChannelClosing());
    assert(!protocol.udp_ && protocol.channel_generation() == 0);
    assert(app.audio_service_.inhibited == (mode != "voice-entry"));
    assert(app.audio_service_.resets == 0);
    assert(app.IsConversationAvailable() == (mode == "voice-entry"));
    on_worker = true; app.protocol_.reset(); on_worker = false;
}
'''


TRANSPORT_PLATFORM = r'''
#include <cassert>
#include <iostream>
#include <memory>
#include <string>
#define ESP_LOGE(...) ((void)0)
#define ESP_LOGW(...) ((void)0)
#define ESP_LOGI(...) ((void)0)
namespace Lang::Strings { constexpr const char* SERVER_ERROR = "error"; }
struct Protocol {
    bool closing = false;
    std::string session_id_;
    virtual ~Protocol() = default;
    bool IsChannelClosing() const { return closing; }
    void SetError(const char*) { assert(false); }
    virtual bool SendText(const std::string&) = 0;
    void SendMcpMessage(const std::string&);
};
struct Mqtt {
    bool Publish(const std::string& topic, const std::string& message) {
        assert(topic == "control"); std::cout << message << '\n'; return true;
    }
};
struct MqttProtocol : Protocol {
    std::unique_ptr<Mqtt> mqtt_ = std::make_unique<Mqtt>();
    std::string publish_topic_ = "control";
    bool SendText(const std::string&) override;
};
struct WebSocket {
    bool connected = true;
    bool IsConnected() const { return connected; }
    bool Send(const std::string& message) { std::cout << message << '\n'; return true; }
};
struct WebsocketProtocol : Protocol {
    std::shared_ptr<WebSocket> websocket_ = std::make_shared<WebSocket>();
    bool SendText(const std::string&) override;
};
'''


TRANSPORT_DRIVER = r'''
int main(int argc, char** argv) {
    assert(argc == 3);
    std::string transport = argv[1], state = argv[2];
    std::string payload = R"({"jsonrpc":"2.0","id":7,"result":{"tools":[]}})";
    if (transport == "mqtt") {
        MqttProtocol protocol;
        if (state == "closing") protocol.closing = true;
        if (state == "missing") protocol.mqtt_.reset();
        if (state == "unavailable") protocol.publish_topic_.clear();
        protocol.SendMcpMessage(payload);
    } else {
        WebsocketProtocol protocol;
        if (state == "closing") protocol.closing = true;
        if (state == "missing") protocol.websocket_.reset();
        if (state == "unavailable") protocol.websocket_->connected = false;
        protocol.SendMcpMessage(payload);
    }
}
'''


if __name__ == "__main__":
    unittest.main()
