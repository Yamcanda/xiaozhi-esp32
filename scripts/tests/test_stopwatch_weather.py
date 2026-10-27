"""Native regressions for the complete board-local Weather .cc and real header.

The LVGL model executes production timer, gesture and dot callbacks, including
retained releases that bypass hit testing. It records geometry, primitives and
text/color updates, not rendered pixels. Source, navigation and generated-font
checks use the same production entry
points. Device/Home clock reads use a deterministic local clock; provider fixed-offset
dates come from the linked production data helpers. The fixture supplies the
display lock and root visibility owned by the app shell. A fake service publishes
snapshots without HTTP or worker tasks. These are modeled-LVGL regressions, not
pixel or hardware validation; the actual LVGL renderer is exercised separately.
"""
import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
BOARD = ROOT / "main/boards/m5stack/stopwatch"
MINGW = pathlib.Path("D:/sdk/mingw64/bin/g++.exe")
COMPILER = str(MINGW) if MINGW.is_file() else shutil.which("g++")
HOST_ENV = os.environ.copy()
if COMPILER:
    # A temporary host executable must also find MinGW's runtime DLLs on Windows.
    HOST_ENV["PATH"] = str(pathlib.Path(COMPILER).parent) + os.pathsep + HOST_ENV.get("PATH", "")


RUNTIME = r'''
#pragma once
#include <cassert>
#include <cstdint>
#include <string>
#include <utility>
#include <vector>
namespace test {
inline int lock_depth = 0, event_depth = 0;
struct Write { std::string space, key, value; };
inline std::vector<Write> writes;
inline int settings_opens = 0;
inline void RequireLock() { assert(lock_depth > 0); }
}
'''

SETTINGS = r'''
#pragma once
#include "test_runtime.h"
class Settings {
public:
    explicit Settings(const std::string& space, bool writable = false)
        : space_(space), writable_(writable) { ++test::settings_opens; }
    void SetString(const std::string& key, const std::string& value) {
        assert(writable_); test::writes.push_back({space_, key, value});
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
    Theme* current_theme = nullptr;
    int theme_sets = 0, theme_reads = 0;
    Theme* GetTheme() { test::RequireLock(); ++theme_reads; return current_theme; }
    void SetTheme(Theme* theme) {
        test::RequireLock(); assert(theme); ++theme_sets; current_theme = theme;
        // Match the existing persistent display/theme contract, so accidental
        // Weather-owned theme writes are observable rather than silently no-op.
        Settings("display", true).SetString("theme", theme->name());
    }
};
class DisplayLockGuard {
public:
    explicit DisplayLockGuard(Display* display) { assert(display); ++test::lock_depth; }
    ~DisplayLockGuard() { --test::lock_depth; }
    DisplayLockGuard(const DisplayLockGuard&) = delete;
    DisplayLockGuard& operator=(const DisplayLockGuard&) = delete;
};
'''

CLOCK = r'''
#pragma once
#include "test_runtime.h"
#include <ctime>
namespace test {
inline time_t now = 0;
inline std::tm local{};
inline bool localtime_fails = false;
inline int time_calls = 0, localtime_calls = 0;
// Proleptic Gregorian days since 1970-01-01, independent of host TZ and libc.
inline int DaysFromCivil(int year, unsigned month, unsigned day) {
    year -= month <= 2;
    const int era = (year >= 0 ? year : year - 399) / 400;
    const unsigned yoe = static_cast<unsigned>(year - era * 400);
    const unsigned doy = (153 * (month > 2 ? month - 3 : month + 9) + 2) / 5 + day - 1;
    const unsigned doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    return era * 146097 + static_cast<int>(doe) - 719468;
}
inline void At(int year, int month, int day, int hour, int minute, int second = 0) {
    const int days = DaysFromCivil(year, month, day);
    now = static_cast<time_t>(days) * 86400 + hour * 3600 + minute * 60 + second;
    local = {};
    local.tm_year = year - 1900;
    local.tm_mon = month - 1;
    local.tm_mday = day;
    local.tm_wday = (days + 4) % 7;
    if (local.tm_wday < 0) local.tm_wday += 7;
    local.tm_hour = hour;
    local.tm_min = minute;
    local.tm_sec = second;
    localtime_fails = false;
}
}
inline time_t StopwatchWeatherHostTime(time_t* destination) {
    test::RequireLock(); ++test::time_calls;
    if (destination) *destination = test::now;
    return test::now;
}
inline std::tm* StopwatchWeatherHostLocaltimeR(const time_t* source, std::tm* destination) {
    test::RequireLock(); assert(source && *source == test::now && destination);
    ++test::localtime_calls;
    if (test::localtime_fails) return nullptr;
    *destination = test::local;
    return destination;
}
'''

# Force-include this only for the production object, after parsing <ctime>.
# The real header and all normal C/C++ library declarations remain unchanged.
CLOCK_BINDINGS = r'''
#include "test_clock.h"
#define time StopwatchWeatherHostTime
#define localtime_r StopwatchWeatherHostLocaltimeR
'''

LVGL = r'''
#pragma once
#include "test_runtime.h"
#include <algorithm>
#include <memory>

enum lv_obj_flag_t : uint32_t {
    LV_OBJ_FLAG_SCROLLABLE = 1, LV_OBJ_FLAG_GESTURE_BUBBLE = 2,
    LV_OBJ_FLAG_CLICKABLE = 4, LV_OBJ_FLAG_HIDDEN = 8
};
enum lv_dir_t { LV_DIR_NONE, LV_DIR_LEFT, LV_DIR_RIGHT, LV_DIR_TOP, LV_DIR_BOTTOM };
enum lv_indev_type_t { LV_INDEV_TYPE_POINTER, LV_INDEV_TYPE_KEYPAD };
enum { LV_EVENT_CLICKED, LV_EVENT_GESTURE, LV_LABEL_LONG_CLIP,
       LV_LABEL_LONG_SCROLL_CIRCULAR, LV_LABEL_LONG_WRAP, LV_COORD_MAX = 32767,
       LV_TEXT_FLAG_NONE = 0, LV_TEXT_ALIGN_CENTER, LV_OPA_COVER = 255, LV_RADIUS_CIRCLE = 32767 };
using lv_color_t = uint32_t;
inline lv_color_t lv_color_hex(uint32_t value) { return value; }
struct lv_font_t { int line_height; int advance = 0; };
struct lv_point_t { int x = 0, y = 0; };
inline void lv_text_get_size(lv_point_t* out, const char* text, const lv_font_t* font,
                             int letter, int line, int max, int flags) {
    test::RequireLock(); assert(!letter && !line && max == LV_COORD_MAX && flags == LV_TEXT_FLAG_NONE);
    out->x = int(std::string(text).size()) * (font->advance ? font->advance : font->line_height / 2);
    out->y = font->line_height;
}
#define LV_FONT_DECLARE(name) extern const lv_font_t name
struct lv_obj_t;
struct lv_event_t { lv_obj_t* target; void* user_data; };
using lv_event_cb_t = void (*)(lv_event_t*);
struct Event { lv_event_cb_t callback; int code; void* data; };
struct lv_obj_t {
    lv_obj_t* parent = nullptr;
    std::string kind, text;
    unsigned flags = LV_OBJ_FLAG_SCROLLABLE | LV_OBJ_FLAG_CLICKABLE;
    int x = 0, y = 0, width = 0, height = 0, radius = 0, rotation = 0;
    int background_opacity = 0, text_align = -1, long_mode = -1;
    int label_writes = 0, background_writes = 0, text_color_writes = 0;
    uint32_t background = 0, text_color = 0;
    const lv_font_t* font = nullptr;
    std::vector<lv_obj_t*> children;
    std::vector<Event> events;
};
inline std::vector<std::unique_ptr<lv_obj_t>> objects;
inline void Live(const lv_obj_t* object) { assert(object); }
inline lv_obj_t* MakeObject(lv_obj_t* parent, const char* kind) {
    test::RequireLock();
    objects.emplace_back(std::make_unique<lv_obj_t>());
    auto* object = objects.back().get();
    object->parent = parent; object->kind = kind;
    if (parent) { Live(parent); parent->children.push_back(object); }
    return object;
}
inline lv_obj_t* lv_obj_create(lv_obj_t* parent) { return MakeObject(parent, "panel"); }
inline lv_obj_t* lv_label_create(lv_obj_t* parent) { return MakeObject(parent, "label"); }
inline lv_obj_t* lv_obj_get_parent(const lv_obj_t* object) { Live(object); return object->parent; }
inline bool lv_obj_has_flag(const lv_obj_t* object, lv_obj_flag_t flag) {
    Live(object); return (object->flags & flag) != 0;
}
inline bool Visible(const lv_obj_t* object) {
    Live(object);
    return !(object->flags & LV_OBJ_FLAG_HIDDEN) && (!object->parent || Visible(object->parent));
}
inline void lv_obj_add_flag(lv_obj_t* object, lv_obj_flag_t flag) {
    test::RequireLock(); Live(object); object->flags |= flag;
}
inline void lv_obj_remove_flag(lv_obj_t* object, lv_obj_flag_t flag) {
    test::RequireLock(); Live(object); object->flags &= ~flag;
}
inline void lv_obj_remove_style_all(lv_obj_t* object) { test::RequireLock(); Live(object); }
inline void lv_obj_set_size(lv_obj_t* object, int width, int height) {
    test::RequireLock(); Live(object); object->width = width; object->height = height;
}
inline void lv_obj_set_width(lv_obj_t* object, int width) {
    test::RequireLock(); Live(object); object->width = width;
}
inline void lv_obj_set_height(lv_obj_t* object, int height) {
    test::RequireLock(); Live(object); object->height = height;
}
inline void lv_obj_set_pos(lv_obj_t* object, int x, int y) {
    test::RequireLock(); Live(object); object->x = x; object->y = y;
}
inline void lv_obj_set_style_bg_color(lv_obj_t* object, lv_color_t color, int selector) {
    test::RequireLock(); Live(object); assert(selector == 0);
    object->background = color; ++object->background_writes;
}
inline void lv_obj_set_style_text_color(lv_obj_t* object, lv_color_t color, int selector) {
    test::RequireLock(); Live(object); assert(selector == 0);
    object->text_color = color; ++object->text_color_writes;
}
inline void lv_obj_set_style_text_font(lv_obj_t* object, const lv_font_t* font, int selector) {
    test::RequireLock(); Live(object); assert(selector == 0); object->font = font;
}
#define STYLE_RECORD(name, member) \
    inline void name(lv_obj_t* object, int value, int selector) { \
        test::RequireLock(); Live(object); assert(selector == 0); object->member = value; \
    }
STYLE_RECORD(lv_obj_set_style_bg_opa, background_opacity)
STYLE_RECORD(lv_obj_set_style_text_align, text_align)
STYLE_RECORD(lv_obj_set_style_radius, radius)
STYLE_RECORD(lv_obj_set_style_transform_rotation, rotation)
#undef STYLE_RECORD
inline void lv_label_set_text(lv_obj_t* object, const char* text) {
    test::RequireLock(); Live(object); object->text = text; ++object->label_writes;
}
inline const char* lv_label_get_text(const lv_obj_t* object) {
    test::RequireLock(); Live(object); return object->text.c_str();
}
inline void lv_label_set_long_mode(lv_obj_t* object, int mode) {
    test::RequireLock(); Live(object); object->long_mode = mode;
}
inline void lv_obj_add_event_cb(lv_obj_t* object, lv_event_cb_t callback, int code, void* data) {
    test::RequireLock(); Live(object); object->events.push_back({callback, code, data});
}
inline void* lv_event_get_target(lv_event_t* event) { return event->target; }
inline void* lv_event_get_user_data(lv_event_t* event) { return event->user_data; }
inline void DeliverSaved(const Event& event, lv_obj_t* target) {
    test::RequireLock(); Live(target);
    ++test::event_depth;
    lv_event_t data{target, event.data}; event.callback(&data);
    --test::event_depth;
}
inline bool SendRetainedEvent(lv_obj_t* target, int code) {
    // Retained input deliberately skips visibility filtering. Hidden target and
    // ancestor checks must be performed by the production callbacks themselves.
    test::RequireLock(); Live(target);
    for (auto* current = target; current != nullptr;) {
        const auto callbacks = current->events;
        for (const auto& event : callbacks) if (event.code == code) DeliverSaved(event, target);
        if (code != LV_EVENT_GESTURE || !(current->flags & LV_OBJ_FLAG_GESTURE_BUBBLE)) break;
        current = current->parent;
    }
    return true;
}
inline bool SendEvent(lv_obj_t* target, int code) {
    if (!Visible(target)) return false;
    return SendRetainedEvent(target, code);
}
struct lv_timer_t {
    void (*callback)(lv_timer_t*);
    void* data;
    unsigned period;
    bool paused = false;
    int pauses = 0, resumes = 0;
};
inline std::vector<std::unique_ptr<lv_timer_t>> timers;
inline lv_timer_t* lv_timer_create(void (*callback)(lv_timer_t*), unsigned period, void* data) {
    test::RequireLock(); assert(period > 0);
    timers.emplace_back(new lv_timer_t{callback, data, period}); return timers.back().get();
}
inline void lv_timer_pause(lv_timer_t* timer) {
    test::RequireLock(); assert(timer); timer->paused = true; ++timer->pauses;
}
inline void lv_timer_resume(lv_timer_t* timer) {
    test::RequireLock(); assert(timer); timer->paused = false; ++timer->resumes;
}
inline void* lv_timer_get_user_data(lv_timer_t* timer) { assert(timer); return timer->data; }
struct lv_indev_t {
    lv_indev_type_t type;
    lv_dir_t direction = LV_DIR_NONE;
    lv_obj_t* pressed_target = nullptr;
    bool pressed = false, waiting = false;
    int wait_calls = 0;
};
inline std::vector<std::unique_ptr<lv_indev_t>> inputs;
inline lv_indev_t* active_input = nullptr;
inline lv_indev_t* MakeInput(lv_indev_type_t type) {
    inputs.emplace_back(new lv_indev_t{type}); return inputs.back().get();
}
inline lv_indev_t* lv_indev_get_next(lv_indev_t* input) {
    if (!input) return inputs.empty() ? nullptr : inputs.front().get();
    for (size_t i = 0; i < inputs.size(); ++i)
        if (inputs[i].get() == input) return i + 1 < inputs.size() ? inputs[i + 1].get() : nullptr;
    assert(false); return nullptr;
}
inline lv_indev_type_t lv_indev_get_type(lv_indev_t* input) { assert(input); return input->type; }
inline lv_indev_t* lv_indev_active() { return active_input; }
inline lv_dir_t lv_indev_get_gesture_dir(lv_indev_t* input) { assert(input); return input->direction; }
inline void lv_indev_wait_release(lv_indev_t* input) {
    test::RequireLock(); assert(input); ++input->wait_calls;
    // An idle input has already produced a released sample. A held press must
    // remain suppressed until its physical release, even if the same UI reopens.
    input->waiting = input->pressed;
}
inline lv_obj_t* HitTest(lv_obj_t* root, int x, int y) {
    test::RequireLock(); Live(root);
    if (!Visible(root) || x < root->x || y < root->y ||
        x >= root->x + root->width || y >= root->y + root->height) return nullptr;
    x -= root->x; y -= root->y;
    for (auto it = root->children.rbegin(); it != root->children.rend(); ++it)
        if (auto* hit = HitTest(*it, x, y)) return hit;
    return root->flags & LV_OBJ_FLAG_CLICKABLE ? root : nullptr;
}
inline void PressPointer(lv_indev_t* input, lv_obj_t* target) {
    test::RequireLock(); assert(input->type == LV_INDEV_TYPE_POINTER && !input->pressed);
    assert(Visible(target) && (target->flags & LV_OBJ_FLAG_CLICKABLE));
    input->pressed = true; input->pressed_target = target;
}
inline bool ReleasePointer(lv_indev_t* input) {
    test::RequireLock(); assert(input->pressed);
    auto* target = input->pressed_target;
    const bool suppressed = input->waiting;
    input->pressed = false; input->waiting = false; input->pressed_target = nullptr;
    auto* previous = active_input; active_input = input;
    if (!suppressed) SendRetainedEvent(target, LV_EVENT_CLICKED);
    active_input = previous;
    return !suppressed;
}
'''

