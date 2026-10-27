"""Host regressions for StopWatch's real app state, input dispatch and variants."""
import csv
import json
import pathlib
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
BOARD_DIR = ROOT / "main/boards/m5stack/stopwatch"
COMPILER = shutil.which("c++") or shutil.which("g++")
MIB = 1024 * 1024


def function(source, signature):
    """Extract a production method, including nested callback bodies."""
    start = source.index(signature)
    brace = source.index("{", start)
    depth, end = 1, brace + 1
    while depth:
        depth += (source[end] == "{") - (source[end] == "}")
        end += 1
    return source[start:end]


def compile_cpp(directory, name, source, *flags):
    binary = pathlib.Path(directory) / (name + ".exe")
    subprocess.run(
        [COMPILER, "-std=c++17", "-Wall", "-Wextra", "-I", str(BOARD_DIR),
         "-I", str(ROOT / "main"), *flags, "-x", "c++", "-", "-o", str(binary)],
        input=source, text=True, check=True, timeout=60,
    )
    return str(binary)


@unittest.skipUnless(COMPILER, "requires a host C++ compiler")
class StopwatchAppStateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="stopwatch-app-state-test-")
        cls.addClassCleanup(cls.tmp.cleanup)
        # Include the actual dependency-free header, not a Python/C++ reimplementation.
        cls.binary = compile_cpp(cls.tmp.name, "app-state-test", r'''
#include "stopwatch_app_state.h"
#include <cassert>
#include <climits>
#include <initializer_list>
#include <string>

int main(int argc, char** argv) {
    assert(argc == 2);
    const std::string test = argv[1];
    using Page = StopwatchAppState::Page;
    StopwatchAppState state;
    StopwatchHomeChord chord;
    if (test == "startup") {
        assert(StopwatchAppState::kAppCount == 5);
        assert(state.page() == Page::Home && state.selection() == 0);
    } else if (test == "selection_wrap") {
        const int forward[] = {4, 1, 3, 2, 0, 4, 1, 3, 2, 0};
        const int backward[] = {2, 3, 1, 4, 0, 2, 3, 1, 4, 0};
        for (int expected : forward) {
            state.Step(1);
            assert(state.selection() == expected && state.page() == Page::Home);
        }
        for (int expected : backward) {
            state.Step(-1);
            assert(state.selection() == expected && state.page() == Page::Home);
        }
    } else if (test == "open_selected") {
        const int order[] = {0, 4, 1, 3, 2};
        const Page pages[] = {Page::Voice, Page::Badge, Page::Settings, Page::Weather, Page::Alarm};
        for (int direction : {-1, 1}) {
            state.Open(0);
            state.ShowHome();
            for (int step = 0; step < StopwatchAppState::kAppCount; ++step) {
                const int position = (step * (direction < 0 ? 4 : 1)) % 5;
                const int index = order[position];
                assert(state.selection() == index);
                state.OpenSelected();
                assert(state.page() == pages[index] && state.selection() == index);
                state.ShowHome();
                state.Step(direction);
            }
        }
    } else if (test == "semantic_ids") {
        const Page pages[] = {Page::Voice, Page::Badge, Page::Settings, Page::Weather, Page::Alarm};
        const int next[] = {4, 3, 0, 2, 1}, previous[] = {2, 4, 3, 1, 0};
        for (int index = 0; index < StopwatchAppState::kAppCount; ++index) {
            state.Open(index);
            assert(state.page() == pages[index] && state.selection() == index);
            state.ShowHome();
            state.Step(1);
            assert(state.selection() == next[index]);
            state.Open(index);
            state.ShowHome();
            state.Step(-1);
            assert(state.selection() == previous[index]);
        }
    } else if (test == "invalid_index") {
        const int invalid[] = {-1, 5, 6, INT_MIN, INT_MAX};
        for (int index = -1; index < StopwatchAppState::kAppCount; ++index) {
            if (index >= 0) state.Open(index);
            const Page page = state.page();
            const int selection = state.selection();
            for (int value : invalid) {
                state.Open(value);
                assert(state.page() == page && state.selection() == selection);
            }
        }
    } else if (test == "home_and_voice") {
        for (int index = 0; index < StopwatchAppState::kAppCount; ++index) {
            state.Open(index);
            state.ShowHome();
            state.ShowHome();
            assert(state.page() == Page::Home && state.selection() == index);
            state.OpenSelected();
            state.ShowVoice();
            assert(state.page() == Page::Voice && state.selection() == 0);
            state.ShowHome();
            assert(state.page() == Page::Home && state.selection() == 0);
        }
    } else if (test == "single_hold") {
        for (int button : {0, 1}) {
            assert(!chord.LongPress(button));
            chord.Press(button);
            assert(!chord.LongPress(button));
            assert(!chord.LongPress(button));
            chord.Release(button);
            assert(!chord.LongPress(button));
        }
    } else if (test == "overlap") {
        for (int first : {0, 1}) {
            const int second = 1 - first;
            chord.Press(first);
            assert(!chord.LongPress(first));
            chord.Press(second);  // Staggered presses require the second long event.
            assert(!chord.LongPress(first));
            assert(chord.LongPress(second));
            assert(!chord.LongPress(first) && !chord.LongPress(second));
            chord.Release(first);
            chord.Release(second);
        }
    } else if (test == "released_early") {
        chord.Press(0);
        chord.Press(1);
        assert(!chord.LongPress(0));
        chord.Release(1);  // B never reached its long-press threshold.
        assert(!chord.LongPress(0));
        chord.Press(1);
        assert(chord.LongPress(1));
        chord.Release(0);
        chord.Release(1);
    } else if (test == "nonoverlap") {
        chord.Press(0);
        assert(!chord.LongPress(0));
        chord.Release(0);
        chord.Press(1);
        assert(!chord.LongPress(1));
        chord.Press(0);
        assert(!chord.LongPress(1));  // A's previous long event must not leak.
        assert(chord.LongPress(0));
    } else if (test == "latch") {
        chord.Press(0);
        chord.Press(1);
        assert(!chord.LongPress(0));
        assert(chord.LongPress(1));
        for (int i = 0; i < 3; ++i) {
            assert(!chord.LongPress(0) && !chord.LongPress(1));
        }
        chord.Release(0);
        chord.Press(0);
        assert(!chord.LongPress(0) && !chord.LongPress(1));
        chord.Release(1);
        chord.Press(1);
        assert(!chord.LongPress(0) && !chord.LongPress(1));
        chord.Release(0);
        chord.Release(1);  // Rearm only when both buttons have been released.
        chord.Press(1);
        chord.Press(0);
        assert(!chord.LongPress(1));
        assert(chord.LongPress(0));
    } else {
        assert(false);
    }
}
''')

    def run_case(self, name):
        subprocess.run([self.binary, name], check=True, timeout=10)

    def test_startup_is_home(self):
        self.run_case("startup")

    def test_selection_wraps_home_order_in_both_directions(self):
        self.run_case("selection_wrap")

    def test_open_selected_keeps_semantic_targets_in_both_home_directions(self):
        self.run_case("open_selected")

    def test_direct_open_preserves_semantic_ids_and_navigation_uses_home_order(self):
        self.run_case("semantic_ids")

    def test_invalid_indices_preserve_page_and_selection(self):
        self.run_case("invalid_index")

    def test_home_preserves_selection_and_voice_selects_zero(self):
        self.run_case("home_and_voice")

    def test_single_a_or_b_hold_does_not_trigger_home_chord(self):
        self.run_case("single_hold")

    def test_staggered_overlap_requires_both_current_long_events(self):
        self.run_case("overlap")

    def test_release_before_both_long_thresholds_does_not_trigger(self):
        self.run_case("released_early")

    def test_nonoverlapping_holds_do_not_reuse_old_long_events(self):
        self.run_case("nonoverlap")

    def test_chord_fires_once_and_rearms_only_after_both_release(self):
        self.run_case("latch")


