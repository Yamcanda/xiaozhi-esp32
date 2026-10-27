"""Full-production StopWatch Settings host regressions with native UI/board stubs.

Compile stopwatch_settings.cc, stopwatch_weather_data.cc and their real headers
unchanged. The small LVGL model runs click/slider/timer callbacks, including
retained releases on hidden live widgets, and records widget state, not pixels.
City-row touch dispatch checks ancestor scroll offsets and viewport clipping;
menu dispatch remains semantic, not proof of physical pointer behavior. Application execution, optional
hardware and the existing persistent SetTheme, backlight and codec contracts are
simulated; real NVS, Wi-Fi drivers, rendering and physical buttons still need
device validation. Voice fallthrough belongs to test_stopwatch_apps.py, not this
board-local Settings fixture.
"""
import pathlib
import shutil
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
BOARD = ROOT / "main/boards/m5stack/stopwatch"
COMPILER = shutil.which("c++") or shutil.which("g++")


RUNTIME = r'''
#pragma once
#include <cassert>
#include <cstdint>
#include <deque>
#include <functional>
#include <string>
#include <utility>
#include <vector>

namespace test {
inline bool on_main = false;
inline int event_depth = 0, lock_depth = 0;
struct Write { std::string space, key, value; };
inline std::vector<Write> writes;
inline void RequireMutationContext() {
    assert(on_main && event_depth == 0 && lock_depth > 0);
}
}
'''

APPLICATION = r'''
#pragma once
#include "test_runtime.h"
#include "device_state.h"
class Application {
public:
    DeviceState state = kDeviceStateIdle;
    int state_reads = 0;
    std::deque<std::function<void()>> pending;
    static Application& GetInstance() { static Application app; return app; }
    DeviceState GetDeviceState() {
        test::RequireMutationContext(); ++state_reads; return state;
    }
    void Schedule(std::function<void()> action) { pending.push_back(std::move(action)); }
    void RunOne() {
        assert(!test::on_main && !test::event_depth && !test::lock_depth && !pending.empty());
        auto action = std::move(pending.front());
        pending.pop_front();
        test::on_main = true;
        action();
        test::on_main = false;
        assert(test::lock_depth == 0);
    }
    void Flush() {
        unsigned count = 0;
        while (!pending.empty()) { assert(++count < 32); RunOne(); }
    }
};
'''

SETTINGS = r'''
#pragma once
#include "test_runtime.h"
class Settings {
public:
    explicit Settings(const std::string& space, bool writable = false)
        : space_(space), writable_(writable) {}
    void SetString(const std::string& key, const std::string& value) {
        test::RequireMutationContext();
        assert(writable_);
        test::writes.push_back({space_, key, value});
    }
    void SetInt(const std::string& key, int value) { SetString(key, std::to_string(value)); }
private:
    std::string space_;
    bool writable_;
};
'''

DISPLAY = r'''
#pragma once
#include "settings.h"
class Theme {
public:
    explicit Theme(std::string name) : name_(std::move(name)) {}
    virtual ~Theme() = default;
    std::string name() const { return name_; }
private:
    std::string name_;
};
class Display {
public:
    Theme* current_theme_ = nullptr;
    int theme_sets = 0;
    std::function<void()> changed;
    Theme* GetTheme() { return current_theme_; }
    void SetTheme(Theme* theme) {
        test::RequireMutationContext();
        assert(theme);
        ++theme_sets;
        // Match Display::SetTheme's existing persistent display/theme contract.
        current_theme_ = theme;
        Settings settings("display", true);
        settings.SetString("theme", theme->name());
        // RoundLcdDisplay/StopwatchApps refresh Settings after an external change.
        if (changed) changed();
    }
};
class DisplayLockGuard {
public:
    explicit DisplayLockGuard(Display* display) { assert(display); ++test::lock_depth; }
    ~DisplayLockGuard() { --test::lock_depth; }
    explicit operator bool() const { return true; }
    DisplayLockGuard(const DisplayLockGuard&) = delete;
    DisplayLockGuard& operator=(const DisplayLockGuard&) = delete;
};
'''

THEMES = r'''
#pragma once
#include "display/display.h"
#include <map>
class LvglTheme : public Theme { public: using Theme::Theme; };
class LvglThemeManager {
public:
    LvglTheme light{"light"}, dark{"dark"};
    std::map<std::string, LvglTheme*> themes{{"light", &light}, {"dark", &dark}};
    static LvglThemeManager& GetInstance() { static LvglThemeManager manager; return manager; }
    LvglTheme* GetTheme(const std::string& name) {
        const auto it = themes.find(name);
        return it == themes.end() ? nullptr : it->second;
    }
};
'''

HARDWARE = r'''
#pragma once
#include "settings.h"
#include <algorithm>
class Backlight {
public:
    uint8_t value = 75;
    int calls = 0;
    uint8_t brightness() const { return value; }
    void SetBrightness(uint8_t brightness, bool permanent = false) {
        test::RequireMutationContext();
        ++calls;
        brightness = std::min<uint8_t>(brightness, 100);
        if (brightness == value) return;
        if (permanent) Settings("display", true).SetInt("brightness", brightness);
        // The host applies the target immediately; no PWM/fade timer is emulated.
        value = brightness;
    }
};
class AudioCodec {
public:
    int volume = 70, calls = 0;
    int output_volume() const { return volume; }
    void SetOutputVolume(int value) {
        test::RequireMutationContext();
        ++calls;
        volume = value;
        Settings("audio", true).SetInt("output_volume", value);
    }
};
class Board {
public:
    Backlight backlight;
    AudioCodec codec;
    bool has_backlight = true, has_codec = true;
    static Board& GetInstance() { static Board board; return board; }
    Backlight* GetBacklight() { return has_backlight ? &backlight : nullptr; }
    AudioCodec* GetAudioCodec() { return has_codec ? &codec : nullptr; }
};
'''

WIFI = r'''
#pragma once
#include <string>
class WifiManager {
public:
    bool connected = false, configuring = false;
    std::string ssid = "watch-network", ip = "192.0.2.10";
    std::string ap_ssid = "XiaoZhi-Setup", ap_url = "http://192.0.2.1";
    static WifiManager& GetInstance() { static WifiManager wifi; return wifi; }
    bool IsConnected() const { return connected; }
    bool IsConfigMode() const { return configuring; }
    std::string GetSsid() const { return ssid; }
    std::string GetIpAddress() const { return ip; }
    std::string GetApSsid() const { return ap_ssid; }
    std::string GetApWebUrl() const { return ap_url; }
    // Deliberately no provisioning/driver API: Settings must use its callback.
};
'''

LVGL = r'''
#pragma once
#include "test_runtime.h"
#include <algorithm>
#include <cstdarg>
#include <cstdio>
#include <memory>

// Keep enum-typed flag arguments like LVGL's C++ headers (OR-ing flags yields int).
enum lv_obj_flag_t : uint32_t {
    LV_OBJ_FLAG_SCROLLABLE = 1, LV_OBJ_FLAG_GESTURE_BUBBLE = 2,
    LV_OBJ_FLAG_CLICKABLE = 4, LV_OBJ_FLAG_HIDDEN = 8
};
enum lv_state_t { LV_STATE_PRESSED = 1, LV_STATE_FOCUSED = 2, LV_STATE_DISABLED = 4 };
enum { LV_ALIGN_TOP_MID, LV_ALIGN_CENTER, LV_TEXT_ALIGN_CENTER,
       LV_EVENT_CLICKED, LV_EVENT_VALUE_CHANGED, LV_DIR_VER,
       LV_SCROLLBAR_MODE_ACTIVE, LV_LABEL_LONG_SCROLL_CIRCULAR, LV_LABEL_LONG_WRAP,
       LV_PART_MAIN = 0x100, LV_PART_INDICATOR = 0x200, LV_PART_KNOB = 0x300,
       LV_ANIM_OFF = 0, LV_OPA_COVER = 255, LV_RADIUS_CIRCLE = 32767,
       LV_INDEV_TYPE_POINTER = 1, LV_INDEV_TYPE_KEYPAD = 2 };
using lv_color_t = uint32_t;
inline lv_color_t lv_color_hex(uint32_t value) { return value; }
inline lv_color_t lv_color_black() { return 0; }
inline lv_color_t lv_color_white() { return 0xffffff; }
struct lv_font_t { int line_height; };
#define LV_FONT_DECLARE(name) extern const lv_font_t name
struct lv_obj_t;
struct lv_event_t { lv_obj_t* target; void* user_data; };
using lv_event_cb_t = void (*)(lv_event_t*);
struct Event { lv_event_cb_t callback; int code; void* data; };
struct lv_obj_t {
    lv_obj_t* parent = nullptr;
    std::string kind, text;
    unsigned flags = LV_OBJ_FLAG_SCROLLABLE | LV_OBJ_FLAG_CLICKABLE, state = 0;
    bool alive = true;
    int x = 0, y = 0, width = 0, height = 0, scroll_y = 0, scroll_requests = 0, label_writes = 0;
    const lv_font_t* font = nullptr;
    int long_mode = -1;
    int minimum = 0, maximum = 100, value = 0;
    uint32_t background = 0;
    std::vector<lv_obj_t*> children;
    std::vector<Event> events;
};
inline std::vector<std::unique_ptr<lv_obj_t>> objects;
inline void Live(const lv_obj_t* object) { assert(object && object->alive); }
inline lv_obj_t* MakeObject(lv_obj_t* parent, const char* kind) {
    objects.emplace_back(std::make_unique<lv_obj_t>());
    auto* object = objects.back().get();
    object->parent = parent;
    object->kind = kind;
    if (parent) {
        Live(parent);
        object->flags |= LV_OBJ_FLAG_GESTURE_BUBBLE;
        parent->children.push_back(object);
    }
    return object;
}
inline lv_obj_t* lv_obj_create(lv_obj_t* parent) { return MakeObject(parent, "panel"); }
inline lv_obj_t* lv_button_create(lv_obj_t* parent) { return MakeObject(parent, "button"); }
inline lv_obj_t* lv_label_create(lv_obj_t* parent) { return MakeObject(parent, "label"); }
inline lv_obj_t* lv_slider_create(lv_obj_t* parent) { return MakeObject(parent, "slider"); }
inline void DeleteChildren(lv_obj_t* object) {
    for (auto* child : object->children) { DeleteChildren(child); child->alive = false; }
    object->children.clear();
}
inline void lv_obj_clean(lv_obj_t* object) { Live(object); DeleteChildren(object); }
inline lv_obj_t* lv_obj_get_child(lv_obj_t* object, int index) {
    Live(object); return object->children.at(index);
}
inline void lv_obj_add_flag(lv_obj_t* object, lv_obj_flag_t flag) { Live(object); object->flags |= flag; }
inline void lv_obj_remove_flag(lv_obj_t* object, lv_obj_flag_t flag) { Live(object); object->flags &= ~flag; }
inline void lv_obj_add_state(lv_obj_t* object, lv_state_t state) { Live(object); object->state |= state; }
inline void lv_obj_remove_state(lv_obj_t* object, lv_state_t state) { Live(object); object->state &= ~state; }
inline bool lv_obj_has_state(const lv_obj_t* object, lv_state_t state) { Live(object); return object->state & state; }
inline lv_obj_t* lv_obj_get_parent(const lv_obj_t* object) { Live(object); return object->parent; }
inline bool lv_obj_has_flag(const lv_obj_t* object, lv_obj_flag_t flag) {
    Live(object); return (object->flags & flag) != 0;
}
inline bool Visible(const lv_obj_t* object) {
    Live(object);
    return !(object->flags & LV_OBJ_FLAG_HIDDEN) && (!object->parent || Visible(object->parent));
}
inline void lv_obj_remove_style_all(lv_obj_t* object) { Live(object); }
inline void lv_obj_set_size(lv_obj_t* object, int width, int height) {
    Live(object); object->width = width; object->height = height;
}
inline void lv_obj_set_width(lv_obj_t* object, int width) { Live(object); object->width = width; }
inline void lv_obj_set_height(lv_obj_t* object, int height) { Live(object); object->height = height; }
inline void lv_obj_set_pos(lv_obj_t* object, int x, int y) { Live(object); object->x = x; object->y = y; }
inline void lv_obj_align(lv_obj_t* object, int align, int x, int y) {
    Live(object); Live(object->parent);
    object->x = (object->parent->width - object->width) / 2 + x;
    object->y = (align == LV_ALIGN_CENTER ? (object->parent->height - object->height) / 2 : 0) + y;
}
inline void lv_obj_center(lv_obj_t* object) { lv_obj_align(object, LV_ALIGN_CENTER, 0, 0); }
inline void lv_obj_set_scroll_dir(lv_obj_t* object, int) { Live(object); }
inline void lv_obj_set_scrollbar_mode(lv_obj_t* object, int) { Live(object); }
inline void lv_obj_scroll_to_y(lv_obj_t* object, int y, int) { Live(object); object->scroll_y = y; }
inline void lv_obj_scroll_to_view(lv_obj_t* object, int) {
    Live(object); ++object->scroll_requests;
    auto* parent = object->parent;
    Live(parent);
    if (!(parent->flags & LV_OBJ_FLAG_SCROLLABLE)) return;
    // Model ensure-visible, not unconditional top alignment: an already visible
    // row must not jump, and advancing one row shifts only its clipped portion.
    if (object->y < parent->scroll_y) parent->scroll_y = object->y;
    else if (object->y + object->height > parent->scroll_y + parent->height)
        parent->scroll_y = object->y + object->height - parent->height;
}
struct Rect { int left, top, right, bottom; };
inline Rect ScreenRect(const lv_obj_t* object) {
    Live(object);
    int x = object->x, y = object->y;
    for (auto* parent = object->parent; parent; parent = parent->parent) {
        Live(parent); x += parent->x; y += parent->y - parent->scroll_y;
    }
    return {x, y, x + object->width, y + object->height};
}
inline Rect VisibleRect(const lv_obj_t* object) {
    auto rect = ScreenRect(object);
    for (auto* parent = object->parent; parent; parent = parent->parent) {
        const auto clip = ScreenRect(parent);
        rect = {std::max(rect.left, clip.left), std::max(rect.top, clip.top),
                std::min(rect.right, clip.right), std::min(rect.bottom, clip.bottom)};
    }
    return rect;
}
inline bool HasVisibleHit(const lv_obj_t* object) {
    if (!Visible(object)) return false;
    const auto rect = VisibleRect(object);
    return rect.left < rect.right && rect.top < rect.bottom;
}
inline void lv_obj_set_style_bg_color(lv_obj_t* object, lv_color_t color, int selector) {
    Live(object); if (selector == 0) object->background = color;
}
inline void lv_obj_set_style_text_font(lv_obj_t* object, const lv_font_t* font, int) {
    Live(object); object->font = font;
}
#define STYLE_STUB(name, type) \
    inline void name(lv_obj_t* object, type, int) { Live(object); }
STYLE_STUB(lv_obj_set_style_bg_opa, int)
STYLE_STUB(lv_obj_set_style_text_color, lv_color_t)
STYLE_STUB(lv_obj_set_style_text_align, int)
STYLE_STUB(lv_obj_set_style_radius, int)
STYLE_STUB(lv_obj_set_style_border_color, lv_color_t)
STYLE_STUB(lv_obj_set_style_border_width, int)
STYLE_STUB(lv_obj_set_style_pad_bottom, int)
STYLE_STUB(lv_obj_set_style_pad_all, int)
#undef STYLE_STUB
inline void lv_label_set_text(lv_obj_t* object, const char* text) {
    Live(object); object->text = text; ++object->label_writes;
}
inline void lv_label_set_text_fmt(lv_obj_t* object, const char* format, ...) {
    char buffer[128];
    va_list args; va_start(args, format);
    std::vsnprintf(buffer, sizeof(buffer), format, args); va_end(args);
    lv_label_set_text(object, buffer);
}
inline void lv_label_set_long_mode(lv_obj_t* object, int mode) { Live(object); object->long_mode = mode; }
inline void lv_slider_set_range(lv_obj_t* object, int low, int high) {
    Live(object); object->minimum = low; object->maximum = high;
}
inline void lv_slider_set_value(lv_obj_t* object, int value, int) {
    Live(object); object->value = std::clamp(value, object->minimum, object->maximum);
}
inline int lv_slider_get_value(lv_obj_t* object) { Live(object); return object->value; }
inline void lv_obj_add_event_cb(lv_obj_t* object, lv_event_cb_t callback, int code, void* data) {
    Live(object); object->events.push_back({callback, code, data});
}
inline void* lv_event_get_user_data(lv_event_t* event) { return event->user_data; }
inline void* lv_event_get_target(lv_event_t* event) { return event->target; }
inline bool SendRetainedEvent(lv_obj_t* object, int code) {
    // LVGL retains the pressed live widget through release even if an ancestor
    // was hidden. Do not let the host's hit-testing filter mask stale callbacks.
    Live(object);
    assert(test::lock_depth > 0);
    const auto events = object->events;  // A callback can clean/rebuild the detail page.
    ++test::event_depth;
    for (const auto& event : events) {
        if (event.code == code) { lv_event_t data{object, event.data}; event.callback(&data); }
    }
    --test::event_depth;
    return true;
}
inline bool SendEvent(lv_obj_t* object, int code) {
    Live(object);
    if (!Visible(object) || (object->state & LV_STATE_DISABLED)) return false;
    return SendRetainedEvent(object, code);
}
struct lv_indev_t { int type = LV_INDEV_TYPE_POINTER, waits = 0; bool held = false; };
inline lv_indev_t pointer, keypad{LV_INDEV_TYPE_KEYPAD, 0, false};
inline lv_indev_t* lv_indev_get_next(lv_indev_t* input) {
    return input == nullptr ? &pointer : input == &pointer ? &keypad : nullptr;
}
inline int lv_indev_get_type(lv_indev_t* input) { return input->type; }
inline void lv_indev_wait_release(lv_indev_t* input) { ++input->waits; input->held = false; }
struct lv_timer_t { void (*callback)(lv_timer_t*); void* data; bool paused; };
inline std::vector<std::unique_ptr<lv_timer_t>> timers;
inline lv_timer_t* lv_timer_create(void (*callback)(lv_timer_t*), unsigned period, void* data) {
    assert(period > 0);
    timers.emplace_back(new lv_timer_t{callback, data, false}); return timers.back().get();
}
inline void lv_timer_pause(lv_timer_t* timer) { timer->paused = true; }
inline void lv_timer_resume(lv_timer_t* timer) { timer->paused = false; }
inline void* lv_timer_get_user_data(lv_timer_t* timer) { return timer->data; }
'''