HARNESS = r'''
#include "stopwatch_weather.h"
#include "stopwatch_weather_service.h"
#include "test_clock.h"
#include <array>
#include <cmath>
#include <cstdlib>
#include <iostream>
#include <sstream>
#include <tuple>

extern const lv_font_t font_noto_sans_basic_20_4{20};
extern const lv_font_t font_noto_sans_basic_30_4{30};
extern "C" {
extern const lv_font_t font_stopwatch_weather_16{16, 8};
extern const lv_font_t font_stopwatch_weather_20{20};
extern const lv_font_t font_stopwatch_weather_30{30, 17};
extern const lv_font_t CommissionerMedium108{108, 66};
}

namespace test {
inline stopwatch_weather::Snapshot snapshot;
inline int copies = 0, versions = 0, refreshes = 0, visible_on = 0, visible_off = 0;
inline bool service_visible = false;
}
// Real header / ownership, fake non-blocking service boundary. The full production
// data helper TU is linked below, so conditions and date/offset formatting are not mocked.
struct StopwatchWeatherService::Impl {};
StopwatchWeatherService::StopwatchWeatherService() = default;
StopwatchWeatherService::~StopwatchWeatherService() = default;
stopwatch_weather::Snapshot StopwatchWeatherService::GetSnapshot() const {
    test::RequireLock(); ++test::copies; return test::snapshot;
}
uint32_t StopwatchWeatherService::Version() const {
    test::RequireLock(); ++test::versions; return test::snapshot.version;
}
void StopwatchWeatherService::SetVisible(bool visible) {
    test::RequireLock(); test::service_visible = visible;
    visible ? ++test::visible_on : ++test::visible_off;
}
void StopwatchWeatherService::RequestRefresh() { test::RequireLock(); ++test::refreshes; }

std::vector<lv_obj_t*> Descendants(lv_obj_t* root) {
    std::vector<lv_obj_t*> result{root};
    for (auto* child : root->children) {
        const auto nested = Descendants(child);
        result.insert(result.end(), nested.begin(), nested.end());
    }
    return result;
}
lv_obj_t* FindLabel(lv_obj_t* root, const std::string& text) {
    for (auto* object : Descendants(root))
        if (object->kind == "label" && object->text == text) return object;
    return nullptr;
}
void Describe(std::ostream& out, const lv_obj_t* object) {
    out << object << ' ' << object->parent << ' ' << object->kind << ' ' << object->text << '\n'
        << object->x << ' ' << object->y << ' ' << object->width << ' ' << object->height << ' '
        << object->radius << ' ' << object->rotation << ' ' << object->font << ' '
        << object->background_opacity << ' ' << object->text_align << ' ' << object->long_mode << ' '
        << object->flags << ' ' << object->events.size() << '\n';
}
void Publish() { ++test::snapshot.version; }
void LiveData() {
    using namespace stopwatch_weather;
    auto& s = test::snapshot; const auto version = s.version; s = {}; s.version = version;
    s.has_data = true; s.age_known = true; s.state = State::Ready;
    s.location = BuiltinLocation(3);
    auto& f = s.forecast; f.location = s.location;
    f.observed_at = 1792886400;  // 2026-10-25 00:00 UTC
    f.utc_offset = 8 * 3600;
    f.temperature = 26; f.feels_like = 27; f.humidity = 64; f.wind = 9; f.code = 2;
    for (int i = 0; i < 4; ++i)
        f.hours[i] = {f.observed_at + (i + 1) * 3600, float(27 + i), float(10 + i), i ? 0 : 61};
    for (int i = 0; i < 3; ++i)
        f.days[i] = {f.observed_at - 8 * 3600 + i * 86400, float(20 + i), float(28 + i), float(5 + i), i ? 0 : 61};
    Publish();
}
struct Fixture {
    Theme light{"light"}, dark{"dark"}, other{"default"};
    Display display;
    StopwatchWeatherService service;
    StopwatchWeather weather;
    lv_obj_t* screen = nullptr;
    lv_obj_t* overlay = nullptr;
    lv_indev_t* pointer = MakeInput(LV_INDEV_TYPE_POINTER);
    lv_indev_t* keypad = MakeInput(LV_INDEV_TYPE_KEYPAD);
    lv_indev_t* second_pointer = MakeInput(LV_INDEV_TYPE_POINTER);
    explicit Fixture(bool no_display = false, bool no_service = false)
        : weather(no_display ? nullptr : &display, no_service ? nullptr : &service) {
        display.current_theme = &light;
        DisplayLockGuard lock(&display);
        screen = lv_obj_create(nullptr); overlay = lv_obj_create(screen);
    }
    lv_obj_t* Root() const { return weather.root(); }
    lv_obj_t* Page(int index) const { return Root()->children.at(index); }
    lv_obj_t* SourceHit() const { return Root()->children.at(3); }
    lv_obj_t* Hit(int index) const { return Root()->children.at(4 + index); }
    lv_obj_t* Dot(int index) const { return Hit(index)->children.at(0); }
    lv_obj_t* Source() const { return Root()->children.at(7); }
    lv_obj_t* Control(int index) const { return Source()->children.at(3 + index); }
    lv_timer_t* Timer() const { assert(timers.size() == 1); return timers[0].get(); }
    void Setup() { DisplayLockGuard lock(&display); weather.Setup(overlay); }
    void Open() {
        DisplayLockGuard lock(&display); weather.Open();
        if (Root()) lv_obj_remove_flag(Root(), LV_OBJ_FLAG_HIDDEN);
    }
    void Start() { Setup(); Open(); }
    void Close() { DisplayLockGuard lock(&display); weather.Close(); }
    void Key(int key) { DisplayLockGuard lock(&display); weather.HandleButton(key); }
    void Hidden(lv_obj_t* object, bool hidden) {
        DisplayLockGuard lock(&display);
        if (hidden) lv_obj_add_flag(object, LV_OBJ_FLAG_HIDDEN);
        else lv_obj_remove_flag(object, LV_OBJ_FLAG_HIDDEN);
    }
    void Tick(bool retained = false) {
        DisplayLockGuard lock(&display);
        if (retained || !Timer()->paused) Timer()->callback(Timer());
    }
    void Event(lv_obj_t* target, bool retained = false) {
        DisplayLockGuard lock(&display);
        if (retained) SendRetainedEvent(target, LV_EVENT_CLICKED);
        else assert(SendEvent(target, LV_EVENT_CLICKED));
    }
    void Click(int index, bool retained = false) { Event(Hit(index), retained); }
    void Gesture(lv_dir_t direction, lv_obj_t* target = nullptr, bool retained = false,
                 bool with_input = true) {
        DisplayLockGuard lock(&display);
        pointer->direction = direction;
        assert(!active_input); active_input = with_input ? pointer : nullptr;
        if (retained) SendRetainedEvent(target ? target : Root(), LV_EVENT_GESTURE);
        else assert(SendEvent(target ? target : Root(), LV_EVENT_GESTURE));
        active_input = nullptr;
    }
    void Press(lv_indev_t* input, lv_obj_t* target) {
        DisplayLockGuard lock(&display); PressPointer(input, target);
    }
    lv_obj_t* At(int x, int y) {
        DisplayLockGuard lock(&display); return HitTest(Root(), x, y);
    }
    void Tap(int x, int y, lv_obj_t* expected) {
        auto* target = At(x, y); assert(target == expected);
        Press(pointer, target); assert(Release(pointer));
    }
    bool Release(lv_indev_t* input) {
        DisplayLockGuard lock(&display); return ReleasePointer(input);
    }
    void Select(int selected, bool source = false) const {
        assert(Root()->children.size() == 8);
        assert(bool(Source()->flags & LV_OBJ_FLAG_HIDDEN) == !source);
        assert(bool(SourceHit()->flags & LV_OBJ_FLAG_HIDDEN) == source);
        for (int i = 0; i < 3; ++i) {
            assert(bool(Page(i)->flags & LV_OBJ_FLAG_HIDDEN) == (source || i != selected));
            assert(bool(Hit(i)->flags & LV_OBJ_FLAG_HIDDEN) == source);
            assert(Dot(i)->width == (i == selected ? 20 : 6) && Dot(i)->height == 6);
            assert(Dot(i)->x == (44 - Dot(i)->width) / 2 && Dot(i)->y == 19);
        }
    }
    lv_obj_t* Label(int page, const char* text) const {
        auto* object = FindLabel(Page(page), text);
        if (!object) std::cerr << "Missing label: page=" << page << " text=" << text << '\n';
        assert(object); return object;
    }
    lv_obj_t* SourceLabel(const char* text) const {
        auto* object = FindLabel(Source(), text); assert(object); return object;
    }
    void NoWrites() const {
        assert(test::settings_opens == 0 && test::writes.empty() && display.theme_sets == 0);
    }
    std::string Layout() const {
        std::ostringstream out;
        for (auto* object : Descendants(Root())) Describe(out, object);
        return out.str();
    }
    void ExternalTheme(Theme* theme) {
        DisplayLockGuard lock(&display);
        display.current_theme = theme; weather.RefreshTheme();
    }
    lv_obj_t* Temperature() const {
        for (auto* object : Descendants(Page(0)))
            if (object->font == &CommissionerMedium108) return object;
        return nullptr;
    }
};
constexpr std::array<uint32_t, 6> kLight{0x20291F, 0x5C6659, 0x64792E, 0x4E5C4B, 0xF4F6EF, 0xD4DACD};
constexpr std::array<uint32_t, 6> kDark{0xF4F5F0, 0xA4ADA4, 0xE7EFAF, 0xF4F5F0, 0x101412, 0x353C34};
struct Painted { lv_obj_t* object; bool text; size_t role; int writes; };
std::vector<Painted> LightPaint(Fixture& f, int selected) {
    std::vector<Painted> result; std::array<int, 6> roles{};
    for (auto* object : Descendants(f.Root())) {
        const bool text = object->kind == "label";
        if (!(text ? object->text_color_writes : object->background_writes)) continue;
        if (object == f.Dot(0) || object == f.Dot(1) || object == f.Dot(2)) continue;
        const auto color = text ? object->text_color : object->background;
        const auto role = std::find(kLight.begin(), kLight.end(), color); assert(role != kLight.end());
        const auto index = static_cast<size_t>(role - kLight.begin()); ++roles[index];
        result.push_back({object, text, index, text ? object->text_color_writes : object->background_writes});
    }
    for (int count : roles) assert(count > 0);
    for (int i = 0; i < 3; ++i) assert(f.Dot(i)->background == kLight[i == selected ? 0 : 1]);
    return result;
}
void Palette(const Fixture& f, const std::vector<Painted>& paint,
             const std::array<uint32_t, 6>& palette, int selected) {
    for (const auto& item : paint) {
        assert((item.text ? item.object->text_color : item.object->background) == palette[item.role]);
        assert((item.text ? item.object->text_color_writes : item.object->background_writes) > item.writes);
    }
    assert(f.Root()->background == palette[4]);
    for (int i = 0; i < 3; ++i) assert(f.Dot(i)->background == palette[i == selected ? 0 : 1]);
}
void ClockLabels(Fixture& f, const char* caption = "10/25 · 星期日", const char* time = "00:00",
                 const char* timestamp = "10/25 00:00") {
    f.Label(0, caption); f.Label(0, time);
    for (int page = 1; page < 3; ++page) f.Label(page, timestamp);
}
void CheckLive(Fixture& f) {
    f.Label(0, "26"); f.Label(0, "多云 · 体感 27°");
    f.Label(0, "最高 28°   最低 20°"); f.Label(0, "64%"); f.Label(0, "9 km/h");
    f.Label(0, "10/25 · 星期日"); f.Label(0, "00:00");
    for (int i = 0; i < 3; ++i) f.Label(i, "深圳");
    for (int i = 1; i < 3; ++i) f.Label(i, "10/25 00:00");
    for (const char* time : {"09:00", "10:00", "11:00", "12:00"}) f.Label(1, time);
    for (const char* day : {"今天", "明天", "后天"}) f.Label(2, day);
}
void LabelGeometry(Fixture& f, int page, const char* text, int x, int y, int width,
                   const lv_font_t* font, uint32_t color = kLight[0]) {
    const auto* label = f.Label(page, text);
    assert(label->x == x && label->y == y && label->width == width && label->font == font);
    assert(label->text_align == LV_TEXT_ALIGN_CENTER && label->text_color == color);
    assert(!(label->flags & LV_OBJ_FLAG_CLICKABLE) && (label->flags & LV_OBJ_FLAG_GESTURE_BUBBLE));
}
int DirectLabels(lv_obj_t* parent) {
    return static_cast<int>(std::count_if(parent->children.begin(), parent->children.end(),
        [](const auto* child) { return child->kind == "label"; }));
}
bool ShownWithin(const lv_obj_t* object, const lv_obj_t* page) {
    for (; object && object != page; object = object->parent)
        if (object->flags & LV_OBJ_FLAG_HIDDEN) return false;
    return object == page;
}
std::pair<int, int> PagePosition(const lv_obj_t* object, const lv_obj_t* page) {
    int x = 0, y = 0;
    for (; object && object != page; object = object->parent) { x += object->x; y += object->y; }
    assert(object == page); return {x, y};
}
// These are modeled object/primitive assertions, not pixel comparison. Exact
// expected geometry comes from the original v2 DrawIcon, independently of the
// production BuildIcon implementation and without private-header access.
using Primitive = std::tuple<int, int, int, int, int, int, int, uint32_t>;
std::vector<Primitive> Primitives(lv_obj_t* page, int x, int y, int size) {
    std::vector<Primitive> result;
    for (auto* object : Descendants(page)) {
        if (object == page || object->kind != "panel" || !ShownWithin(object, page) ||
            object->background_opacity != LV_OPA_COVER) continue;
        const auto pos = PagePosition(object, page);
        if (pos.first < x || pos.second < y || pos.first + object->width > x + size ||
            pos.second + object->height > y + size) continue;
        assert(!(object->flags & LV_OBJ_FLAG_CLICKABLE) && (object->flags & LV_OBJ_FLAG_GESTURE_BUBBLE));
        result.emplace_back(pos.first, pos.second, object->width, object->height,
                            object->radius, object->rotation, object->background_opacity, object->background);
    }
    std::sort(result.begin(), result.end()); return result;
}
std::vector<Primitive> OriginalIcon(const std::string& icon, int x, int y, int size) {
    std::vector<Primitive> result;
    auto shape = [&](int sx, int sy, int w, int h, uint32_t color, int rotation = 0) {
        result.emplace_back(x + sx * size / 64, y + sy * size / 64,
                            (w * size + 63) / 64, (h * size + 63) / 64,
                            LV_RADIUS_CIRCLE, rotation, LV_OPA_COVER, color);
    };
    auto cloud = [&](int offset) {
        shape(8, 29 + offset, 23, 23, kLight[3]);
        shape(23, 19 + offset, 30, 31, kLight[3]);
        shape(42, 32 + offset, 19, 20, kLight[3]);
        shape(17, 38 + offset, 35, 14, kLight[3]);
    };
    if (icon == "sun") {
        shape(20, 20, 24, 24, kLight[2]);
        for (const auto& ray : std::array<std::array<int, 2>, 8>{{
                {{29, 2}}, {{29, 54}}, {{2, 29}}, {{54, 29}},
                {{10, 10}}, {{49, 10}}, {{10, 49}}, {{49, 49}}}})
            shape(ray[0], ray[1], 6, 6, kLight[2]);
    } else if (icon == "partly") {
        shape(12, 9, 25, 25, kLight[2]);
        shape(22, 0, 5, 6, kLight[2]);
        shape(2, 19, 6, 5, kLight[2]);
        shape(7, 4, 5, 5, kLight[2]);
        shape(38, 4, 5, 5, kLight[2]);
        cloud(0);
    } else if (icon == "cloud") {
        cloud(-5);
    } else if (icon == "rain") {
        cloud(-12);
        for (int i = 0; i < 3; ++i) shape(17 + i * 16, 47, 4, 12, kLight[0], 220);
    } else if (icon == "drop") {
        shape(13, 23, 38, 38, kLight[1]);
        shape(23, 11, 18, 27, kLight[1]);
        shape(29, 3, 6, 22, kLight[1]);
    } else if (icon == "wind") {
        shape(3, 17, 43, 5, kLight[1]);
        shape(3, 30, 57, 5, kLight[1]);
        shape(3, 43, 31, 5, kLight[1]);
    } else assert(false);
    std::sort(result.begin(), result.end()); return result;
}
void CheckIcon(Fixture& f, int page, const char* icon, int x, int y, int size) {
    const auto actual = Primitives(f.Page(page), x, y, size);
    const auto expected = OriginalIcon(icon, x, y, size);
    if (actual != expected) {
        std::cerr << "Weather primitive mismatch: page=" << page << " icon=" << icon
                  << " x=" << x << " y=" << y << " size=" << size
                  << " actual=" << actual.size() << " expected=" << expected.size() << '\n';
        assert(false);
    }
}
void Separator(Fixture& f, int page, int x, int y, int width) {
    int matches = 0;
    for (auto* child : f.Page(page)->children)
        if (child->kind == "panel" && child->x == x && child->y == y &&
            child->width == width && child->height == 1 && child->radius == 0 &&
            child->background == kLight[5] && child->background_opacity == LV_OPA_COVER) ++matches;
    assert(matches == 1);
}

int main(int argc, char** argv) {
    assert(argc == 2); const std::string name = argv[1];
    LiveData(); test::At(2026, 10, 25, 0, 0);
    if (name == "missing" || name == "null_service") test::snapshot = {};
    Fixture f(name == "no_display", name == "null_service");
    if (name == "setup") {
        { DisplayLockGuard lock(&f.display);
          f.weather.Open(); f.weather.Close(); f.weather.RefreshTheme();
          for (int key : {0, 1, 2, -1}) f.weather.HandleButton(key); }
        assert(!f.Root() && timers.empty() && !test::copies && !test::refreshes);
        f.Setup(); const auto layout = f.Layout(); const auto count = objects.size();
        { DisplayLockGuard lock(&f.display); f.weather.Setup(f.screen); }
        assert(f.Layout() == layout && objects.size() == count && f.Timer()->paused);
        assert(f.Root()->width == 466 && f.Root()->height == 466 && !Visible(f.Root()));
        assert(!(f.Root()->flags & LV_OBJ_FLAG_GESTURE_BUBBLE));
        f.Select(0);
        for (int i = 0; i < 3; ++i) {
            assert(f.Hit(i)->width == 44 && f.Hit(i)->height == 44);
            assert(f.Hit(i)->x == 167 + i * 44 && f.Hit(i)->y == 405);
            assert(f.Hit(i)->y + f.Dot(i)->y == 424);
            assert(f.Hit(i)->flags & LV_OBJ_FLAG_GESTURE_BUBBLE);
            assert(f.SourceHit()->y + f.SourceHit()->height <= f.Hit(i)->y);
        }
        assert(f.SourceHit()->x == 83 && f.SourceHit()->y == 68 &&
               f.SourceHit()->width == 300 && f.SourceHit()->height == 44);
        assert(f.SourceHit()->children.empty() && f.SourceHit()->background_opacity == 0 &&
               !f.SourceHit()->background_writes && !f.SourceHit()->text_color_writes);
        assert(f.SourceHit()->flags & LV_OBJ_FLAG_CLICKABLE);
        assert(f.SourceHit()->flags & LV_OBJ_FLAG_GESTURE_BUBBLE);
        assert(f.SourceHit()->y + f.SourceHit()->height == 112); // Original hitbox is unchanged.
        assert(!FindLabel(f.Root(), "深圳 · 演示") && !FindLabel(f.Root(), "open-meteo.com"));
        assert(!test::copies && !test::refreshes);
    } else if (name == "layout") {
        f.Start(); CheckLive(f);
        LabelGeometry(f, 0, "10/25 · 星期日", 93, 40, 280, &font_stopwatch_weather_20, kLight[1]);
        LabelGeometry(f, 0, "00:00", 114, 79, 100, &font_noto_sans_basic_30_4);
        LabelGeometry(f, 0, "深圳", 222, 85, 146, &font_stopwatch_weather_20, kLight[1]);
        LabelGeometry(f, 0, "26", 192, 157, 140, &CommissionerMedium108);
        LabelGeometry(f, 0, "°", 326, 154, 30, &font_noto_sans_basic_30_4);
        LabelGeometry(f, 0, "多云 · 体感 27°", 70, 283, 326, &font_stopwatch_weather_20);
        LabelGeometry(f, 0, "最高 28°   最低 20°", 80, 329, 306, &font_stopwatch_weather_20, kLight[1]);
        LabelGeometry(f, 0, "64%", 151, 370, 60, &font_noto_sans_basic_20_4, kLight[1]);
        LabelGeometry(f, 0, "9 km/h", 268, 370, 80, &font_noto_sans_basic_20_4, kLight[1]);
        assert(DirectLabels(f.Page(0)) == 9); // No independent state/automatic row.
        for (int page = 1; page < 3; ++page) {
            LabelGeometry(f, page, "深圳", 100, 36, 266, &font_stopwatch_weather_20);
            LabelGeometry(f, page, "10/25 00:00", 80, 72, 306, &font_stopwatch_weather_20, kLight[1]);
            assert(DirectLabels(f.Page(page)) == 16); // Only the large heading is removed.
            for (auto* child : f.Page(page)->children)
                assert(!(child->kind == "label" && child->x == 80 && child->y == 112 && child->width == 156));
            Separator(f, page, 88, 365, 290);
            int footers = 0;
            for (auto* child : f.Page(page)->children) if (child->kind == "label" && child->y == 379) {
                ++footers;
                assert(child->x == 80 && child->width == 306 &&
                       child->font == &font_stopwatch_weather_20 && child->text_color == kLight[1]);
            }
            assert(footers == 1);
        }
        LabelGeometry(f, 1, "未来 4 小时", 255, 121, 132, &font_stopwatch_weather_20, kLight[1]);
        for (int i = 0; i < 4; ++i) {
            const int x = 54 + i * 90;
            const auto time = stopwatch_weather::FormatLocalTime(test::snapshot.forecast.hours[i].time, 8 * 3600);
            const auto temperature = std::to_string(27 + i) + "°";
            const auto rain = std::to_string(10 + i) + "%";
            LabelGeometry(f, 1, time.c_str(), x, 173, 88, &font_stopwatch_weather_20, kLight[1]);
            LabelGeometry(f, 1, temperature.c_str(), x, 277, 88, &font_noto_sans_basic_30_4);
            LabelGeometry(f, 1, rain.c_str(), x + 27, 323, 56, &font_noto_sans_basic_20_4, kLight[1]);
        }
        LabelGeometry(f, 2, "低温 / 高温", 255, 121, 132, &font_stopwatch_weather_20, kLight[1]);
        constexpr const char* names[] = {"今天", "明天", "后天"};
        for (int i = 0; i < 3; ++i) {
            const int y = 174 + i * 64;
            LabelGeometry(f, 2, names[i], 79, y + 8, 58, &font_stopwatch_weather_20);
            LabelGeometry(f, 2, (std::to_string(5 + i) + "%").c_str(), 211, y + 8, 54,
                          &font_noto_sans_basic_20_4, kLight[1]);
            LabelGeometry(f, 2, (std::to_string(20 + i) + "°").c_str(), 276, y + 8, 52,
                          &font_noto_sans_basic_20_4, kLight[1]);
            LabelGeometry(f, 2, (std::to_string(28 + i) + "°").c_str(), 335, y + 8, 52,
                          &font_noto_sans_basic_20_4);
            if (i < 2) Separator(f, 2, 79, y + 58, 308);
        }
        for (int page = 0; page < 3; ++page) {
            for (auto* child : f.Page(page)->children) if (child->kind == "label")
                assert(child->font != &font_stopwatch_weather_16);
            assert(f.Label(page, "深圳")->long_mode == LV_LABEL_LONG_SCROLL_CIRCULAR);
        }
        assert(!FindLabel(f.Root(), "open-meteo.com") && !FindLabel(f.Page(0), "已更新"));
    } else if (name == "primitives") {
        f.Start(); const auto count = objects.size();
        CheckIcon(f, 0, "drop", 126, 376, 22); CheckIcon(f, 0, "wind", 239, 376, 22);
        for (int i = 0; i < 4; ++i) CheckIcon(f, 1, "drop", 63 + i * 90, 333, 15);
        for (int i = 0; i < 3; ++i) CheckIcon(f, 2, "drop", 193, 191 + i * 64, 14);
        for (const auto& sample : std::vector<std::pair<int, const char*>>{
                 {0, "sun"}, {1, "partly"}, {2, "partly"}, {3, "cloud"}, {61, "rain"}}) {
            test::snapshot.forecast.code = sample.first;
            for (auto& hour : test::snapshot.forecast.hours) hour.code = sample.first;
            for (auto& day : test::snapshot.forecast.days) day.code = sample.first;
            Publish(); f.Tick(); CheckIcon(f, 0, sample.second, 100, 164, 78);
            for (int i = 0; i < 4; ++i) CheckIcon(f, 1, sample.second, 75 + i * 90, 220, 45);
            for (int i = 0; i < 3; ++i) CheckIcon(f, 2, sample.second, 145, 177 + i * 64, 37);
            assert(objects.size() == count);
        }
    } else if (name == "source_hit") {
        f.Start(); const auto count = objects.size();
        for (int selected = 0; selected < 3; ++selected) {
            f.Click(selected); f.Select(selected);
            assert(f.At(83, 68) == f.SourceHit() && f.At(382, 111) == f.SourceHit());
            assert(f.At(82, 90) != f.SourceHit() && f.At(383, 90) != f.SourceHit());
            assert(f.At(233, 67) != f.SourceHit() && f.At(233, 112) == f.Page(selected));
            f.Tap(233, 90, f.SourceHit()); f.Select(selected, true); f.SourceLabel("Weather data");
            for (int i = 0; i < 3; ++i) f.Click(i, true);
            f.Event(f.SourceHit(), true); f.Select(selected, true); f.SourceLabel("Weather data");
            f.Tap(233, 408, f.Control(1)); f.Select(selected);
            for (int i = 0; i < 3; ++i) {
                f.Tap(189 + 44 * i, 427, f.Hit(i)); f.Select(i);
            }
            f.Click(selected);
            f.Press(f.pointer, f.SourceHit()); f.Gesture(LV_DIR_LEFT, f.SourceHit());
            f.Select((selected + 1) % 3); assert(!f.Release(f.pointer)); f.Select((selected + 1) % 3);
            f.Press(f.pointer, f.SourceHit()); f.Close(); f.Open();
            assert(!f.Release(f.pointer)); f.Select(0);
            f.Tap(233, 90, f.SourceHit()); f.Select(0, true); f.Key(2); f.Select(0);
        }
        assert(objects.size() == count && !f.keypad->wait_calls);
    } else if (name == "lifecycle") {
        f.Setup(); const auto count = objects.size(); auto* timer = f.Timer();
        f.Tick(); assert(!test::copies);
        { DisplayLockGuard lock(&f.display); f.weather.Open(); }
        assert(!timer->paused && !Visible(f.Root()) && test::service_visible);
        assert(test::visible_on == 1 && test::refreshes == 1); CheckLive(f);
        f.Hidden(f.Root(), false); f.Click(2); f.Select(2);
        f.Close(); assert(timer->paused && !test::service_visible && test::visible_off == 1);
        const auto copies = test::copies; Publish(); f.Tick(true); assert(test::copies == copies);
        f.Open(); f.Select(0); assert(test::copies == copies + 1 && test::refreshes == 2);
        assert(objects.size() == count && f.Timer() == timer);
    } else if (name == "buttons") {
        f.Start(); f.Key(0); f.Select(2); f.Key(1); f.Select(0);
        for (int i = 1; i <= 9; ++i) { f.Key(1); f.Select(i % 3); }
        for (int i = 1; i <= 9; ++i) { f.Key(0); f.Select((3 - i % 3) % 3); }
        const auto layout = f.Layout(); for (int key : {-1, 3, 99}) f.Key(key);
        assert(f.Layout() == layout && test::refreshes == 1);
    } else if (name == "gestures") {
        f.Start(); f.Gesture(LV_DIR_LEFT); f.Select(1);
        f.Gesture(LV_DIR_LEFT, f.Label(1, "未来 4 小时")); f.Select(2);
        f.Gesture(LV_DIR_LEFT); f.Select(0); f.Gesture(LV_DIR_RIGHT); f.Select(2);
        f.Gesture(LV_DIR_RIGHT); f.Select(1); f.Gesture(LV_DIR_RIGHT); f.Select(0);
        assert(f.pointer->wait_calls == 6 && !f.keypad->wait_calls);
        for (auto direction : {LV_DIR_NONE, LV_DIR_TOP, LV_DIR_BOTTOM}) f.Gesture(direction);
        f.Gesture(LV_DIR_LEFT, nullptr, false, false); f.Select(0);
    } else if (name == "dots") {
        f.Start(); const auto count = objects.size();
        for (int i : {2, 1, 0, 0, 1, 2, 0}) { f.Click(i); f.Select(i); }
        f.Press(f.pointer, f.Hit(1)); assert(f.Release(f.pointer)); f.Select(1);
        assert(objects.size() == count);
    } else if (name == "inactive" || name == "hidden_root") {
        f.Start(); f.Click(1);
        if (name == "inactive") { f.Close(); f.Hidden(f.Root(), false); }
        else f.Hidden(f.Root(), true);
        const auto copies = test::copies; const auto waits = f.pointer->wait_calls;
        for (int i = 0; i < 3; ++i) f.Click(i, true);
        f.Event(f.SourceHit(), true); f.Gesture(LV_DIR_LEFT, nullptr, true);
        for (int key : {0, 1, 2}) f.Key(key);
        Publish(); f.Tick(true); f.Select(1);
        assert(test::copies == copies && f.pointer->wait_calls == waits);
    } else if (name == "hidden_ancestors") {
        f.Start();
        for (auto* hidden : {f.screen, f.overlay, f.Hit(2)}) {
            f.Hidden(hidden, true); const auto waits = f.pointer->wait_calls;
            f.Click(2, true); f.Gesture(LV_DIR_LEFT, f.Hit(2), true);
            assert(f.pointer->wait_calls == waits); f.Hidden(hidden, false); f.Select(0);
        }
        f.Hidden(f.overlay, true); const auto copies = test::copies;
        Publish(); f.Tick(true); f.Key(2); f.Select(0); assert(test::copies == copies);
        f.Hidden(f.overlay, false); f.Tick(); assert(test::copies == copies + 1);
    } else if (name == "foreign_targets") {
        f.Start();
        { DisplayLockGuard lock(&f.display); auto* foreign = lv_obj_create(f.screen);
          DeliverSaved(f.Hit(2)->events[0], foreign);
          f.pointer->direction = LV_DIR_LEFT; active_input = f.pointer;
          DeliverSaved(f.Root()->events[0], foreign); active_input = nullptr; }
        f.Select(0); assert(!f.pointer->wait_calls);
    } else if (name == "old_page_events") {
        f.Start(); auto* old = f.Label(0, "深圳"); f.Click(1);
        f.Gesture(LV_DIR_LEFT, old, true); f.Select(1);
        auto* hourly = f.Label(1, "未来 4 小时"); f.Click(2);
        f.Gesture(LV_DIR_LEFT, hourly, true); f.Select(2);
        f.Close(); f.Open(); const auto waits = f.pointer->wait_calls;
        f.Gesture(LV_DIR_LEFT, hourly, true); f.Select(0); assert(f.pointer->wait_calls == waits);
    } else if (name == "held_close_reopen" || name == "held_source_close") {
        f.Start();
        if (name == "held_source_close") f.Key(2);
        f.Press(f.pointer, name == "held_source_close" ? f.Control(2) : f.Hit(2));
        f.Press(f.second_pointer, name == "held_source_close" ? f.Control(1) : f.Hit(1));
        f.Close(); assert(f.pointer->waiting && f.second_pointer->waiting && !f.keypad->wait_calls);
        f.Open(); f.Select(0); assert(!f.Release(f.pointer)); assert(!f.Release(f.second_pointer));
        f.Select(0); f.Press(f.pointer, f.Hit(2)); assert(f.Release(f.pointer)); f.Select(2);
    } else if (name == "held_gesture") {
        f.Start(); f.Press(f.pointer, f.Hit(2)); f.Gesture(LV_DIR_LEFT, f.Hit(2)); f.Select(1);
        assert(f.pointer->waiting && !f.Release(f.pointer)); f.Select(1);
    } else if (name == "theme" || name == "no_display") {
        f.Start(); CheckLive(f);
        for (int selected = 0; selected < 3; ++selected) {
            f.ExternalTheme(&f.light); f.Click(selected); const auto paint = LightPaint(f, selected);
            const auto layout = f.Layout(); const auto count = objects.size(); const auto copies = test::copies;
            for (auto* theme : {&f.dark, &f.light, &f.other, static_cast<Theme*>(nullptr), &f.dark}) {
                f.ExternalTheme(theme); f.Select(selected);
                Palette(f, paint, name != "no_display" && theme == &f.dark ? kDark : kLight, selected);
                assert(f.Layout() == layout && objects.size() == count && !f.Timer()->paused);
                assert(test::copies == copies);
            }
        }
        f.Key(2); f.Select(2, true); const auto layout = f.Layout();
        f.ExternalTheme(&f.light); assert(f.Layout() == layout);
        if (name == "no_display") assert(!f.display.theme_reads);
    } else if (name == "version") {
        f.Start(); CheckLive(f); const auto copies = test::copies;
        auto* temperature = f.Label(0, "26"); const auto writes = temperature->label_writes;
        for (int i = 0; i < 50; ++i) { test::At(2026, 10, 25, 0, i); f.Tick(); }
        assert(test::copies == copies && temperature->label_writes == writes);
        f.Click(2); test::snapshot.forecast.temperature = 12; Publish(); f.Tick();
        f.Select(2); assert(temperature->text == "12" && test::copies == copies + 1);
        const auto count = objects.size();
        for (int i = 0; i < 100; ++i) {
            test::snapshot.forecast.code = i; Publish(); f.Tick();
            f.ExternalTheme(i % 2 ? &f.dark : &f.light); assert(objects.size() == count);
        }
        assert(test::refreshes == 1);  // Navigation/timer never fabricates HTTP work.
    } else if (name == "missing" || name == "null_service") {
        f.Start(); ClockLabels(f);
        for (int i = 0; i < 3; ++i) f.Label(i, "位置未知");
        for (int i = 1; i < 3; ++i) f.Label(i, "未加载");
        f.Label(0, "--%"); f.Label(0, "-- km/h"); f.Label(0, "未加载 · 体感 --°");
        assert(!Visible(f.Label(0, "°")) && !FindLabel(f.Root(), "26"));
        if (name == "null_service") {
            assert(!test::copies && !test::refreshes && !test::visible_on);
            test::At(2026, 10, 26, 1, 2); f.Tick();
            ClockLabels(f, "10/26 · 星期一", "01:02", "10/26 01:02");
            f.Close(); test::At(2027, 1, 1, 3, 4); f.Open();
            ClockLabels(f, "01/01 · 星期五", "03:04", "01/01 03:04");
            assert(!test::copies && !test::refreshes && !test::visible_on);
        } else {
            for (auto state : {stopwatch_weather::State::Locating, stopwatch_weather::State::Updating,
                               stopwatch_weather::State::Offline, stopwatch_weather::State::Error}) {
                test::snapshot.state = state; Publish(); f.Tick();
            }
            f.Label(0, "更新失败 · 体感 --°"); LiveData(); f.Tick(); CheckLive(f);
            test::snapshot.forecast.humidity = stopwatch_weather::kMissing;
            test::snapshot.forecast.wind = stopwatch_weather::kMissing;
            test::snapshot.forecast.feels_like = stopwatch_weather::kMissing;
            for (auto& h : test::snapshot.forecast.hours) h.precipitation = stopwatch_weather::kMissing;
            for (auto& d : test::snapshot.forecast.days) d.precipitation = stopwatch_weather::kMissing;
            Publish(); f.Tick(); f.Label(0, "--%"); f.Label(0, "-- km/h");
            f.Label(0, "多云 · 体感 --°"); f.Label(1, "--%"); f.Label(2, "--%");
            test::snapshot.has_data = false; test::snapshot.location = stopwatch_weather::BuiltinLocation(0);
            test::snapshot.forecast = {}; Publish(); f.Tick(); f.Label(0, "北京");
            assert(!FindLabel(f.Root(), "26") && !FindLabel(f.Root(), "64%"));
        }
    } else if (name == "temperature") {
        f.Start(); auto* temperature = f.Label(0, "26");
        auto* page = f.Page(0); lv_obj_t* sign = nullptr;
        for (auto* object : page->children)
            if (object->kind == "panel" && object->width == 18 && object->height == 5) sign = object;
        assert(sign); auto* degree = f.Label(0, "°"); const auto count = objects.size();
        for (float value : {-40.f, -1.f, 0.f, 99.f, 100.f, -0.1f, stopwatch_weather::kMissing}) {
            test::snapshot.forecast.temperature = value; Publish(); f.Tick();
            const bool valid = std::isfinite(value); const long rounded = valid ? std::lround(value) : 0;
            assert(Visible(temperature) == valid && Visible(sign) == (valid && rounded < 0));
            assert(Visible(degree) == valid);
            if (valid) {
                assert(temperature->text == std::to_string(std::abs(rounded)));
                assert(temperature->text.find('-') == std::string::npos);
                assert(temperature->x >= 192 && temperature->x + temperature->width <= 356);
                assert(degree->x >= temperature->x + temperature->width - 6 &&
                       degree->x + degree->width <= 356);
                const bool fallback = value == -40 || value == 100;
                assert(temperature->font == (fallback ? &font_stopwatch_weather_30 : &CommissionerMedium108));
                if (rounded >= 0 && rounded <= 99) {
                    assert(temperature->x == 192 && temperature->y == 157 && temperature->width == 140);
                    assert(degree->x == 326 && degree->y == 154 && degree->width == 30);
                }
                if (rounded < 0) assert(sign->x >= 192 && sign->x + sign->width <= temperature->x);
            }
            // Local sign/large-value fallbacks must not permanently recenter the
            // ordinary two-digit layout or move the restored icon/degree anchors.
            test::snapshot.forecast.temperature = 26; Publish(); f.Tick();
            LabelGeometry(f, 0, "26", 192, 157, 140, &CommissionerMedium108);
            LabelGeometry(f, 0, "°", 326, 154, 30, &font_noto_sans_basic_30_4);
            CheckIcon(f, 0, "partly", 100, 164, 78);
            assert(!Visible(sign) && objects.size() == count);
        }
    } else if (name == "conditions") {
        f.Start(); const auto count = objects.size(); std::string previous;
        for (const auto& sample : std::vector<std::pair<int, const char*>>{{0,"晴"},{2,"多云"},{45,"雾"},
                 {61,"雨"},{75,"雪"},{95,"雷雨"},{97,"雷雨"},{-1,"未知"},{999,"未知"}}) {
            test::snapshot.forecast.code = sample.first; Publish(); f.Tick();
            f.Label(0, (std::string(sample.second) + " · 体感 27°").c_str());
            assert(objects.size() == count);
        }
        test::snapshot.forecast.code = 0; test::snapshot.forecast.is_day = false; Publish(); f.Tick();
        f.Label(0, "晴夜 · 体感 27°");
        test::snapshot.forecast.code = 97; Publish(); f.Tick(); f.Label(0, "雷雨 · 体感 27°");
    } else if (name == "clock_minutes") {
        test::At(2026, 10, 25, 0, 59, 58); f.Start();
        ClockLabels(f, "10/25 · 星期日", "00:59", "10/25 00:59");
        const auto copies = test::copies; const auto count = objects.size();
        const auto conversions = test::localtime_calls;
        auto* clock = f.Label(0, "00:59"); const auto writes = clock->label_writes;
        test::At(2026, 10, 25, 0, 59, 59); f.Tick();
        assert(test::localtime_calls == conversions && clock->label_writes == writes);
        test::At(2026, 10, 25, 1, 0); f.Tick();
        ClockLabels(f, "10/25 · 星期日", "01:00", "10/25 01:00");
        f.Label(1, "10/25 09");
        LabelGeometry(f, 1, "逐时预报", 255, 121, 132, &font_stopwatch_weather_20, kLight[1]);
        f.Label(1, "逐时预报，最高降雨 13%");
        assert(!FindLabel(f.Page(1), "未来 4 小时") && !FindLabel(f.Page(1), "未来4小时，最高降雨 13%"));
        assert(test::copies == copies && test::localtime_calls == conversions + 1 && objects.size() == count);
        // A clock correction can make the same rows future again, without a new snapshot.
        test::At(2026, 10, 25, 0, 59); f.Tick();
        f.Label(1, "09:00"); f.Label(1, "未来 4 小时"); f.Label(1, "未来4小时，最高降雨 13%");
        assert(test::copies == copies && test::refreshes == 1);
    } else if (name == "clock_midnight" || name == "clock_year") {
        const bool year = name == "clock_year";
        const int month = year ? 12 : 10, day = year ? 31 : 25;
        auto& forecast = test::snapshot.forecast;
        test::At(2026, month, day, 23, 59, 59);
        forecast.utc_offset = 0; forecast.observed_at = test::now;
        for (int i = 0; i < 4; ++i) forecast.hours[i].time = test::now + 61 + i * 3600;
        for (int i = 0; i < 3; ++i)
            forecast.days[i].time = static_cast<int64_t>(test::DaysFromCivil(2026, month, day)) * 86400 + i * 86400;
        Publish(); f.Start();
        ClockLabels(f, year ? "12/31 · 星期四" : "10/25 · 星期日", "23:59",
                    year ? "12/31 23:59" : "10/25 23:59");
        for (const char* caption : {"今天", "明天", "后天"}) f.Label(2, caption);
        f.Label(1, year ? "01/01 00" : "10/26 00");
        const auto copies = test::copies; const auto count = objects.size();
        test::At(year ? 2027 : 2026, year ? 1 : 10, year ? 1 : 26, 0, 0); f.Tick();
        ClockLabels(f, year ? "01/01 · 星期五" : "10/26 · 星期一", "00:00",
                    year ? "01/01 00:00" : "10/26 00:00");
        f.Label(1, "00:01");
        for (const char* caption : year ? std::array<const char*, 3>{"12/31", "01/01", "01/02"}
                                       : std::array<const char*, 3>{"10/25", "10/26", "10/27"}) f.Label(2, caption);
        assert(!FindLabel(f.Page(2), "今天") && !FindLabel(f.Page(2), "明天") && !FindLabel(f.Page(2), "后天"));
        assert(test::copies == copies && objects.size() == count);
        // Previous-year data with the same month/day must still be absolute.
        if (year) {
            test::At(2028, 12, 31, 23, 59); f.Tick();
            ClockLabels(f, "12/31 · 星期日", "23:59", "12/31 23:59");
            f.Label(2, "12/31"); assert(!FindLabel(f.Page(2), "今天"));
            f.Label(1, "01/01 00"); f.Label(1, "逐时预报");
            assert(test::copies == copies);
        }
    } else if (name == "clock_invalid") {
        test::now = 1577836799; f.Start();
        ClockLabels(f, "--/-- · 星期--", "--:--", "--/-- --:--");
        f.Label(1, "10/25 09"); f.Label(1, "逐时预报"); f.Label(1, "逐时预报，最高降雨 13%");
        for (const char* caption : {"10/25", "10/26", "10/27"}) f.Label(2, caption);
        assert(test::time_calls == 1 && !test::localtime_calls);
        const auto copies = test::copies;
        for (time_t invalid : {time_t(0), time_t(-1), time_t(1577836799)}) {
            test::now = invalid; f.Tick();
            ClockLabels(f, "--/-- · 星期--", "--:--", "--/-- --:--");
        }
        assert(!test::localtime_calls && test::copies == copies);
        test::At(2020, 1, 1, 0, 0); f.Tick(); // Inclusive Home validity threshold.
        ClockLabels(f, "01/01 · 星期三", "00:00", "01/01 00:00");
        test::At(2026, 10, 25, 0, 0); f.Tick(); CheckLive(f);
        test::now = -1; f.Tick();
        ClockLabels(f, "--/-- · 星期--", "--:--", "--/-- --:--");
        assert(!FindLabel(f.Page(0), "08:00") && !FindLabel(f.Page(0), "10/25 · 星期日"));
        f.Key(2); f.Key(0); f.SourceLabel("Data status");
        const auto body = f.Source()->children.at(1)->text;
        assert(body.find("2026/10/25 08:00") != std::string::npos && body.find("UTC+08:00") != std::string::npos);
        assert(test::copies == copies);
    } else if (name == "clock_conversion") {
        test::localtime_fails = true; f.Start();
        ClockLabels(f, "--/-- · 星期--", "--:--", "--/-- --:--");
        f.Label(1, "10/25 09"); f.Label(2, "10/25");
        const auto copies = test::copies; const auto calls = test::localtime_calls;
        f.Tick(); assert(test::localtime_calls == calls + 1);
        test::localtime_fails = false; f.Tick(); CheckLive(f); // No minute or version advance.
        test::At(2026, 10, 25, 0, 1); test::localtime_fails = true; f.Tick();
        ClockLabels(f, "--/-- · 星期--", "--:--", "--/-- --:--");
        test::localtime_fails = false; f.Tick();
        ClockLabels(f, "10/25 · 星期日", "00:01", "10/25 00:01");
        for (const char* caption : {"今天", "明天", "后天"}) f.Label(2, caption);
        assert(test::copies == copies);
    } else if (name == "clock_visibility") {
        f.Setup(); f.Tick(true); assert(!test::time_calls && !test::localtime_calls && !test::versions);
        { DisplayLockGuard lock(&f.display); f.weather.Open(); }
        assert(!Visible(f.Root())); ClockLabels(f); // Open updates before the shell unhides root.
        const auto initial_reads = test::time_calls; f.Tick(true);
        assert(test::time_calls == initial_reads);
        f.Hidden(f.Root(), false);
        for (auto* ancestor : {f.Root(), f.overlay, f.screen}) {
            const auto reads = test::time_calls, conversions = test::localtime_calls;
            const auto versions = test::versions, copies = test::copies;
            const auto layout = f.Layout();
            f.Hidden(ancestor, true); test::At(2026, 10, 25, 0, 1); f.Tick(true);
            assert(test::time_calls == reads && test::localtime_calls == conversions);
            assert(test::versions == versions && test::copies == copies);
            f.Hidden(ancestor, false); assert(f.Layout() == layout);
            test::At(2026, 10, 25, 0, 0);
        }
        f.Close(); const auto reads = test::time_calls, versions = test::versions;
        test::At(2026, 10, 26, 0, 2); f.Hidden(f.Root(), false); f.Tick(true);
        assert(test::time_calls == reads && test::versions == versions);
        f.Hidden(f.Root(), true);
        { DisplayLockGuard lock(&f.display); f.weather.Open(); }
        assert(!Visible(f.Root())); ClockLabels(f, "10/26 · 星期一", "00:02", "10/26 00:02");
        f.Label(2, "10/25"); f.Hidden(f.Root(), false);
        const auto copies = test::copies;
        test::At(2026, 10, 26, 0, 3); f.Tick();
        ClockLabels(f, "10/26 · 星期一", "00:03", "10/26 00:03");
        assert(test::copies == copies && test::refreshes == 2);
    } else if (name == "clock_local_zone") {
        // time() is UTC seconds; localtime_r supplies the device's own zone, not the forecast's.
        test::At(2026, 10, 24, 23, 45); test::now += 7 * 3600; f.Start();
        ClockLabels(f, "10/24 · 星期六", "23:45", "10/24 23:45");
        for (const char* caption : {"10/25", "10/26", "10/27"}) f.Label(2, caption);
        f.Label(1, "10/25 09");
        const auto copies = test::copies;
        test::At(2026, 10, 25, 0, 0); test::now += 7 * 3600; f.Tick();
        ClockLabels(f); // Same provider data, now agreeing on the date but already past UTC hours.
        f.Label(1, "10/25 09");
        for (const char* caption : {"今天", "明天", "后天"}) f.Label(2, caption);
        assert(test::copies == copies);
    } else if (name == "clock_offline") {
        test::snapshot.state = stopwatch_weather::State::Offline; f.Start(); ClockLabels(f);
        f.Label(0, "离线 · 多云 · 体感 27°"); f.Label(1, "离线");
        const auto copies = test::copies;
        test::At(2026, 10, 26, 12, 34); f.Tick();
        ClockLabels(f, "10/26 · 星期一", "12:34", "10/26 12:34");
        f.Label(1, "10/25 09"); f.Label(2, "10/25"); f.Label(0, "26");
        assert(test::copies == copies && test::refreshes == 1);
    } else if (name == "dates") {
        f.Start(); const char* old_tz = std::getenv("TZ"); const std::string tz = old_tz ? old_tz : "";
        auto& forecast = test::snapshot.forecast;
        for (int offset : {19800, 20700, -12600}) {
            forecast.utc_offset = offset; Publish(); f.Tick(); ClockLabels(f);
            f.Key(2); f.Key(0); f.SourceLabel("Data status");
            const auto body = f.Source()->children.at(1)->text;
            const auto stamp = stopwatch_weather::FormatLocalTime(forecast.observed_at, offset, "%Y/%m/%d %H:%M");
            assert(body.find(stamp) != std::string::npos);
            assert(body.find(stopwatch_weather::FormatUtcOffset(offset)) != std::string::npos);
            f.Key(2);
        }
        // Provider's previous day cannot be relabeled as the device's "today".
        for (const char* day : {"10/24", "10/25", "10/26"}) f.Label(2, day);
        f.Label(1, "10/24 21"); f.Label(1, "00:30");
        forecast.utc_offset = 20700; forecast.observed_at += 21 * 3600;
        forecast.hours[0].time = forecast.observed_at + 3600;
        forecast.hours[1].time = forecast.observed_at + 23 * 3600;
        forecast.hours[2].time = 0;
        forecast.days[0].time = 1792886400 + 2 * 86400;
        forecast.days[1].time = 0;
        forecast.days[2].time = 1792886400;
        Publish(); f.Tick(); ClockLabels(f);
        f.Label(1, "10/26 03"); f.Label(1, "10/27 01"); f.Label(1, "--:--");
        for (const char* day : {"10/27", "--/--", "10/25"}) f.Label(2, day);
        test::At(2026, 10, 26, 0, 0); f.Tick();
        ClockLabels(f, "10/26 · 星期一", "00:00", "10/26 00:00");
        f.Label(1, "10/26 03"); // Already past in UTC, even though provider-local hour is 03.
        f.Label(2, "明天"); f.Label(2, "10/25");
        forecast.observed_at = static_cast<int64_t>(test::DaysFromCivil(2026, 12, 31)) * 86400 + 23 * 3600;
        forecast.hours[0].time = forecast.observed_at + 3600;
        forecast.hours[1].time = forecast.observed_at + 86400;
        forecast.days[0].time = static_cast<int64_t>(test::DaysFromCivil(2027, 1, 1)) * 86400 - forecast.utc_offset;
        forecast.days[1].time = forecast.days[0].time + 2 * 86400;
        forecast.days[2].time = forecast.days[0].time - 86400;
        test::At(2027, 1, 1, 0, 0); Publish(); f.Tick();
        ClockLabels(f, "01/01 · 星期五", "00:00", "01/01 00:00");
        f.Label(1, "01/01 05"); f.Label(1, "01/02 04");
        for (const char* day : {"今天", "后天", "12/31"}) f.Label(2, day);
        assert(tz == (std::getenv("TZ") ? std::getenv("TZ") : ""));
    } else if (name == "history") {
        f.Start(); f.Click(1);
        test::snapshot.cached = true; test::snapshot.age_known = false; Publish(); f.Tick();
        f.Select(1); ClockLabels(f); f.Label(1, "10/25 09");
        LabelGeometry(f, 1, "逐时预报", 255, 121, 132, &font_stopwatch_weather_20, kLight[1]);
        for (int page = 1; page < 3; ++page) f.Label(page, "历史预报 · 已更新");
        for (const char* day : {"10/25", "10/26", "10/27"}) f.Label(2, day);
        const auto copies = test::copies;
        test::At(2026, 10, 26, 0, 0); f.Tick(); assert(test::copies == copies);
        ClockLabels(f, "10/26 · 星期一", "00:00", "10/26 00:00");
        for (const char* day : {"10/25", "10/26", "10/27"}) f.Label(2, day);
        test::snapshot.cached = false; test::snapshot.age_known = true;
        test::snapshot.stale = true; test::snapshot.state = stopwatch_weather::State::Offline;
        Publish(); f.Tick(); f.Label(1, "历史预报 · 离线");
        // Retained observation metadata does not keep expired forecast values alive.
        test::snapshot.has_data = false; Publish(); f.Tick();
        assert(!FindLabel(f.Page(1), "27°")); f.Label(1, "--:--"); f.Label(2, "--/--");
        ClockLabels(f, "10/26 · 星期一", "00:00", "10/26 00:00");
    } else if (name == "summaries") {
        f.Start(); const auto count = objects.size();
        f.Label(1, "未来4小时，最高降雨 13%"); f.Label(2, "三日预报，最高降雨 7%");
        test::snapshot.forecast.hours[0].precipitation = stopwatch_weather::kMissing;
        test::snapshot.forecast.days[0].precipitation = stopwatch_weather::kMissing;
        Publish(); f.Tick();
        f.Label(1, "已知预报，最高降雨 13%"); f.Label(2, "已知预报，最高降雨 7%");
        for (auto& hour : test::snapshot.forecast.hours) hour.precipitation = stopwatch_weather::kMissing;
        for (auto& day : test::snapshot.forecast.days) day.precipitation = stopwatch_weather::kMissing;
        Publish(); f.Tick();
        f.Label(1, "暂无降雨概率"); f.Label(2, "暂无降雨概率");
        for (auto& hour : test::snapshot.forecast.hours) hour.precipitation = 0;
        for (auto& day : test::snapshot.forecast.days) day.precipitation = 0;
        Publish(); f.Tick();
        f.Label(1, "未来4小时，最高降雨 0%"); f.Label(2, "三日预报，最高降雨 0%");
        test::snapshot.forecast.hours[0].time = 0;
        test::snapshot.forecast.hours[0].precipitation = 99;
        test::snapshot.forecast.days[0].time = 0;
        test::snapshot.forecast.days[0].precipitation = 98;
        Publish(); f.Tick();
        f.Label(1, "已知预报，最高降雨 0%"); f.Label(2, "已知预报，最高降雨 0%");
        assert(!FindLabel(f.Page(1), "99%") && !FindLabel(f.Page(2), "98%"));
        assert(objects.size() == count);
    } else if (name == "states") {
        f.Start(); const auto count = objects.size();
        for (const auto& state : std::vector<std::pair<stopwatch_weather::State, const char*>>{
                {stopwatch_weather::State::Starting, "未加载"},
                {stopwatch_weather::State::Locating, "定位中"},
                {stopwatch_weather::State::Updating, "更新中"},
                {stopwatch_weather::State::Offline, "离线"},
                {stopwatch_weather::State::Error, "更新失败"}}) {
            test::snapshot.has_data = false; test::snapshot.state = state.first; Publish(); f.Tick();
            ClockLabels(f);
            f.Label(0, (std::string(state.second) + " · 体感 --°").c_str());
            for (int page = 1; page < 3; ++page) {
                int matches = 0;
                for (auto* child : f.Page(page)->children)
                    if (child->kind == "label" && child->text == state.second && child->y == 379) ++matches;
                assert(matches == 1); // State stays in the footer; the header is the device clock.
            }
        }
        LiveData(); test::snapshot.stale = true; test::snapshot.state = stopwatch_weather::State::Offline;
        Publish(); f.Tick();
        f.Label(1, "历史预报 · 离线"); f.Label(2, "历史预报 · 离线");
        assert(DirectLabels(f.Page(0)) == 9 && DirectLabels(f.Page(1)) == 16 && DirectLabels(f.Page(2)) == 16);
        LiveData(); Publish(); f.Tick(); CheckLive(f);
        assert(!FindLabel(f.Page(0), "已更新") && !FindLabel(f.Page(0), "自动"));
        assert(objects.size() == count);
    } else if (name == "city") {
        f.Start(); auto& loc = test::snapshot.forecast.location;
        loc.display_name = "A very long printable city name with a region and country to disambiguate";
        loc.city = "不会显示的任意中文"; Publish(); f.Tick();
        for (int i = 0; i < 3; ++i) {
            auto* city = f.Label(i, loc.display_name.c_str());
            assert(city->long_mode == LV_LABEL_LONG_SCROLL_CIRCULAR && city->width == (i == 0 ? 146 : 266));
        }
        auto* scrolling_city = f.Label(0, loc.display_name.c_str());
        const int city_writes = scrolling_city->label_writes;
        test::snapshot.state = stopwatch_weather::State::Updating; Publish(); f.Tick();
        assert(scrolling_city->label_writes == city_writes); // State polling does not reset scroll.
        loc.display_name = "未知外文名"; loc.city = "Reykjavik"; Publish(); f.Tick(); f.Label(0, "Reykjavik");
        loc.city = "不可显示"; loc.id = "geonames:12345"; Publish(); f.Tick(); f.Label(0, "geonames:12345");
        loc.id.clear(); Publish(); f.Tick(); f.Label(0, "位置未知");
    } else if (name == "source") {
        f.Start(); const auto count = objects.size();
        constexpr const char* titles[] = {"Weather data", "City search", "License", "Display changes",
                                        "Privacy / limits", "Data status"};
        constexpr const char* bodies[] = {
            "Open-Meteo\nhttps://open-meteo.com\nWeather API / documentation\nhttps://open-meteo.com/\nen/docs",
            "Open-Meteo Geocoding\nhttps://open-meteo.com/\nen/docs/geocoding-api\nCity data: GeoNames\nhttps://www.geonames.org/",
            "Data: CC BY 4.0\nhttps://creativecommons.org/\nlicenses/by/4.0/\nhttps://open-meteo.com/\nen/licence",
            "Values rounded for display.\nConditions translated / grouped.\nIcons are simplified.\nClock: device local time.\nForecast: provider UTC offset.\nNot an official weather alert.",
            "IP location: https://ipwho.is\nhttps://ipwhois.io/documentation\nProviders see public IP / location.\nFree API: non-commercial use.\nNo availability guarantee.",
        };
        for (int selected = 0; selected < 3; ++selected) {
            f.Click(selected); assert(Visible(f.SourceHit())); f.Key(2); f.Select(selected, true);
            for (int detail = 0; detail < 6; ++detail) {
                f.SourceLabel(titles[detail]);
                const auto body = f.Source()->children.at(1)->text;
                if (detail < 5) assert(body == bodies[detail]); // Preserve complete original notices/URLs.
                else {
                    assert(body.find("2026/10/25") != std::string::npos && body.find("08:00") != std::string::npos);
                    assert(body.find("UTC+08:00") != std::string::npos);
                }
                assert(f.Source()->children.at(2)->text == std::to_string(detail + 1) + " / 6   Double B: Back");
                f.Key(1);
            }
            f.SourceLabel("Weather data"); f.Key(0); f.SourceLabel("Data status");
            f.Event(f.Control(1)); f.Select(selected);
            f.Event(f.SourceHit()); f.Select(selected, true); f.Event(f.Control(2)); f.SourceLabel("City search");
            f.Event(f.Control(0)); f.SourceLabel("Weather data"); f.Key(2); f.Select(selected);
            f.Event(f.Control(2), true); f.Select(selected); // Hidden source release is ignored.
        }
        f.Key(2); f.Key(0); f.SourceLabel("Data status");
        test::snapshot.forecast.utc_offset = 20700; Publish(); f.Tick();
        auto body = f.Source()->children.at(1)->text;
        assert(body.find("05:45") != std::string::npos && body.find("UTC+05:45") != std::string::npos);
        test::snapshot.cached = true; test::snapshot.age_known = false; Publish(); f.Tick();
        f.SourceLabel("Data status"); f.Key(2); f.Select(2);
        assert(objects.size() == count && test::refreshes == 1);
    } else if (name == "source_metadata") {
        f.Start(); const auto count = objects.size(); f.Key(2); f.Key(0); f.SourceLabel("Data status");
        auto body = f.Source()->children.at(1)->text;
        assert(body.find("2026/10/25 08:00") != std::string::npos && body.find("UTC+08:00") != std::string::npos);
        test::snapshot.cached = true; test::snapshot.age_known = false; Publish(); f.Tick();
        body = f.Source()->children.at(1)->text;
        assert(body.find("2026/10/25 08:00") != std::string::npos && body.find("age unknown") != std::string::npos);
        test::snapshot.has_data = false; test::snapshot.stale = true;
        test::snapshot.state = stopwatch_weather::State::Offline; Publish(); f.Tick();
        body = f.Source()->children.at(1)->text;
        assert(body.find("2026/10/25 08:00") != std::string::npos && body.find("No values") != std::string::npos);
        f.Key(2); ClockLabels(f); f.Label(0, "离线 · 体感 --°"); f.Label(1, "离线"); f.Label(2, "离线");
        const auto provider_body = f.Source()->children.at(1)->text;
        test::At(2027, 1, 1, 12, 34); f.Tick();
        assert(f.Source()->children.at(1)->text == provider_body);
        ClockLabels(f, "01/01 · 星期五", "12:34", "01/01 12:34");
        test::snapshot.location = stopwatch_weather::BuiltinLocation(0); Publish(); f.Tick();
        for (int page = 0; page < 3; ++page) f.Label(page, "北京");
        f.Key(2); f.Key(0); f.SourceLabel("Data status");
        body = f.Source()->children.at(1)->text;
        assert(body.find("2026/10/25") == std::string::npos && body.find("No provider update available") != std::string::npos);
        assert(objects.size() == count && test::refreshes == 1);
    } else {
        std::cerr << "Unknown Weather case: " << name << '\n'; return 2;
    }
    f.NoWrites();
    assert(test::event_depth == 0 && test::lock_depth == 0 && !active_input);
    return 0;
}
'''