@unittest.skipUnless(COMPILER, "requires a host C++ compiler")
class StopwatchInputTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="stopwatch-app-input-test-")
        cls.addClassCleanup(cls.tmp.cleanup)
        board = (BOARD_DIR / "m5stack_stopwatch.cc").read_text(encoding="utf-8")
        apps = (BOARD_DIR / "stopwatch_apps.cc").read_text(encoding="utf-8")
        # Only hardware, rendering and worker execution are stubbed. Production
        # button registration/dispatch and app methods are compiled verbatim.
        source = r'''
#include "stopwatch_app_state.h"
#include <atomic>
#include <cassert>
#include <functional>
#include <string>
#include <utility>
#include <vector>

struct lv_obj_t { std::string text; };
void lv_label_set_text(lv_obj_t* label, const char* text) { label->text = text; }

class StopwatchSettings {
public:
    int opens = 0, closes = 0, menu_depth = 0;
    std::vector<int> buttons;
    void Open() { ++opens; menu_depth = 0; }
    void Close() { ++closes; menu_depth = 0; }
    void HandleButton(int button) { buttons.push_back(button); }
};

class StopwatchWeather {
public:
    int opens = 0, closes = 0;
    bool active = false;
    std::vector<int> buttons;
    void Open() { ++opens; active = true; }
    void Close() { ++closes; active = false; }
    void HandleButton(int button) { buttons.push_back(button); }
};

class StopwatchAlarm : public StopwatchWeather {};

class StopwatchApps {
public:
    enum class Work { Initialize, Load, Previous, Next, StartEditor, StopEditor };
    StopwatchAppState state_;
    StopwatchSettings settings_;
    StopwatchWeather weather_;
    StopwatchAlarm alarm_;
    std::atomic<bool> editor_requested_{false};
    bool boot_home_pending_ = true, editing_ = false, home_animating_ = false;
    int navigation_target_ = -1;
    uint32_t navigation_generation_ = 1;
    lv_obj_t overlay, editor_url;
    lv_obj_t* overlay_ = &overlay;
    lv_obj_t* editor_url_ = &editor_url;
    std::vector<Work> work;
    std::vector<bool> wake_requests;
    std::function<void(bool)> wake_policy_ = [this](bool enabled) {
        wake_requests.push_back(enabled);
    };
    void SetWakeWordEnabled(bool enabled);
    bool QueueWork(Work value) { work.push_back(value); return true; }
    void RenderPage() {}  // No LVGL widget tree or real worker in host tests.
    // Animation timing is exercised separately against the production helpers.
    void AnimateHome(int direction) { state_.Step(direction); }
    bool HandleButton(int button);
    void ShowHome();
    void OpenSelected();
    void Step(int direction);
    void StartEditor();
    void StopEditor();
};
'''
        for signature in (
            "bool StopwatchApps::HandleButton(int button)",
            "void StopwatchApps::ShowHome()",
            "void StopwatchApps::SetWakeWordEnabled(bool enabled)",
            "void StopwatchApps::OpenSelected()",
            "void StopwatchApps::Step(int direction)",
            "void StopwatchApps::StartEditor()",
            "void StopwatchApps::StopEditor()",
        ):
            source += function(apps, signature) + "\n"
        source += r'''
enum DeviceState { kDeviceStateStarting, kDeviceStateIdle };
struct Application {
    bool on_main = false;
    DeviceState state = kDeviceStateIdle;
    int chat_toggles = 0;
    std::vector<std::function<void()>> pending;
    static Application& GetInstance() { static Application app; return app; }
    void Schedule(std::function<void()> callback) { pending.push_back(callback); }
    DeviceState GetDeviceState() { assert(on_main); return state; }
    void ToggleChatState() { assert(on_main); ++chat_toggles; }
    void Flush() {
        assert(!on_main);
        auto callbacks = std::move(pending);
        pending.clear();
        on_main = true;
        for (auto& callback : callbacks) callback();
        on_main = false;
        assert(pending.empty());
    }
};
struct Button {
    std::function<void()> click, hold, double_click, down, up;
    void OnPressDown(std::function<void()> callback) { assert(!down); down = callback; }
    void OnPressUp(std::function<void()> callback) { assert(!up); up = callback; }
    void OnClick(std::function<void()> callback) { assert(!click); click = callback; }
    void OnLongPress(std::function<void()> callback) { assert(!hold); hold = callback; }
    void OnDoubleClick(std::function<void()> callback) {
        assert(!double_click); double_click = callback;
    }
};
struct WifiManager {
    bool connected = false;
    static WifiManager& GetInstance() { static WifiManager wifi; return wifi; }
    bool IsConnected() { return connected; }
};
struct Codec {
    int volume = 90, writes = 0;
    int output_volume() { assert(Application::GetInstance().on_main); return volume; }
    void SetOutputVolume(int value) {
        assert(Application::GetInstance().on_main); volume = value; ++writes;
    }
};
namespace Lang { namespace Strings { constexpr const char* VOLUME = "Volume"; } }
struct RoundLcdDisplay {
    StopwatchApps apps;
    int home_calls = 0;
    std::vector<int> buttons;
    std::vector<std::string> notifications;
    bool HandleAppButton(int button) {
        assert(Application::GetInstance().on_main);
        buttons.push_back(button);
        return apps.HandleButton(button);
    }
    void ShowHome() {
        assert(Application::GetInstance().on_main); ++home_calls; apps.ShowHome();
    }
    void ShowNotification(const std::string& text) {
        assert(Application::GetInstance().on_main); notifications.push_back(text);
    }
};
class M5StackStopwatchBoard {
public:
    Button button1_, button2_;
    StopwatchHomeChord home_chord_;
    RoundLcdDisplay display;
    RoundLcdDisplay* display_ = &display;
    Codec codec;
    int wifi_config_calls = 0;
    Codec* GetAudioCodec() { return &codec; }
    RoundLcdDisplay* GetDisplay() { return display_; }
    bool EnterWifiConfigMode() {
        assert(Application::GetInstance().on_main); ++wifi_config_calls; return true;
    }
'''
        source += function(board, "void InitializeButtons()")
        source += r'''
};
void Dispatch(const std::function<void()>& callback) {
    auto& app = Application::GetInstance();
    assert(callback && app.pending.empty() && !app.on_main);
    callback();
    assert(app.pending.size() == 1);  // Work must wait for the application task.
    app.Flush();
}
int main(int argc, char** argv) {
    assert(argc == 2);
    const std::string test = argv[1];
    using Page = StopwatchAppState::Page;
    using Work = StopwatchApps::Work;
    M5StackStopwatchBoard board;
    board.InitializeButtons();
    auto& app = Application::GetInstance();
    auto& apps = board.display.apps;
    // Model the first idle transition that makes the local Home page interactive.
    if (test != "boot_passthrough") {
        apps.ShowHome();
        apps.work.clear();
    }
    assert(board.button1_.click && board.button2_.click);
    if (test == "boot_passthrough") {
        assert(apps.boot_home_pending_ && apps.state_.page() == Page::Home);
        for (int button = 0; button < 3; ++button) {
            assert(!apps.HandleButton(button));
        }
        assert(apps.boot_home_pending_ && apps.state_.selection() == 0);
        assert(apps.work.empty() && apps.wake_requests.empty());
        app.state = kDeviceStateStarting;
        board.button1_.click();
        assert(app.pending.size() == 1 && board.wifi_config_calls == 0);
        app.Flush();
        assert(board.wifi_config_calls == 1 && app.chat_toggles == 0);
        assert(apps.boot_home_pending_ && apps.state_.page() == Page::Home);
        apps.ShowHome();
        apps.overlay_ = nullptr;
        for (int button = 0; button < 3; ++button) {
            assert(!apps.HandleButton(button));
        }
        return 0;
    }
    if (test == "voice_fallback") {
#if CONFIG_STOPWATCH_LOCAL_APPS
        apps.state_.ShowVoice();
#else
        // The stock build must not dispatch even if a local overlay were present.
        assert(apps.state_.page() == Page::Home);
        assert(!board.button1_.hold && !board.button2_.hold && !board.button2_.double_click);
        assert(!board.button1_.down && !board.button1_.up);
        assert(!board.button2_.down && !board.button2_.up);
#endif
        board.button1_.click();
        assert(app.chat_toggles == 0 && board.wifi_config_calls == 0);
        assert(board.display.buttons.empty() && app.pending.size() == 1);
        app.Flush();
        assert(app.chat_toggles == 1);
        app.state = kDeviceStateStarting;
        Dispatch(board.button1_.click);
        assert(board.wifi_config_calls == 1 && app.chat_toggles == 1);
        WifiManager::GetInstance().connected = true;
        Dispatch(board.button1_.click);
        assert(board.wifi_config_calls == 1 && app.chat_toggles == 2);
        board.button2_.click();
        assert(board.codec.volume == 90 && board.codec.writes == 0);
        assert(board.display.notifications.empty() && app.pending.size() == 1);
        app.Flush();
        assert(board.codec.volume == 100 && board.codec.writes == 1);
        Dispatch(board.button2_.click);
        assert(board.codec.volume == 0 && board.codec.writes == 2);
        assert((board.display.notifications == std::vector<std::string>{
            "Volume:100%", "Volume:0%"}));
#if CONFIG_STOPWATCH_LOCAL_APPS
        assert((board.display.buttons == std::vector<int>{0, 0, 0, 1, 1}));
#else
        assert(board.display.buttons.empty());
#endif
        return 0;
    }
    assert(CONFIG_STOPWATCH_LOCAL_APPS);
    assert(board.button1_.down && board.button1_.up && board.button1_.hold);
    assert(board.button2_.down && board.button2_.up && board.button2_.hold);
    if (test == "wake_policy") {
        assert((apps.wake_requests == std::vector<bool>{false}));
        apps.HandleButton(1);  // Highlight Alarms without enabling wake or opening it.
        assert(apps.state_.selection() == 4 && apps.wake_requests.back() == false);
        assert(apps.wake_requests.size() == 1);
        apps.HandleButton(2);
        assert(apps.state_.page() == Page::Alarm && apps.alarm_.active);
        assert(apps.wake_requests.back() == false);
        apps.ShowHome();
        const auto after_alarm = apps.wake_requests.size();
        apps.HandleButton(1);  // Badge follows Alarms and retains ID 1.
        assert(apps.state_.selection() == 1 && apps.wake_requests.back() == false);
        assert(apps.wake_requests.size() == after_alarm);
        apps.HandleButton(2);
        assert(apps.state_.page() == Page::Badge && apps.wake_requests.back() == false);
        apps.StartEditor();
        apps.Step(-1);
        apps.Step(1);
        const auto count = apps.wake_requests.size();
        apps.StopEditor();
        assert(apps.wake_requests.size() == count && apps.wake_requests.back() == false);
        apps.ShowHome();
        assert(apps.wake_requests.back() == false);
        const auto before_select = apps.wake_requests.size();
        apps.HandleButton(1);  // Weather follows Badge visually, retaining app ID 3.
        assert(apps.state_.selection() == 3 && apps.wake_requests.size() == before_select);
        apps.OpenSelected();
        assert(apps.state_.page() == Page::Weather && apps.weather_.opens == 1);
        assert(apps.weather_.active && apps.wake_requests.back() == false);
        apps.ShowHome();
        assert(apps.state_.selection() == 3 && !apps.weather_.active);
        apps.HandleButton(1);  // Settings is last visually, retaining app ID 2.
        assert(apps.state_.selection() == 2 && apps.wake_requests.back() == false);
        apps.OpenSelected();
        assert(apps.state_.page() == Page::Settings && apps.settings_.opens == 1);
        assert(apps.wake_requests.back() == false);
        apps.ShowHome();
        assert(apps.state_.selection() == 2 && apps.wake_requests.back() == false);
        apps.HandleButton(1);  // Wrap to Voice, but do not opt in yet.
        assert(apps.state_.selection() == 0 && apps.wake_requests.back() == false);
        const auto before_open = apps.wake_requests.size();
        apps.OpenSelected();
        assert(apps.state_.page() == Page::Voice && apps.wake_requests.back() == true);
        assert(apps.wake_requests.size() == before_open + 1);
        apps.ShowHome();
        assert(apps.wake_requests.back() == false);
        apps.wake_policy_ = {};  // Constructor's optional callback remains safe for hosts.
        apps.OpenSelected();
        apps.ShowHome();
    } else if (test == "home_dispatch") {
        const Page pages[] = {Page::Voice, Page::Badge, Page::Settings, Page::Weather, Page::Alarm};
        const int orders[2][5] = {{2, 3, 1, 4, 0}, {4, 1, 3, 2, 0}};
        std::vector<int> buttons;
        for (int direction : {-1, 1}) {
            for (int expected : orders[direction < 0 ? 0 : 1]) {
                const int button = direction < 0 ? 0 : 1;
                Dispatch(button == 0 ? board.button1_.click : board.button2_.click);
                buttons.push_back(button);
                assert(apps.state_.page() == Page::Home && apps.state_.selection() == expected);
                Dispatch(board.button2_.double_click);
                buttons.push_back(2);
                assert(apps.state_.page() == pages[expected]);
                assert(apps.state_.selection() == expected);
                assert(apps.wake_requests.back() == (expected == 0));
                apps.ShowHome();
                assert(apps.state_.page() == Page::Home && apps.state_.selection() == expected);
                Dispatch(board.button2_.double_click);  // Reopen the same semantic app.
                buttons.push_back(2);
                assert(apps.state_.page() == pages[expected]);
                apps.ShowHome();
                apps.work.clear();
            }
        }
        assert(board.display.buttons == buttons);
        assert(apps.settings_.opens == 4 && apps.weather_.opens == 4 && apps.alarm_.opens == 4);
    } else if (test == "settings_dispatch") {
        Dispatch(board.button1_.click);  // Previous from XiaoZhi wraps to Settings, ID 2.
        Dispatch(board.button2_.double_click);
        assert(apps.state_.page() == Page::Settings && apps.state_.selection() == 2);
        assert(apps.settings_.opens == 1 && apps.settings_.menu_depth == 0);
        assert(!apps.wake_requests.back());
        const auto wake_requests = apps.wake_requests;
        apps.settings_.menu_depth = 3;  // A nested Settings subpage owns its navigation.
        Dispatch(board.button1_.click);
        Dispatch(board.button2_.click);
        Dispatch(board.button2_.double_click);
        assert((apps.settings_.buttons == std::vector<int>{0, 1, 2}));
        assert(apps.state_.page() == Page::Settings && apps.state_.selection() == 2);
        assert(apps.settings_.opens == 1 && apps.settings_.menu_depth == 3);
        assert(apps.work.empty() && !apps.editing_ && !apps.editor_requested_.load());
        assert(apps.wake_requests == wake_requests);
        const int closes = apps.settings_.closes;
        apps.ShowHome();
        assert(apps.settings_.closes == closes + 1 && apps.settings_.menu_depth == 0);
        assert(apps.state_.page() == Page::Home && apps.state_.selection() == 2);
        assert(!apps.wake_requests.back());
        Dispatch(board.button2_.double_click);
        assert(apps.state_.page() == Page::Settings && apps.settings_.opens == 2);
        assert(apps.settings_.menu_depth == 0 && !apps.wake_requests.back());
        assert((board.display.buttons == std::vector<int>{0, 2, 0, 1, 2, 2}));
    } else if (test == "weather_dispatch") {
        Dispatch(board.button1_.click);  // Settings is last in Home order.
        Dispatch(board.button1_.click);  // Weather precedes Settings but keeps app ID 3.
        Dispatch(board.button2_.double_click);
        assert(apps.state_.page() == Page::Weather && apps.state_.selection() == 3);
        assert(apps.weather_.opens == 1 && apps.weather_.active);
        assert(apps.settings_.opens == 0 && !apps.wake_requests.back());
        const auto wake_requests = apps.wake_requests;
        const int settings_closes = apps.settings_.closes;
        Dispatch(board.button1_.click);
        Dispatch(board.button2_.click);
        Dispatch(board.button2_.double_click);
        assert((apps.weather_.buttons == std::vector<int>{0, 1, 2}));
        assert(apps.state_.page() == Page::Weather && apps.state_.selection() == 3);
        assert(apps.weather_.opens == 1 && apps.weather_.active);
        apps.Step(-1);
        apps.Step(1);
        apps.StartEditor();
        assert(apps.work.empty() && !apps.editing_ && !apps.editor_requested_.load());
        assert(apps.settings_.buttons.empty() && apps.settings_.closes == settings_closes);
        assert(apps.wake_requests == wake_requests);
        const int closes = apps.weather_.closes;
        apps.ShowHome();
        assert(apps.weather_.closes == closes + 1 && !apps.weather_.active);
        assert(apps.state_.page() == Page::Home && apps.state_.selection() == 3);
        assert(!apps.wake_requests.back());
        Dispatch(board.button2_.double_click);
        assert(apps.state_.page() == Page::Weather && apps.weather_.opens == 2);
        assert(apps.weather_.active && !apps.wake_requests.back());
        assert((board.display.buttons == std::vector<int>{0, 0, 2, 0, 1, 2, 2}));
    } else if (test == "alarm_dispatch") {
        Dispatch(board.button2_.click);  // Alarms follows XiaoZhi, stable ID 4.
        Dispatch(board.button2_.double_click);
        assert(apps.state_.page() == Page::Alarm && apps.state_.selection() == 4);
        assert(apps.alarm_.opens == 1 && apps.alarm_.active && !apps.wake_requests.back());
        const auto wake_requests = apps.wake_requests;
        Dispatch(board.button1_.click);
        Dispatch(board.button2_.click);
        Dispatch(board.button2_.double_click);
        assert((apps.alarm_.buttons == std::vector<int>{0, 1, 2}));
        apps.Step(-1); apps.Step(1); apps.StartEditor();
        assert(apps.work.empty() && !apps.editing_ && !apps.editor_requested_.load());
        assert(apps.settings_.buttons.empty() && apps.weather_.buttons.empty());
        assert(apps.wake_requests == wake_requests);
        const auto closes = apps.alarm_.closes;
        apps.ShowHome();
        assert(apps.alarm_.closes == closes + 1 && !apps.alarm_.active);
        assert(apps.state_.page() == Page::Home && apps.state_.selection() == 4);
        Dispatch(board.button2_.double_click);
        assert(apps.alarm_.opens == 2 && apps.alarm_.active && !apps.wake_requests.back());
        assert((board.display.buttons == std::vector<int>{1, 2, 0, 1, 2, 2}));
    } else if (test == "badge_dispatch") {
        apps.state_.Open(1);
        Dispatch(board.button1_.click);
        Dispatch(board.button2_.click);
        assert((apps.work == std::vector<Work>{Work::Previous, Work::Next}));
        Dispatch(board.button2_.double_click);
        assert(apps.editing_ && apps.editor_requested_.load());
        assert(apps.work.back() == Work::StartEditor);
        Dispatch(board.button1_.click);
        assert(!apps.editing_ && !apps.editor_requested_.load());
        assert(apps.work.back() == Work::StopEditor);
    } else if (test == "single_hold_callbacks") {
        for (int scenario = 0; scenario < 6; ++scenario) {
            const int index = scenario >= 3 ? scenario - 1 : (scenario == 0 ? 0 : 1);
            for (Button* button : {&board.button1_, &board.button2_}) {
                apps.state_.Open(index);
                apps.editing_ = scenario == 2;
                apps.editor_requested_.store(apps.editing_);
                const Page page = apps.state_.page();
                const bool editing = apps.editing_;
                const auto work_count = apps.work.size();
                const auto settings_buttons = apps.settings_.buttons;
                const int settings_closes = apps.settings_.closes;
                const auto weather_buttons = apps.weather_.buttons;
                const int weather_closes = apps.weather_.closes;
                const auto alarm_buttons = apps.alarm_.buttons;
                const int alarm_closes = apps.alarm_.closes;
                button->down();
                button->hold();
                button->hold();
                button->up();
                assert(app.pending.empty() && board.display.home_calls == 0);
                assert(apps.state_.page() == page && apps.state_.selection() == index);
                assert(apps.editing_ == editing && apps.editor_requested_.load() == editing);
                assert(apps.work.size() == work_count);
                assert(apps.settings_.buttons == settings_buttons);
                assert(apps.settings_.closes == settings_closes);
                assert(apps.weather_.buttons == weather_buttons);
                assert(apps.weather_.closes == weather_closes);
                assert(apps.alarm_.buttons == alarm_buttons);
                assert(apps.alarm_.closes == alarm_closes);
            }
        }
    } else if (test == "chord_dispatch") {
        // All five apps and Badge's editor return through the same scheduled chord.
        for (int scenario = 0; scenario < 6; ++scenario) {
            const int selection = scenario >= 3 ? scenario - 1 : (scenario == 0 ? 0 : 1);
            apps.state_.Open(selection);
            apps.editing_ = scenario == 2;
            apps.editor_requested_.store(apps.editing_);
            apps.settings_.menu_depth = scenario == 3 ? 2 : 0;
            const int settings_closes = apps.settings_.closes;
            if (selection == 3) apps.weather_.Open();
            const int weather_closes = apps.weather_.closes;
            if (selection == 4) apps.alarm_.Open();
            const int alarm_closes = apps.alarm_.closes;
            const Page page = apps.state_.page();
            const bool editing = apps.editing_;
            board.button1_.down();
            board.button1_.hold();  // A alone: no Home, no main-task work.
            assert(app.pending.empty());
            board.button2_.down();
            board.button1_.hold();  // B has not reached its own threshold.
            assert(app.pending.empty() && apps.state_.page() == page);
            board.button2_.hold();
            assert(app.pending.size() == 1 && board.display.home_calls == scenario);
            assert(apps.state_.page() == page && apps.editing_ == editing);
            board.button1_.hold();
            board.button2_.hold();
            assert(app.pending.size() == 1);  // One Home while both remain down.
            app.Flush();
            assert(board.display.home_calls == scenario + 1);
            assert(apps.state_.page() == Page::Home && apps.state_.selection() == selection);
            assert(!apps.boot_home_pending_ && !apps.editing_ && !apps.editor_requested_.load());
            assert(apps.work.back() == Work::StopEditor);
            assert(apps.settings_.closes == settings_closes + 1 && apps.settings_.menu_depth == 0);
            assert(apps.weather_.closes == weather_closes + 1 && !apps.weather_.active);
            assert(apps.alarm_.closes == alarm_closes + 1 && !apps.alarm_.active);
            assert(!apps.wake_requests.back());
            board.button1_.up();
            board.button2_.up();
            assert(app.pending.empty() && board.display.home_calls == scenario + 1);
        }
        assert(board.display.buttons.empty());
    } else if (test == "chord_latch_callbacks") {
        board.button1_.down();
        board.button2_.down();
        board.button1_.hold();
        assert(app.pending.empty());
        Dispatch(board.button2_.hold);
        assert(board.display.home_calls == 1);
        board.button1_.up();
        board.button1_.down();
        board.button1_.hold();
        board.button2_.hold();
        assert(app.pending.empty() && board.display.home_calls == 1);
        board.button2_.up();
        board.button1_.up();
        assert(app.pending.empty());
        board.button2_.down();
        board.button1_.down();
        board.button2_.hold();
        assert(app.pending.empty());
        Dispatch(board.button1_.hold);
        assert(board.display.home_calls == 2);
        board.button1_.up();
        board.button2_.up();
        assert(app.pending.empty());
    } else if (test == "chord_early_release_callbacks") {
        apps.state_.Open(1);
        board.button1_.down();
        board.button2_.down();
        board.button1_.hold();
        board.button1_.up();
        board.button2_.hold();
        board.button2_.up();
        assert(app.pending.empty() && board.display.home_calls == 0);
        // Non-overlapping completed holds also cannot form a chord.
        board.button1_.down();
        board.button1_.hold();
        board.button1_.up();
        board.button2_.down();
        board.button2_.hold();
        board.button2_.up();
        assert(app.pending.empty() && apps.state_.page() == Page::Badge);
    } else {
        assert(false);
    }
    assert(app.chat_toggles == 0 && board.wifi_config_calls == 0);
    assert(board.codec.writes == 0 && board.display.notifications.empty());
    if (test != "home_dispatch" && test != "badge_dispatch" && test != "settings_dispatch" &&
        test != "weather_dispatch" && test != "alarm_dispatch") {
        assert(board.display.buttons.empty());  // No synthetic click on release.
    }
}
'''
        cls.binaries = {
            enabled: compile_cpp(cls.tmp.name, f"input-test-{enabled}", source,
                                 f"-DCONFIG_STOPWATCH_LOCAL_APPS={enabled}")
            for enabled in (0, 1)
        }

    def run_case(self, name, enabled=1):
        subprocess.run([self.binaries[enabled], name], check=True, timeout=10)

    def test_boot_pending_and_missing_overlay_preserve_startup_wifi_controls(self):
        self.run_case("boot_passthrough")

    def test_only_explicit_voice_open_enables_wake_all_local_apps_and_editor_do_not(self):
        self.run_case("wake_policy")

    def test_home_buttons_select_and_double_b_opens_without_voice_side_effects(self):
        self.run_case("home_dispatch")

    def test_settings_buttons_are_consumed_without_chat_volume_or_badge_actions(self):
        self.run_case("settings_dispatch")

    def test_weather_buttons_are_consumed_without_chat_volume_wake_or_badge_actions(self):
        self.run_case("weather_dispatch")

    def test_alarm_buttons_are_consumed_without_chat_volume_wake_or_badge_actions(self):
        self.run_case("alarm_dispatch")

    def test_badge_buttons_dispatch_worker_requests_and_editor(self):
        self.run_case("badge_dispatch")

    def test_single_a_or_b_hold_has_no_home_chat_or_volume_action(self):
        self.run_case("single_hold_callbacks")

    def test_all_five_apps_and_badge_editor_return_home_only_after_both_long_events(self):
        self.run_case("chord_dispatch")

    def test_chord_callbacks_latch_until_both_release_then_rearm(self):
        self.run_case("chord_latch_callbacks")

    def test_early_release_and_nonoverlapping_callbacks_do_not_return_home(self):
        self.run_case("chord_early_release_callbacks")

    def test_voice_page_preserves_scheduled_chat_wifi_and_volume_controls(self):
        self.run_case("voice_fallback")

    def test_stock_build_preserves_controls_without_local_app_callbacks(self):
        self.run_case("voice_fallback", enabled=0)