SERVICE = r'''
#include "stopwatch_weather_service.h"
#include "test_runtime.h"
#include <map>

namespace test {
struct WeatherModel {
    stopwatch_weather::Snapshot snapshot;
    std::map<uint32_t, stopwatch_weather::SearchResult> searches;
    uint32_t next_request = 1;
    int snapshot_reads = 0, search_reads = 0, search_calls = 0, cancel_calls = 0;
    int setter_calls = 0, config_writes = 0;
    bool last_automatic = true;
    stopwatch_weather::Location last_location;
    uint32_t last_revision = stopwatch_weather::kAnyRevision;
    std::string query, rejection, write_error;
    std::function<void()> after_set;
} weather;
void Bump() { ++weather.snapshot.version; }
void Complete(uint32_t id, std::initializer_list<stopwatch_weather::Location> locations) {
    auto& search = weather.searches[id];
    search.state = stopwatch_weather::SearchState::Ready;
    search.count = locations.size();
    size_t i = 0;
    for (const auto& location : locations) search.candidates[i++] = {"token-" + location.id, location};
    ++search.version; Bump();
}
void ExternalLocation(bool automatic, const stopwatch_weather::Location& location) {
    assert(test::lock_depth == 0);
    weather.snapshot.automatic = automatic;
    weather.snapshot.location = location;
    ++weather.snapshot.config_revision; Bump();
}
}

struct StopwatchWeatherService::Impl {};
StopwatchWeatherService::StopwatchWeatherService() = default;
StopwatchWeatherService::~StopwatchWeatherService() = default;
stopwatch_weather::Snapshot StopwatchWeatherService::GetSnapshot() const {
    ++test::weather.snapshot_reads; return test::weather.snapshot;
}
uint32_t StopwatchWeatherService::Version() const { return test::weather.snapshot.version; }
uint32_t StopwatchWeatherService::SearchVersion(uint32_t id) const {
    const auto it = test::weather.searches.find(id);
    return it == test::weather.searches.end() ? 0 : it->second.version;
}
stopwatch_weather::SearchResult StopwatchWeatherService::GetSearch(uint32_t id) const {
    ++test::weather.search_reads;
    const auto it = test::weather.searches.find(id);
    return it == test::weather.searches.end() ? stopwatch_weather::SearchResult{} : it->second;
}
stopwatch_weather::Result StopwatchWeatherService::SearchCity(const std::string& query,
                                                            const std::string& country) {
    assert(test::lock_depth > 0);  // Nonblocking queueing is permitted in UI callbacks.
    assert(country.empty());
    ++test::weather.search_calls; test::weather.query = query;
    if (!test::weather.rejection.empty()) return {false, 0, 0, test::weather.rejection, false};
    const uint32_t id = test::weather.next_request++;
    auto& search = test::weather.searches[id];
    search.request_id = id;
    search.config_revision = test::weather.snapshot.config_revision;
    search.version = 1;
    search.state = stopwatch_weather::SearchState::Pending;
    test::Bump();
    return {true, id, search.config_revision, "", true};
}
void StopwatchWeatherService::CancelSearch(uint32_t id) {
    ++test::weather.cancel_calls;
    auto& search = test::weather.searches.at(id);
    search.state = stopwatch_weather::SearchState::Superseded;
    ++search.version; test::Bump();
}
stopwatch_weather::Result StopwatchWeatherService::SetLocation(
        bool automatic, const stopwatch_weather::Location& location, uint32_t revision) {
    // Unlike the preserved brightness/theme contract, checked city NVS must be
    // called on main but outside the display lock and outside any LVGL callback.
    assert(test::on_main && test::event_depth == 0 && test::lock_depth == 0);
    ++test::weather.setter_calls;
    test::weather.last_automatic = automatic;
    test::weather.last_location = location;
    test::weather.last_revision = revision;
    auto& snapshot = test::weather.snapshot;
    if (revision != snapshot.config_revision) return {false, 0, snapshot.config_revision, "conflict", false};
    if (!test::weather.write_error.empty()) return {false, 0, revision, test::weather.write_error, false};
    if (snapshot.automatic != automatic || (!automatic && snapshot.location.id != location.id)) {
        ++test::weather.config_writes; ++snapshot.config_revision;
    }
    snapshot.automatic = automatic;
    if (!automatic) {
        snapshot.location = location;
        snapshot.recent[0] = location;
        snapshot.recent_count = 1;
    }
    test::Bump();
    if (test::weather.after_set) test::weather.after_set();
    return {true, 0, snapshot.config_revision, "", true};
}
'''

