"""Compiled production wake activation and timer-driven local-app navigation.

Only LVGL primitives, the worker queue and local app side effects are faked.
The real state header, navigation, animation, page rendering and timer methods
run with a deterministic uint32_t clock; no firmware or hardware is required.
"""
import pathlib
import shutil
import subprocess
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[2]
BOARD = ROOT / "main/boards/m5stack/stopwatch"
COMPILER = shutil.which("c++") or shutil.which("g++")


def function(source, signature):
    start = source.index(signature)
    brace = source.index("{", start)
    depth, end = 1, brace + 1
    while depth:
        depth += (source[end] == "{") - (source[end] == "}")
        end += 1
    return source[start:end] + "\n"


PLATFORM = r'''
#include "stopwatch_app_state.h"
#include "device_state.h"
#include <algorithm>
#include <atomic>
#include <cassert>
#include <climits>
#include <cstdint>
#include <deque>
#include <functional>
#include <mutex>
#include <string>
#include <tuple>
#include <vector>

constexpr unsigned LV_OBJ_FLAG_HIDDEN = 1;
constexpr int LV_OPA_50 = 128, LV_OPA_COVER = 255;
struct lv_obj_t {
    bool hidden = true;
    int x = 0, y = 0, width = 0, height = 0, opacity = 255;
    std::string text;
};
void lv_obj_set_pos(lv_obj_t* object, int x, int y) { object->x = x; object->y = y; }
void lv_obj_set_x(lv_obj_t* object, int x) { object->x = x; }
void lv_obj_set_size(lv_obj_t* object, int width, int height) {
    object->width = width; object->height = height;
}
void lv_obj_set_style_bg_opa(lv_obj_t* object, int opacity, int) { object->opacity = opacity; }
void lv_obj_set_style_opa(lv_obj_t* object, int opacity, int) { object->opacity = opacity; }
void lv_label_set_text(lv_obj_t* object, const char* text) { object->text = text; }
void lv_obj_add_flag(lv_obj_t* object, unsigned flag) {
    assert(flag == LV_OBJ_FLAG_HIDDEN); object->hidden = true;
}
void lv_obj_remove_flag(lv_obj_t* object, unsigned flag) {
    assert(flag == LV_OBJ_FLAG_HIDDEN); object->hidden = false;
}
int foreground_moves = 0;
void lv_obj_move_foreground(lv_obj_t*) { ++foreground_moves; }

uint32_t ticks = 0;
uint32_t lv_tick_get() { return ticks; }
uint32_t lv_tick_elaps(uint32_t since) { return ticks - since; }
using lv_anim_exec_xcb_t = void (*)(void*, int32_t);
struct lv_anim_t {
    void* var = nullptr;
    lv_anim_exec_xcb_t exec = nullptr;
    int32_t from = 0, to = 0;
    uint32_t duration = 0;
};
lv_anim_t running_animation;
bool animation_active = false;
int animation_starts = 0, animation_deletes = 0;
int32_t lv_anim_path_ease_out(const lv_anim_t*) { return 0; }
void lv_anim_init(lv_anim_t* animation) { *animation = {}; }
void lv_anim_set_var(lv_anim_t* animation, void* var) { animation->var = var; }
void lv_anim_set_exec_cb(lv_anim_t* animation, lv_anim_exec_xcb_t exec) { animation->exec = exec; }
void lv_anim_set_values(lv_anim_t* animation, int32_t from, int32_t to) {
    animation->from = from; animation->to = to;
}
void lv_anim_set_duration(lv_anim_t* animation, uint32_t duration) { animation->duration = duration; }
void lv_anim_set_path_cb(lv_anim_t*, int32_t (*)(const lv_anim_t*)) {}
bool lv_anim_delete(void* var, lv_anim_exec_xcb_t exec) {
    ++animation_deletes;
    const bool found = animation_active && running_animation.var == var &&
                       (!exec || running_animation.exec == exec);
    if (found) animation_active = false;
    return found;
}
lv_anim_t* lv_anim_start(lv_anim_t* animation) {
    assert(!animation_active);  // Automatic and manual navigation cannot queue slides.
    assert(animation->from == 0 && animation->to == 466 && animation->duration == 360);
    running_animation = *animation;
    animation_active = true;
    ++animation_starts;
    animation->exec(animation->var, animation->from);
    return &running_animation;
}
void AnimationFrame(int32_t progress) {
    assert(animation_active);
    const auto animation = running_animation;
    animation.exec(animation.var, progress);
    if (progress >= animation.to) animation_active = false;
}
struct lv_timer_t { void* user_data; };
void* lv_timer_get_user_data(lv_timer_t* timer) { return timer->user_data; }

struct StopwatchSettings {
    lv_obj_t object;
    int opens = 0, closes = 0, menu_depth = 0;
    std::vector<DeviceState> voice_states;
    std::vector<int> buttons;
    lv_obj_t* root() { return &object; }
    void Open() { ++opens; menu_depth = 0; }
    void Close() { ++closes; menu_depth = 0; object.hidden = true; }
    void ObserveVoiceState(DeviceState state) { voice_states.push_back(state); }
    void HandleButton(int button) { buttons.push_back(button); }
};
struct StopwatchWeather {
    lv_obj_t object;
    int opens = 0, closes = 0;
    bool active = false;
    std::vector<int> buttons;
    lv_obj_t* root() { return &object; }
    void Open() { ++opens; active = true; }
    void Close() { ++closes; active = false; object.hidden = true; }
    void HandleButton(int button) { buttons.push_back(button); }
};
struct StopwatchAlarm : StopwatchWeather {};
class StopwatchApps {
public:
    enum class Work { Initialize, Load, Previous, Next, StartEditor, StopEditor };
    StopwatchAppState state_;
    StopwatchSettings settings_;
    StopwatchWeather weather_;
    StopwatchAlarm alarm_;
    std::atomic<bool> editor_requested_{false}, badge_changed_{false};
    lv_obj_t overlay, home, badge, editor_panel, editor_url, home_name, icons[5], dots[5];
    lv_obj_t* overlay_ = &overlay;
    lv_obj_t* home_ = &home;
    lv_obj_t* badge_ = &badge;
    lv_obj_t* editor_panel_ = &editor_panel;
    lv_obj_t* editor_url_ = &editor_url;
    lv_obj_t* home_name_ = &home_name;
    std::vector<Work> work;
    std::vector<bool> wake_requests;
    std::function<void(bool)> wake_policy_ = [this](bool enabled) {
        wake_requests.push_back(enabled);
    };
    bool queue_accepts = true;
    int clock_updates = 0;
    bool QueueWork(Work value) {
        if (!queue_accepts) return false;
        work.push_back(value); return true;
    }
    void UpdateHomeClock() { ++clock_updates; }
    StopwatchApps() {
        for (int index = 0; index < 5; ++index) {
            home_icons_[index] = &icons[index];
            home_dots_[index] = &dots[index];
        }
    }
    void ShowHome();
    void SetWakeWordEnabled(bool enabled);
    bool CanNavigate() const;
    bool ActivateVoiceByWakeWord();
    bool ActivateVoiceForAlarm();
    // ALARM_FOREGROUND_GUARDS
    NavigationResult NavigateToApp(int index);
    void AdvanceNavigation();
    void OpenSelected();
    void Step(int direction);
    bool HandleButton(int button);
    void StartEditor();
    void StopEditor();
    void ObserveVoiceState(DeviceState state, bool allow_boot_home = false);
    void RenderPage();
    void RenderHome();
    void AnimateHome(int direction);
    static void HomeAnimation(void* user_data, int32_t progress);
    static void Tick(lv_timer_t* timer);
'''