@unittest.skipUnless(COMPILER, "requires a host C++ compiler")
class StopwatchWorkerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="stopwatch-app-worker-test-")
        cls.addClassCleanup(cls.tmp.cleanup)
        apps = (BOARD_DIR / "stopwatch_apps.cc").read_text(encoding="utf-8")
        source = r'''
#include <atomic>
#include <cassert>
#include <functional>
#include <string>
#include <utility>
#include <vector>

constexpr int portMAX_DELAY = -1, pdTRUE = 1;
struct Queue { std::vector<int> work; unsigned next = 0; };
template <typename Work>
int xQueueReceive(Queue* queue, Work* work, int timeout) {
    assert(timeout == portMAX_DELAY);
    if (queue->next == queue->work.size()) return 0;
    *work = static_cast<Work>(queue->work[queue->next++]);
    return pdTRUE;
}
int tasks_deleted = 0;
void vTaskDelete(void* task) { assert(task == nullptr); ++tasks_deleted; }
struct Display {};
struct DisplayLockGuard {
    explicit DisplayLockGuard(Display*) {}
    explicit operator bool() const { return true; }
};
struct lv_obj_t { std::string text; };
void lv_label_set_text(lv_obj_t* object, const char* text) { object->text = text; }
struct Store { void Initialize() {} void StepSlot(int) {} };
struct Editor {
    bool running = true;
    int generation = 1, starts = 0, stops = 0;
    std::string url = "http://watch/edit?token=1";
    std::vector<std::string> calls;
    std::function<void()> after_stop;
    void Stop() {
        ++stops;
        calls.push_back("stop");
        running = false;
        if (after_stop) after_stop();
    }
    std::string Start() {
        ++starts;
        calls.push_back("start");
        // Like the real editor, an already-running server reuses its URL.
        if (!running) {
            running = true;
            url = "http://watch/edit?token=" + std::to_string(++generation);
        }
        return url;
    }
};
class StopwatchApps {
public:
    enum class Work { Initialize, Load, Previous, Next, StartEditor, StopEditor };
    Queue queue;
    Queue* work_queue_ = &queue;
    Store store_;
    Editor editor_;
    Display display;
    Display* display_ = &display;
    std::atomic<bool> editor_requested_{true};
    bool editing_ = true;
    lv_obj_t editor_url;
    lv_obj_t* editor_url_ = &editor_url;
    void RefreshBadge() {}
    void Worker();
    void Enqueue(Work value) { queue.work.push_back(static_cast<int>(value)); }
};
'''
        source += function(apps, "void StopwatchApps::Worker()") + "\n"
        source += r'''
int main(int argc, char** argv) {
    assert(argc == 2);
    const std::string test = argv[1];
    StopwatchApps apps;
    const std::string old_url = apps.editor_.url;
    if (test == "reopen") {
        // The UI closes and reopens before the worker receives the queued stop.
        apps.Enqueue(StopwatchApps::Work::StopEditor);
        apps.Enqueue(StopwatchApps::Work::StartEditor);
        apps.Worker();
        assert((apps.editor_.calls == std::vector<std::string>{"stop", "start"}));
        assert(apps.editor_.stops == 1 && apps.editor_.starts == 1);
        assert(apps.editor_.running && apps.editor_.url != old_url);
        assert(apps.editor_url.text == "http://watch/edit?token=2");
    } else if (test == "cancel_before_start") {
        apps.editor_requested_.store(false);
        apps.editing_ = false;
        apps.Enqueue(StopwatchApps::Work::StartEditor);
        apps.Worker();
        assert(apps.editor_.starts == 0 && !apps.editor_.running);
        assert(apps.editor_url.text.empty());
    } else if (test == "cancel_during_stop") {
        apps.editor_.after_stop = [&]() {
            apps.editor_requested_.store(false);
            apps.editing_ = false;
        };
        apps.Enqueue(StopwatchApps::Work::StartEditor);
        apps.Worker();
        assert(apps.editor_.stops == 1 && apps.editor_.starts == 0);
        assert(!apps.editor_.running && apps.editor_url.text.empty());
    } else {
        assert(false);
    }
    assert(tasks_deleted == 1);
}
'''
        cls.binary = compile_cpp(cls.tmp.name, "worker-test", source)

    def test_editor_reopen_rotates_url_when_queued_stop_is_superseded(self):
        subprocess.run([self.binary, "reopen"], check=True, timeout=10)

    def test_cancelled_editor_start_does_not_start_server(self):
        subprocess.run([self.binary, "cancel_before_start"], check=True, timeout=10)

    def test_editor_start_rechecks_cancellation_after_stopping_old_server(self):
        subprocess.run([self.binary, "cancel_during_stop"], check=True, timeout=10)