HARNESS = r'''
#include "stopwatch_settings.h"
#include "stopwatch_weather_service.h"
#include "weather_test_service.h"
#include "application.h"
#include "board.h"
#include "lvgl_theme.h"
#include "wifi_manager.h"
#include <cstring>
#include <iostream>

extern const lv_font_t font_noto_sans_basic_20_4{20};
extern const lv_font_t font_noto_sans_basic_30_4{30};
extern "C" {
extern const lv_font_t MontserratSemiBold26{26};
extern const lv_font_t CommissionerMedium108{108};
extern const lv_font_t font_stopwatch_weather_20{20};
}

lv_obj_t* Find(lv_obj_t* object, const std::string& kind, const std::string& text = "") {
    if (!Visible(object)) return nullptr;
    if (object->kind == kind && (text.empty() || object->text == text)) return object;
    if (kind == "button" && object->kind == "button" && !object->children.empty() &&
        object->children.front()->text == text) return object;
    for (auto* child : object->children) if (auto* found = Find(child, kind, text)) return found;
    return nullptr;
}
bool IsCityRow(const lv_obj_t* object) {
    return object->kind == "button" && object->width == 310 && object->height == 52 &&
           object->children.size() == 2;
}
struct Fixture {
    Application& app = Application::GetInstance();
    Board& board = Board::GetInstance();
    WifiManager& wifi = WifiManager::GetInstance();
    LvglThemeManager& themes = LvglThemeManager::GetInstance();
    Display display;
    int home_calls = 0, wifi_calls = 0;
    bool wifi_accepts_request = true;
    StopwatchWeatherService weather_service;
    StopwatchSettings settings;
    lv_obj_t* screen = lv_obj_create(nullptr);
    explicit Fixture(bool with_home = true, bool with_wifi = true, bool with_weather = true)
        : settings(&display,
              with_home ? std::function<void()>([this]() {
                  test::RequireMutationContext(); ++home_calls; settings.Close();
              }) : std::function<void()>{},
              with_wifi ? std::function<bool()>([this]() {
                  test::RequireMutationContext(); ++wifi_calls;
                  return wifi_accepts_request;
              }) : std::function<bool()>{}, with_weather ? &weather_service : nullptr) {
        display.current_theme_ = themes.GetTheme("light");
        display.changed = [this]() { settings.RefreshTheme(); };
    }
    void Setup() {
        DisplayLockGuard lock(&display);
        lv_obj_set_size(screen, 466, 466);
        lv_obj_remove_flag(screen, LV_OBJ_FLAG_SCROLLABLE);
        settings.Setup(screen);
    }
    void Open() {
        DisplayLockGuard lock(&display);
        settings.Open();
        // StopwatchApps::RenderPage owns root visibility, not Settings::Open.
        if (settings.root()) lv_obj_remove_flag(settings.root(), LV_OBJ_FLAG_HIDDEN);
    }
    void Start() { Setup(); Open(); }
    void Close() { DisplayLockGuard lock(&display); settings.Close(); }
    void Observe(DeviceState state) {
        assert(!test::on_main);
        app.state = state;
        test::on_main = true;
        { DisplayLockGuard lock(&display); settings.ObserveVoiceState(state); }
        test::on_main = false;
    }
    void Retained(lv_obj_t* object, int code = LV_EVENT_CLICKED) {
        DisplayLockGuard lock(&display); SendRetainedEvent(object, code);
    }
    void Key(int button) {
        assert(!test::on_main);
        test::on_main = true;  // The board dispatcher already schedules physical keys.
        { DisplayLockGuard lock(&display); settings.HandleButton(button); }
        test::on_main = false;
    }
    lv_obj_t* Button(const std::string& text) {
        auto* button = Find(screen, "button", text); assert(button); return button;
    }
    lv_obj_t* Label(const std::string& text) {
        auto* label = Find(screen, "label", text); assert(label); return label;
    }
    lv_obj_t* Slider() { auto* slider = Find(screen, "slider"); assert(slider); return slider; }
    bool Click(const std::string& text) {
        DisplayLockGuard lock(&display);
        auto* button = Button(text);
        // Offscreen list rows are discoverable for semantic assertions, not
        // touchable. Tests must scroll/focus first instead of dispatching them.
        if (IsCityRow(button) && !HasVisibleHit(button)) return false;
        return SendEvent(button, LV_EVENT_CLICKED);
    }
    bool Slide(int value) {
        DisplayLockGuard lock(&display);
        auto* slider = Slider();
        if (slider->state & LV_STATE_DISABLED) return false;
        lv_slider_set_value(slider, value, LV_ANIM_OFF);
        return SendEvent(slider, LV_EVENT_VALUE_CHANGED);
    }
    void Tick() {
        DisplayLockGuard lock(&display);
        for (auto& timer : timers) if (!timer->paused) timer->callback(timer.get());
    }
    void ExternalTheme(const char* name) {
        test::on_main = true;
        { DisplayLockGuard lock(&display); display.SetTheme(themes.GetTheme(name)); }
        test::on_main = false;
    }
    void Value(int expected) {
        assert(Slider()->value == expected); Label(std::to_string(expected));
    }
    void ThemeChoice(const char* selected) {
        assert(Button(selected)->background == 0x4AD78C);
        assert(Button(std::strcmp(selected, "Light") == 0 ? "Dark" : "Light")->background == 0x4C4C4C);
    }
    void NoWrites() {
        assert(test::writes.empty() && !board.backlight.calls && !board.codec.calls);
        assert(!display.theme_sets && !home_calls && !wifi_calls);
    }
    void Written(const char* space, const char* key, const char* value) {
        assert(test::writes.size() == 1);
        assert(test::writes[0].space == space && test::writes[0].key == key &&
               test::writes[0].value == value);
    }
};

void QueueAction(Fixture& f, const std::string& action) {
    if (action == "brightness") { f.Click("Brightness"); f.Slide(83); f.Click("OK"); }
    else if (action == "volume") { f.Click("Volume"); f.Slide(85); f.Click("OK"); }
    else if (action == "theme") { f.Click("Theme"); f.Click("Dark"); f.Click("OK"); }
    else if (action == "wifi") { f.Click("Wi-Fi"); f.Click("Configure"); }
    else if (action == "home") f.Click("Home");
    else assert(false);
    assert(f.app.pending.size() == 1);
    f.NoWrites();
}

void Focused(Fixture& f, const std::string& text) {
    assert(f.Button(text)->state & LV_STATE_FOCUSED);
}
void CheckCityGeometry(Fixture& f) {
    std::function<void(lv_obj_t*)> check = [&](lv_obj_t* object) {
        if (!Visible(object)) return;
        if (object->kind == "button") {
            assert(object->width >= 44 && object->height >= 44);
            if (object->children.size() == 2) {
                const auto* name = object->children[0];
                const auto* identity = object->children[1];
                // Actual weather20/basic20 ASCII glyphs occupy offsets 5..24.
                // Leave space for the focused row's 3px border and descenders.
                assert(name->y + 24 < identity->y + 5);
                assert(3 + identity->y + 24 < object->height);
            }
            // Scroll children use ancestor screen coordinates minus scroll,
            // clipped by their viewport. Keep the circular-display bound check
            // for every visible portion, not raw content coordinates offscreen.
            if (HasVisibleHit(object)) {
                const auto rect = VisibleRect(object);
                for (int x : {rect.left, rect.right})
                    for (int y : {rect.top, rect.bottom}) {
                        const int dx = x - 233, dy = y - 233;
                        assert(dx * dx + dy * dy <= 233 * 233);
                    }
            }
        }
        for (auto* child : object->children) check(child);
    };
    check(f.settings.root());
}
void EqualCity(const stopwatch_weather::Location& actual, const stopwatch_weather::Location& expected) {
    assert(actual.id == expected.id && actual.city == expected.city &&
           actual.display_name == expected.display_name && actual.region == expected.region &&
           actual.country == expected.country && actual.country_code == expected.country_code &&
           actual.latitude == expected.latitude && actual.longitude == expected.longitude);
}
lv_obj_t* CommonViewport(Fixture& f) {
    auto* detail = f.Button("Back")->parent;
    lv_obj_t* viewport = nullptr;
    for (auto* child : detail->children) {
        if (child->kind != "panel") continue;
        assert(viewport == nullptr); viewport = child;
    }
    assert(viewport && viewport->parent == detail && viewport->x == 78 && viewport->y == 112);
    assert(viewport->width == 310 && viewport->height == 270);
    assert(viewport->flags & LV_OBJ_FLAG_SCROLLABLE);
    assert(!(viewport->flags & LV_OBJ_FLAG_GESTURE_BUBBLE));
    assert(!(detail->flags & LV_OBJ_FLAG_SCROLLABLE));
    assert(!(f.settings.root()->flags & LV_OBJ_FLAG_SCROLLABLE));
    return viewport;
}
void CommonRows(Fixture& f, const std::vector<stopwatch_weather::Location>& expected) {
    auto* viewport = CommonViewport(f);
    assert(viewport->children.size() == expected.size() && viewport->scroll_y == 0);
    assert(!Find(f.screen, "button", "Prev") && !Find(f.screen, "button", "Next"));
    for (size_t i = 0; i < expected.size(); ++i) {
        auto* row = viewport->children[i];
        assert(IsCityRow(row) && row->parent == viewport && row->x == 0 && row->y == int(i) * 54);
        assert(row->children[0]->text == expected[i].display_name);
        assert(row->children[0]->font == &font_stopwatch_weather_20);
        assert(bool(row->state & LV_STATE_FOCUSED) == (i == 0));
        assert(row->scroll_requests == 0);
    }
    auto* back = f.Button("Back");
    assert(back->parent == viewport->parent && back->x == 163 && back->y == 390);
    assert(back->width == 140 && back->height == 44 && back->scroll_requests == 0);
    assert(back->parent->scroll_y == 0 && f.settings.root()->scroll_y == 0);
    CheckCityGeometry(f);
}
std::vector<stopwatch_weather::Location> Builtins() {
    std::vector<stopwatch_weather::Location> result;
    for (int i = 0; i < stopwatch_weather::kCityCount; ++i)
        result.push_back(stopwatch_weather::BuiltinLocation(i));
    return result;
}
void FocusRow(Fixture& f, lv_obj_t* row) {
    for (int i = 0; !(row->state & LV_STATE_FOCUSED); ++i) {
        assert(i <= stopwatch_weather::kCityCount + 1); f.Key(1);
    }
    const auto full = ScreenRect(row), clipped = VisibleRect(row);
    assert(HasVisibleHit(row) && full.left == clipped.left && full.top == clipped.top &&
           full.right == clipped.right && full.bottom == clipped.bottom);
}
void TypeCity(Fixture& f) {
    f.Click("A"); f.Click("Page"); f.Click("M"); f.Click("Page"); f.Click("Z");
    f.Click("2"); f.Click("Space"); f.Click(","); f.Click("'"); f.Click("-");
    assert(test::weather.search_calls == 0);
    f.Click("Delete");
    f.Label("AMZ2 ,'");
}
void StartSearch(Fixture& f) {
    f.Click("Weather location"); f.Click("Search city"); f.Click("A"); f.Click("Search");
    assert(test::weather.search_calls == 1);
    f.Label("Searching... Back to cancel");
}
stopwatch_weather::Location NamedCity(const std::string& id, const std::string& name,
                                    const std::string& region, const std::string& country) {
    return {id, "arbitrary native UTF-8", name, region, country, "US", 34, -118};
}

int main(int argc, char** argv) {
    assert(argc == 2);
    const std::string test = argv[1];
    Fixture f(test != "optional_callbacks", test != "optional_callbacks", test != "city_optional_service");
    if (test == "setup_and_menu") {
        f.settings.RefreshTheme(); f.settings.Open(); f.Close(); f.Key(2);
        assert(f.settings.root() == nullptr && f.app.pending.empty());
        f.Setup();
        auto* root = f.settings.root();
        const size_t count = objects.size();
        f.Setup();
        assert(objects.size() == count && timers.size() == 1 && timers[0]->paused);
        assert(root->flags & LV_OBJ_FLAG_HIDDEN);
        assert(!(root->flags & LV_OBJ_FLAG_GESTURE_BUBBLE));
        f.Key(2); assert(f.app.pending.empty());
        f.Open();
        assert(!timers[0]->paused);
        auto* menu = f.Button("Brightness")->parent;
        assert(menu->flags & LV_OBJ_FLAG_SCROLLABLE);
        assert(!(menu->flags & LV_OBJ_FLAG_GESTURE_BUBBLE));
        f.Label("Device"); f.Label("XiaoZhi"); f.Label("Firmware"); f.Label("Light");
        const char* rows[] = {"Brightness", "Volume", "Wi-Fi", "Weather location", "Theme", "Version", "Home"};
        assert(f.Button(rows[0])->state & LV_STATE_FOCUSED);
        f.Key(0); assert(f.Button("Home")->state & LV_STATE_FOCUSED);
        f.Key(1); assert(f.Button("Brightness")->state & LV_STATE_FOCUSED);
        for (int i = 1; i <= 7; ++i) {
            f.Key(1);
            for (int j = 0; j < 7; ++j)
                assert(bool(f.Button(rows[j])->state & LV_STATE_FOCUSED) == (j == i % 7));
        }
        assert(f.Button("Brightness")->scroll_requests > 0);
        f.Key(2); f.Value(75); assert(!Find(f.screen, "button", "Volume"));
        f.NoWrites();
    } else if (test == "menu_clicks_and_version") {
        f.Start();
        f.Click("Version");
        f.Label("Firmware"); f.Label("XiaoZhi / M5Stack StopWatch");
        f.Label("settings-host-1.2.3"); f.Label("v6.1-native-stub");
        f.Click("OK");
        assert(f.Button("Version")->state & LV_STATE_FOCUSED);
        f.Key(2); f.Label("settings-host-1.2.3"); f.Key(0);
        f.Click("Wi-Fi"); f.Label("Not connected\nUse Configure to set up Wi-Fi");
        f.Click("Back"); f.Click("Theme"); f.ThemeChoice("Light");
        assert(f.app.pending.empty()); f.NoWrites();
    } else if (test == "brightness_touch") {
        f.Start(); f.Click("Brightness"); f.Value(75);
        assert(f.Slider()->minimum == 10 && f.Slider()->maximum == 100);
        f.Slide(67); f.Value(67); f.Slide(82); f.Value(82);
        f.NoWrites(); assert(f.app.pending.empty() && f.board.backlight.value == 75);
        f.Click("OK"); assert(f.app.pending.size() == 1); f.NoWrites();
        f.Click("OK"); f.Key(0); f.Key(1); f.Key(2); f.Slide(99);
        f.Label("82"); assert(f.app.pending.size() == 1); f.NoWrites();
        f.app.Flush();
        assert(f.board.backlight.calls == 1 && f.board.backlight.value == 82);
        f.Written("display", "brightness", "82");
        assert(f.Button("Brightness")->state & LV_STATE_FOCUSED);
    } else if (test == "brightness_buttons_bounds") {
        f.Start(); f.Key(2);
        for (int i = 0; i < 120; ++i) f.Key(0);
        f.Value(10);
        for (int i = 0; i < 120; ++i) f.Key(1);
        f.Value(100); f.NoWrites(); assert(f.app.pending.empty());
        f.Key(2); assert(f.app.pending.size() == 1); f.NoWrites();
        f.app.Flush(); f.Written("display", "brightness", "100");
    } else if (test == "brightness_initial_clamp") {
        f.board.backlight.value = 0;
        f.Start(); f.Click("Brightness"); f.Value(10); f.NoWrites();
        f.Click("OK"); f.app.Flush(); f.Written("display", "brightness", "10");
    } else if (test == "volume_touch_and_buttons") {
        f.Start(); f.Key(1); f.Key(2); f.Value(70);
        assert(f.Slider()->minimum == 0 && f.Slider()->maximum == 100);
        f.Slide(63); f.Value(65); f.Slide(62); f.Value(60);
        f.Key(0); f.Value(55); f.Key(1); f.Value(60);
        f.Slide(-1); f.Value(0); f.Slide(101); f.Value(100);
        f.Slide(78); f.Value(80); f.NoWrites(); assert(f.app.pending.empty());
        f.Click("OK"); f.Click("OK"); assert(f.app.pending.size() == 1); f.NoWrites();
        f.app.Flush(); assert(f.board.codec.calls == 1 && f.board.codec.volume == 80);
        f.Written("audio", "output_volume", "80");
    } else if (test == "unchanged_values") {
        f.Start(); f.Click("Brightness"); f.Click("OK"); f.app.Flush();
        assert(f.board.backlight.calls == 1 && test::writes.empty());
        f.Click("Volume"); f.Key(2); f.app.Flush();
        assert(f.board.codec.calls == 0 && test::writes.empty());
        f.Click("Theme"); f.Click("OK"); f.app.Flush();
        assert(f.display.theme_sets == 0 && test::writes.empty());
    } else if (test == "theme_commit") {
        f.Start(); f.Click("Theme"); f.ThemeChoice("Light");
        f.Click("Dark"); f.ThemeChoice("Dark"); f.NoWrites();
        f.Click("OK"); f.Click("Light"); f.Key(0); f.ThemeChoice("Dark");
        assert(f.app.pending.size() == 1); f.NoWrites(); f.app.Flush();
        assert(f.display.GetTheme() == f.themes.GetTheme("dark"));
        f.Written("display", "theme", "dark"); f.Label("Dark");
        f.Click("Theme"); f.ThemeChoice("Dark"); f.Key(0); f.ThemeChoice("Light");
        f.Key(2); assert(test::writes.size() == 1); f.app.Flush();
        assert(f.display.GetTheme() == f.themes.GetTheme("light"));
        assert(f.display.theme_sets == 2 && test::writes.size() == 2);
        assert(test::writes[1].space == "display" && test::writes[1].key == "theme" &&
               test::writes[1].value == "light");
        f.Label("Light");
    } else if (test == "theme_external_sync") {
        f.Start(); f.ExternalTheme("dark"); f.Label("Dark");
        f.Click("Theme"); f.ThemeChoice("Dark");
        f.ExternalTheme("light"); f.ThemeChoice("Light");  // Unedited choice follows MCP.
        f.Click("Dark"); f.ThemeChoice("Dark");
        f.ExternalTheme("dark"); f.ThemeChoice("Dark");
        f.ExternalTheme("light"); f.ThemeChoice("Dark");  // External change cannot erase the edit.
        assert(f.app.pending.empty());
        const size_t writes = test::writes.size();
        f.Click("OK"); f.ExternalTheme("light"); f.ThemeChoice("Dark");
        assert(test::writes.size() == writes + 1 && f.app.pending.size() == 1);
        f.app.Flush();
        assert(f.display.GetTheme() == f.themes.GetTheme("dark"));
        assert(test::writes.size() == writes + 2 && test::writes.back().value == "dark");
        f.Label("Dark");
    } else if (test == "theme_missing") {
        f.display.current_theme_ = nullptr;
        f.Start(); f.Label("Light"); f.Click("Theme"); f.ThemeChoice("Light");
        f.themes.themes.erase("dark");
        f.Click("Dark"); f.Click("OK"); f.app.Flush();
        f.NoWrites(); f.Label("Light");
        f.Click("Theme"); f.Click("OK"); f.app.Flush();
        f.Written("display", "theme", "light");
    } else if (test == "wifi_status") {
        f.Start(); f.Click("Wi-Fi");
        f.Label("Not connected\nUse Configure to set up Wi-Fi");
        f.wifi.connected = true; f.Tick(); f.Label("Connected\nwatch-network\n192.0.2.10");
        f.wifi.ssid = "changed-network"; f.wifi.ip = "198.51.100.2"; f.Tick();
        auto* status = f.Label("Connected\nchanged-network\n198.51.100.2");
        f.wifi.configuring = true; f.Tick();
        f.Label("Setup hotspot\nXiaoZhi-Setup\nhttp://192.0.2.1");
        f.wifi.ap_ssid = "Setup-2"; f.wifi.ap_url = "http://198.51.100.1"; f.Tick();
        f.Label("Setup hotspot\nSetup-2\nhttp://198.51.100.1");
        const int writes = status->label_writes;
        f.Close(); assert(timers[0]->paused); f.wifi.configuring = false; f.Tick();
        assert(status->label_writes == writes);
        f.Open(); f.Tick(); assert(status->label_writes == writes);  // Menu does not poll Wi-Fi.
        f.NoWrites();
    } else if (test == "wifi_callback") {
        f.Start(); f.Click("Wi-Fi"); f.Click("Configure");
        assert(f.app.pending.size() == 1); f.NoWrites();
        f.Click("Configure"); f.Key(2); assert(f.app.pending.size() == 1);
        f.app.Flush(); assert(f.wifi_calls == 1 && test::writes.empty());
        f.Click("Configure"); f.Key(2); f.app.Flush();
        assert(f.wifi_calls == 1);  // Dispatch finished, but state transition is still delayed.
        f.Observe(kDeviceStateIdle); f.Click("Configure"); f.app.Flush();
        assert(f.wifi_calls == 1);  // Unrelated/repeated Idle cannot release the in-flight latch.
        f.Observe(kDeviceStateWifiConfiguring); f.Observe(kDeviceStateIdle);
        f.Key(2); assert(f.wifi_calls == 1 && f.app.pending.size() == 1);
        f.app.Flush(); assert(f.wifi_calls == 2);
        f.Key(0); assert(f.Button("Wi-Fi")->state & LV_STATE_FOCUSED);
    } else if (test == "wifi_pending_navigation") {
        f.Start(); f.Click("Wi-Fi"); f.Click("Configure"); f.app.Flush();
        assert(f.wifi_calls == 1);
        f.Click("Back"); f.Click("Wi-Fi"); f.Click("Configure"); f.Key(2); f.app.Flush();
        assert(f.wifi_calls == 1);  // Back/reentry must not reset an invoked request.
        f.Close(); f.Open(); f.Click("Wi-Fi"); f.Click("Configure"); f.Key(2); f.app.Flush();
        assert(f.wifi_calls == 1);  // Neither does leaving/reopening Settings.
        for (DeviceState state : {kDeviceStateUnknown, kDeviceStateStarting,
                kDeviceStateConnecting, kDeviceStateListening, kDeviceStateSpeaking,
                kDeviceStateNotifying, kDeviceStateActivating, kDeviceStateAudioTesting,
                kDeviceStateIdle}) f.Observe(state);
        f.Click("Configure"); f.app.Flush(); assert(f.wifi_calls == 1);
        assert(test::writes.empty());
    } else if (test == "wifi_callback_rejection_retry") {
        f.wifi_accepts_request = false;
        f.Start(); f.Click("Wi-Fi"); f.Click("Configure");
        f.Click("Configure"); f.Key(2); assert(f.app.pending.size() == 1);
        f.app.Flush(); assert(f.wifi_calls == 1 && f.app.pending.empty());
        // A rejected board request (for example task allocation failure) cannot
        // produce a terminal observation; another request must work immediately.
        f.Key(2); assert(f.app.pending.size() == 1 && f.wifi_calls == 1);
        f.app.Flush(); assert(f.wifi_calls == 2 && f.app.pending.empty());
        f.wifi_accepts_request = true;
        f.Click("Configure"); assert(f.app.pending.size() == 1);
        f.app.Flush(); assert(f.wifi_calls == 3);
        f.Click("Configure"); f.Key(2); f.app.Flush();
        assert(f.wifi_calls == 3 && f.app.state == kDeviceStateIdle && test::writes.empty());
    } else if (test == "wifi_rejected_retry_navigation") {
        f.wifi_accepts_request = false;
        f.Start(); f.Click("Wi-Fi"); f.Click("Configure"); f.app.Flush();
        assert(f.wifi_calls == 1);
        f.Click("Back"); f.Click("Wi-Fi"); f.Click("Configure"); f.app.Flush();
        assert(f.wifi_calls == 2);  // Back/reentry after rejection permits a retry.
        f.Close(); f.Open(); f.Click("Wi-Fi"); f.Click("Configure"); f.app.Flush();
        assert(f.wifi_calls == 3);  // So does Close/Open, without any state observation.
        f.Click("Configure"); assert(f.app.pending.size() == 1);
        f.Click("Back"); f.Click("Wi-Fi"); f.app.Flush();
        assert(f.wifi_calls == 3);  // A queued retry abandoned via Back never invokes the board.
        f.Click("Configure"); assert(f.app.pending.size() == 1);
        f.Close(); f.Open(); f.Click("Wi-Fi");
        f.wifi_accepts_request = true;
        f.Click("Configure"); assert(f.app.pending.size() == 2);
        f.app.RunOne(); assert(f.wifi_calls == 3 && f.app.pending.size() == 1);
        f.Click("Configure"); f.Key(2); assert(f.app.pending.size() == 1);
        f.app.Flush(); assert(f.wifi_calls == 4);  // Only the current accepted retry executes.
        f.Click("Back"); f.Click("Wi-Fi"); f.Click("Configure"); f.app.Flush();
        f.Close(); f.Open(); f.Click("Wi-Fi"); f.Click("Configure"); f.app.Flush();
        assert(f.wifi_calls == 4 && f.app.state == kDeviceStateIdle && test::writes.empty());
    } else if (test == "wifi_observed_completion") {
        f.Start(); f.Click("Wi-Fi"); f.Click("Configure"); f.app.Flush();
        f.Close(); f.Observe(kDeviceStateWifiConfiguring);  // Observation must work while inactive.
        f.Open(); f.Click("Wi-Fi"); f.Click("Configure"); f.app.Flush();
        assert(f.wifi_calls == 1);  // Configuring state blocks a second driver request.
        f.wifi.configuring = true; f.Observe(kDeviceStateIdle);
        f.Click("Configure"); f.app.Flush(); assert(f.wifi_calls == 1);
        f.wifi.configuring = false;
        f.Click("Configure"); assert(f.app.pending.size() == 1); f.app.Flush();
        assert(f.wifi_calls == 2 && test::writes.empty());
    } else if (test == "wifi_allowed_states") {
        f.Start(); f.Click("Wi-Fi");
        const DeviceState allowed[] = {kDeviceStateStarting, kDeviceStateIdle,
            kDeviceStateListening, kDeviceStateSpeaking, kDeviceStateNotifying};
        int expected = 0;
        for (DeviceState state : allowed) {
            f.app.state = state;
            f.Click("Configure"); assert(f.app.pending.size() == 1);
            assert(f.wifi_calls == expected);
            f.app.Flush(); assert(f.wifi_calls == ++expected);
            f.Observe(kDeviceStateWifiConfiguring); f.Observe(kDeviceStateIdle);
        }
        assert(f.app.state_reads >= expected && test::writes.empty());
    } else if (test == "wifi_rejected_states_retry") {
        f.Start(); f.Click("Wi-Fi");
        const DeviceState rejected[] = {kDeviceStateUnknown, kDeviceStateWifiConfiguring,
            kDeviceStateConnecting, kDeviceStateUpgrading, kDeviceStateActivating,
            kDeviceStateAudioTesting, kDeviceStateFatalError};
        int expected = 0;
        for (DeviceState state : rejected) {
            f.app.state = kDeviceStateIdle;
            f.Click("Configure"); assert(f.app.pending.size() == 1);
            f.app.state = state;  // Must re-check at deferred execution, not only at click.
            f.app.Flush(); assert(f.wifi_calls == expected);
            f.app.state = kDeviceStateIdle;
            f.Click("Configure"); assert(f.app.pending.size() == 1);
            f.app.Flush(); assert(f.wifi_calls == ++expected);  // Rejection never latches.
            f.Observe(kDeviceStateWifiConfiguring); f.Observe(kDeviceStateIdle);
        }
        assert(test::writes.empty());
    } else if (test == "wifi_config_mode_rejection") {
        f.Start(); f.Click("Wi-Fi"); f.Click("Configure");
        assert(f.app.pending.size() == 1);
        f.wifi.configuring = true; f.app.Flush(); f.NoWrites();
        f.wifi.configuring = false;
        f.Click("Configure"); assert(f.app.pending.size() == 1); f.app.Flush();
        assert(f.wifi_calls == 1 && test::writes.empty());
    } else if (test == "wifi_system_observations_release_latch") {
        f.Start(); f.Click("Wi-Fi");
        int expected = 0;
        for (DeviceState state : {kDeviceStateUpgrading, kDeviceStateFatalError}) {
            f.Click("Configure"); f.app.Flush(); assert(f.wifi_calls == ++expected);
            f.Close(); f.Observe(state); f.Open(); f.Click("Wi-Fi");
            f.Click("Configure"); f.app.Flush(); assert(f.wifi_calls == expected);
            f.Observe(kDeviceStateIdle);
            f.Click("Configure"); f.app.Flush(); assert(f.wifi_calls == ++expected);
            f.Observe(kDeviceStateWifiConfiguring); f.Observe(kDeviceStateIdle);
        }
        assert(test::writes.empty());
    } else if (test == "wifi_stale_closed_request_never_latches") {
        f.Start(); QueueAction(f, "wifi"); f.Close(); f.Open(); f.Click("Wi-Fi");
        f.app.Flush(); f.NoWrites();
        f.Click("Configure"); assert(f.app.pending.size() == 1); f.app.Flush();
        assert(f.wifi_calls == 1 && test::writes.empty());
    } else if (test == "wifi_stale_back_request_never_latches") {
        f.Start(); QueueAction(f, "wifi"); f.Click("Back"); f.Click("Wi-Fi");
        f.app.Flush(); f.NoWrites();
        f.Click("Configure"); assert(f.app.pending.size() == 1); f.app.Flush();
        assert(f.wifi_calls == 1 && test::writes.empty());
    } else if (test == "home_callback") {
        f.Start(); f.Key(0); f.Key(2); f.NoWrites(); assert(f.app.pending.size() == 1);
        f.Click("Home"); f.Key(2); assert(f.app.pending.size() == 1);
        f.app.Flush(); assert(f.home_calls == 1 && !Visible(f.settings.root()));
        assert(timers[0]->paused && test::writes.empty());
        f.Key(1); assert(f.app.pending.empty());
        f.Open(); assert(f.Button("Brightness")->state & LV_STATE_FOCUSED);
    } else if (test == "optional_controls") {
        f.board.has_backlight = f.board.has_codec = false;
        f.Start(); f.Click("Brightness"); f.Label("Not available");
        assert(f.Slider()->state & LV_STATE_DISABLED);
        assert(f.Button("OK")->state & LV_STATE_DISABLED);
        assert(!f.Slide(90) && !f.Click("OK") && f.app.pending.empty());
        f.Key(0); f.Key(1); f.Key(2); f.app.Flush();
        f.Click("Volume"); f.Label("Not available");
        assert(!f.Click("OK") && !f.Slide(90));
        f.Key(1); f.Key(2); f.app.Flush(); f.NoWrites();
    } else if (test == "controls_removed_before_commit") {
        f.Start(); f.Click("Brightness"); f.Slide(90); f.Click("OK");
        f.board.has_backlight = false; f.app.Flush();
        f.Click("Volume"); f.Slide(90); f.Click("OK");
        f.board.has_codec = false; f.app.Flush(); f.NoWrites();
    } else if (test == "optional_callbacks") {
        f.Start(); f.Click("Wi-Fi");
        assert(f.Button("Configure")->state & LV_STATE_DISABLED);
        assert(!f.Click("Configure")); f.Key(2); assert(f.app.pending.empty());
        f.Click("Back"); f.Click("Home"); f.app.Flush(); f.NoWrites();
        assert(Visible(f.settings.root()));
    } else if (test == "reopen_resets") {
        f.Start(); auto* menu = f.Button("Brightness")->parent;
        f.Key(1); f.Key(1); f.Key(1); assert(menu->scroll_y > 0);
        f.Click("Brightness"); f.Slide(99); f.Close(); f.NoWrites();
        f.board.backlight.value = 43;
        f.Open(); assert(menu->scroll_y == 0);
        assert(f.Button("Brightness")->state & LV_STATE_FOCUSED);
        f.Key(2); f.Value(43); f.Close();
        f.board.codec.volume = 25;
        f.Open(); f.Click("Volume"); f.Value(25);
        assert(f.app.pending.empty()); f.NoWrites();
    } else if (test == "retained_theme_reopen") {
        f.Start(); f.Click("Theme");
        auto* light = f.Button("Light"); auto* dark = f.Button("Dark");
        f.Close(); f.Open();  // The old theme widgets are live, but their detail is hidden.
        f.Retained(light); f.Retained(dark);
        assert(f.app.pending.empty()); f.NoWrites();
        assert(f.Button("Brightness")->state & LV_STATE_FOCUSED);
        assert(light->background == 0x4AD78C && dark->background == 0x4C4C4C);
        f.Click("Theme"); f.ThemeChoice("Light");
    } else if (test == "retained_theme_inactive") {
        f.Start(); f.Click("Theme");
        auto* light = f.Button("Light"); auto* dark = f.Button("Dark");
        f.Close(); f.Retained(light); f.Retained(dark);
        assert(light->background == 0x4AD78C && dark->background == 0x4C4C4C);
        assert(!Visible(f.settings.root()) && f.app.pending.empty()); f.NoWrites();
        f.Open(); f.Click("Theme"); f.ThemeChoice("Light");
    } else if (test == "retained_menu_on_detail") {
        f.Start();
        std::vector<lv_obj_t*> held;
        for (const char* row : {"Brightness", "Volume", "Wi-Fi", "Weather location", "Theme", "Version", "Home"})
            held.push_back(f.Button(row));
        f.Click("Brightness"); f.Slide(88);
        auto* slider = f.Slider();
        for (auto* button : held) {
            f.Retained(button);  // Hidden menu cannot navigate away from the current detail.
            assert(slider->alive && f.Slider() == slider); f.Value(88);
            assert(f.app.pending.empty());
        }
        f.NoWrites();
    } else if (test == "retained_menu_inactive") {
        f.Start(); auto* held = f.Button("Home"); f.Close(); f.Retained(held);
        assert(f.app.pending.empty() && !Visible(f.settings.root())); f.NoWrites();
        f.Open(); assert(f.Button("Brightness")->state & LV_STATE_FOCUSED);
    } else if (test == "retained_hidden_root_menu") {
        f.Start(); auto* held = f.Button("Volume");
        lv_obj_add_flag(f.settings.root(), LV_OBJ_FLAG_HIDDEN);
        f.Retained(held);  // active_ alone is insufficient when the owner hides its root.
        lv_obj_remove_flag(f.settings.root(), LV_OBJ_FLAG_HIDDEN);
        assert(f.Button("Brightness")->state & LV_STATE_FOCUSED);
        assert(f.app.pending.empty()); f.NoWrites();
    } else if (test == "retained_wifi_controls") {
        f.Start(); f.Click("Wi-Fi");
        auto* configure = f.Button("Configure"); auto* back = f.Button("Back");
        f.Click("Back");
        f.Retained(configure); assert(f.app.pending.empty()); f.NoWrites();
        f.Click("Home"); assert(f.app.pending.size() == 1);
        f.Retained(back);  // A hidden old Back must not invalidate the newer Home generation.
        assert(f.app.pending.size() == 1); f.app.Flush();
        assert(f.home_calls == 1 && !f.wifi_calls && test::writes.empty());
    } else if (test == "retained_wifi_inactive") {
        f.Start(); f.Click("Wi-Fi");
        auto* configure = f.Button("Configure"); auto* back = f.Button("Back");
        f.Close(); f.Retained(configure); f.Retained(back);
        assert(f.app.pending.empty() && !Visible(f.settings.root())); f.NoWrites();
        f.Open(); assert(f.Button("Brightness")->state & LV_STATE_FOCUSED);
    } else if (test == "retained_about_ok") {
        f.Start(); f.Click("Version"); auto* held = f.Button("OK");
        f.Click("OK"); f.Click("Home"); assert(f.app.pending.size() == 1);
        f.Retained(held); f.app.Flush();
        assert(f.home_calls == 1 && test::writes.empty());
    } else if (test == "retained_value_controls") {
        f.Start(); f.Click("Brightness"); f.Slide(84);
        auto* slider = f.Slider(); auto* ok = f.Button("OK"); auto* label = f.Label("84");
        f.Click("OK"); f.app.Flush(); f.Written("display", "brightness", "84");
        const int label_writes = label->label_writes;
        f.Retained(ok); f.Retained(slider, LV_EVENT_VALUE_CHANGED);
        assert(f.app.pending.empty() && label->label_writes == label_writes);
        assert(f.Button("Brightness")->state & LV_STATE_FOCUSED);
        f.Close(); f.Open();
        f.Retained(ok); f.Retained(slider, LV_EVENT_VALUE_CHANGED);
        assert(f.app.pending.empty() && label->label_writes == label_writes);
        assert(f.board.backlight.calls == 1 && test::writes.size() == 1);
    } else if (test == "retained_hidden_root_detail") {
        f.Start(); f.Click("Theme");
        auto* dark = f.Button("Dark"); auto* ok = f.Button("OK");
        lv_obj_add_flag(f.settings.root(), LV_OBJ_FLAG_HIDDEN);
        f.Retained(dark); f.Retained(ok);
        assert(dark->background == 0x4C4C4C && f.app.pending.empty()); f.NoWrites();
        lv_obj_remove_flag(f.settings.root(), LV_OBJ_FLAG_HIDDEN); f.ThemeChoice("Light");
    } else if (test == "retained_hidden_widget") {
        f.Start(); f.Click("Theme"); auto* dark = f.Button("Dark");
        lv_obj_add_flag(dark, LV_OBJ_FLAG_HIDDEN); f.Retained(dark);
        assert(dark->background == 0x4C4C4C && f.app.pending.empty()); f.NoWrites();
        lv_obj_remove_flag(dark, LV_OBJ_FLAG_HIDDEN); f.ThemeChoice("Light");
    } else if (test.compare(0, 6, "close_") == 0) {
        f.Start(); QueueAction(f, test.substr(6));
        f.Close(); f.Close(); f.Key(2); f.app.Flush(); f.NoWrites();
        assert(!Visible(f.settings.root()) && timers[0]->paused);
        f.Open(); f.Click("Brightness"); f.Slide(84); f.Click("OK"); f.app.Flush();
        f.Written("display", "brightness", "84");
    } else if (test == "reopen_invalidates_pending") {
        f.Start(); QueueAction(f, "brightness"); f.Close();
        f.board.backlight.value = 40;
        f.Open(); f.Key(2); f.Value(40); f.Key(1); f.Value(41); f.Key(2);
        assert(f.app.pending.size() == 2);
        f.app.RunOne(); f.NoWrites(); assert(f.app.pending.size() == 1);
        f.Key(1); f.Value(41); f.Click("OK"); assert(f.app.pending.size() == 1);
        f.app.Flush(); f.Written("display", "brightness", "41");
    } else if (test == "home_then_row_supersedes") {
        f.Start(); f.Click("Home"); assert(f.app.pending.size() == 1);
        // The menu is still visible while Home awaits the application task.
        f.Click("Brightness"); f.Slide(86); f.Value(86); f.Click("OK");
        assert(f.app.pending.size() == 2); f.NoWrites();
        f.app.RunOne(); f.NoWrites(); assert(f.app.pending.size() == 1);
        f.Key(0); f.Value(86); f.Click("OK"); assert(f.app.pending.size() == 1);
        f.app.Flush(); f.Written("display", "brightness", "86");
        assert(f.home_calls == 0 && Visible(f.settings.root()));
    } else if (test == "wifi_back_invalidates_pending") {
        f.Start(); QueueAction(f, "wifi"); f.Click("Back");
        f.Click("Brightness"); f.Slide(87); f.Click("OK");
        assert(f.app.pending.size() == 2); f.app.RunOne(); f.NoWrites();
        f.Key(1); f.Value(87); assert(f.app.pending.size() == 1);
        f.app.Flush(); f.Written("display", "brightness", "87"); assert(!f.wifi_calls);
    } else if (test == "wifi_reenter_invalidates_pending") {
        f.Start(); QueueAction(f, "wifi"); f.Click("Back");
        f.Click("Wi-Fi"); f.Click("Configure"); assert(f.app.pending.size() == 2);
        f.app.RunOne(); f.NoWrites(); assert(f.app.pending.size() == 1);
        f.Click("Configure"); assert(f.app.pending.size() == 1);
        f.app.Flush(); assert(f.wifi_calls == 1 && test::writes.empty());
    } else if (test == "open_invalidates_pending") {
        f.Start(); QueueAction(f, "home"); f.Open();
        assert(f.Button("Brightness")->state & LV_STATE_FOCUSED);
        f.app.Flush(); f.NoWrites();
        f.Click("Home"); f.app.Flush(); assert(f.home_calls == 1);
    } else if (test == "city_optional_service") {
        f.Start(); f.Click("Weather location"); f.Label("Location service unavailable");
        assert(f.Button("Automatic (IP)")->state & LV_STATE_DISABLED);
        assert(f.Button("Common cities")->state & LV_STATE_DISABLED);
        assert(f.Button("Recent cities (3)")->state & LV_STATE_DISABLED);
        assert(f.Button("Search city")->state & LV_STATE_DISABLED);
        assert(!f.Click("Automatic (IP)")); f.Key(2);
        assert(f.app.pending.empty()); f.Click("Back");
        Focused(f, "Weather location"); f.NoWrites();
    } else if (test.compare(0, 21, "city_current_builtin_") == 0) {
        const auto suffix = test.substr(21);
        const auto split = suffix.find('_');
        const bool automatic = suffix.substr(0, split) == "auto";
        const int index = std::stoi(suffix.substr(split + 1));
        const auto current = stopwatch_weather::BuiltinLocation(index);
        test::weather.snapshot.automatic = automatic;
        test::weather.snapshot.location = current;
        f.Start(); f.Click("Weather location"); f.Click("Common cities");
        std::vector<stopwatch_weather::Location> expected{current};
        for (int i = 0; i < stopwatch_weather::kCityCount; ++i)
            if (i != index) expected.push_back(stopwatch_weather::BuiltinLocation(i));
        CommonRows(f, expected); Focused(f, current.display_name);
        assert(f.Click(current.display_name)); f.Click("OK"); f.app.Flush();
        EqualCity(test::weather.last_location, current);
        assert(!test::weather.last_automatic && test::weather.setter_calls == 1);
        assert(test::weather.config_writes == (automatic ? 1 : 0)); f.NoWrites();
    } else if (test == "city_current_custom_auto" || test == "city_current_custom_manual") {
        const auto current = NamedCity("ip:34,-118", "Los Angeles", "California", "United States");
        test::weather.snapshot.automatic = test == "city_current_custom_auto";
        test::weather.snapshot.location = current;
        f.Start(); f.Click("Weather location"); f.Click("Common cities");
        auto expected = Builtins(); expected.insert(expected.begin(), current);
        CommonRows(f, expected); Focused(f, "Los Angeles");
        assert(f.Click("Los Angeles")); f.Label("California / United States / ip:34,-118");
        f.Click("OK"); f.app.Flush(); EqualCity(test::weather.last_location, current);
        assert(!test::weather.last_automatic && !test::weather.search_calls); f.NoWrites();
    } else if (test.compare(0, 11, "city_alias_") == 0) {
        const auto variant = test.substr(11);
        const auto builtin = stopwatch_weather::BuiltinLocation(0);
        auto current = builtin;
        current.id = "ip:39.91,116.42";
        current.display_name = "Current Beijing";
        current.region = "Provider region"; current.country = "Provider country";
        current.latitude += 0.1; current.longitude += 0.1;
        bool dedup = true;
        if (variant == "chinese") current.city = builtin.display_name;
        else if (variant == "inside") {
            current.latitude = builtin.latitude + 0.499;
            current.longitude = builtin.longitude - 0.499;
        } else if (variant == "name_guard") {
            current.city = "Other city"; current.display_name = builtin.display_name; dedup = false;
        } else if (variant == "country_guard") { current.country_code = "US"; dedup = false; }
        else if (variant == "latitude_guard") { current.latitude = builtin.latitude + 0.5; dedup = false; }
        else if (variant == "longitude_guard") { current.longitude = builtin.longitude - 0.5; dedup = false; }
        else assert(variant == "ascii");
        assert(stopwatch_weather::ValidLocation(current));
        test::weather.snapshot.location = current;
        f.Start(); f.Click("Weather location"); f.Click("Common cities");
        auto expected = Builtins();
        if (dedup) expected.erase(expected.begin());
        expected.insert(expected.begin(), current); CommonRows(f, expected);
        // Select the actual first object, including a duplicate display alias,
        // and check the service receives every original provider field intact.
        f.Key(2); f.Label("Provider region / Provider country / ip:39.91,116.42");
        f.Click("OK"); f.app.Flush(); EqualCity(test::weather.last_location, current);
        assert(test::weather.setter_calls == 1 && !test::weather.last_automatic); f.NoWrites();
    } else if (test.compare(0, 21, "city_invalid_current_") == 0) {
        const auto variant = test.substr(21);
        auto current = stopwatch_weather::BuiltinLocation(1);
        if (variant == "empty") current = {};
        else if (variant == "id") current.id.clear();
        else if (variant == "city") current.city.clear();
        else if (variant == "display") current.display_name = "unsupported native alias: \xE6\x9D\xB1";
        else if (variant == "country") current.country_code = "cn";
        else if (variant == "latitude") current.latitude = 90.1;
        else if (variant == "longitude") current.longitude = -180.1;
        else if (variant == "nan") current.latitude = std::numeric_limits<double>::quiet_NaN();
        else assert(false);
        assert(!stopwatch_weather::ValidLocation(current));
        test::weather.snapshot.location = current;
        f.Start(); f.Click("Weather location"); f.Click("Common cities");
        CommonRows(f, Builtins()); Focused(f, "北京"); f.Key(2);
        f.Label("北京"); f.Click("OK"); f.app.Flush();
        EqualCity(test::weather.last_location, stopwatch_weather::BuiltinLocation(0)); f.NoWrites();
    } else if (test == "city_common_wrap_12" || test == "city_common_wrap_13") {
        auto expected = Builtins();
        if (test == "city_common_wrap_13") {
            auto current = NamedCity("ip:34,-118", "Los Angeles", "California", "United States");
            test::weather.snapshot.location = current; expected.insert(expected.begin(), current);
        }
        f.Start(); f.Click("Weather location"); f.Click("Common cities");
        CommonRows(f, expected);
        auto* viewport = CommonViewport(f); auto* detail = viewport->parent;
        const auto rows = viewport->children; auto* back = f.Button("Back");
        const auto back_rect = ScreenRect(back);
        auto isolated = [&]() {
            assert(f.settings.root()->scroll_y == 0 && detail->scroll_y == 0);
            const auto rect = ScreenRect(back);
            assert(rect.left == back_rect.left && rect.top == back_rect.top &&
                   rect.right == back_rect.right && rect.bottom == back_rect.bottom);
            assert(back->scroll_requests == 0); CheckCityGeometry(f);
        };
        // A wraps to fixed Back without scrolling the viewport, then reaches
        // every row in reverse and brings only that row into view.
        f.Key(0); Focused(f, "Back"); assert(viewport->scroll_y == 0); isolated();
        for (int i = int(rows.size()) - 1; i >= 0; --i) {
            f.Key(0); assert(rows[i]->state & LV_STATE_FOCUSED);
            const auto rect = ScreenRect(rows[i]), clip = VisibleRect(rows[i]);
            assert(rect.top == clip.top && rect.bottom == clip.bottom && HasVisibleHit(rows[i]));
            isolated();
        }
        assert(viewport->scroll_y == 0);
        f.Key(0); Focused(f, "Back"); f.Key(1); Focused(f, expected[0].display_name);
        // B reaches every row and Back, then wraps to the first. Ensure-visible
        // leaves the first five rows at scroll=0, rather than top-aligning them.
        for (size_t i = 1; i < rows.size(); ++i) {
            f.Key(1); assert(rows[i]->state & LV_STATE_FOCUSED);
            const auto rect = ScreenRect(rows[i]), clip = VisibleRect(rows[i]);
            assert(rect.top == clip.top && rect.bottom == clip.bottom && HasVisibleHit(rows[i]));
            assert(viewport->scroll_y == std::max(0, int(i) * 54 + 52 - 270)); isolated();
        }
        const int bottom = viewport->scroll_y;
        f.Key(1); Focused(f, "Back"); assert(viewport->scroll_y == bottom); isolated();
        f.Key(1); Focused(f, expected[0].display_name); assert(viewport->scroll_y == 0); isolated();
        assert(test::weather.setter_calls == 0 && test::weather.search_calls == 0); f.NoWrites();
    } else if (test == "city_common_touch_preserves_scroll") {
        f.Start(); f.Click("Weather location"); f.Click("Common cities");
        auto* viewport = CommonViewport(f); auto* row = viewport->children[2];
        assert(!f.Click("台北") && viewport->scroll_y == 0);
        { DisplayLockGuard lock(&f.display); lv_obj_scroll_to_y(viewport, 120, LV_ANIM_OFF); }
        assert(ScreenRect(row).top < VisibleRect(row).top && HasVisibleHit(row));
        CheckCityGeometry(f);
        // A partially visible row can be hit inside its visible clipped area.
        // Touch must not call ensure-visible before replacing the old page.
        assert(f.Click("广州"));
        assert(!viewport->alive && viewport->scroll_y == 120 && row->scroll_requests == 0);
        f.Label("广州"); f.Click("OK"); f.app.Flush();
        EqualCity(test::weather.last_location, stopwatch_weather::BuiltinLocation(2)); f.NoWrites();
    } else if (test == "city_common_manual_and_auto") {
        f.Start(); f.Click("Weather location"); CheckCityGeometry(f);
        f.Label("Automatic (IP)"); f.Label("Not located");
        assert(f.Button("Recent cities (3)")->state & LV_STATE_DISABLED);
        f.Click("Common cities"); CheckCityGeometry(f);
        auto* name = f.Label("北京");
        assert(name->font == &font_stopwatch_weather_20);
        f.Click("北京"); CheckCityGeometry(f);
        f.Label("Not saved - OK to confirm");
        assert(test::weather.setter_calls == 0 && f.app.pending.empty());
        f.Click("OK");
        f.Label("Saving configuration...");
        assert(test::weather.setter_calls == 0 && f.app.pending.size() == 1);
        f.Click("OK"); f.Key(2); assert(f.app.pending.size() == 1);
        f.app.Flush();
        assert(test::weather.setter_calls == 1 && test::weather.config_writes == 1);
        assert(!test::weather.snapshot.automatic && test::weather.snapshot.location.id == "builtin:Beijing");
        f.Label("Configuration saved - weather may be pending"); f.Label("Manual"); f.Label("北京");
        f.Click("Automatic (IP)"); f.Label("Use approximate public IP location");
        f.Label("Not saved - OK to confirm");
        f.Click("OK"); f.app.Flush();
        assert(test::weather.snapshot.automatic && test::weather.config_writes == 2);
        assert(test::weather.setter_calls == 2 && test::weather.search_calls == 0);
        f.Label("Configuration saved - weather may be pending"); f.NoWrites();
    } else if (test == "city_common_scroll_and_recent") {
        test::weather.snapshot.recent = {stopwatch_weather::BuiltinLocation(11),
            stopwatch_weather::BuiltinLocation(5), stopwatch_weather::BuiltinLocation(0)};
        test::weather.snapshot.recent_count = 3;
        f.Start(); f.Click("Weather location"); f.Click("Common cities");
        CommonRows(f, Builtins());
        auto* viewport = CommonViewport(f);
        assert(!HasVisibleHit(f.Button("台北")) && !f.Click("台北"));
        assert(viewport->alive && viewport->scroll_y == 0 && test::weather.setter_calls == 0);
        FocusRow(f, f.Button("台北")); CheckCityGeometry(f);
        assert(viewport->scroll_y == 376 && viewport->children.back()->scroll_requests == 1);
        assert(f.Click("Back"));
        f.Click("Recent cities (3)"); CheckCityGeometry(f);
        f.Label("台北"); f.Label("成都"); f.Label("北京");
        auto* detail = f.Button("Back")->parent;
        int rows = 0;
        for (auto* child : detail->children) {
            assert(child->kind != "panel");
            if (IsCityRow(child)) {
                assert(child->x == 78 && child->y == 112 + rows * 54);
                assert(child->scroll_requests == 0); ++rows;
            }
        }
        assert(rows == 3 && f.Button("Back")->y == 390);
        f.Key(0); Focused(f, "Back"); f.Key(0); Focused(f, "北京");
        assert(detail->scroll_y == 0);
        f.Click("台北"); f.Click("OK"); f.app.Flush();
        assert(test::weather.config_writes == 1 && test::weather.snapshot.location.id == "builtin:Taipei");
        EqualCity(test::weather.last_location, stopwatch_weather::BuiltinLocation(11));
        assert(!test::weather.search_calls); f.NoWrites();
    } else if (test == "city_cancel_and_home_no_save") {
        f.Start(); f.Click("Weather location"); f.Click("Common cities"); f.Click("上海");
        f.Click("Back");
        assert(test::weather.config_writes == 0 && test::weather.setter_calls == 0);
        f.Click("Back"); f.Click("Home"); f.app.Flush();
        assert(f.home_calls == 1 && test::weather.config_writes == 0);
        f.Open(); f.Click("Weather location"); f.Click("Common cities"); f.Click("广州");
        f.Close(); f.Open(); f.Click("Weather location"); f.Label("Automatic (IP)");
        assert(test::weather.config_writes == 0 && f.app.pending.empty());
    } else if (test == "city_keyboard_touch_and_explicit_search") {
        f.Start(); f.Click("Weather location"); f.Click("Search city"); CheckCityGeometry(f);
        f.Click("Search"); f.Label("Enter a city name or ZIP");
        assert(test::weather.search_calls == 0);
        TypeCity(f); CheckCityGeometry(f);
        f.Click("Search"); f.Label("Searching... Back to cancel"); CheckCityGeometry(f);
        assert(test::weather.query == "AMZ2 ,'" && test::weather.search_calls == 1);
        assert(test::weather.config_writes == 0 && f.app.pending.empty());
        const int snapshots = test::weather.snapshot_reads, reads = test::weather.search_reads;
        const size_t objects_before = objects.size();
        for (int i = 0; i < 30; ++i) f.Tick();
        assert(test::weather.search_reads == reads && objects.size() == objects_before);
        assert(test::weather.snapshot_reads <= snapshots + 1);
        f.Click("Back"); f.Label("AMZ2 ,'");
        assert(test::weather.cancel_calls == 1 && test::weather.config_writes == 0);
    } else if (test == "city_search_candidates_confirm_disambiguation") {
        f.Start(); StartSearch(f);
        const auto one = NamedCity("geonames:1", "Springfield", "Illinois", "United States");
        const auto two = NamedCity("geonames:2", "Springfield", "Massachusetts", "United States");
        const auto three = NamedCity("geonames:3", "Long city name spanning the full circular display", "浙江", "中国");
        const auto four = NamedCity("geonames:4", "Fourth", "Region", "Country");
        const auto five = NamedCity("geonames:5", "Fifth", "Region", "Country");
        test::Complete(1, {one, two, three, four, five}); f.Tick(); CheckCityGeometry(f);
        f.Label("Illinois / United States / geonames:1");
        f.Label("Massachusetts / United States / geonames:2");
        f.Label("US / geonames:3"); f.Label("Fifth");
        assert(f.Label(three.display_name)->font == &font_stopwatch_weather_20);
        assert(f.Label(three.display_name)->long_mode == LV_LABEL_LONG_SCROLL_CIRCULAR);
        assert(test::weather.config_writes == 0);
        f.Click(three.display_name);
        f.Label("US / geonames:3"); f.Label("Not saved - OK to confirm"); CheckCityGeometry(f);
        f.Click("OK"); assert(test::weather.config_writes == 0); f.app.Flush();
        assert(test::weather.config_writes == 1 && test::weather.snapshot.location.id == three.id);
        assert(test::weather.cancel_calls == 1); f.NoWrites();
    } else if (test == "city_search_failure_busy_empty_retry") {
        f.Start(); f.Click("Weather location"); f.Click("Search city"); f.Click("A");
        test::weather.rejection = "busy"; f.Click("Search");
        f.Label("Search not started: busy");
        assert(test::weather.searches.empty() && test::weather.config_writes == 0);
        test::weather.rejection.clear(); f.Click("Search"); f.Label("Searching... Back to cancel");
        test::Complete(1, {}); f.Tick(); f.Label("No matches. Back to edit.");
        f.Click("Back"); f.Click("Search");
        auto& search = test::weather.searches[2];
        search.state = stopwatch_weather::SearchState::Error; search.error = "offline";
        ++search.version; test::Bump(); f.Tick(); f.Label("Search failed: offline");
        f.Click("Back"); f.Click("Search");
        auto& expired = test::weather.searches[3];
        expired.state = stopwatch_weather::SearchState::Expired; ++expired.version; test::Bump();
        f.Tick(); f.Label("Search expired. Back to retry.");
        assert(test::weather.config_writes == 0 && f.app.pending.empty());
    } else if (test == "city_search_generation_cancel_close_reopen") {
        f.Start(); StartSearch(f); const uint32_t old_id = 1;
        f.Click("Back"); f.Click("Page"); f.Click("M"); f.Click("Search");
        assert(test::weather.search_calls == 2 && test::weather.cancel_calls == 1);
        test::Complete(old_id, {NamedCity("geonames:1", "OLD", "Old", "Country")});
        f.Tick(); f.Label("Searching... Back to cancel"); assert(!Find(f.screen, "label", "OLD"));
        const int reads = test::weather.search_reads;
        f.Close(); test::Complete(2, {NamedCity("geonames:2", "LATE", "Late", "Country")});
        for (int i = 0; i < 10; ++i) f.Tick();
        assert(test::weather.search_reads == reads && !Visible(f.settings.root()));
        f.Open(); f.Click("Weather location");
        assert(!Find(f.screen, "label", "LATE") && test::weather.cancel_calls == 2);
        assert(test::weather.config_writes == 0 && f.app.pending.empty());
    } else if (test == "city_external_clean_and_edited_conflict") {
        f.Start(); f.Click("Weather location");
        auto* location = f.Label("Not located");
        test::ExternalLocation(false, stopwatch_weather::BuiltinLocation(1)); f.Tick();
        assert(location->alive && location->text == "上海"); f.Label("Manual");
        f.Click("Common cities"); f.Click("北京");
        test::ExternalLocation(false, stopwatch_weather::BuiltinLocation(3)); f.Tick();
        f.Label("北京"); f.Label("Configuration changed. Back and choose again.");
        assert(f.Button("OK")->state & LV_STATE_DISABLED);
        assert(!f.Click("OK")); f.Key(2);
        assert(test::weather.setter_calls == 0 && f.app.pending.empty());
        f.Click("Back"); f.Label("深圳");
        f.Click("Common cities"); f.Click("北京"); f.Click("OK"); f.app.Flush();
        assert(test::weather.config_writes == 1 && test::weather.snapshot.location.id == "builtin:Beijing");
    } else if (test == "city_unedited_confirm_follows_voice") {
        test::weather.snapshot.automatic = false;
        test::weather.snapshot.location = stopwatch_weather::BuiltinLocation(0);
        f.Start(); f.Click("Weather location"); f.Click("Common cities"); f.Click("北京");
        test::ExternalLocation(false, stopwatch_weather::BuiltinLocation(1)); f.Tick();
        f.Label("上海"); f.Label("Not saved - OK to confirm");
        assert(!(f.Button("OK")->state & LV_STATE_DISABLED));
        f.Click("OK"); f.app.Flush();
        assert(test::weather.setter_calls == 1 && test::weather.config_writes == 0);
        assert(test::weather.snapshot.location.id == "builtin:Shanghai");
    } else if (test == "city_typed_query_external_conflict") {
        f.Start(); f.Click("Weather location"); f.Click("Search city"); f.Click("A");
        test::ExternalLocation(false, stopwatch_weather::BuiltinLocation(4)); f.Tick();
        f.Label("A"); f.Label("Configuration changed. Back and choose again.");
        f.Click("Search"); assert(test::weather.search_calls == 0);
        f.Click("Back"); f.Label("杭州"); f.Click("Search city");
        f.Click("Search"); assert(test::weather.search_calls == 1);
        auto candidate = NamedCity("geonames:9", "Candidate", "Region", "Country");
        test::Complete(1, {candidate}); f.Tick();
        test::ExternalLocation(false, stopwatch_weather::BuiltinLocation(2)); f.Tick();
        f.Click("Candidate"); f.Label("Configuration changed. Back and choose again.");
        assert(f.Button("OK")->state & LV_STATE_DISABLED);
        f.Key(2); assert(test::weather.setter_calls == 0 && f.app.pending.empty());
    } else if (test == "city_queued_revision_and_checked_write_failure") {
        f.Start(); f.Click("Weather location"); f.Click("Common cities"); f.Click("北京");
        f.Click("OK"); assert(f.app.pending.size() == 1);
        test::ExternalLocation(false, stopwatch_weather::BuiltinLocation(1));
        f.app.Flush(); f.Label("Configuration changed. Back and choose again.");
        assert(test::weather.config_writes == 0 && test::weather.setter_calls == 1);
        f.Click("Back"); f.Click("Common cities"); f.Click("北京");
        test::weather.write_error = "nvs_full"; f.Click("OK"); f.app.Flush();
        f.Label("Not saved: nvs_full"); assert(test::weather.config_writes == 0);
        assert(test::weather.snapshot.location.id == "builtin:Shanghai");
        test::weather.write_error.clear(); f.Click("OK"); f.app.Flush();
        assert(test::weather.config_writes == 1 && test::weather.setter_calls == 3);
        f.Label("Configuration saved - weather may be pending");
    } else if (test == "city_close_back_cancels_queued_commit") {
        f.Start(); f.Click("Weather location"); f.Click("Common cities"); f.Click("北京");
        f.Click("OK"); f.Close(); f.Open(); f.Click("Weather location");
        f.Click("Common cities"); f.Click("上海"); f.Click("OK");
        assert(f.app.pending.size() == 2); f.app.RunOne();
        assert(test::weather.setter_calls == 0 && f.app.pending.size() == 1);
        f.Key(2); assert(f.app.pending.size() == 1); f.app.Flush();
        assert(test::weather.setter_calls == 1 && test::weather.snapshot.location.id == "builtin:Shanghai");
        f.Click("Common cities"); f.Click("北京"); f.Click("OK");
        f.Click("Back"); f.app.Flush();
        assert(test::weather.setter_calls == 1);  // Back invalidates a queued city save.
        // Home/Close also cancels queued work; no cancel falsely rolls back a
        // setter that has already executed on the main task.
        f.Click("Common cities"); f.Click("广州"); f.Click("OK"); f.Close();
        f.app.Flush(); assert(test::weather.setter_calls == 1);
    } else if (test == "city_close_during_executed_commit_no_reopen") {
        f.Start(); f.Click("Weather location"); f.Click("Common cities"); f.Click("北京");
        test::weather.after_set = [&]() { f.Close(); f.Open(); };
        f.Click("OK"); f.app.Flush();
        assert(test::weather.config_writes == 1 && test::weather.snapshot.location.id == "builtin:Beijing");
        Focused(f, "Brightness"); assert(!Find(f.screen, "label", "Configuration saved - weather may be pending"));
        f.Click("Weather location"); f.Label("北京");
    } else if (test == "city_common_confirm_freezes_identity") {
        auto current = NamedCity("ip:34,-118", "Los Angeles", "California", "United States");
        current.city = "Provider original city"; current.latitude = 34.1234; current.longitude = -118.4567;
        test::weather.snapshot.location = current; test::weather.snapshot.config_revision = 42;
        f.Start(); f.Click("Weather location"); f.Click("Common cities");
        assert(f.Click("Los Angeles"));
        auto* name = f.Label("Los Angeles"); auto* confirm = f.Button("OK");
        // The auto location can move after selection without a configuration
        // revision. This edited manual choice must retain its frozen row value.
        test::weather.snapshot.location = stopwatch_weather::BuiltinLocation(1); test::Bump();
        f.Tick(); assert(name->alive && confirm->alive && f.Label("Los Angeles") == name);
        f.Label("California / United States / ip:34,-118");
        f.Click("OK");
        test::weather.snapshot.location = stopwatch_weather::BuiltinLocation(2); test::Bump(); f.Tick();
        assert(f.app.pending.size() == 1); f.app.Flush();
        EqualCity(test::weather.last_location, current);
        assert(test::weather.last_revision == 42 && !test::weather.last_automatic);
        assert(test::weather.config_writes == 1); f.NoWrites();
    } else if (test == "city_recent_freezes_identity") {
        const auto original = NamedCity("geonames:1", "Recent original", "Region", "Country");
        test::weather.snapshot.recent[0] = original; test::weather.snapshot.recent_count = 1;
        test::weather.snapshot.config_revision = 17;
        f.Start(); f.Click("Weather location"); f.Click("Recent cities (3)");
        auto* row = f.Button("Recent original"); const size_t before = objects.size();
        test::weather.snapshot.recent[0] = NamedCity("geonames:2", "Replacement", "Other", "Country");
        test::Bump(); f.Tick(); assert(row->alive && objects.size() == before);
        assert(f.Click("Recent original")); f.Click("OK"); f.app.Flush();
        EqualCity(test::weather.last_location, original); assert(test::weather.last_revision == 17); f.NoWrites();
    } else if (test == "city_common_stale_retained_auto_move") {
        const auto original = NamedCity("ip:34,-118", "Los Angeles", "California", "United States");
        test::weather.snapshot.location = original; test::weather.snapshot.config_revision = 7;
        f.Start(); f.Click("Weather location"); f.Click("Common cities");
        auto* viewport = CommonViewport(f); auto* held = viewport->children[0];
        const int waits = pointer.waits, reads = test::weather.snapshot_reads;
        pointer.held = true;
        const auto replacement = stopwatch_weather::BuiltinLocation(1);
        test::weather.snapshot.location = replacement; test::Bump();
        // Deliver the original live target without a timer tick. PollCity must
        // invalidate this callback's generation, not activate row zero again.
        f.Retained(held);
        assert(!viewport->alive && !held->alive && pointer.waits == waits + 1 && !pointer.held);
        assert(test::weather.snapshot_reads == reads + 1 && keypad.waits == 0);
        auto expected = Builtins(); expected.erase(expected.begin() + 1); expected.insert(expected.begin(), replacement);
        CommonRows(f, expected); assert(CommonViewport(f) != viewport);
        assert(!Find(f.screen, "label", "Confirm location") && !Find(f.screen, "label", "Los Angeles"));
        assert(test::weather.setter_calls == 0 && f.app.pending.empty());
        f.Key(2); f.Label("上海"); f.Click("OK"); f.app.Flush();
        EqualCity(test::weather.last_location, replacement); assert(test::weather.last_revision == 7); f.NoWrites();
    } else if (test.compare(0, 20, "city_location_field_") == 0) {
        const auto field = test.substr(20);
        auto current = NamedCity("ip:34,-118", "Los Angeles", "California", "United States");
        test::weather.snapshot.location = current; test::weather.snapshot.config_revision = 9;
        f.Start(); f.Click("Weather location"); f.Click("Common cities");
        auto* old_viewport = CommonViewport(f); const auto old_rows = old_viewport->children;
        FocusRow(f, old_rows.back()); assert(old_viewport->scroll_y > 0);
        const int waits = pointer.waits; pointer.held = true;
        if (field == "id") current.id = "geonames:1";
        else if (field == "city") current.city = "Provider renamed city";
        else if (field == "display_name") current.display_name = "Los Angeles renamed";
        else if (field == "region") current.region = "New region";
        else if (field == "country") current.country = "New country";
        else if (field == "country_code") current.country_code = "CA";
        else if (field == "latitude") current.latitude += 0.0000001;
        else if (field == "longitude") current.longitude -= 0.0000001;
        else assert(false);
        assert(stopwatch_weather::ValidLocation(current));
        test::weather.snapshot.location = current; test::Bump(); f.Tick();
        assert(test::weather.snapshot.config_revision == 9 && !old_viewport->alive);
        for (auto* row : old_rows) assert(!row->alive);
        assert(pointer.waits == waits + 1 && !pointer.held && keypad.waits == 0);
        auto expected = Builtins(); expected.insert(expected.begin(), current); CommonRows(f, expected);
        assert(CommonViewport(f) != old_viewport);
        // Even sub-SameLocation coordinate changes rebuild a clean list. The
        // new first-row confirm retains the complete updated location.
        f.Key(2); f.Click("OK"); f.app.Flush(); EqualCity(test::weather.last_location, current);
        assert(test::weather.last_revision == 9); f.NoWrites();
    } else if (test == "city_common_weather_refresh_preserves_scroll") {
        test::weather.snapshot.location = stopwatch_weather::BuiltinLocation(1);
        f.Start(); f.Click("Weather location"); f.Click("Common cities");
        auto* viewport = CommonViewport(f); const auto rows = viewport->children;
        FocusRow(f, rows.back());
        const int scroll = viewport->scroll_y, waits = pointer.waits;
        const int reads = test::weather.snapshot_reads; const size_t before = objects.size();
        std::vector<int> requests;
        for (auto* row : rows) requests.push_back(row->scroll_requests);
        auto unchanged = [&]() {
            assert(viewport->alive && CommonViewport(f) == viewport && objects.size() == before);
            assert(viewport->children == rows && viewport->scroll_y == scroll);
            assert(pointer.waits == waits && rows.back()->state & LV_STATE_FOCUSED);
            for (size_t i = 0; i < rows.size(); ++i)
                assert(rows[i]->alive && rows[i]->scroll_requests == requests[i]);
            assert(f.settings.root()->scroll_y == 0 && viewport->parent->scroll_y == 0);
            CheckCityGeometry(f);
        };
        for (int i = 0; i < 30; ++i) f.Tick();
        unchanged(); assert(test::weather.snapshot_reads == reads);
        test::weather.snapshot.forecast.temperature = 28.5f; test::Bump(); f.Tick();
        unchanged(); assert(test::weather.snapshot_reads == reads + 1);
        test::weather.snapshot.pending = true; test::weather.snapshot.state = stopwatch_weather::State::Updating;
        test::weather.snapshot.error = "offline"; test::Bump(); f.Tick();
        unchanged(); assert(test::weather.snapshot_reads == reads + 2);
        test::Bump(); f.Tick(); unchanged();  // Version alone is not a location change.
        assert(test::weather.snapshot_reads == reads + 3);
        for (int i = 0; i < 30; ++i) f.Tick();
        unchanged(); assert(test::weather.snapshot_reads == reads + 3);
        // The retained current row still has its exact original value.
        FocusRow(f, rows.front()); assert(f.Click("上海")); f.Click("OK"); f.app.Flush();
        EqualCity(test::weather.last_location, stopwatch_weather::BuiltinLocation(1)); f.NoWrites();
    } else if (test == "city_common_navigation_scroll_isolation") {
        f.Start(); auto* menu = f.Button("Brightness")->parent;
        for (int i = 0; i < 3; ++i) f.Key(1);
        const int menu_scroll = menu->scroll_y; assert(menu_scroll > 0);
        f.Key(2); f.Click("Common cities");
        auto* viewport = CommonViewport(f); auto* detail = viewport->parent;
        FocusRow(f, viewport->children.back());
        assert(viewport->scroll_y > 0 && menu->scroll_y == menu_scroll);
        f.Click("Back");
        assert(!viewport->alive && detail->scroll_y == 0 && f.settings.root()->scroll_y == 0);
        f.Click("Search city"); CheckCityGeometry(f);
        assert(f.Button("A")->y == 142 && detail->scroll_y == 0 && menu->scroll_y == menu_scroll);
        f.Click("Back"); f.Click("Common cities"); CommonRows(f, Builtins());
        auto* next_viewport = CommonViewport(f); assert(next_viewport != viewport);
        FocusRow(f, next_viewport->children.back()); f.Close(); f.Open();
        assert(menu->scroll_y == 0 && detail->scroll_y == 0 && f.settings.root()->scroll_y == 0);
        Focused(f, "Brightness"); f.Click("Theme");
        assert(!next_viewport->alive && f.Button("Light")->y == 102 && detail->scroll_y == 0);
        f.Click("OK"); f.app.Flush(); f.Click("Weather location"); f.Click("Common cities");
        CommonRows(f, Builtins());
        assert(menu->scroll_y == 0 && test::weather.setter_calls == 0); f.NoWrites();
    } else if (test == "city_buttons_entire_flow") {
        f.Start(); f.Key(1); f.Key(1); f.Key(1); Focused(f, "Weather location"); f.Key(2);
        Focused(f, "Automatic (IP)"); f.Key(1); Focused(f, "Common cities"); f.Key(2);
        Focused(f, "北京"); f.Key(2); Focused(f, "OK");
        f.Key(1); Focused(f, "Back"); f.Key(2);  // Confirm cancellation.
        assert(test::weather.config_writes == 0);
        f.Key(1); f.Key(1); f.Key(1); Focused(f, "Search city"); f.Key(2);
        Focused(f, "A"); f.Key(2); f.Label("A");
        for (int i = 0; i < 18; ++i) f.Key(1);
        Focused(f, "Search"); f.Key(2); Focused(f, "Back");
        test::Complete(1, {NamedCity("geonames:10", "Button city", "Region", "Country")}); f.Tick();
        Focused(f, "Button city"); f.Key(2); Focused(f, "OK"); f.Key(2);
        f.app.Flush(); assert(test::weather.config_writes == 1);
        Focused(f, "Automatic (IP)"); f.Key(2); f.Key(2); f.app.Flush();
        assert(test::weather.snapshot.automatic && test::weather.config_writes == 2);
    } else if (test == "city_keyboard_limit_and_page_cycle") {
        f.Start(); f.Click("Weather location"); f.Click("Search city");
        for (int i = 0; i < 96; ++i) f.Click("A");
        f.Click("B"); f.Label("Maximum 96 ASCII characters");
        f.Click("Page"); f.Click("Page"); f.Click("Page");
        f.Label(std::string(96, 'A')); assert(test::weather.search_calls == 0);
        f.Click("Search"); assert(test::weather.query == std::string(96, 'A'));
        assert(test::weather.config_writes == 0);
    } else if (test == "city_candidate_expires_on_confirmation") {
        f.Start(); StartSearch(f);
        test::Complete(1, {NamedCity("geonames:3", "Expiring city", "Region", "Country")});
        f.Tick(); f.Click("Expiring city");
        auto& search = test::weather.searches[1];
        search.state = stopwatch_weather::SearchState::Expired; ++search.version; test::Bump();
        f.Tick(); f.Label("Search expired. Back and search again.");
        assert(f.Button("OK")->state & LV_STATE_DISABLED);
        f.Key(2); assert(test::weather.setter_calls == 0 && f.app.pending.empty());
        f.Click("Back"); f.Click("Search city"); f.Click("Search");
        f.Label("Searching... Back to cancel");
        assert(test::weather.search_calls == 2 && test::weather.config_writes == 0);
    } else if (test == "city_hidden_menu_does_not_poll_or_rebuild") {
        f.Start(); f.Click("Weather location"); auto* name = f.Label("Not located");
        const auto before = objects.size(); const int snapshots = test::weather.snapshot_reads;
        for (int i = 0; i < 20; ++i) f.Tick();
        assert(objects.size() == before && test::weather.snapshot_reads == snapshots);
        f.Click("Back"); test::ExternalLocation(false, stopwatch_weather::BuiltinLocation(3));
        for (int i = 0; i < 20; ++i) f.Tick();
        assert(name->alive && name->text == "Not located" && test::weather.snapshot_reads == snapshots);
        f.Click("Weather location"); f.Label("深圳");
        f.Close(); const int closed_reads = test::weather.snapshot_reads;
        test::ExternalLocation(false, stopwatch_weather::BuiltinLocation(2));
        f.Tick(); assert(test::weather.snapshot_reads == closed_reads && timers[0]->paused);
    } else if (test == "city_buttons_back_invalidates_pending_commit") {
        f.Start(); f.Click("Weather location"); f.Click("Common cities"); f.Click("北京");
        f.Key(2); assert(f.app.pending.size() == 1); f.Key(1); Focused(f, "Back"); f.Key(2);
        f.app.Flush(); assert(test::weather.setter_calls == 0 && test::weather.config_writes == 0);
        f.Label("Weather location");
    } else if (test == "city_retained_events_hidden_and_pointer_release") {
        f.Start(); f.Click("Weather location");
        auto* common = f.Button("Common cities");
        f.Close(); f.Open(); const int waits = pointer.waits;
        pointer.held = true; f.Retained(common);
        assert(f.app.pending.empty() && test::weather.config_writes == 0);
        f.Click("Weather location"); assert(pointer.waits > waits && !pointer.held && keypad.waits == 0);
        f.Click("Common cities"); f.Click("北京");
        auto* ok = f.Button("OK"); auto* back = f.Button("Back");
        f.Close(); f.Open(); f.Retained(ok); f.Retained(back);
        assert(f.app.pending.empty()); Focused(f, "Brightness");
        f.Click("Weather location"); f.Click("Search city"); auto* key = f.Button("A");
        lv_obj_add_flag(f.settings.root(), LV_OBJ_FLAG_HIDDEN); f.Retained(key);
        lv_obj_remove_flag(f.settings.root(), LV_OBJ_FLAG_HIDDEN); f.Label("Type name / ZIP");
        f.Click("Back"); assert(!key->alive); f.settings.RefreshTheme();
        assert(test::weather.config_writes == 0 && test::weather.search_calls == 0);
    } else {
        std::cerr << "Unknown Settings test: " << test << '\n'; assert(false);
    }
    assert(f.app.pending.empty() && test::event_depth == 0 && test::lock_depth == 0);
}
'''