CHECKS = r'''
using Page = StopwatchAppState::Page;
using Result = StopwatchApps::NavigationResult;
using Work = StopwatchApps::Work;
constexpr Page kExpectedPages[] = {Page::Voice, Page::Badge, Page::Settings, Page::Weather, Page::Alarm};
// Independent expectations, not aliases of production kHomeOrder.
constexpr int kExpectedOrder[] = {0, 4, 1, 3, 2};
int Position(int app) {
    for (int position = 0; position < 5; ++position) {
        if (kExpectedOrder[position] == app) return position;
    }
    assert(false);
    return -1;
}
void Poll(StopwatchApps& apps) {
    lv_timer_t timer{&apps};
    const int updates = apps.clock_updates;
    apps.Tick(&timer);  // Exercise the real Tick -> AdvanceNavigation integration.
    assert(apps.clock_updates == updates + 1);
}
auto Snapshot(const StopwatchApps& apps) {
    return std::make_tuple(
        apps.state_.page(), apps.state_.selection(), apps.boot_home_pending_,
        apps.last_voice_state_, apps.editing_, apps.editor_requested_.load(),
        apps.navigation_target_, apps.navigation_tick_, apps.navigation_generation(), apps.home_animating_,
        apps.settings_.opens, apps.settings_.closes, apps.settings_.menu_depth,
        apps.weather_.opens, apps.weather_.closes, apps.weather_.active,
        apps.work, apps.wake_requests, apps.settings_.voice_states,
        apps.overlay.hidden, foreground_moves, animation_starts, animation_deletes,
        apps.alarm_.opens, apps.alarm_.closes, apps.alarm_.active, apps.alarm_.buttons);
}
void Ready(StopwatchApps& apps) {
    apps.ObserveVoiceState(kDeviceStateIdle, true);
    assert(!apps.boot_home_pending_ && apps.CanNavigate() && !apps.overlay.hidden);
    apps.work.clear();
    apps.wake_requests.clear();
}
void Foreground(StopwatchApps& apps, int index, bool home = false, bool editor = false) {
    Ready(apps);
    apps.state_.Open(index);
    apps.ShowHome();
    if (!home) apps.OpenSelected();
    if (index == 2 && !home) apps.settings_.menu_depth = 3;
    if (editor) apps.StartEditor();
    assert(apps.state_.selection() == index);
    assert(apps.state_.page() == (home ? Page::Home : kExpectedPages[index]));
    apps.work.clear();
    apps.wake_requests.clear();
}
void AssertHome(const StopwatchApps& apps, int selected, int target) {
    assert(apps.state_.page() == Page::Home && apps.state_.selection() == selected);
    assert(apps.navigation_target_ == target && !apps.overlay.hidden && !apps.home.hidden);
    assert(apps.settings_.object.hidden && apps.weather_.object.hidden && !apps.weather_.active);
    assert(apps.alarm_.object.hidden && !apps.alarm_.active);
    assert(apps.badge.hidden && apps.editor_panel.hidden);
    assert(!apps.editing_ && !apps.editor_requested_.load());
    assert((apps.wake_requests == std::vector<bool>{false}));
}
void AssertNoManualInput(StopwatchApps& apps) {
    const auto snapshot = Snapshot(apps);
    for (int count = 0; count < 100; ++count) {
        apps.Step(-1);
        apps.Step(1);
        apps.OpenSelected();
        assert(apps.HandleButton(0) && apps.HandleButton(1) && apps.HandleButton(2));
    }
    assert(Snapshot(apps) == snapshot);
}
void CompleteRoute(StopwatchApps& apps, int target) {
    uint32_t settled_at = ticks;
    const int settings_opens = apps.settings_.opens, weather_opens = apps.weather_.opens;
    const int alarm_opens = apps.alarm_.opens;
    const auto work = apps.work;
    int slides = 0;
    while (true) {
        const int selected = apps.state_.selection();
        const int starts = animation_starts;
        ticks = settled_at + 199;
        Poll(apps);
        AssertHome(apps, selected, target);
        assert(!apps.home_animating_ && animation_starts == starts);
        AssertNoManualInput(apps);
        ticks = settled_at + 200;
        Poll(apps);
        if (selected == target) break;
        const int forward = (Position(target) - Position(selected) + 5) % 5;
        const int direction = forward <= 2 ? 1 : -1;
        const int next = kExpectedOrder[(Position(selected) + (direction > 0 ? 1 : 4)) % 5];
        AssertHome(apps, next, target);
        assert(apps.home_animating_ && animation_active && animation_starts == starts + 1);
        assert(apps.home_direction_ == direction && apps.home_from_ == selected);
        assert(!apps.icons[selected].hidden && !apps.icons[next].hidden);
        assert(apps.settings_.opens == settings_opens && apps.weather_.opens == weather_opens);
        assert(apps.alarm_.opens == alarm_opens);
        assert(apps.work == work);
        AssertNoManualInput(apps);
        ticks += 300;
        AnimationFrame(350);
        Poll(apps);  // A poll during a slide cannot advance/open the selected target.
        AssertHome(apps, next, target);
        assert(apps.home_animating_ && animation_starts == starts + 1);
        ticks += 60;
        AnimationFrame(466);
        assert(!apps.home_animating_ && !animation_active && apps.icons[next].x == 133);
        settled_at = ticks;  // The entire next 200 ms must be idle, after the final frame.
        assert(++slides <= 2);  // Five apps always have a route of at most two slides.
    }
    assert(apps.state_.page() == kExpectedPages[target] && apps.state_.selection() == target);
    assert(apps.navigation_target_ == -1 && !apps.home_animating_ && !animation_active);
    assert(apps.settings_.opens == settings_opens + (target == 2));
    assert(apps.weather_.opens == weather_opens + (target == 3));
    assert(apps.weather_.active == (target == 3));
    assert(apps.alarm_.opens == alarm_opens + (target == 4));
    assert(apps.alarm_.active == (target == 4));
    assert((apps.wake_requests == std::vector<bool>{false, false}));
    assert(apps.work == work);  // Intermediate Home tiles must not start any app/editor.
    const auto snapshot = Snapshot(apps);
    ticks += 10000;
    Poll(apps);
    apps.OpenSelected();  // A late tap cannot reopen/reset an already-open app.
    assert(Snapshot(apps) == snapshot);
}
void AssertCancelled(StopwatchApps& apps, Page page, int selected) {
    assert(apps.navigation_target_ == -1 && !apps.home_animating_ && !animation_active);
    assert(apps.state_.page() == page && apps.state_.selection() == selected);
    const auto snapshot = Snapshot(apps);
    StopwatchApps::HomeAnimation(&apps, 466);  // A queued stale animation frame is harmless.
    ticks += 10000;
    Poll(apps);
    assert(Snapshot(apps) == snapshot);
}
int main(int argc, char** argv) {
    assert(argc == 2);
    const std::string test = argv[1];
    if (test == "wake_cleanup") {
        // Home, Badge/editor, Settings, Weather and Alarms share cleanup.
        for (int scenario = 0; scenario < 6; ++scenario) {
            StopwatchApps apps;
            const int index = scenario == 0 ? 3 : (scenario <= 2 ? 1 : scenario - 1);
            Foreground(apps, index, scenario == 0, scenario == 2);
            const int settings_closes = apps.settings_.closes, weather_closes = apps.weather_.closes;
            const int alarm_closes = apps.alarm_.closes;
            const int starts = animation_starts;
            assert(apps.ActivateVoiceByWakeWord());
            assert(apps.state_.page() == Page::Voice && apps.state_.selection() == 0);
            assert(apps.overlay.hidden && apps.settings_.object.hidden && apps.weather_.object.hidden);
            assert(!apps.editing_ && !apps.editor_requested_.load() && !apps.weather_.active);
            assert(apps.settings_.menu_depth == 0 && apps.settings_.closes == settings_closes + 1);
            assert(apps.weather_.closes == weather_closes + 1);
            assert(apps.alarm_.closes == alarm_closes + 1 && !apps.alarm_.active);
            assert(apps.alarm_.object.hidden);
            assert((apps.work == std::vector<Work>{Work::StopEditor}));
            assert((apps.wake_requests == std::vector<bool>{false, true}));
            assert(animation_starts == starts && !apps.home_animating_ && !animation_active);
            auto snapshot = Snapshot(apps);
            for (int count = 0; count < 3; ++count) {
                assert(apps.ActivateVoiceByWakeWord());
                ++std::get<8>(snapshot);  // Cancel queued navigation without resetting Voice.
                std::get<17>(snapshot).push_back(true);  // Restore suspended wake policy.
                assert(Snapshot(apps) == snapshot);
            }
        }
    } else if (test == "wake_voice_policy") {
        StopwatchApps apps;
        Foreground(apps, 0);
        // Alarm text playback may suspend policy while XiaoZhi already owns foreground.
        apps.SetWakeWordEnabled(false);
        auto snapshot = Snapshot(apps);
        assert(apps.ActivateVoiceByWakeWord());
        ++std::get<8>(snapshot);
        std::get<17>(snapshot).push_back(true);
        assert(Snapshot(apps) == snapshot);  // No ShowHome, Close, editor work, or rerender.
        assert((apps.wake_requests == std::vector<bool>{false, true}));
        assert(apps.IsVoiceForeground() && apps.navigation_target_ == -1);
    } else if (test == "alarm_voice_cleanup") {
        for (int index = 0; index < 5; ++index) {
            for (bool home : {false, true}) {
                StopwatchApps apps;
                Foreground(apps, index, home, !home && index == 1);
                assert(apps.CanPresentAlarm());
                assert(apps.IsVoiceForeground() == (!home && index == 0));
                const auto generation = apps.navigation_generation();
                const auto alarm_closes = apps.alarm_.closes;
                assert(apps.ActivateVoiceForAlarm());
                assert(apps.IsVoiceForeground() && apps.state_.selection() == 0);
                assert(apps.navigation_generation() == generation + 1);
                assert(apps.navigation_target_ == -1 && !apps.editing_ && !apps.editor_requested_.load());
                assert(!apps.alarm_.active && apps.alarm_.closes == alarm_closes + 1);
                assert(apps.alarm_.object.hidden && apps.weather_.object.hidden && apps.overlay.hidden);
                assert((apps.work == std::vector<Work>{Work::StopEditor}));
                assert((apps.wake_requests == std::vector<bool>{false, true}));
                assert(!apps.home_animating_ && !animation_active);
            }
        }
    } else if (test == "alarm_voice_cancels_route") {
        for (int phase = 0; phase < 2; ++phase) {
            StopwatchApps apps; Foreground(apps, 4);
            assert(apps.NavigateToApp(2) == Result::Accepted);
            if (phase) { ticks += 200; Poll(apps); AnimationFrame(116); }
            assert(apps.CanPresentAlarm() && !apps.IsVoiceForeground());
            assert(apps.ActivateVoiceForAlarm());
            assert(apps.IsVoiceForeground());
            AssertCancelled(apps, Page::Voice, 0);
            assert(apps.settings_.opens == 0 && !apps.alarm_.active);
        }
    } else if (test == "unavailable") {
        for (int scenario = 0; scenario < 5; ++scenario) {
            StopwatchApps apps;
            if (scenario == 0) {
                apps.overlay_ = nullptr;
            } else if (scenario >= 2) {
                Ready(apps);
                const DeviceState system[] = {
                    kDeviceStateWifiConfiguring, kDeviceStateUpgrading, kDeviceStateFatalError};
                apps.ObserveVoiceState(system[scenario - 2]);
            }
            // scenario 1 retains startup's pending Home, so voice startup UI owns foreground.
            assert(!apps.CanNavigate());
            const auto snapshot = Snapshot(apps);
            assert(!apps.ActivateVoiceByWakeWord());
            assert(!apps.ActivateVoiceForAlarm() && !apps.CanPresentAlarm() && !apps.IsVoiceForeground());
            for (int target : {0, 1, 2, 3, 4}) assert(apps.NavigateToApp(target) == Result::Unavailable);
            assert(Snapshot(apps) == snapshot);
        }
        for (DeviceState state : {kDeviceStateIdle, kDeviceStateConnecting, kDeviceStateListening,
                                  kDeviceStateSpeaking, kDeviceStateNotifying}) {
            StopwatchApps apps;
            Ready(apps);
            apps.ObserveVoiceState(state);
            assert(apps.CanNavigate() && apps.ActivateVoiceByWakeWord());
        }
    } else if (test == "invalid_and_busy") {
        StopwatchApps apps;
        Foreground(apps, 0);
        const auto before = Snapshot(apps);
        for (int index : {INT_MIN, -1, 0, 5, INT_MAX}) {
            assert(apps.NavigateToApp(index) == Result::Unavailable);
            assert(Snapshot(apps) == before);  // Remote tools can never target Voice.
        }
        assert(apps.NavigateToApp(3) == Result::Accepted);
        const auto pending = Snapshot(apps);
        for (int target : {1, 2, 3, 4}) {
            assert(apps.NavigateToApp(target) == Result::Busy);
            assert(Snapshot(apps) == pending);  // Do not replace or extend an accepted route.
        }
        for (int target : {0, -1, 5}) {
            assert(apps.NavigateToApp(target) == Result::Unavailable);
            assert(Snapshot(apps) == pending);
        }
        AssertNoManualInput(apps);
        ticks += 200;
        Poll(apps);
        assert(apps.home_animating_);
        const auto sliding = Snapshot(apps);
        assert(apps.NavigateToApp(1) == Result::Busy);
        assert(Snapshot(apps) == sliding);
        AssertNoManualInput(apps);
        apps.ShowHome();
        AssertCancelled(apps, Page::Home, 2);
    } else if (test == "already_open") {
        for (int index : {1, 2, 3, 4}) {
            StopwatchApps apps;
            Foreground(apps, index, false, index == 1);
            const auto snapshot = Snapshot(apps);
            for (int count = 0; count < 3; ++count) {
                assert(apps.NavigateToApp(index) == Result::AlreadyOpen);
                apps.OpenSelected();  // Guard direct open calls from every non-Home page too.
            }
            assert(Snapshot(apps) == snapshot);
        }
    } else if (test == "routes" || test == "home_routes") {
        for (int from = 0; from < 5; ++from) {
            for (int target : {1, 2, 3, 4}) {
                if (test == "routes" && from == target) continue;
                StopwatchApps apps;
                Foreground(apps, from, test == "home_routes", test == "routes" && from == 1);
                const int starts = animation_starts;
                const int settings_closes = apps.settings_.closes, weather_closes = apps.weather_.closes;
                assert(apps.NavigateToApp(target) == Result::Accepted);
                AssertHome(apps, from, target);  // Voice/local app -> visible Home, not target.
                assert(apps.navigation_tick_ == ticks && animation_starts == starts);
                assert(apps.settings_.closes == settings_closes + 1);
                assert(apps.weather_.closes == weather_closes + 1 && apps.settings_.menu_depth == 0);
                assert((apps.work == std::vector<Work>{Work::StopEditor}));
                CompleteRoute(apps, target);
            }
        }
    } else if (test == "animation_completion_dwell") {
        StopwatchApps apps;
        Foreground(apps, 4);  // Alarm -> Badge is one visual edge in the five-app order.
        assert(apps.NavigateToApp(1) == Result::Accepted);
        ticks = 200;
        Poll(apps);
        assert(apps.home_animating_ && apps.state_.selection() == 1);
        ticks = 500;
        Poll(apps);  // Last periodic timer observation is 60 ms before completion.
        ticks = 560;
        AnimationFrame(466);
        ticks = 759;
        Poll(apps);
        AssertHome(apps, 1, 1);  // 259 ms since the poll, but only 199 ms of actual idle dwell.
        ticks = 760;
        Poll(apps);
        assert(apps.state_.page() == Page::Badge && apps.navigation_target_ == -1);
    } else if (test == "cancel_home" || test == "cancel_wake" || test == "cancel_system") {
        const DeviceState system[] = {
            kDeviceStateWifiConfiguring, kDeviceStateUpgrading, kDeviceStateFatalError};
        for (int phase = 0; phase < 2; ++phase) {
            for (int which = 0; which < (test == "cancel_system" ? 3 : 1); ++which) {
                StopwatchApps apps;
                Foreground(apps, 0);
                assert(apps.NavigateToApp(3) == Result::Accepted);
                if (phase) {
                    ticks += 200;
                    Poll(apps);
                    AnimationFrame(116);
                    assert(apps.home_animating_ && animation_active);
                }
                const int selected = apps.state_.selection();
                if (test == "cancel_home") {
                    apps.ShowHome();
                    AssertCancelled(apps, Page::Home, selected);
                } else if (test == "cancel_wake") {
                    assert(apps.ActivateVoiceByWakeWord());
                    assert((apps.wake_requests == std::vector<bool>{false, false, true}));
                    AssertCancelled(apps, Page::Voice, 0);
                } else {
                    apps.ObserveVoiceState(system[which]);
                    assert((apps.wake_requests == std::vector<bool>{false}));
                    assert(!apps.CanNavigate());
                    AssertCancelled(apps, Page::Voice, 0);
                }
                assert(apps.settings_.opens == 0 && apps.weather_.opens == 0);
                assert(!apps.editing_ && !apps.editor_requested_.load() && !apps.weather_.active);
            }
        }
    } else if (test == "cancel_guard") {
        for (int reason = 0; reason < 3; ++reason) {
            StopwatchApps apps;
            Foreground(apps, 0);
            assert(apps.NavigateToApp(3) == Result::Accepted);
            if (reason == 0) apps.overlay_ = nullptr;
            if (reason == 1) apps.boot_home_pending_ = true;
            if (reason == 2) { apps.state_.ShowVoice(); apps.RenderPage(); }
            ticks += 1000;
            Poll(apps);
            assert(apps.navigation_target_ == -1 && !animation_active && !apps.home_animating_);
            assert(apps.settings_.opens == 0 && apps.weather_.opens == 0);
        }
    } else if (test == "clock_wrap") {
        StopwatchApps apps;
        Foreground(apps, 0);
        ticks = UINT32_MAX - 100;
        assert(apps.NavigateToApp(2) == Result::Accepted);
        CompleteRoute(apps, 2);  // Both initial and post-slide dwell cross uint32_t arithmetic.
    } else if (test == "normal_voice_states") {
        StopwatchApps apps;
        Foreground(apps, 0);
        assert(apps.NavigateToApp(3) == Result::Accepted);
        for (DeviceState state : {kDeviceStateConnecting, kDeviceStateListening,
                                  kDeviceStateSpeaking, kDeviceStateNotifying, kDeviceStateIdle}) {
            apps.ObserveVoiceState(state);
            AssertHome(apps, 0, 3);
        }
        CompleteRoute(apps, 3);
    } else {
        assert(false);
    }
}
'''