@unittest.skipUnless(COMPILER, "requires a host C++ compiler")
class StopwatchStartupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="stopwatch-app-startup-test-")
        cls.addClassCleanup(cls.tmp.cleanup)
        apps = (BOARD_DIR / "stopwatch_apps.cc").read_text(encoding="utf-8")
        header = (BOARD_DIR / "stopwatch_apps.h").read_text(encoding="utf-8")
        board = (BOARD_DIR / "m5stack_stopwatch.cc").read_text(encoding="utf-8")
        source = r'''
#include "stopwatch_app_state.h"
#include "device_state.h"
#include <algorithm>
#include <atomic>
#include <cassert>
#include <cstring>
#include <functional>
#include <string>
#include <vector>

struct lv_obj_t { bool hidden = true; };
constexpr int LV_OBJ_FLAG_HIDDEN = 1;
void lv_obj_add_flag(lv_obj_t* object, int flag) {
    assert(flag == LV_OBJ_FLAG_HIDDEN); object->hidden = true;
}
class StopwatchSettings {
public:
    int closes = 0, menu_depth = 0;
    std::vector<DeviceState> voice_states;
    void Close() { ++closes; menu_depth = 0; }
    void ObserveVoiceState(DeviceState state) { voice_states.push_back(state); }
};
class StopwatchWeather {
public:
    int closes = 0;
    bool active = false;
    void Open() { active = true; }
    void Close() { ++closes; active = false; }
};
class StopwatchAlarm : public StopwatchWeather {};
class StopwatchApps {
public:
    enum class Work { StopEditor };
    StopwatchAppState state_;
    StopwatchSettings settings_;
    StopwatchWeather weather_;
    StopwatchAlarm alarm_;
    std::atomic<bool> editor_requested_{false};
    bool editing_ = false;
    int navigation_target_ = -1;
    uint32_t navigation_generation_ = 1;
    lv_obj_t overlay;
    lv_obj_t* overlay_ = &overlay;
    std::vector<Work> work;
    std::vector<bool> wake_requests;
    std::function<void(bool)> wake_policy_ = [this](bool enabled) {
        wake_requests.push_back(enabled);
    };
    void SetWakeWordEnabled(bool enabled);
    int renders = 0;
    bool QueueWork(Work value) { work.push_back(value); return true; }
    void RenderPage() {
        ++renders;
        overlay.hidden = state_.page() == StopwatchAppState::Page::Voice;
    }
    void ShowHome();
'''
        # Use the production declaration/default and initial dedup/pending state.
        for token in ("void ObserveVoiceState(", "DeviceState last_voice_state_", "bool boot_home_pending_"):
            source += next(line.strip() for line in header.splitlines() if line.strip().startswith(token)) + "\n"
        source += "};\n"
        source += function(apps, "void StopwatchApps::ShowHome()") + "\n"
        source += function(apps, "void StopwatchApps::SetWakeWordEnabled(bool enabled)") + "\n"
        source += function(apps, "void StopwatchApps::ObserveVoiceState(") + "\n"
        source += r'''
struct Application {
    DeviceState state = kDeviceStateStarting;
    static Application& GetInstance() { static Application app; return app; }
    DeviceState GetDeviceState() { return state; }
};
struct SpiLcdDisplay {
    int cleared = 0, polls = 0;
    virtual void SetStatus(const char*) {}
    virtual bool SetEmotion(const char*) { return true; }
    virtual void UpdateStatusBar(bool = false) { ++polls; }
    void ClearChatMessages() { ++cleared; }
};
struct DisplayLockGuard {
    explicit DisplayLockGuard(SpiLcdDisplay*) {}
    explicit operator bool() const { return true; }
};
struct Board {
    static Board& GetInstance() { static Board board; return board; }
    bool GetBatteryLevel(int&, bool&, bool&) { return false; }
};
void lv_label_set_text(lv_obj_t*, const char*) {}
void lv_label_set_text_fmt(lv_obj_t*, const char*, int) {}
struct RoundLcdDisplay : SpiLcdDisplay {
    StopwatchApps apps;
    StopwatchApps* apps_ = &apps;
    lv_obj_t alarm_overlay;
    lv_obj_t* alarm_overlay_ = &alarm_overlay;
    lv_obj_t* battery_percentage_label_ = nullptr;
    int battery_level_ = -1;
    void ApplyBatteryColor() {}
'''
        for signature in (
            "void ObserveVoiceState(bool allow_boot_home",
            "void SetStatus(const char* status)",
            "bool SetEmotion(const char* emotion)",
            "void UpdateStatusBar(bool update_all",
        ):
            source += function(board, signature) + "\n"
        source += r'''
};
int main(int argc, char** argv) {
    assert(argc == 2);
    const std::string test = argv[1];
    using Page = StopwatchAppState::Page;
    RoundLcdDisplay display;
    auto& app = Application::GetInstance();
    auto& apps = display.apps;
    if (test == "normal_idle") {
        display.SetStatus("Starting");
        app.state = kDeviceStateIdle;
        display.SetStatus("Standby");  // Idle has already been observed here.
        assert(apps.last_voice_state_ == kDeviceStateIdle);
        assert(apps.boot_home_pending_ && apps.overlay.hidden && apps.renders == 0);
        assert(apps.wake_requests.empty());
        display.ClearChatMessages();
        display.SetEmotion("neutral");
        assert(display.cleared == 1 && !apps.boot_home_pending_);
        assert(apps.state_.page() == Page::Home && !apps.overlay.hidden && apps.renders == 1);
        assert((apps.wake_requests == std::vector<bool>{false}));
    } else if (test == "error_idle") {
        app.state = kDeviceStateIdle;
        display.SetStatus("Unable to connect");
        display.SetEmotion("cancel");
        display.SetEmotion(nullptr);
        display.SetEmotion("");
        for (int i = 0; i < 3; ++i) display.UpdateStatusBar(i == 0);
        assert(display.polls == 3 && apps.last_voice_state_ == kDeviceStateIdle);
        assert(apps.boot_home_pending_ && apps.overlay.hidden && apps.renders == 0);
        display.SetStatus("Standby");
        display.ClearChatMessages();
        display.SetEmotion("neutral");
        assert(!apps.boot_home_pending_ && !apps.overlay.hidden && apps.state_.page() == Page::Home);
    } else if (test == "idle_poll") {
        app.state = kDeviceStateIdle;
        for (int i = 0; i < 3; ++i) display.UpdateStatusBar();
        display.SetStatus("Idle");
        assert(apps.boot_home_pending_ && apps.overlay.hidden && apps.renders == 0);
        display.SetEmotion("neutral");
        assert(!apps.boot_home_pending_ && apps.renders == 1);
        display.UpdateStatusBar();
        display.SetEmotion("neutral");
        assert(apps.renders == 1);  // The boot transition is consumed only once.
    } else if (test == "nonidle_neutral") {
        const DeviceState states[] = {
            kDeviceStateStarting, kDeviceStateActivating, kDeviceStateConnecting,
            kDeviceStateListening, kDeviceStateSpeaking, kDeviceStateNotifying,
            kDeviceStateWifiConfiguring, kDeviceStateUpgrading, kDeviceStateFatalError,
        };
        for (DeviceState state : states) {
            app.state = state;
            display.SetEmotion("neutral");
            assert(apps.boot_home_pending_ && apps.overlay.hidden);
        }
        app.state = kDeviceStateIdle;
        display.SetStatus("Idle");
        assert(apps.boot_home_pending_ && apps.overlay.hidden);
        display.SetEmotion("neutral");
        assert(!apps.boot_home_pending_ && !apps.overlay.hidden && apps.state_.page() == Page::Home);
    } else if (test == "explicit_home") {
        app.state = kDeviceStateIdle;
        display.SetEmotion("neutral");
        apps.state_.Open(0);  // The user explicitly opens XiaoZhi first.
        apps.RenderPage();
        app.state = kDeviceStateListening;
        display.SetStatus("Listening");
        assert(apps.state_.page() == Page::Voice && apps.overlay.hidden);
        apps.ShowHome();  // Explicit user navigation while voice state is active.
        const int renders = apps.renders;
        const auto work = apps.work;
        display.SetStatus("Listening");
        display.SetEmotion("neutral");
        display.UpdateStatusBar();
        assert(apps.state_.page() == Page::Home && !apps.overlay.hidden && apps.renders == renders);
        app.state = kDeviceStateSpeaking;
        display.SetStatus("Speaking");
        assert(apps.state_.page() == Page::Home && !apps.overlay.hidden && apps.renders == renders);
        assert(apps.work == work && !apps.editing_ && !apps.editor_requested_.load());
    } else if (test == "local_pages") {
        app.state = kDeviceStateIdle;
        display.SetEmotion("neutral");
        const DeviceState states[] = {
            kDeviceStateConnecting, kDeviceStateListening, kDeviceStateSpeaking,
            kDeviceStateNotifying, kDeviceStateIdle,
        };
        for (int scenario = 0; scenario < 9; ++scenario) {
            const int selection = scenario >= 7 ? 4 : (scenario >= 5 ? 3 : (scenario >= 3 ? 2 : 1));
            apps.state_.Open(selection);
            apps.ShowHome();  // Home preserves each local app's selection.
            if (scenario != 0 && scenario != 3 && scenario != 5 && scenario != 7) apps.state_.Open(selection);
            if (scenario == 6) apps.weather_.Open();
            if (scenario == 8) apps.alarm_.Open();
            apps.settings_.menu_depth = scenario == 4 ? 2 : 0;
            apps.editing_ = scenario == 2;
            apps.editor_requested_.store(apps.editing_);
            apps.RenderPage();
            const Page page = apps.state_.page();
            const bool editing = apps.editing_;
            const int renders = apps.renders;
            const auto work = apps.work;
            const auto wake_requests = apps.wake_requests;
            const int settings_closes = apps.settings_.closes;
            const int settings_depth = apps.settings_.menu_depth;
            const int weather_closes = apps.weather_.closes;
            const bool weather_active = apps.weather_.active;
            const int alarm_closes = apps.alarm_.closes;
            const bool alarm_active = apps.alarm_.active;
            for (DeviceState state : states) {
                app.state = state;
                display.SetStatus("Background voice event");
                display.SetEmotion("neutral");
                display.UpdateStatusBar();
                assert(apps.last_voice_state_ == state);
                assert(!apps.boot_home_pending_ && !apps.overlay.hidden);
                assert(apps.state_.page() == page && apps.state_.selection() == selection);
                assert(apps.editing_ == editing && apps.editor_requested_.load() == editing);
                assert(apps.renders == renders && apps.work == work);
                assert(apps.wake_requests == wake_requests && !apps.wake_requests.back());
                assert(apps.settings_.closes == settings_closes);
                assert(apps.settings_.menu_depth == settings_depth);
                assert(apps.weather_.closes == weather_closes && apps.weather_.active == weather_active);
                assert(apps.alarm_.closes == alarm_closes && apps.alarm_.active == alarm_active);
            }
        }
    } else if (test == "system_states") {
        app.state = kDeviceStateIdle;
        display.SetEmotion("neutral");
        const DeviceState states[] = {
            kDeviceStateWifiConfiguring, kDeviceStateUpgrading, kDeviceStateFatalError,
        };
        for (int scenario = 0; scenario < 9; ++scenario) {
            const int selection = scenario >= 7 ? 4 : (scenario >= 5 ? 3 : (scenario >= 3 ? 2 : 1));
            for (DeviceState state : states) {
                apps.state_.Open(selection);
                apps.ShowHome();
                if (scenario != 0 && scenario != 3 && scenario != 5 && scenario != 7) apps.state_.Open(selection);
                if (scenario == 6) apps.weather_.Open();
                if (scenario == 8) apps.alarm_.Open();
                apps.settings_.menu_depth = scenario == 4 ? 3 : 0;
                apps.editing_ = scenario == 2;
                apps.editor_requested_.store(apps.editing_);
                apps.RenderPage();
                const int renders = apps.renders;
                const auto count = apps.work.size();
                const auto wake_requests = apps.wake_requests;
                const int settings_closes = apps.settings_.closes;
                const int weather_closes = apps.weather_.closes;
                const int alarm_closes = apps.alarm_.closes;
                display.alarm_overlay.hidden = false;
                app.state = state;
                display.SetStatus("System state");
                assert(apps.last_voice_state_ == state && !apps.boot_home_pending_);
                assert(apps.state_.page() == Page::Voice && apps.state_.selection() == 0);
                assert(apps.overlay.hidden && !apps.editing_ && !apps.editor_requested_.load());
                assert(apps.renders == renders + 1 && apps.work.size() == count + 1);
                assert(apps.work.back() == StopwatchApps::Work::StopEditor);
                assert(apps.wake_requests == wake_requests && !apps.wake_requests.back());
                assert(apps.settings_.closes == settings_closes + 1 && apps.settings_.menu_depth == 0);
                assert(apps.weather_.closes == weather_closes + 1 && !apps.weather_.active);
                assert(apps.alarm_.closes == alarm_closes + 1 && !apps.alarm_.active);
                assert(display.alarm_overlay.hidden);
                display.SetEmotion("neutral");
                display.UpdateStatusBar();
                assert(apps.renders == renders + 1 && apps.work.size() == count + 1);
                assert(apps.settings_.closes == settings_closes + 1);
                assert(apps.weather_.closes == weather_closes + 1 && !apps.weather_.active);
                assert(apps.alarm_.closes == alarm_closes + 1 && !apps.alarm_.active);
                assert(display.alarm_overlay.hidden);
            }
        }
    } else if (test == "settings_voice_state_forwarding") {
        apps.ShowHome();
        const DeviceState states[] = {
            kDeviceStateStarting, kDeviceStateWifiConfiguring, kDeviceStateIdle,
            kDeviceStateConnecting, kDeviceStateListening, kDeviceStateSpeaking,
            kDeviceStateNotifying, kDeviceStateUpgrading, kDeviceStateActivating,
            kDeviceStateAudioTesting, kDeviceStateFatalError, kDeviceStateUnknown,
        };
        std::vector<DeviceState> expected;
        // Forward transitions even with Settings inactive: Home, Voice, Badge,
        // Badge editor, Settings, Weather and Alarms observe the same deduplicated stream.
        for (int scenario = 0; scenario < 7; ++scenario) {
            for (DeviceState state : states) {
                apps.state_.Open(scenario >= 4 ? scenario - 2 : (scenario >= 2 ? 1 : 0));
                if (scenario == 0) apps.state_.ShowHome();
                apps.editing_ = scenario == 3;
                apps.editor_requested_.store(apps.editing_);
                app.state = state;
                display.SetStatus("Changed device state");
                expected.push_back(state);
                assert(apps.last_voice_state_ == state);
                assert(apps.settings_.voice_states == expected);
                display.SetStatus("Unchanged device state");
                display.SetEmotion("neutral");
                display.UpdateStatusBar();
                assert(apps.settings_.voice_states == expected);
            }
        }
    } else if (test == "missing_overlay") {
        apps.overlay_ = nullptr;
        for (DeviceState state : {kDeviceStateIdle, kDeviceStateListening,
                                  kDeviceStateWifiConfiguring, kDeviceStateFatalError}) {
            app.state = state;
            display.SetStatus("No local overlay");
            display.SetEmotion("neutral");
            display.UpdateStatusBar();
            assert(apps.boot_home_pending_ && apps.last_voice_state_ == kDeviceStateUnknown);
            assert(apps.state_.page() == Page::Home && apps.renders == 0 && apps.work.empty());
            assert(apps.settings_.voice_states.empty());
        }
    } else {
        assert(false);
    }
}
'''
        cls.binary = compile_cpp(cls.tmp.name, "startup-test", source)

    def run_case(self, name):
        subprocess.run([self.binary, name], check=True, timeout=10)

    def test_normal_idle_sequence_shows_home_after_idle_already_observed(self):
        self.run_case("normal_idle")

    def test_idle_error_cancel_emotion_does_not_consume_boot_home(self):
        self.run_case("error_idle")

    def test_idle_status_and_polling_wait_for_neutral_emotion(self):
        self.run_case("idle_poll")

    def test_neutral_emotion_outside_idle_does_not_enable_home(self):
        self.run_case("nonidle_neutral")

    def test_explicit_home_survives_unchanged_and_changed_voice_states(self):
        self.run_case("explicit_home")

    def test_home_badge_settings_weather_alarm_and_editor_remain_local_for_normal_voice_states(self):
        self.run_case("local_pages")

    def test_wifi_upgrade_and_fatal_states_override_local_pages_once_and_close_weather_and_alarm(self):
        self.run_case("system_states")

    def test_changed_voice_states_are_forwarded_once_to_settings_on_every_page(self):
        self.run_case("settings_voice_state_forwarding")

    def test_missing_overlay_keeps_boot_pending_and_voice_state_unobserved(self):
        self.run_case("missing_overlay")