def compile_settings(directory, enabled):
    """Use the complete production translation unit, never extracted methods."""
    directory = pathlib.Path(directory)
    directory.mkdir()
    (directory / "sdkconfig.h").write_text(
        f"#define CONFIG_STOPWATCH_LOCAL_APPS {int(enabled)}\n", encoding="utf-8"
    )
    if enabled:
        headers = {
            # Bridge to the actual dependency-free enum, without exposing main's
            # real application/display headers ahead of the lightweight stubs.
            "device_state.h": '#include "' + (ROOT / "main/device_state.h").as_posix() + '"\n',
            "test_runtime.h": RUNTIME,
            "weather_test_service.h": SERVICE,
            "application.h": APPLICATION,
            "settings.h": SETTINGS,
            "display/display.h": DISPLAY,
            "lvgl_theme.h": THEMES,
            "test_hardware.h": HARDWARE,
            "audio_codec.h": '#include "test_hardware.h"\n',
            "board.h": '#include "test_hardware.h"\n',
            "wifi_manager.h": WIFI,
            "lvgl.h": LVGL,
            "esp_app_desc.h": r'''
#pragma once
struct esp_app_desc_t { char version[32]; char idf_ver[32]; };
inline const esp_app_desc_t* esp_app_get_description() {
    static const esp_app_desc_t description{"settings-host-1.2.3", "v6.1-native-stub"};
    return &description;
}
''',
        }
        for name, content in headers.items():
            path = directory / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        source = HARNESS
    else:
        # No LVGL/Application/board headers exist in this include directory.
        source = '#include "stopwatch_settings.h"\nint main() { return 0; }\n'
    harness = directory / "settings_test.cc"
    harness.write_text(source, encoding="utf-8")
    binary = directory / "settings_test.exe"
    subprocess.run(
        [COMPILER, "-std=c++17", "-Wall", "-Wextra", "-Werror",
         "-I", str(directory), "-I", str(BOARD), str(harness),
         str(BOARD / "stopwatch_settings.cc"), str(BOARD / "stopwatch_weather_data.cc"),
         "-o", str(binary)],
        check=True, timeout=60,
    )
    return str(binary)


