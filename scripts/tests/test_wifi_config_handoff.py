"""Host regression for WifiBoard's actual provisioning handoff acceptance result.

Only EnterWifiConfigMode is extracted; device states come from the real header.
The stubs record task allocation, delayed execution and hardware-boundary calls.
Wi-Fi drivers, provisioning backends and Settings' latch belong to other tests.
"""
import pathlib
import shutil
import subprocess
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[2]
COMPILER = shutil.which("c++") or shutil.which("g++")


def production_method(source, signature):
    """Keep nested worker bodies intact when extracting the production method."""
    start = source.index(signature)
    brace = source.index("{", start)
    depth, end = 1, brace + 1
    while depth:
        depth += (source[end] == "{") - (source[end] == "}")
        end += 1
    return source[start:end]


STUBS = r'''
#include "device_state.h"
#include <cassert>
#include <cstdint>
#include <deque>
#include <string>
#include <vector>

using BaseType_t = int;
using TickType_t = uint32_t;
using TaskHandle_t = void*;
using esp_timer_handle_t = const void*;
constexpr BaseType_t pdPASS = 1;
constexpr BaseType_t errCOULD_NOT_ALLOCATE_REQUIRED_MEMORY = -1;
constexpr TickType_t pdMS_TO_TICKS(uint32_t milliseconds) { return milliseconds / 10; }

namespace test {
struct Task {
    void (*entry)(void*);
    std::string name;
    uint32_t stack;
    void* argument;
    unsigned priority;
    TaskHandle_t* output_handle;
};
inline bool allocation_succeeds = true, on_worker = false;
inline int errors = 0, deleted_tasks = 0;
inline std::vector<std::string> trace;
inline std::vector<Task> attempts;
inline std::deque<Task> pending;
inline std::vector<TickType_t> delays;
inline std::vector<esp_timer_handle_t> stopped_timers;

template <class... Args>
void Log(bool error, const char*, const char*, Args...) { errors += error; }

void Reset() {
    allocation_succeeds = true;
    on_worker = false;
    errors = deleted_tasks = 0;
    trace.clear(); attempts.clear(); pending.clear(); delays.clear(); stopped_timers.clear();
}

void RunWorker() {
    assert(!on_worker && pending.size() == 1);
    auto task = pending.front();
    pending.pop_front();
    on_worker = true;
    task.entry(task.argument);
    on_worker = false;
}
}

#define ESP_LOGI(tag, ...) test::Log(false, tag, __VA_ARGS__)
#define ESP_LOGE(tag, ...) test::Log(true, tag, __VA_ARGS__)
constexpr const char* TAG = "WifiBoard";

BaseType_t xTaskCreate(void (*entry)(void*), const char* name, uint32_t stack,
                      void* argument, unsigned priority, TaskHandle_t* output_handle) {
    test::trace.push_back("task-create");
    test::Task task{entry, name, stack, argument, priority, output_handle};
    test::attempts.push_back(task);
    if (!test::allocation_succeeds) return errCOULD_NOT_ALLOCATE_REQUIRED_MEMORY;
    test::pending.push_back(task);
    return pdPASS;
}

void vTaskDelay(TickType_t delay) {
    assert(test::on_worker);
    test::trace.push_back("delay");
    test::delays.push_back(delay);
}

void vTaskDelete(TaskHandle_t task) {
    assert(test::on_worker && task == nullptr);
    test::trace.push_back("task-delete");
    ++test::deleted_tasks;
}

int esp_timer_stop(esp_timer_handle_t timer) {
    assert(timer != nullptr);
    test::trace.push_back("timer-stop");
    test::stopped_timers.push_back(timer);
    return 0;
}

namespace Lang::Strings {
constexpr const char* ENTERING_WIFI_CONFIG_MODE = "Entering Wi-Fi configuration";
}

class Display {
public:
    int notifications = 0;
    void ShowNotification(const char* text) {
        assert(std::string(text) == Lang::Strings::ENTERING_WIFI_CONFIG_MODE);
        test::trace.push_back("notify");
        ++notifications;
    }
};

class Application {
public:
    DeviceState state = kDeviceStateIdle;
    int resets = 0;
    static Application& GetInstance() { static Application app; return app; }
    DeviceState GetDeviceState() const { return state; }
    void ResetProtocol() { test::trace.push_back("reset"); ++resets; }
};

class WifiManager {
public:
    int stops = 0;
    static WifiManager& GetInstance() { static WifiManager wifi; return wifi; }
    void StopStation() { test::trace.push_back("station-stop"); ++stops; }
};

class WifiBoard {
public:
    int timer_token = 0, config_starts = 0;
    esp_timer_handle_t connect_timer_ = &timer_token;
    Display display;
    Display* GetDisplay() { return &display; }
    void StartWifiConfigMode() {
        test::trace.push_back("config-start");
        ++config_starts;
        Application::GetInstance().state = kDeviceStateWifiConfiguring;
    }
    bool EnterWifiConfigMode();
};
'''