@unittest.skipUnless(COMPILER, "requires a host C++ compiler")
class StopwatchGestureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="stopwatch-app-gesture-test-")
        cls.addClassCleanup(cls.tmp.cleanup)
        apps = (BOARD_DIR / "stopwatch_apps.cc").read_text(encoding="utf-8")
        source = r'''
#include "stopwatch_app_state.h"
#include <cassert>
#include <memory>
#include <string>
#include <vector>

constexpr unsigned LV_OBJ_FLAG_SCROLLABLE = 1, LV_OBJ_FLAG_GESTURE_BUBBLE = 2,
                   LV_OBJ_FLAG_CLICKABLE = 4;
constexpr int LV_ALIGN_TOP_MID = 1, LV_TEXT_ALIGN_CENTER = 2,
              LV_EVENT_CLICKED = 3, LV_EVENT_GESTURE = 4,
              LV_DIR_LEFT = 5, LV_DIR_RIGHT = 6, LV_DIR_TOP = 7;
int font_noto_sans_basic_20_4;
struct lv_obj_t;
struct lv_event_t { lv_obj_t* target; void* user_data; };
using lv_event_cb_t = void (*)(lv_event_t*);
struct Event { lv_event_cb_t callback; int code; void* user_data; };
struct lv_obj_t {
    lv_obj_t* parent;
    unsigned flags;
    std::vector<lv_obj_t*> children;
    std::vector<Event> events;
};
std::vector<std::unique_ptr<lv_obj_t>> objects;
lv_obj_t* lv_obj_create(lv_obj_t* parent) {
    // LVGL lv_obj_constructor: child objects bubble gestures by default.
    unsigned flags = LV_OBJ_FLAG_SCROLLABLE | LV_OBJ_FLAG_CLICKABLE;
    if (parent) flags |= LV_OBJ_FLAG_GESTURE_BUBBLE;
    objects.emplace_back(new lv_obj_t{parent, flags, {}, {}});
    auto* object = objects.back().get();
    if (parent) parent->children.push_back(object);
    return object;
}
lv_obj_t* lv_button_create(lv_obj_t* parent) { return lv_obj_create(parent); }
lv_obj_t* lv_label_create(lv_obj_t* parent) { return lv_obj_create(parent); }
void lv_obj_remove_style_all(lv_obj_t*) {}
void lv_obj_set_size(lv_obj_t*, int, int) {}
void lv_obj_set_width(lv_obj_t*, int) {}
void lv_obj_align(lv_obj_t*, int, int, int) {}
void lv_obj_set_style_pad_all(lv_obj_t*, int, int) {}
void lv_obj_set_style_text_align(lv_obj_t*, int, int) {}
void lv_obj_set_style_text_font(lv_obj_t*, const int*, int) {}
void lv_label_set_text(lv_obj_t*, const char*) {}
void lv_obj_center(lv_obj_t*) {}
void lv_obj_remove_flag(lv_obj_t* object, unsigned flags) { object->flags &= ~flags; }
void lv_obj_add_flag(lv_obj_t* object, unsigned flags) { object->flags |= flags; }
void lv_obj_add_event_cb(lv_obj_t* object, lv_event_cb_t callback, int code, void* data) {
    object->events.push_back({callback, code, data});
}
void* lv_event_get_user_data(lv_event_t* event) { return event->user_data; }
struct lv_indev_t { int direction = LV_DIR_LEFT; bool wait_release = false; };
lv_indev_t input;
lv_indev_t* active_input = &input;
lv_indev_t* lv_indev_active() { return active_input; }
int lv_indev_get_gesture_dir(lv_indev_t* device) { return device->direction; }
void lv_indev_wait_release(lv_indev_t* device) { device->wait_release = true; }

class StopwatchApps {
public:
    enum class Work { Previous, Next };
    StopwatchAppState state_;
    bool editing_ = false;
    int navigation_target_ = -1;
    std::vector<Work> work;
    int clicks = 0;
    bool QueueWork(Work value) { work.push_back(value); return true; }
    // The actual slide is covered by the dedicated Home-animation harness.
    void AnimateHome(int direction) { state_.Step(direction); }
    void Step(int direction);
    static lv_obj_t* Panel(lv_obj_t* parent);
    static lv_obj_t* Label(lv_obj_t* parent, const char* text, int width, int y);
    lv_obj_t* Action(lv_obj_t* parent, const char* text, int width, int height,
                     int x, int y, lv_event_cb_t callback);
    static void Gesture(lv_event_t* event);
};
'''
        for signature in (
            "lv_obj_t* StopwatchApps::Panel(lv_obj_t* parent)",
            "lv_obj_t* StopwatchApps::Label(lv_obj_t* parent,",
            "lv_obj_t* StopwatchApps::Action(lv_obj_t* parent,",
            "void StopwatchApps::Step(int direction)",
            "void StopwatchApps::Gesture(lv_event_t* event)",
        ):
            source += function(apps, signature) + "\n"
        source += r'''
int screen_gestures = 0;
lv_obj_t* Swipe(lv_obj_t* hit, int direction) {
    input.direction = direction;
    input.wait_release = false;
    // Hit testing ignores non-clickable labels, then lv_indev.c's gesture
    // target discovery follows GESTURE_BUBBLE up the actual widget tree.
    while (hit && !(hit->flags & LV_OBJ_FLAG_CLICKABLE)) hit = hit->parent;
    auto* target = hit;
    while (target && (target->flags & LV_OBJ_FLAG_GESTURE_BUBBLE)) target = target->parent;
    assert(target);
    for (auto& event : target->events) {
        if (event.code == LV_EVENT_GESTURE) {
            lv_event_t data{target, event.user_data};
            event.callback(&data);
        }
    }
    // Waiting for release suppresses a subsequent tile click after a swipe.
    if (!input.wait_release) {
        for (auto& event : hit->events) {
            if (event.code == LV_EVENT_CLICKED) {
                lv_event_t data{hit, event.user_data};
                event.callback(&data);
            }
        }
    }
    return target;
}
int main(int argc, char** argv) {
    assert(argc == 2);
    const std::string test = argv[1];
    StopwatchApps apps;
    auto* screen = lv_obj_create(nullptr);
    lv_obj_add_event_cb(screen, [](lv_event_t*) { ++screen_gestures; }, LV_EVENT_GESTURE, nullptr);
    auto* overlay = apps.Panel(screen);
    auto* home = apps.Panel(overlay);
    auto* badge = apps.Panel(overlay);
    lv_obj_add_event_cb(home, StopwatchApps::Gesture, LV_EVENT_GESTURE, &apps);
    lv_obj_add_event_cb(badge, StopwatchApps::Gesture, LV_EVENT_GESTURE, &apps);
    if (test == "page_background") {
        assert(!(home->flags & LV_OBJ_FLAG_SCROLLABLE));
        assert(Swipe(home, LV_DIR_RIGHT) == home);
        assert(apps.state_.selection() == 2 && input.wait_release);
        assert(Swipe(home, LV_DIR_LEFT) == home);
        assert(apps.state_.selection() == 0 && input.wait_release);
        apps.state_.Open(1);
        assert(Swipe(badge, LV_DIR_LEFT) == badge);
        assert(input.wait_release);
        assert(Swipe(badge, LV_DIR_RIGHT) == badge);
        assert((apps.work == std::vector<StopwatchApps::Work>{
            StopwatchApps::Work::Next, StopwatchApps::Work::Previous}));
    } else if (test == "central_tile") {
        auto* tile = apps.Action(home, "App", 200, 194, 0, 123, [](lv_event_t* event) {
            ++static_cast<StopwatchApps*>(lv_event_get_user_data(event))->clicks;
        });
        auto* label = tile->children.at(0);
        assert(Swipe(tile, LV_DIR_LEFT) == home);
        assert(apps.state_.selection() == 4 && input.wait_release && apps.clicks == 0);
        assert(Swipe(label, LV_DIR_RIGHT) == home);
        assert(apps.state_.selection() == 0 && input.wait_release && apps.clicks == 0);
        assert(apps.state_.page() == StopwatchAppState::Page::Home);
    } else if (test == "editor_swipe") {
        apps.state_.Open(1);
        apps.editing_ = true;
        assert(Swipe(badge, LV_DIR_LEFT) == badge && input.wait_release);
        assert(Swipe(badge, LV_DIR_RIGHT) == badge && input.wait_release);
        assert(apps.state_.page() == StopwatchAppState::Page::Badge);
        assert(apps.editing_ && apps.work.empty() && apps.clicks == 0);
    } else if (test == "ignored_gestures") {
        assert(Swipe(home, LV_DIR_TOP) == home && !input.wait_release);
        assert(apps.state_.selection() == 0 && apps.work.empty());
        active_input = nullptr;
        lv_event_t event{home, &apps};
        StopwatchApps::Gesture(&event);
        assert(apps.state_.selection() == 0 && apps.work.empty());
    } else {
        assert(false);
    }
    assert(screen_gestures == 0);
}
'''
        cls.binary = compile_cpp(cls.tmp.name, "gesture-test", source)

    def test_empty_home_and_badge_page_swipes_target_page_not_screen(self):
        subprocess.run([self.binary, "page_background"], check=True, timeout=10)

    def test_central_tile_and_label_swipes_reach_home_without_opening_tile(self):
        subprocess.run([self.binary, "central_tile"], check=True, timeout=10)

    def test_editor_horizontal_swipes_are_consumed_without_changing_badges(self):
        subprocess.run([self.binary, "editor_swipe"], check=True, timeout=10)

    def test_vertical_gestures_and_missing_input_leave_selection_unchanged(self):
        subprocess.run([self.binary, "ignored_gestures"], check=True, timeout=10)