@unittest.skipUnless(COMPILER, "requires a host C++ compiler")
class StopwatchSettingsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="stopwatch-settings-test-")
        cls.addClassCleanup(cls.tmp.cleanup)
        cls.binary = compile_settings(pathlib.Path(cls.tmp.name) / "enabled", True)
        cls.disabled_binary = compile_settings(pathlib.Path(cls.tmp.name) / "disabled", False)

    def run_case(self, name):
        subprocess.run([self.binary, name], check=True, timeout=10)

    def test_setup_is_idempotent_and_real_buttons_wrap_menu_selection(self):
        self.run_case("setup_and_menu")

    def test_real_menu_clicks_open_pages_and_version_returns_without_commit(self):
        self.run_case("menu_clicks_and_version")

    def test_brightness_drag_stages_then_one_main_task_commit_captures_value(self):
        self.run_case("brightness_touch")

    def test_brightness_buttons_clamp_and_double_b_schedules_save(self):
        self.run_case("brightness_buttons_bounds")

    def test_uninitialized_brightness_is_clamped_without_immediate_write(self):
        self.run_case("brightness_initial_clamp")

    def test_volume_drag_and_buttons_quantize_clamp_and_commit_once_on_main(self):
        self.run_case("volume_touch_and_buttons")

    def test_unchanged_values_do_not_write_nvs_again(self):
        self.run_case("unchanged_values")

    def test_light_and_dark_commit_through_existing_persistent_display_contract(self):
        self.run_case("theme_commit")

    def test_external_theme_refresh_follows_unedited_but_preserves_edited_and_pending_choice(self):
        self.run_case("theme_external_sync")

    def test_missing_current_or_registered_theme_is_safe(self):
        self.run_case("theme_missing")

    def test_wifi_status_tracks_disconnected_station_and_setup_hotspot_on_timer(self):
        self.run_case("wifi_status")

    def test_wifi_configure_uses_scheduled_callback_not_raw_driver(self):
        self.run_case("wifi_callback")

    def test_inflight_wifi_request_survives_back_reentry_close_open_and_normal_observations(self):
        self.run_case("wifi_pending_navigation")

    def test_rejected_wifi_callback_allows_retry_without_terminal_observation(self):
        self.run_case("wifi_callback_rejection_retry")

    def test_rejected_wifi_retries_survive_navigation_and_stale_retry_never_latches(self):
        self.run_case("wifi_rejected_retry_navigation")

    def test_observed_wifi_setup_and_config_mode_block_duplicates_until_completion_allows_retry(self):
        self.run_case("wifi_observed_completion")

    def test_wifi_request_accepts_each_documented_runtime_state_on_main(self):
        self.run_case("wifi_allowed_states")

    def test_wifi_request_rechecks_rejected_state_when_deferred_and_does_not_latch(self):
        self.run_case("wifi_rejected_states_retry")

    def test_wifi_config_mode_is_rechecked_when_deferred_and_rejection_allows_retry(self):
        self.run_case("wifi_config_mode_rejection")

    def test_upgrade_and_fatal_observations_clear_inflight_latch_even_while_inactive(self):
        self.run_case("wifi_system_observations_release_latch")

    def test_stale_closed_wifi_request_never_sets_inflight_latch(self):
        self.run_case("wifi_stale_closed_request_never_latches")

    def test_stale_back_navigation_wifi_request_never_sets_inflight_latch(self):
        self.run_case("wifi_stale_back_request_never_latches")

    def test_retained_theme_release_after_close_open_is_ignored_without_crash(self):
        self.run_case("retained_theme_reopen")

    def test_retained_light_dark_releases_are_ignored_while_settings_inactive(self):
        self.run_case("retained_theme_inactive")

    def test_retained_hidden_menu_clicks_cannot_replace_active_detail(self):
        self.run_case("retained_menu_on_detail")

    def test_retained_home_menu_release_while_inactive_does_not_schedule(self):
        self.run_case("retained_menu_inactive")

    def test_retained_menu_release_checks_hidden_root_even_if_active(self):
        self.run_case("retained_hidden_root_menu")

    def test_retained_hidden_configure_and_back_are_ignored_without_cancelling_new_home(self):
        self.run_case("retained_wifi_controls")

    def test_retained_configure_and_back_are_ignored_while_settings_inactive(self):
        self.run_case("retained_wifi_inactive")

    def test_retained_about_ok_cannot_cancel_newer_home_navigation(self):
        self.run_case("retained_about_ok")

    def test_retained_ok_and_slider_callbacks_are_ignored_after_commit_and_reopen(self):
        self.run_case("retained_value_controls")

    def test_retained_theme_and_ok_check_hidden_root_even_if_active(self):
        self.run_case("retained_hidden_root_detail")

    def test_retained_theme_release_checks_hidden_target_not_just_page(self):
        self.run_case("retained_hidden_widget")

    def test_home_is_scheduled_once_and_reopen_resets_selection(self):
        self.run_case("home_callback")

    def test_missing_backlight_and_codec_disable_touch_and_never_commit_hardware(self):
        self.run_case("optional_controls")

    def test_controls_removed_after_ok_before_main_task_are_safe(self):
        self.run_case("controls_removed_before_commit")

    def test_missing_home_and_wifi_callbacks_are_safe(self):
        self.run_case("optional_callbacks")

    def test_reopen_discards_staged_edits_resets_scroll_and_reads_current_values(self):
        self.run_case("reopen_resets")

    def test_close_invalidates_all_queued_mutations_and_home_commands(self):
        for action in ("brightness", "volume", "theme", "wifi", "home"):
            with self.subTest(action=action):
                self.run_case("close_" + action)

    def test_stale_previous_session_commit_cannot_apply_or_unlock_new_pending_commit(self):
        self.run_case("reopen_invalidates_pending")

    def test_actual_home_click_then_menu_row_supersedes_home_without_freezing_detail(self):
        self.run_case("home_then_row_supersedes")

    def test_wifi_back_cancels_queued_provisioning_without_unlocking_new_save(self):
        self.run_case("wifi_back_invalidates_pending")

    def test_reentered_wifi_drops_old_request_but_preserves_new_request(self):
        self.run_case("wifi_reenter_invalidates_pending")

    def test_open_without_close_invalidates_preexisting_queued_home(self):
        self.run_case("open_invalidates_pending")

    def test_city_ui_modes_manual_common_recent_and_cancel(self):
        for case in ("city_optional_service", "city_common_manual_and_auto", "city_common_scroll_and_recent",
                     "city_cancel_and_home_no_save"):
            with self.subTest(case=case):
                self.run_case(case)

    def test_common_cities_puts_each_valid_builtin_first_in_auto_and_manual_modes(self):
        for mode in ("auto", "manual"):
            for index in range(12):
                with self.subTest(mode=mode, index=index):
                    self.run_case(f"city_current_builtin_{mode}_{index}")

    def test_common_cities_puts_valid_nonbuiltin_current_first_in_both_modes(self):
        for mode in ("auto", "manual"):
            with self.subTest(mode=mode):
                self.run_case("city_current_custom_" + mode)

    def test_common_cities_ui_alias_dedup_keeps_exact_location_and_name_country_coordinate_guards(self):
        for alias in ("ascii", "chinese", "inside", "name_guard", "country_guard",
                      "latitude_guard", "longitude_guard"):
            with self.subTest(alias=alias):
                self.run_case("city_alias_" + alias)

    def test_common_cities_empty_or_invalid_current_defaults_to_beijing_and_all_builtins(self):
        for invalid in ("empty", "id", "city", "display", "country", "latitude", "longitude", "nan"):
            with self.subTest(invalid=invalid):
                self.run_case("city_invalid_current_" + invalid)

    def test_common_cities_all_12_or_13_rows_and_fixed_back_reachable_with_both_button_wraps(self):
        for count in (12, 13):
            with self.subTest(count=count):
                self.run_case(f"city_common_wrap_{count}")

    def test_common_cities_touch_requires_visible_clip_and_does_not_artificially_scroll(self):
        self.run_case("city_common_touch_preserves_scroll")

    def test_common_and_recent_rows_confirm_frozen_full_location_and_revision(self):
        for case in ("city_common_confirm_freezes_identity", "city_recent_freezes_identity"):
            with self.subTest(case=case):
                self.run_case(case)

    def test_common_cities_same_revision_auto_move_discards_old_retained_click_not_rebinds(self):
        self.run_case("city_common_stale_retained_auto_move")

    def test_common_cities_rebuilds_for_each_full_location_field_even_without_revision_change(self):
        for field in ("id", "city", "display_name", "region", "country", "country_code", "latitude", "longitude"):
            with self.subTest(field=field):
                self.run_case("city_location_field_" + field)

    def test_common_cities_temperature_status_and_unchanged_versions_keep_objects_focus_and_scroll(self):
        self.run_case("city_common_weather_refresh_preserves_scroll")

    def test_common_cities_scroll_isolated_from_root_menu_other_pages_and_reentry(self):
        self.run_case("city_common_navigation_scroll_isolation")

    def test_city_ascii_keyboard_submits_explicitly_and_results_disambiguate_before_confirm(self):
        for case in ("city_keyboard_touch_and_explicit_search", "city_search_candidates_confirm_disambiguation",
                     "city_search_failure_busy_empty_retry", "city_search_generation_cancel_close_reopen",
                     "city_keyboard_limit_and_page_cycle", "city_candidate_expires_on_confirmation"):
            with self.subTest(case=case):
                self.run_case(case)

    def test_city_external_voice_revision_conflicts_and_checked_persistence(self):
        for case in ("city_external_clean_and_edited_conflict", "city_unedited_confirm_follows_voice",
                     "city_typed_query_external_conflict", "city_queued_revision_and_checked_write_failure"):
            with self.subTest(case=case):
                self.run_case(case)

    def test_city_generations_and_close_cancel_only_unexecuted_commit(self):
        for case in ("city_close_back_cancels_queued_commit", "city_close_during_executed_commit_no_reopen",
                     "city_retained_events_hidden_and_pointer_release", "city_hidden_menu_does_not_poll_or_rebuild",
                     "city_buttons_back_invalidates_pending_commit"):
            with self.subTest(case=case):
                self.run_case(case)

    def test_city_buttons_without_touch_can_search_choose_cancel_confirm_and_restore_auto(self):
        self.run_case("city_buttons_entire_flow")

    def test_feature_disabled_compiles_full_source_without_firmware_dependency_stubs(self):
        subprocess.run([self.disabled_binary], check=True, timeout=10)