HARNESS = r'''
struct Fixture {
    Application& app = Application::GetInstance();
    WifiManager& wifi = WifiManager::GetInstance();
    WifiBoard board;
    explicit Fixture(DeviceState state = kDeviceStateIdle) {
        test::Reset();
        app.state = state;
        app.resets = wifi.stops = 0;
    }
    void NoHandoff() const {
        assert(wifi.stops == 0 && board.config_starts == 0);
        assert(test::stopped_timers.empty() && test::delays.empty());
        assert(test::deleted_tasks == 0);
    }
    void Queued(DeviceState original_state) const {
        assert(app.state == original_state && app.resets == 1);
        assert(board.display.notifications == 1 && test::errors == 0);
        assert(test::attempts.size() == 1 && test::pending.size() == 1);
        const auto& task = test::pending.front();
        assert(task.entry != nullptr && task.name == "wifi_cfg_delay");
        assert(task.stack == 4096 && task.argument == &board);
        assert(task.priority == 2 && task.output_handle == nullptr);
        assert((test::trace == std::vector<std::string>{"notify", "reset", "task-create"}));
        NoHandoff();
    }
    void Finished() const {
        assert(app.state == kDeviceStateWifiConfiguring);
        assert(wifi.stops == 1 && board.config_starts == 1);
        assert(test::stopped_timers.size() == 1);
        assert(test::stopped_timers.front() == board.connect_timer_);
        assert(test::pending.empty());
    }
};

int main(int argc, char** argv) {
    assert(argc == 2);
    const std::string name = argv[1];
    if (name == "state_matrix") {
        // Cover every current production enum value, not a parallel stub enum.
        for (int value = kDeviceStateUnknown; value <= kDeviceStateFatalError; ++value) {
            const auto state = static_cast<DeviceState>(value);
            Fixture f(state);
            const bool accepted = f.board.EnterWifiConfigMode();
            if (state == kDeviceStateStarting) {
                assert(accepted);
                f.Finished();
                assert(f.app.resets == 0 && test::attempts.empty());
            } else if (state == kDeviceStateIdle || state == kDeviceStateListening ||
                       state == kDeviceStateSpeaking || state == kDeviceStateNotifying) {
                assert(accepted);
                f.Queued(state);
            } else {
                assert(!accepted && f.app.state == state && f.app.resets == 0);
                assert(test::attempts.empty() && test::pending.empty());
                assert(test::errors == 1);
                f.NoHandoff();
            }
        }
    } else if (name == "allocation_failure") {
        for (auto state : {kDeviceStateIdle, kDeviceStateListening,
                           kDeviceStateSpeaking, kDeviceStateNotifying}) {
            Fixture f(state);
            test::allocation_succeeds = false;
            assert(!f.board.EnterWifiConfigMode());
            assert(f.app.state == state && f.app.resets == 1);
            assert(test::attempts.size() == 1 && test::pending.empty());
            assert(test::errors == 1);
            assert((test::trace == std::vector<std::string>{"notify", "reset", "task-create"}));
            f.NoHandoff();
        }
    } else if (name == "delayed_worker") {
        for (auto state : {kDeviceStateIdle, kDeviceStateListening,
                           kDeviceStateSpeaking, kDeviceStateNotifying}) {
            Fixture f(state);
            assert(f.board.EnterWifiConfigMode());
            f.Queued(state);
            test::RunWorker();
            f.Finished();
            assert(test::delays.size() == 1 && test::delays.front() == pdMS_TO_TICKS(1000));
            assert(test::deleted_tasks == 1 && f.app.resets == 1);
            assert((test::trace == std::vector<std::string>{
                "notify", "reset", "task-create", "delay", "timer-stop",
                "station-stop", "config-start", "task-delete"}));
        }
    } else if (name == "starting_synchronous") {
        Fixture f(kDeviceStateStarting);
        test::allocation_succeeds = false;  // Starting must not need worker allocation.
        assert(f.board.EnterWifiConfigMode());
        f.Finished();
        assert(test::attempts.empty() && f.app.resets == 0);
        assert(test::delays.empty() && test::deleted_tasks == 0 && test::errors == 0);
        assert((test::trace == std::vector<std::string>{
            "notify", "timer-stop", "station-stop", "config-start"}));
    } else if (name == "retry_after_failure") {
        Fixture f;
        test::allocation_succeeds = false;
        assert(!f.board.EnterWifiConfigMode());
        f.NoHandoff();
        assert(test::pending.empty() && f.app.state == kDeviceStateIdle);
        test::allocation_succeeds = true;
        assert(f.board.EnterWifiConfigMode());
        assert(test::attempts.size() == 2 && test::pending.size() == 1);
        assert(f.app.resets == 2 && f.board.display.notifications == 2);
        f.NoHandoff();
        test::RunWorker();
        f.Finished();
        assert(test::deleted_tasks == 1 && test::delays.size() == 1);
        assert(test::errors == 1);
    } else {
        assert(false);
    }
}
'''


@unittest.skipUnless(COMPILER, "requires a host C++ compiler")
class WifiConfigHandoffTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="wifi-config-handoff-test-")
        cls.addClassCleanup(cls.tmp.cleanup)
        production = (ROOT / "main/boards/common/wifi_board.cc").read_text(encoding="utf-8")
        method = production_method(production, "bool WifiBoard::EnterWifiConfigMode()")
        cls.binary = str(pathlib.Path(cls.tmp.name) / "wifi-config-handoff.exe")
        subprocess.run(
            [COMPILER, "-std=c++17", "-Wall", "-Wextra", "-Werror",
             "-I", str(ROOT / "main"), "-x", "c++", "-", "-o", cls.binary],
            input=STUBS + method + HARNESS, text=True, check=True, timeout=60,
        )

    def run_case(self, name):
        subprocess.run([self.binary, name], check=True, timeout=10)

    def test_all_real_device_states_report_handoff_acceptance(self):
        self.run_case("state_matrix")

    def test_delayed_task_allocation_failure_rejects_without_worker_or_station_stop(self):
        self.run_case("allocation_failure")

    def test_successful_worker_delays_then_stops_station_and_starts_provisioning(self):
        self.run_case("delayed_worker")

    def test_starting_handoff_is_synchronous_and_needs_no_task(self):
        self.run_case("starting_synchronous")

    def test_retry_after_prior_allocation_failure_can_complete(self):
        self.run_case("retry_after_failure")