def assignments(lines):
    return dict(line.strip().split("=", 1) for line in lines
                if line.strip().startswith("CONFIG_") and "=" in line)


def partition_number(value):
    match = re.fullmatch(r"(0x[0-9a-fA-F]+|[0-9]+)([kKmM]?)", value.strip())
    if not match:
        raise ValueError(f"Invalid partition quantity: {value!r}")
    number, suffix = match.groups()
    return int(number, 16 if number.startswith("0x") else 10) * {
        "": 1, "K": 1024, "M": MIB,
    }[suffix.upper()]


def partitions(path):
    """Resolve CSV offsets, including IDF's aligned, implicit OTA-slot offset."""
    result, end = {}, 0
    lines = path.read_text(encoding="utf-8").splitlines()
    for row in csv.reader(line for line in lines
                          if line.strip() and not line.lstrip().startswith("#")):
        name, kind, subtype, offset, size, *flags = (field.strip() for field in row)
        if name in result:
            raise ValueError(f"Duplicate partition: {name}")
        alignment = 0x10000 if kind == "app" else 0x1000
        offset = partition_number(offset) if offset else (end + alignment - 1) // alignment * alignment
        size = partition_number(size)
        result[name] = dict(type=kind, subtype=subtype, offset=offset, size=size,
                            flags=flags[0] if flags else "")
        end = offset + size
    return result


class StopwatchVariantTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = json.loads((BOARD_DIR / "config.json").read_text(encoding="utf-8"))
        cls.builds = {build["name"]: build for build in cls.config["builds"]}
        cls.stock_options = assignments(cls.builds["m5stack-stopwatch"]["sdkconfig_append"])
        cls.apps_options = assignments(cls.builds["m5stack-stopwatch-apps"]["sdkconfig_append"])
        cls.defaults = assignments((ROOT / "sdkconfig.defaults").read_text(encoding="utf-8").splitlines())
        stock_path = json.loads(cls.defaults["CONFIG_PARTITION_TABLE_CUSTOM_FILENAME"])
        apps_path = json.loads(cls.apps_options["CONFIG_PARTITION_TABLE_CUSTOM_FILENAME"])
        cls.stock = partitions(ROOT / stock_path)
        cls.apps = partitions(ROOT / apps_path)

    def test_stock_variant_has_no_local_apps_or_partition_override(self):
        self.assertNotIn("CONFIG_STOPWATCH_LOCAL_APPS", self.stock_options)
        self.assertFalse(any(key.startswith("CONFIG_PARTITION_TABLE") for key in self.stock_options))
        self.assertEqual(self.defaults["CONFIG_PARTITION_TABLE_CUSTOM"], "y")
        self.assertEqual(self.defaults["CONFIG_ESPTOOLPY_FLASHSIZE_16MB"], "y")
        self.assertNotIn("badge", self.stock)

    def test_apps_variant_is_distinct_and_explicitly_opts_in(self):
        self.assertEqual(self.config["type"], "m5stack-stopwatch")
        self.assertEqual(self.config["target"], "esp32s3")
        self.assertEqual(len(self.builds), len(self.config["builds"]), "variant names must be unique")
        self.assertNotEqual(self.builds["m5stack-stopwatch"]["name"],
                            self.builds["m5stack-stopwatch-apps"]["name"])
        self.assertEqual(self.apps_options["CONFIG_STOPWATCH_LOCAL_APPS"], "y")
        self.assertEqual(self.apps_options["CONFIG_SPIRAM"], "y")
        self.assertEqual(json.loads(self.apps_options["CONFIG_PARTITION_TABLE_CUSTOM_FILENAME"]),
                         "main/boards/m5stack/stopwatch/partitions_apps.csv")
        self.assertNotEqual(self.apps_options["CONFIG_PARTITION_TABLE_CUSTOM_FILENAME"],
                            self.defaults["CONFIG_PARTITION_TABLE_CUSTOM_FILENAME"])

    def test_system_and_ota_layout_and_assets_offset_match_stock(self):
        self.assertEqual(set(self.apps), set(self.stock) | {"badge"})
        for name, stock in self.stock.items():
            with self.subTest(partition=name):
                if name == "assets":
                    for field in ("type", "subtype", "offset", "flags"):
                        self.assertEqual(self.apps[name][field], stock[field])
                else:
                    self.assertEqual(self.apps[name], stock)
        self.assertEqual(self.stock["ota_1"]["offset"], 0x410000)
        self.assertEqual(self.apps["assets"]["offset"], 0x800000)

    def test_badge_is_dedicated_two_mib_and_layout_ends_at_sixteen_mib(self):
        assets, badge = self.apps["assets"], self.apps["badge"]
        self.assertEqual((assets["type"], assets["subtype"], assets["size"]),
                         ("data", "spiffs", 6 * MIB))
        self.assertEqual((badge["type"], badge["subtype"], badge["size"]),
                         ("data", "fat", 2 * MIB))
        self.assertEqual(assets["offset"] + assets["size"], badge["offset"])
        self.assertEqual(badge["offset"], 14 * MIB)
        self.assertEqual(badge["offset"] + badge["size"], 16 * MIB)
        for layout in (self.stock, self.apps):
            end = 0
            for name, part in sorted(layout.items(), key=lambda item: item[1]["offset"]):
                with self.subTest(partition=name, badge_layout="badge" in layout):
                    self.assertGreater(part["size"], 0)
                    self.assertGreaterEqual(part["offset"], end, "partitions must not overlap")
                    alignment = 0x10000 if part["type"] == "app" else 0x1000
                    self.assertEqual(part["offset"] % alignment, 0)
                    end = part["offset"] + part["size"]
                    self.assertLessEqual(end, 16 * MIB)
            self.assertEqual(end, 16 * MIB)