def compile_weather(directory, enabled):
    """Compile complete production UI/data TUs and real headers, never sliced methods."""
    directory = pathlib.Path(directory)
    directory.mkdir()
    (directory / "sdkconfig.h").write_text(
        f"#define CONFIG_STOPWATCH_LOCAL_APPS {int(enabled)}\n", encoding="utf-8"
    )
    flags = [COMPILER, "-std=c++17", "-Wall", "-Wextra", "-Werror",
             "-finput-charset=UTF-8", "-fexec-charset=UTF-8",
             "-I", str(directory), "-I", str(BOARD)]
    binary = directory / "weather_test.exe"
    harness = directory / "weather_test.cc"
    if enabled:
        headers = {"test_runtime.h": RUNTIME, "settings.h": SETTINGS,
                   "display/display.h": DISPLAY, "test_clock.h": CLOCK,
                   "test_clock_bindings.h": CLOCK_BINDINGS, "lvgl.h": LVGL}
        for name, content in headers.items():
            path = directory / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        harness.write_text(HARNESS, encoding="utf-8")
        production_object = directory / "stopwatch_weather.o"
        subprocess.run(flags + ["-include", str(directory / "test_clock_bindings.h"), "-c",
                       str(BOARD / "stopwatch_weather.cc"), "-o", str(production_object)],
                       check=True, timeout=60, env=HOST_ENV)
        sources = [str(harness), str(production_object), str(BOARD / "stopwatch_weather_data.cc")]
    else:
        # No LVGL, service, display, settings, clock or firmware dependencies.
        harness.write_text('#include "stopwatch_weather.h"\nint main() { return 0; }\n', encoding="utf-8")
        sources = [str(harness), str(BOARD / "stopwatch_weather.cc")]
    result = subprocess.run(flags + sources + ["-o", str(binary)], timeout=60, env=HOST_ENV,
                            capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode:
        raise AssertionError(result.stdout + result.stderr)
    return str(binary)


@unittest.skipUnless(COMPILER, "requires g++ (D:/sdk/mingw64/bin/g++.exe or PATH)")
class StopwatchWeatherTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="stopwatch-weather-test-")
        cls.addClassCleanup(cls.tmp.cleanup)
        cls.binary = compile_weather(pathlib.Path(cls.tmp.name) / "enabled", True)
        cls.disabled_binary = compile_weather(pathlib.Path(cls.tmp.name) / "disabled", False)

    def run_case(self, name, timezone=None):
        env = HOST_ENV if timezone is None else {**HOST_ENV, "TZ": timezone}
        result = subprocess.run([self.binary, name], timeout=10, env=env,
                                capture_output=True, text=True, encoding="utf-8", errors="replace")
        self.assertEqual(result.returncode, 0,
                         f"Weather case {name!r} exited {result.returncode}\n{result.stdout}{result.stderr}")

    def test_setup_is_idempotent_with_three_pages_and_separate_transparent_source(self): self.run_case("setup")
    def test_original_v2_geometry_fonts_and_no_extra_status_or_date_rows(self): self.run_case("layout")
    def test_original_partly_cloud_rain_and_three_shape_drop_primitives_use_ceil_scaling(self): self.run_case("primitives")
    def test_transparent_header_single_click_swipe_tail_close_reopen_and_dot_separation(self): self.run_case("source_hit")
    def test_open_close_notifies_service_and_pauses_one_shell_owned_timer(self): self.run_case("lifecycle")
    def test_a_b_wrap_three_pages_without_refresh(self): self.run_case("buttons")
    def test_swipes_wrap_ignore_other_axes_and_wait_release(self): self.run_case("gestures")
    def test_dots_and_real_pointer_release_keep_persistent_objects(self): self.run_case("dots")
    def test_inactive_retained_events_timer_and_keys_are_ignored(self): self.run_case("inactive")
    def test_hidden_root_rejects_retained_events_timer_and_keys(self): self.run_case("hidden_root")
    def test_hidden_target_and_ancestors_reject_events_and_timer(self): self.run_case("hidden_ancestors")
    def test_saved_callbacks_reject_foreign_targets(self): self.run_case("foreign_targets")
    def test_old_page_events_do_not_mutate_new_page_or_reopened_session(self): self.run_case("old_page_events")
    def test_close_reopen_suppresses_all_held_pointers_not_keypad(self): self.run_case("held_close_reopen")
    def test_source_close_reopen_has_same_held_release_guard(self): self.run_case("held_source_close")
    def test_swipe_discards_trailing_dot_click(self): self.run_case("held_gesture")
    def test_theme_repaints_all_prebuilt_inks_without_rebuild_nvs_or_page_change(self): self.run_case("theme")
    def test_missing_display_uses_light_palette(self): self.run_case("no_display")
    def test_only_new_snapshot_versions_copy_and_update_without_object_churn(self): self.run_case("version")
    def test_no_data_states_and_missing_optional_measurements_never_become_demo_or_zero(self): self.run_case("missing")
    def test_null_service_is_explicit_placeholder_only(self): self.run_case("null_service")
    def test_negative_zero_three_digit_and_missing_temperature_layout(self): self.run_case("temperature")
    def test_all_conditions_night_97_thunderstorm_and_unknown_reuse_prebuilt_icons(self): self.run_case("conditions")
    def test_device_minute_updates_and_forecast_expiry_without_snapshot_copy(self): self.run_case("clock_minutes")
    def test_unchanged_snapshot_relabels_hours_and_days_across_midnight(self): self.run_case("clock_midnight")
    def test_year_rollover_and_same_month_day_in_different_year_stay_unambiguous(self): self.run_case("clock_year")
    def test_invalid_clock_never_falls_back_to_observation_and_recovers_at_home_threshold(self): self.run_case("clock_invalid")
    def test_localtime_failure_retries_and_recovers_in_same_minute(self): self.run_case("clock_conversion")
    def test_clock_open_before_unhide_closed_hidden_ancestors_and_reopen(self): self.run_case("clock_visibility")
    def test_device_local_calendar_is_not_utc_or_provider_offset(self): self.run_case("clock_local_zone")
    def test_offline_unchanged_snapshot_keeps_current_clock(self): self.run_case("clock_offline")
    def test_provider_dates_half_and_quarter_offsets_do_not_use_or_change_global_tz(self):
        for timezone in ("UTC0", "EST5EDT", "JST-9"):
            with self.subTest(timezone=timezone):
                self.run_case("dates", timezone)
                self.run_case("clock_local_zone", timezone)
    def test_boot_cache_and_stale_history_use_absolute_dates_and_expired_values_disappear(self): self.run_case("history")
    def test_rain_summaries_derive_maximum_ignore_missing_times_and_distinguish_unknown_from_zero(self): self.run_case("summaries")
    def test_no_data_and_history_states_reuse_original_condition_and_footer_rows(self): self.run_case("states")
    def test_long_city_scrolls_and_unsupported_name_has_safe_ascii_identity(self): self.run_case("city")
    def test_source_detail_touch_and_button_navigation_return_to_original_main_page(self): self.run_case("source")
    def test_source_preserves_same_location_expired_metadata_but_never_leaks_an_old_city_update(self): self.run_case("source_metadata")

    def test_clock_remains_read_only_and_ui_has_no_timezone_network_or_persistence_calls(self):
        import re
        source = (BOARD / "stopwatch_weather.cc").read_text(encoding="utf-8")
        forbidden = (
            r"\b(?:settimeofday|clock_settime|stime|adjtime|adjtimex|setenv|unsetenv|"
            r"putenv|_putenv|_putenv_s|tzset|_tzset|sntp_set_time_sync_notification_cb|"
            r"esp_netif_sntp_init|esp_sntp_init|sntp_init|esp_http_client_init|"
            r"esp_http_client_perform|esp_http_client_open|socket|connect|send|recv|"
            r"fopen|fwrite|nvs_set_\w+|nvs_commit)\s*\("
        )
        self.assertIsNone(re.search(forbidden, source))
        self.assertNotRegex(source, r"\bSettings\s*\(|\bSetTheme\s*\(")
        self.assertNotRegex(source, r"#\s*include\s*[<\"](?:settings|esp_sntp|esp_http_client|nvs)[^>\"]*[>\"]")
        self.assertEqual(len(re.findall(r"\blv_timer_create\s*\(", source)), 1)

    def test_feature_disabled_compiles_without_firmware_stubs(self):
        result = subprocess.run([self.disabled_binary], timeout=10, env=HOST_ENV)
        self.assertEqual(result.returncode, 0)

    def test_generator_is_reproducible_and_covers_condition_city_settings_literals(self):
        import runpy
        import sys
        generator = ROOT / "scripts/generate_stopwatch_weather_fonts.py"
        generated = subprocess.run([sys.executable, str(generator), "--check"], capture_output=True,
                                   text=True, encoding="utf-8", errors="replace", timeout=30)
        self.assertEqual(generated.returncode, 0, generated.stdout + generated.stderr)
        namespace = runpy.run_path(str(generator))
        twenty = set(namespace["TEXT_20"])
        needed = ("晴夜多云雾雨雪雷未知北京上海广州深圳杭州成都重庆武汉南京西安香港台北"
                  "确认取消返回历史年龄未知定位中更新失败离线星期一二三四五六日今天明后"
                  "未来小时最高降雨概率暂无已知预报")
        self.assertFalse(set(needed) - twenty)
        import re
        for filename, charset in (("stopwatch_weather.cc", twenty), ("stopwatch_settings.cc", twenty)):
            source = (BOARD / filename).read_text(encoding="utf-8")
            # All fixed CJK string literals in these two controlled UIs must be offline-covered.
            literals = re.findall(r'"([^"\n]*)"', source)
            chinese = {c for text in literals for c in text if "\u4e00" <= c <= "\u9fff"}
            self.assertFalse(chinese - charset, f"{filename}: uncovered glyphs {sorted(chinese - charset)}")


if __name__ == "__main__":
    unittest.main()