def ui_source():
    """Share the identical real UI methods between the UI and queued-tool harnesses."""
    apps = (BOARD / "stopwatch_apps.cc").read_text(encoding="utf-8")
    header = (BOARD / "stopwatch_apps.h").read_text(encoding="utf-8")
    navigation_enum = next(line.strip() for line in header.splitlines()
                           if line.strip().startswith("enum class NavigationResult"))
    # The enum must precede its method declaration, while all field defaults
    # come from the real class rather than being silently fixed by the fake.
    source = PLATFORM.replace("    NavigationResult NavigateToApp(int index);",
                              f"    {navigation_enum}\n    NavigationResult NavigateToApp(int index);")
    source = source.replace("// ALARM_FOREGROUND_GUARDS", "\n".join((
        function(header, "bool CanPresentAlarm() const"),
        function(header, "bool IsVoiceForeground() const"),
    )))
    for token in ("lv_obj_t* home_icons_[", "lv_obj_t* home_dots_[", "bool boot_home_pending_",
                  "bool editing_", "DeviceState last_voice_state_", "int home_from_",
                  "int home_direction_", "bool home_animating_", "int navigation_target_",
                  "uint32_t navigation_tick_", "uint32_t navigation_generation_",
                  "uint32_t navigation_generation() const"):
        source += next(line.strip() for line in header.splitlines()
                       if line.strip().startswith(token)) + "\n"
    source += "};\n"
    source += next(line for line in apps.splitlines()
                   if line.startswith("constexpr const char* kNames[]")) + "\n"
    for signature in (
        "void StopwatchApps::ShowHome()",
        "void StopwatchApps::SetWakeWordEnabled(bool enabled)",
        "bool StopwatchApps::CanNavigate() const",
        "bool StopwatchApps::ActivateVoiceByWakeWord()",
        "bool StopwatchApps::ActivateVoiceForAlarm()",
        "StopwatchApps::NavigationResult StopwatchApps::NavigateToApp(int index)",
        "void StopwatchApps::AdvanceNavigation()",
        "void StopwatchApps::OpenSelected()",
        "void StopwatchApps::Step(int direction)",
        "bool StopwatchApps::HandleButton(int button)",
        "void StopwatchApps::StartEditor()",
        "void StopwatchApps::StopEditor()",
        "void StopwatchApps::ObserveVoiceState(",
        "void StopwatchApps::RenderPage()",
        "void StopwatchApps::RenderHome()",
        "void StopwatchApps::AnimateHome(int direction)",
        "void StopwatchApps::HomeAnimation(void* user_data, int32_t progress)",
        "void StopwatchApps::Tick(lv_timer_t* timer)",
    ):
        source += function(apps, signature)
    return source