def without_comments(source):
    return re.sub(r"//[^\n]*|/\*.*?\*/", "", source, flags=re.DOTALL)


class StopwatchIntegrationTests(unittest.TestCase):
    def test_both_home_chord_buttons_keep_eight_hundred_ms_driver_threshold(self):
        config = without_comments((BOARD_DIR / "config.h").read_text(encoding="utf-8"))
        self.assertRegex(config, r"(?m)^\s*#\s*define\s+STOPWATCH_HOME_HOLD_MS\s+800\b")
        board = without_comments((BOARD_DIR / "m5stack_stopwatch.cc").read_text(encoding="utf-8"))
        for number in (1, 2):
            self.assertRegex(
                board,
                rf"button{number}_\s*\(\s*BUTTON{number}_GPIO\s*,\s*false\s*,\s*"
                r"STOPWATCH_HOME_HOLD_MS\s*\)",
            )

    def test_optional_low_battery_popup_uses_own_display_top_layer_only_for_apps(self):
        board = (BOARD_DIR / "m5stack_stopwatch.cc").read_text(encoding="utf-8")
        setup = without_comments(function(board, "void SetupUI()"))
        feature_blocks = re.findall(
            r"^\s*#\s*if\s+CONFIG_STOPWATCH_LOCAL_APPS\s*\n(.*?)^\s*#\s*endif\b",
            setup, re.MULTILINE | re.DOTALL,
        )
        popup_blocks = [block for block in feature_blocks if "low_battery_popup_" in block]
        self.assertEqual(len(popup_blocks), 1)
        block = popup_blocks[0]
        self.assertEqual(block.count("low_battery_popup_"), setup.count("low_battery_popup_"),
                         "stock SetupUI must not acquire popup/app dependencies")
        guard = re.search(r"if\s*\(\s*low_battery_popup_\s*!=\s*nullptr\s*\)", block)
        self.assertIsNotNone(guard, "the system popup is optional")
        guarded = function(block, guard.group(0))
        self.assertRegex(
            guarded,
            r"lv_obj_set_parent\s*\(\s*low_battery_popup_\s*,\s*"
            r"lv_display_get_layer_top\s*\(\s*display_\s*\)\s*\)",
        )

    def test_board_sources_export_exactly_one_factory(self):
        factories = []
        for pattern in ("*.cc", "*.c"):
            for path in BOARD_DIR.glob(pattern):
                source = without_comments(path.read_text(encoding="utf-8"))
                factories.extend(re.findall(r"\bDECLARE_BOARD\s*\(\s*(\w+)\s*\)", source))
        self.assertEqual(factories, ["M5StackStopwatchBoard"])

    def test_core_has_no_stopwatch_or_board_config_dependency(self):
        main = ROOT / "main"
        violations = []
        for path in main.rglob("*"):
            if path.suffix not in {".h", ".hpp", ".cc", ".cpp", ".c"}:
                continue
            relative = path.relative_to(main)
            # Common board interfaces are core; concrete boards may own config.h.
            if relative.parts[0] == "boards" and relative.parts[1] != "common":
                continue
            if relative.parts[0] == "assets":  # Generated/localized content is not core.
                continue
            source = without_comments(path.read_text(encoding="utf-8"))
            includes = re.findall(r'^\s*#\s*include\s*[<"]([^">]+)[">]', source, re.MULTILINE)
            if any(pathlib.PurePosixPath(include).name == "config.h" or "stopwatch" in include.lower()
                   for include in includes):
                violations.append(str(relative))
            elif re.search(r"\b(?:CONFIG_STOPWATCH_LOCAL_APPS|StopwatchApps|StopwatchAppState|"
                           r"StopwatchHomeChord|M5StackStopwatchBoard)\b", source):
                violations.append(str(relative))
        self.assertEqual(violations, [], "board-local app dependencies leaked into core")