@unittest.skipUnless(COMPILER, "requires a host C++ compiler")
class StopwatchNavigationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="stopwatch-navigation-test-")
        cls.addClassCleanup(cls.tmp.cleanup)
        source = ui_source() + CHECKS
        cls.binary = pathlib.Path(cls.tmp.name) / "stopwatch-navigation-test.exe"
        result = subprocess.run(
            [COMPILER, "-std=c++17", "-Wall", "-Wextra", "-Werror", "-I", str(BOARD),
             "-I", str(ROOT / "main"), "-x", "c++", "-", "-o", str(cls.binary)],
            input=source, text=True, encoding="utf-8", errors="replace",
            capture_output=True, timeout=60,
        )
        if result.returncode:
            raise AssertionError(result.stdout + result.stderr)

    def run_case(self, name):
        result = subprocess.run([str(self.binary), name], capture_output=True,
                                text=True, encoding="utf-8", errors="replace", timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_wake_directly_opens_voice_from_home_badge_editor_settings_and_weather_with_cleanup(self):
        self.run_case("wake_cleanup")

    def test_wake_on_existing_voice_restores_alarm_suspended_policy_without_page_reset(self):
        self.run_case("wake_voice_policy")

    def test_alarm_voice_entry_cleans_every_page_without_borrowing_the_wake_hook(self):
        self.run_case("alarm_voice_cleanup")

    def test_alarm_voice_entry_cancels_waiting_and_sliding_routes(self):
        self.run_case("alarm_voice_cancels_route")

    def test_startup_missing_overlay_and_system_foreground_reject_wake_and_remote_navigation(self):
        self.run_case("unavailable")

    def test_voice_invalid_targets_and_pending_requests_do_not_replace_or_restart_navigation(self):
        self.run_case("invalid_and_busy")

    def test_already_open_target_does_not_reopen_reset_nested_settings_or_stop_badge_editor(self):
        self.run_case("already_open")

    def test_all_local_app_routes_leave_voice_or_local_app_via_home_then_shortest_animated_path(self):
        self.run_case("routes")

    def test_home_routes_keep_semantic_ids_take_shortest_path_and_ignore_manual_inputs(self):
        self.run_case("home_routes")

    def test_full_two_hundred_ms_idle_dwell_starts_at_actual_animation_completion(self):
        self.run_case("animation_completion_dwell")

    def test_explicit_home_cancels_waiting_and_in_flight_routes_and_stale_animation_frames(self):
        self.run_case("cancel_home")

    def test_wake_cancels_waiting_and_in_flight_routes_and_activates_voice_only_once(self):
        self.run_case("cancel_wake")

    def test_wifi_upgrade_and_fatal_states_cancel_waiting_and_in_flight_routes_without_wake(self):
        self.run_case("cancel_system")

    def test_timer_discards_routes_if_overlay_boot_or_page_ownership_is_lost(self):
        self.run_case("cancel_guard")

    def test_dwell_and_animation_navigation_survive_lvgl_tick_wraparound(self):
        self.run_case("clock_wrap")

    def test_normal_background_voice_states_do_not_cancel_or_skip_navigation(self):
        self.run_case("normal_voice_states")


MCP_PLATFORM = r'''
#include "mcp_server.h"
#include <algorithm>
#include <atomic>
#include <cassert>
#include <cstdint>
#include <deque>
#include <mutex>
#include <string>
#include <utility>
#include <vector>

#define ESP_LOGI(...) ((void)0)
#define ESP_LOGW(...) ((void)0)
#define ESP_LOGE(...) ((void)0)
std::vector<std::string> events;
struct FakeProtocol {
    std::vector<std::string> sent;
    void SendMcpMessage(const std::string& payload) { sent.push_back(payload); events.push_back("reply"); }
};
constexpr uint32_t MAIN_EVENT_SCHEDULE = 1;
struct EventGroup { uint32_t bits = 0; };
void xEventGroupSetBits(EventGroup* group, uint32_t bits) { group->bits |= bits; }
class Application {
public:
    std::mutex mutex_;
    std::deque<std::function<void()>> main_tasks_;
    EventGroup event_group;
    EventGroup* event_group_ = &event_group;
    FakeProtocol protocol;
    FakeProtocol* protocol_ = &protocol;
    // MCP_LIFECYCLE_FIELDS
    std::function<void(const std::string&)> mcp_broadcast_callback_;
    std::function<bool()> wake_handler;
    int wake_registrations = 0;
    bool on_main = false;
    static Application& GetInstance() { static Application app; return app; }
    void Schedule(std::function<void()>&& callback);
    void SendMcpMessage(const std::string& payload);
    void SetWakeWordActivationHandler(std::function<bool()> callback) {
        assert(!wake_handler); ++wake_registrations; wake_handler = std::move(callback);
    }
    void ConversationPolicyMayCloseTransport() {
        assert(on_main);
        Schedule([this]() { events.push_back("policy"); protocol_closing_.store(true); });
    }
    void FlushTurn();
};
struct LockOwner { bool lock_allowed = true; int lock_depth = 0; };
struct DisplayLockGuard {
    LockOwner* owner;
    bool acquired;
    explicit DisplayLockGuard(LockOwner* owner) : owner(owner), acquired(owner->lock_allowed) {
        if (acquired) ++owner->lock_depth;
    }
    ~DisplayLockGuard() { if (acquired) --owner->lock_depth; }
    explicit operator bool() const { return acquired; }
};
struct StopwatchAlarmRuntime {
    LockOwner* owner = nullptr;
    int cancels = 0;
    void CancelForUser() {
        assert(owner && owner->lock_depth == 0);  // Runtime may reenter display callbacks.
        ++cancels;
    }
};
struct RoundLcdDisplay : LockOwner {
    StopwatchApps apps;
    StopwatchApps* apps_ = &apps;
    StopwatchAlarmRuntime runtime{this};
    StopwatchAlarmRuntime* alarm_runtime_ = &runtime;
    int policy_calls = 0, navigation_calls = 0;
    RoundLcdDisplay() {
        Foreground(apps, 0);
        apps.navigation_probe = [this]() {
            assert(lock_depth == 1 && Application::GetInstance().on_main); ++navigation_calls;
        };
        apps.wake_policy_ = [this](bool enabled) {
            assert(lock_depth == 1);
            apps.wake_requests.push_back(enabled);
            ++policy_calls;
            events.push_back(enabled ? "voice" : "navigate");
            if (!enabled) Application::GetInstance().ConversationPolicyMayCloseTransport();
        };
    }
    // DISPLAY_WRAPPERS
    void RegisterWakeHandler() {
        // WAKE_REGISTRATION
    }
};
struct StopwatchWeatherService {};
struct StopwatchAlarmService {};
int weather_registrations = 0, alarm_registrations = 0;
void RegisterStopwatchWeatherTools(StopwatchWeatherService&) { ++weather_registrations; }
void RegisterStopwatchAlarmTools(StopwatchAlarmService&) { ++alarm_registrations; }
struct M5StackStopwatchBoard {
    StopwatchWeatherService weather_service_;
    StopwatchAlarmService alarm_service_;
    RoundLcdDisplay display;
    RoundLcdDisplay* display_ = &display;
    // TOOL_REGISTRATION
};
// The public ParseMessage overloads are test bridges to private dispatch/schema
// state. Production argument validation, callback execution, result generation,
// ReplyResult/ReplyError and SendMcpMessage are all compiled without changes.
void McpServer::ParseMessage(const cJSON* args, ResponseSender sender) {
    DoToolCall(7, "self.app.open", args, std::move(sender));
}
void McpServer::ParseMessage(const std::string& request, ResponseSender sender) {
    assert(request == "schema" && tools_.size() == 1 && sender);
    assert(!tools_.front()->user_only());
    sender(tools_.front()->to_json());
}
'''


MCP_CHECKS = r'''
void Prepare(RoundLcdDisplay& display, int index, bool home = false, bool editor = false) {
    auto policy = std::move(display.apps.wake_policy_);
    display.apps.ShowHome();  // Reuse the fixture after the preceding app/system scenario.
    Foreground(display.apps, index, home, editor);
    display.apps.wake_policy_ = std::move(policy);
}
void ClearEffects(RoundLcdDisplay& display) {
    auto& app = Application::GetInstance();
    assert(app.main_tasks_.empty() && !app.on_main && display.lock_depth == 0);
    events.clear();
    app.protocol.sent.clear();
    app.protocol_closing_.store(false);
    display.policy_calls = display.navigation_calls = 0;
}
using Result = StopwatchApps::NavigationResult;
cJSON* Field(cJSON* object, const char* name) {
    auto* value = cJSON_GetObjectItemCaseSensitive(object, name); assert(value); return value;
}
std::string Text(cJSON* object, const char* name) {
    auto* value = Field(object, name); assert(cJSON_IsString(value)); return value->valuestring;
}
CJsonUniquePtr Json(const std::string& text) {
    CJsonUniquePtr value(cJSON_Parse(text.c_str())); assert(value); return value;
}
void Request(const std::string& args) {
    auto json = Json(args);
    McpServer::GetInstance().ParseMessage(json.get());
}
void Drain() {
    auto& app = Application::GetInstance();
    int turns = 0;
    while (!app.main_tasks_.empty()) { app.FlushTurn(); assert(++turns <= 6); }
}
std::string ReplyText() {
    const auto& sent = Application::GetInstance().protocol.sent;
    assert(sent.size() == 1);
    auto json = Json(sent.front());
    assert(cJSON_IsNumber(Field(json.get(), "id")) && Field(json.get(), "id")->valueint == 7);
    auto* result = Field(json.get(), "result");
    assert(cJSON_IsFalse(Field(result, "isError")));
    auto* content = Field(result, "content");
    assert(cJSON_IsArray(content) && cJSON_GetArraySize(content) == 1);
    auto* item = cJSON_GetArrayItem(content, 0);
    assert(Text(item, "type") == "text");
    return Text(item, "text");
}
int main(int argc, char** argv) {
    assert(argc == 2);
    const std::string test = argv[1];
    M5StackStopwatchBoard board;
    board.InitializeTools();
    assert(weather_registrations == 1 && alarm_registrations == 1);
    auto& app = Application::GetInstance();
    if (test == "schema") {
        std::string serialized;
        McpServer::GetInstance().ParseMessage(std::string("schema"),
            [&serialized](const std::string& result) { serialized = result; });
        auto json = Json(serialized);
        assert(Text(json.get(), "name") == "self.app.open");
        const auto description = Text(json.get(), "description");
        for (const char* name : {"badge", "settings", "weather", "alarm"}) {
            assert(description.find(name) != std::string::npos);
        }
        auto* schema = Field(json.get(), "inputSchema");
        assert(Text(schema, "type") == "object");
        auto* properties = Field(schema, "properties");
        assert(cJSON_GetArraySize(properties) == 1);
        auto* property = Field(properties, "app");
        assert(Text(property, "type") == "string" && Field(property, "maxLength")->valueint == 8);
        assert(!cJSON_GetObjectItemCaseSensitive(property, "default"));
        auto* required = Field(schema, "required");
        assert(cJSON_GetArraySize(required) == 1);
        assert(std::string(cJSON_GetArrayItem(required, 0)->valuestring) == "app");
        assert(events.empty() && app.main_tasks_.empty() && board.display.navigation_calls == 0);
    } else if (test == "fifo_badge" || test == "fifo_settings" || test == "fifo_weather" || test == "fifo_alarm") {
        const std::string name = test.substr(5);
        const int index = name == "badge" ? 1 : name == "settings" ? 2 : name == "weather" ? 3 : 4;
        Request("{\"app\":\"" + name + "\"}");
        assert(app.main_tasks_.size() == 1 && events.empty());  // Main-task tool dispatch.
        app.FlushTurn();
        assert(app.main_tasks_.size() == 2 && events.empty());  // First hop, then scheduled reply.
        app.FlushTurn();
        assert((events == std::vector<std::string>{"reply"}));
        assert(board.display.navigation_calls == 0 && app.main_tasks_.size() == 1);
        const auto reply = ReplyText();
        assert(reply.find("Navigation scheduled") != std::string::npos);
        assert(reply.find("already-open target is left unchanged") != std::string::npos);
        assert(reply.find("Busy or unavailable UI will ignore") != std::string::npos);
        assert(!app.protocol_closing_.load());
        app.FlushTurn();
        assert(board.display.navigation_calls == 1 && board.display.apps.navigation_target_ == index);
        assert(board.display.apps.state_.page() == Page::Home);
        assert((events == std::vector<std::string>{"reply", "navigate"}));
        assert(!app.protocol_closing_.load());
        app.FlushTurn();
        assert((events == std::vector<std::string>{"reply", "navigate", "policy"}));
        assert(app.protocol_closing_.load() && app.main_tasks_.empty() && board.display.lock_depth == 0);
    } else if (test == "already_open" || test == "busy" || test == "unavailable") {
        Prepare(board.display, 2, false);
        if (test == "busy") {
            board.display.apps.state_.ShowHome();
            board.display.apps.RenderPage();
            board.display.apps.navigation_target_ = 3;
        } else if (test == "unavailable") {
            board.display.apps.last_voice_state_ = kDeviceStateUpgrading;
        }
        const auto snapshot = Snapshot(board.display.apps);
        Request("{\"app\":\"settings\"}");
        Drain();
        assert((events == std::vector<std::string>{"reply"}));
        assert(board.display.navigation_calls == 1 && Snapshot(board.display.apps) == snapshot);
        assert(!app.protocol_closing_.load());
        assert(ReplyText().find("Navigation scheduled") != std::string::npos);
    } else if (test == "invalid") {
        for (const char* args : {"{}", "null", "{\"app\":true}", "{\"app\":1}",
                "{\"app\":null}", "{\"app\":\"\"}", "{\"app\":\"voice\"}",
                "{\"app\":\"home\"}", "{\"app\":\"BADGE\"}", "{\"app\":\"123456789\"}"}) {
            app.protocol.sent.clear();
            events.clear();
            Request(args);
            Drain();
            assert((events == std::vector<std::string>{"reply"}));
            assert(board.display.navigation_calls == 0 && app.protocol.sent.size() == 1);
            auto reply = Json(app.protocol.sent.front());
            assert(cJSON_IsObject(Field(reply.get(), "error")));
            assert(!cJSON_GetObjectItemCaseSensitive(reply.get(), "result"));
        }
    } else if (test == "wrappers") {
        board.display.RegisterWakeHandler();
        assert(app.wake_registrations == 1 && app.wake_handler);
        Prepare(board.display, 1, false, true);
        const auto generation = board.display.apps.navigation_generation();
        app.on_main = true;
        assert(app.wake_handler());
        app.on_main = false;
        assert(board.display.apps.navigation_generation() == generation + 1);
        assert(board.display.apps.state_.page() == Page::Voice && !board.display.apps.editing_);
        assert(board.display.policy_calls == 2);
        auto snapshot = Snapshot(board.display.apps);
        assert(app.wake_handler());
        ++std::get<8>(snapshot);
        std::get<17>(snapshot).push_back(true);
        assert(Snapshot(board.display.apps) == snapshot && board.display.policy_calls == 3);
        Drain();
        ClearEffects(board.display);
        board.display.lock_allowed = false;
        assert(!app.wake_handler() && !board.display.ScheduleAppNavigation(1));
        board.display.lock_allowed = true;
        board.display.apps_ = nullptr;
        assert(!app.wake_handler() && !board.display.ScheduleAppNavigation(1));
        assert(board.display.navigation_calls == 0 && app.main_tasks_.empty());
        assert(board.display.lock_depth == 0);
    } else if (test == "schedule_unavailable") {
        for (int reason = 0; reason < 2; ++reason) {
            board.display.lock_allowed = reason != 0;
            board.display.apps_ = reason == 1 ? nullptr : &board.display.apps;
            Request("{\"app\":\"weather\"}");
            Drain();
            assert(board.display.navigation_calls == 0 && app.protocol.sent.size() == 1);
            auto reply = Json(app.protocol.sent.front());
            assert(Text(Field(reply.get(), "error"), "message") == "App UI unavailable");
            assert((events == std::vector<std::string>{"reply"}));
            ClearEffects(board.display);
        }
    } else if (test == "queued_cancel_home" || test == "queued_cancel_wake" ||
               test == "queued_cancel_system" || test == "queued_cancel_open") {
        for (int phase = 0; phase < 2; ++phase) {
            Prepare(board.display, 1, test == "queued_cancel_open", test == "queued_cancel_wake");
            const auto generation = board.display.apps.navigation_generation();
            Request("{\"app\":\"weather\"}");
            app.FlushTurn();  // Tool callback captures generation; no UI mutation yet.
            assert(board.display.navigation_calls == 0 && app.main_tasks_.size() == 2);
            if (phase) {
                app.FlushTurn();  // First hop executes, and the MCP response gets its send turn.
                assert((events == std::vector<std::string>{"reply"}));
            }
            app.on_main = true;  // Model a user/wake/system event before the next schedule batch.
            if (test == "queued_cancel_home") {
                board.display.ShowHome();
            } else if (test == "queued_cancel_wake") {
                assert(board.display.ActivateVoiceByWakeWord());
            } else {
                DisplayLockGuard lock(&board.display);
                if (test == "queued_cancel_system") {
                    board.display.apps.ObserveVoiceState(kDeviceStateUpgrading);
                } else {
                    board.display.apps.OpenSelected();
                }
            }
            app.on_main = false;
            assert(board.display.apps.navigation_generation() == generation + 1);
            const auto snapshot = Snapshot(board.display.apps);
            const int policy_calls = board.display.policy_calls;
            Drain();
            assert(board.display.navigation_calls == 0);  // Skip the actual NavigateToApp entry.
            assert(Snapshot(board.display.apps) == snapshot && board.display.policy_calls == policy_calls);
            assert(board.display.apps.navigation_target_ == -1 && board.display.lock_depth == 0);
            assert(ReplyText().find("Navigation scheduled") != std::string::npos);
            ClearEffects(board.display);
        }
    } else if (test == "queued_lost_lock" || test == "queued_lost_apps") {
        for (int phase = 0; phase < 2; ++phase) {
            Request("{\"app\":\"weather\"}");
            app.FlushTurn();
            if (phase) app.FlushTurn();
            const auto snapshot = Snapshot(board.display.apps);
            if (test == "queued_lost_lock") board.display.lock_allowed = false;
            else board.display.apps_ = nullptr;
            Drain();
            assert(board.display.navigation_calls == 0 && Snapshot(board.display.apps) == snapshot);
            assert((events == std::vector<std::string>{"reply"}));
            assert(ReplyText().find("Navigation scheduled") != std::string::npos);
            board.display.lock_allowed = true;
            board.display.apps_ = &board.display.apps;
            ClearEffects(board.display);
        }
    } else if (test == "queued_voice_wake" || test == "queued_already_open") {
        for (int phase = 0; phase < 2; ++phase) {
            Prepare(board.display, test == "queued_voice_wake" ? 0 : 2);
            Request("{\"app\":\"weather\"}");
            app.FlushTurn();
            if (phase) app.FlushTurn();
            auto snapshot = Snapshot(board.display.apps);
            const auto generation = board.display.apps.navigation_generation();
            if (test == "queued_voice_wake") {
                assert(board.display.ActivateVoiceByWakeWord());
                ++std::get<8>(snapshot);
                std::get<17>(snapshot).push_back(true);
                assert(board.display.apps.navigation_generation() == generation + 1);
                assert(Snapshot(board.display.apps) == snapshot);
                Drain();
                assert(board.display.navigation_calls == 0 && board.display.policy_calls == 1);
                assert(Snapshot(board.display.apps) == snapshot);
                assert(events == (phase ? std::vector<std::string>{"reply", "voice"}
                                        : std::vector<std::string>{"voice", "reply"}));
                ClearEffects(board.display);
                continue;
            } else {
                app.on_main = true;
                {
                    DisplayLockGuard lock(&board.display);
                    assert(board.display.apps.NavigateToApp(2) == Result::AlreadyOpen);
                }
                app.on_main = false;
                --board.display.navigation_calls;  // The explicit no-op call is not the queued one.
            }
            assert(board.display.apps.navigation_generation() == generation);
            assert(Snapshot(board.display.apps) == snapshot);
            Drain();
            assert(board.display.navigation_calls == 1 && board.display.apps.navigation_target_ == 3);
            assert(board.display.apps.state_.page() == Page::Home);
            assert((events == std::vector<std::string>{"reply", "navigate", "policy"}));
            ClearEffects(board.display);
        }
    } else {
        assert(false);
    }
}
'''


CJSON = ROOT / "managed_components/espressif__cjson/cJSON"


@unittest.skipUnless(COMPILER and (CJSON / "cJSON.c").is_file(),
                     "requires C++23 and the managed cJSON source")
class StopwatchNavigationToolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="stopwatch-navigation-tool-test-")
        cls.addClassCleanup(cls.tmp.cleanup)
        directory = pathlib.Path(cls.tmp.name)
        (directory / "mbedtls").mkdir()
        (directory / "esp_system.h").write_text(
            "#pragma once\n#include <cstdlib>\n"
            "[[noreturn]] inline void esp_system_abort(const char*) { std::abort(); }\n",
            encoding="utf-8")
        (directory / "mbedtls/base64.h").write_text(
            "#pragma once\n#include <cstddef>\n#include <cstdlib>\n"
            "inline int mbedtls_base64_encode(unsigned char*, size_t, size_t*, "
            "const unsigned char*, size_t) { std::abort(); }\n", encoding="utf-8")
        board = (BOARD / "m5stack_stopwatch.cc").read_text(encoding="utf-8")
        application = (ROOT / "main/application.cc").read_text(encoding="utf-8")
        application_header = (ROOT / "main/application.h").read_text(encoding="utf-8")
        server = (ROOT / "main/mcp_server.cc").read_text(encoding="utf-8")
        source = ui_source() + CHECKS[:CHECKS.index("int main(int argc, char** argv)")]
        # Instrument method entry without changing its implementation, so queued
        # cancellation must skip NavigateToApp itself, not merely hide its result.
        source = source.replace("    NavigationResult NavigateToApp(int index);",
                                "    std::function<void()> navigation_probe;\n"
                                "    NavigationResult NavigateToApp(int index);")
        source = source.replace("StopwatchApps::NavigateToApp(int index) {",
                                "StopwatchApps::NavigateToApp(int index) {\n"
                                "    if (navigation_probe) navigation_probe();")
        source += MCP_PLATFORM
        # Keep lifecycle fields inert for FIFO tests, with production types,
        # defaults and queue bounds instead of inventing a parallel contract.
        lifecycle_fields = (
            "std::atomic<bool> protocol_closing_", "std::atomic<bool> protocol_opening_",
            "bool protocol_initialization_pending_", "std::atomic<bool> reset_protocol_pending_",
            "static constexpr size_t kMaxDeferredMcpBytes", "std::deque<std::string> deferred_mcp_messages_",
            "size_t deferred_mcp_bytes_",
        )
        source = source.replace("// MCP_LIFECYCLE_FIELDS", "\n".join(
            next(line.strip() for line in application_header.splitlines()
                 if line.strip().startswith(token)) for token in lifecycle_fields))
        source = source.replace("// DISPLAY_WRAPPERS", "\n".join(function(board, signature) for signature in (
            "void ShowHome()", "bool ActivateVoiceByWakeWord()", "bool ScheduleAppNavigation(int index)")))
        source = source.replace("// TOOL_REGISTRATION", function(board, "void InitializeTools()"))
        source = source.replace("// WAKE_REGISTRATION", function(
            function(board, "void SetupUI()"),
            "Application::GetInstance().SetWakeWordActivationHandler(") + ");")
        source += function(application, "void Application::Schedule(")
        source += function(application, "void Application::SendMcpMessage(")
        # Use the production event-loop batch swap/drain, not a synchronous fake
        # Schedule, so nested callbacks really wait until the following turn.
        source += "void Application::FlushTurn() {\nassert(!on_main && !main_tasks_.empty());\n"
        source += "on_main = true; const auto bits = event_group.bits; event_group.bits = 0;\n"
        source += function(application, "if (bits & MAIN_EVENT_SCHEDULE)")
        source += "on_main = false;\n}\n"
        for signature in (
            "McpServer::McpServer()", "McpServer::~McpServer() = default;",
            "void McpServer::AddTool(std::unique_ptr<McpTool> tool)",
            "void McpServer::AddTool(const std::string& name",
            "void McpServer::DoToolCall(", "void McpServer::SendResponse(",
            "void McpServer::ReplyResult(", "void McpServer::ReplyError(int id, int code",
            "void McpServer::ReplyError(int id, const std::string& message",
        ):
            if signature.endswith("= default;"):
                source += signature + "\n"
            else:
                source += function(server, signature)
        source += MCP_CHECKS
        driver = directory / "navigation_tool_driver.cc"
        driver.write_text(source, encoding="utf-8")
        cls.binary = directory / "stopwatch-navigation-tool-test.exe"
        result = subprocess.run(
            [COMPILER, "-std=c++23", "-fno-exceptions", "-Wall", "-Wextra", "-Werror",
             "-I", str(directory), "-I", str(BOARD), "-I", str(ROOT / "main"), "-I", str(CJSON),
             str(driver), "-x", "c++", str(CJSON / "cJSON.c"), "-o", str(cls.binary)],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
        )
        if result.returncode:
            raise AssertionError(result.stdout + result.stderr)

    def run_case(self, name):
        result = subprocess.run([str(self.binary), name], capture_output=True,
                                text=True, encoding="utf-8", errors="replace", timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_voice_visible_app_tool_schema_has_one_required_string_with_eight_byte_limit(self):
        self.run_case("schema")

    def test_badge_settings_and_weather_replies_send_before_two_hop_navigation_and_close_policy(self):
        for name in ("badge", "settings", "weather", "alarm"):
            with self.subTest(app=name):
                self.run_case("fifo_" + name)

    def test_scheduled_result_does_not_claim_target_entry_when_ui_is_already_open_busy_or_unavailable(self):
        for state in ("already_open", "busy", "unavailable"):
            with self.subTest(state=state):
                self.run_case(state)

    def test_missing_wrong_type_voice_unknown_and_overlong_targets_never_schedule_navigation(self):
        self.run_case("invalid")

    def test_registered_wake_handler_and_navigation_wrappers_obey_lock_and_missing_app_guards(self):
        self.run_case("wrappers")

    def test_app_tool_returns_unavailable_error_if_lock_or_app_is_missing_at_capture(self):
        self.run_case("schedule_unavailable")

    def test_home_cancels_queued_navigation_before_first_hop_and_between_hops(self):
        self.run_case("queued_cancel_home")

    def test_successful_wake_cancels_queued_navigation_before_first_hop_and_between_hops(self):
        self.run_case("queued_cancel_wake")

    def test_system_foreground_and_successful_manual_open_invalidate_both_queued_hops(self):
        for action in ("system", "open"):
            with self.subTest(action=action):
                self.run_case("queued_cancel_" + action)

    def test_queued_navigation_rechecks_lock_and_optional_apps_at_second_hop(self):
        for reason in ("lock", "apps"):
            with self.subTest(reason=reason):
                self.run_case("queued_lost_" + reason)

    def test_voice_wake_cancels_both_queued_hops_without_resetting_conversation(self):
        self.run_case("queued_voice_wake")

    def test_already_open_noop_does_not_cancel_a_queued_request(self):
        self.run_case("queued_already_open")
