"""Compile the real alarm UI/header against a non-rendering LVGL/service model.

Exercises touch press generations, persistent row rebinding, physical-button
focus, async acceptance versus commit, conflicts, drafts, and hidden timers.
No ESP-IDF build, real NVS, pixel rendering, audio, or hardware is simulated.
"""
import pathlib
import shutil
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
BOARD = ROOT / "main/boards/m5stack/stopwatch"
COMPILER = shutil.which("c++") or shutil.which("g++")

LVGL = r'''
#pragma once
#include <algorithm>
#include <cassert>
#include <cstdint>
#include <memory>
#include <string>
#include <vector>

enum lv_obj_flag_t : uint32_t {
    LV_OBJ_FLAG_SCROLLABLE = 1, LV_OBJ_FLAG_GESTURE_BUBBLE = 2,
    LV_OBJ_FLAG_CLICKABLE = 4, LV_OBJ_FLAG_HIDDEN = 8, LV_OBJ_FLAG_EVENT_BUBBLE = 16
};
enum lv_state_t {
    LV_STATE_PRESSED = 1, LV_STATE_FOCUSED = 2, LV_STATE_DISABLED = 4, LV_STATE_CHECKED = 8
};
enum {
    LV_ALIGN_CENTER, LV_TEXT_ALIGN_CENTER,
    LV_EVENT_PRESSED, LV_EVENT_CLICKED, LV_EVENT_VALUE_CHANGED, LV_EVENT_LONG_PRESSED,
    LV_DIR_VER, LV_SCROLLBAR_MODE_ACTIVE, LV_LABEL_LONG_SCROLL_CIRCULAR,
    LV_PART_MAIN = 0x100, LV_PART_INDICATOR = 0x200, LV_PART_KNOB = 0x300,
    LV_PART_SELECTED = 0x400, LV_ANIM_OFF = 0, LV_OPA_COVER = 255,
    LV_RADIUS_CIRCLE = 32767, LV_ROLLER_MODE_INFINITE = 1,
    LV_INDEV_TYPE_POINTER = 1, LV_INDEV_TYPE_KEYPAD = 2,
    LV_INDEV_STATE_RELEASED = 0, LV_INDEV_STATE_PRESSED = 1
};
using lv_color_t = uint32_t;
inline lv_color_t lv_color_hex(uint32_t color) { return color; }
inline lv_color_t lv_color_black() { return 0; }
inline lv_color_t lv_color_white() { return 0xffffff; }
struct lv_font_t { int line_height; };
#define LV_FONT_DECLARE(name) extern const lv_font_t name
struct lv_obj_t;
struct lv_event_t { lv_obj_t* target; void* data; int code; };
using lv_event_cb_t = void (*)(lv_event_t*);
struct Binding { lv_event_cb_t callback; int code; void* data; };
struct lv_obj_t {
    lv_obj_t* parent = nullptr;
    std::string kind, text, options;
    unsigned flags = LV_OBJ_FLAG_SCROLLABLE | LV_OBJ_FLAG_CLICKABLE, state = 0;
    bool alive = true;
    int x = 0, y = 0, width = 0, height = 0, scroll_y = 0, value = 0, rows = 0;
    int label_writes = 0, scroll_requests = 0;
    const lv_font_t* font = nullptr;
    std::vector<lv_obj_t*> children;
    std::vector<Binding> events;
};
inline int event_depth = 0;
inline std::vector<std::unique_ptr<lv_obj_t>> objects;
inline void Live(const lv_obj_t* object) { assert(object && object->alive); }
inline lv_obj_t* Make(lv_obj_t* parent, const char* kind) {
    objects.emplace_back(std::make_unique<lv_obj_t>());
    auto* object = objects.back().get(); object->parent = parent; object->kind = kind;
    if (parent) { Live(parent); parent->children.push_back(object); }
    return object;
}
inline lv_obj_t* lv_obj_create(lv_obj_t* parent) { return Make(parent, "panel"); }
inline lv_obj_t* lv_button_create(lv_obj_t* parent) { return Make(parent, "button"); }
inline lv_obj_t* lv_label_create(lv_obj_t* parent) { return Make(parent, "label"); }
inline lv_obj_t* lv_switch_create(lv_obj_t* parent) { return Make(parent, "switch"); }
inline lv_obj_t* lv_roller_create(lv_obj_t* parent) { return Make(parent, "roller"); }
inline void lv_obj_delete(lv_obj_t* object) {
    Live(object); assert(event_depth == 0);
    const auto children = object->children;
    for (auto* child : children) lv_obj_delete(child);
    object->children.clear(); object->alive = false;
    if (object->parent) {
        auto& siblings = object->parent->children;
        siblings.erase(std::remove(siblings.begin(), siblings.end(), object), siblings.end());
    }
}
inline void lv_obj_add_flag(lv_obj_t* object, lv_obj_flag_t flag) { Live(object); object->flags |= flag; }
inline void lv_obj_remove_flag(lv_obj_t* object, lv_obj_flag_t flag) { Live(object); object->flags &= ~flag; }
inline bool lv_obj_has_flag(const lv_obj_t* object, lv_obj_flag_t flag) { Live(object); return object->flags & flag; }
inline void lv_obj_add_state(lv_obj_t* object, lv_state_t state) { Live(object); object->state |= state; }
inline void lv_obj_remove_state(lv_obj_t* object, lv_state_t state) { Live(object); object->state &= ~state; }
inline bool lv_obj_has_state(const lv_obj_t* object, lv_state_t state) { Live(object); return object->state & state; }
inline lv_obj_t* lv_obj_get_parent(const lv_obj_t* object) { Live(object); return object->parent; }
inline bool TestVisible(lv_obj_t* object) {
    Live(object); return !(object->flags & LV_OBJ_FLAG_HIDDEN) && (!object->parent || TestVisible(object->parent));
}
inline void lv_obj_remove_style_all(lv_obj_t* object) { Live(object); }
inline void lv_obj_set_size(lv_obj_t* object, int width, int height) {
    Live(object); object->width = width; object->height = height;
}
inline void lv_obj_set_width(lv_obj_t* object, int width) { Live(object); object->width = width; }
inline void lv_obj_set_pos(lv_obj_t* object, int x, int y) { Live(object); object->x = x; object->y = y; }
inline void lv_obj_center(lv_obj_t* object) {
    Live(object); object->x = (object->parent->width - object->width) / 2;
    object->y = (object->parent->height - object->height) / 2;
}
inline void lv_obj_set_style_text_font(lv_obj_t* object, const lv_font_t* font, int) { Live(object); object->font = font; }
#define STYLE(name, type) inline void name(lv_obj_t* object, type, int) { Live(object); }
STYLE(lv_obj_set_style_bg_color, lv_color_t)
STYLE(lv_obj_set_style_bg_opa, int)
STYLE(lv_obj_set_style_text_color, lv_color_t)
STYLE(lv_obj_set_style_text_align, int)
STYLE(lv_obj_set_style_text_line_space, int)
STYLE(lv_obj_set_style_radius, int)
STYLE(lv_obj_set_style_border_color, lv_color_t)
STYLE(lv_obj_set_style_border_width, int)
STYLE(lv_obj_set_style_pad_bottom, int)
#undef STYLE
inline void lv_obj_set_scroll_dir(lv_obj_t* object, int) { Live(object); }
inline void lv_obj_set_scrollbar_mode(lv_obj_t* object, int) { Live(object); }
inline void lv_obj_scroll_to_y(lv_obj_t* object, int y, int) { Live(object); object->scroll_y = y; }
inline void lv_obj_scroll_to_view(lv_obj_t* object, int) {
    Live(object); ++object->scroll_requests;
    auto* parent = object->parent;
    if (object->y < parent->scroll_y) parent->scroll_y = object->y;
    else if (object->y + object->height > parent->scroll_y + parent->height)
        parent->scroll_y = object->y + object->height - parent->height;
}
inline void lv_label_set_text(lv_obj_t* object, const char* text) { Live(object); object->text = text; ++object->label_writes; }
inline const char* lv_label_get_text(lv_obj_t* object) { Live(object); return object->text.c_str(); }
inline void lv_label_set_long_mode(lv_obj_t* object, int) { Live(object); }
inline void lv_roller_set_options(lv_obj_t* object, const char* options, int) { Live(object); object->options = options; }
inline void lv_roller_set_visible_row_count(lv_obj_t* object, int rows) { Live(object); object->rows = rows; }
inline void lv_roller_set_selected(lv_obj_t* object, unsigned value, int) { Live(object); object->value = value; }
inline unsigned lv_roller_get_selected(lv_obj_t* object) { Live(object); return object->value; }
inline void lv_obj_add_event_cb(lv_obj_t* object, lv_event_cb_t callback, int code, void* data) {
    Live(object); object->events.push_back({callback, code, data});
}
inline void* lv_event_get_user_data(lv_event_t* event) { return event->data; }
inline lv_obj_t* lv_event_get_target(lv_event_t* event) { return event->target; }
inline int lv_event_get_code(lv_event_t* event) { return event->code; }
inline void Emit(lv_obj_t* object, int code) {
    Live(object); const auto bindings = object->events; ++event_depth;
    for (const auto& binding : bindings) if (binding.code == code) {
        lv_event_t event{object, binding.data, code}; binding.callback(&event);
    }
    --event_depth;
}
struct lv_indev_t { int type = LV_INDEV_TYPE_POINTER, state = LV_INDEV_STATE_RELEASED, waits = 0; };
inline lv_indev_t pointer, keypad{LV_INDEV_TYPE_KEYPAD, LV_INDEV_STATE_RELEASED, 0};
inline lv_indev_t* lv_indev_get_next(lv_indev_t* input) {
    return !input ? &pointer : input == &pointer ? &keypad : nullptr;
}
inline int lv_indev_get_type(lv_indev_t* input) { return input->type; }
inline int lv_indev_get_state(lv_indev_t* input) { return input->state; }
inline void lv_indev_wait_release(lv_indev_t* input) { ++input->waits; }
struct lv_timer_t {
    void (*callback)(lv_timer_t*); void* data; unsigned period;
    bool paused = false, alive = true;
};
inline std::vector<std::unique_ptr<lv_timer_t>> timers;
inline lv_timer_t* lv_timer_create(void (*callback)(lv_timer_t*), unsigned period, void* data) {
    assert(period >= 200 && period <= 500);
    timers.emplace_back(new lv_timer_t{callback, data, period}); return timers.back().get();
}
inline void lv_timer_pause(lv_timer_t* timer) { assert(timer->alive); timer->paused = true; }
inline void lv_timer_resume(lv_timer_t* timer) { assert(timer->alive); timer->paused = false; }
inline void lv_timer_delete(lv_timer_t* timer) { assert(timer->alive); timer->alive = false; }
inline void* lv_timer_get_user_data(lv_timer_t* timer) { return timer->data; }
'''

SERVICE = r'''
#pragma once
#include "stopwatch_alarm_service.h"
#include <cassert>
#include <map>

namespace test {
using namespace stopwatch_alarm;
struct Command { int kind; uint32_t id, revision; bool enabled; int hour, minute; };
inline Snapshot snapshot;
inline std::map<uint32_t, Result> results;
inline std::map<uint32_t, Command> commands;
inline int calls = 0, reads = 0, status_reads = 0, versions = 0;
inline uint32_t next_request = 1, next_alarm = 100;
inline std::string rejection;
inline void Bump() { ++snapshot.version; }
inline void Changed() { ++snapshot.revision; Bump(); }
inline Result Accept(Command command) {
    ++calls;
    if (!rejection.empty()) return {false, false, 0, 0, snapshot.revision, rejection};
    const auto id = next_request++;
    commands[id] = command;
    results[id] = {true, true, id, 0, snapshot.revision, ""};
    snapshot.pending = true; ++snapshot.pending_count; Bump();
    return results[id];
}
inline void Complete(uint32_t id, std::string error = "") {
    auto& result = results.at(id); const auto& command = commands.at(id);
    assert(result.pending); result.pending = false;
    if (error.empty() && command.kind != 0 && command.revision != snapshot.revision) error = "conflict";
    if (error.empty()) {
        if (command.kind == 0) {
            result.id = next_alarm++;
            snapshot.alarms.push_back({result.id, command.hour, command.minute, true, "", "", ""});
        } else {
            auto it = std::find_if(snapshot.alarms.begin(), snapshot.alarms.end(),
                                   [&](const Alarm& alarm) { return alarm.id == command.id; });
            if (it == snapshot.alarms.end()) error = "not_found";
            else if (command.kind == 1) it->enabled = command.enabled;
            else snapshot.alarms.erase(it);
            result.id = command.id;
        }
    }
    result.ok = error.empty(); result.error = error;
    if (result.ok) Changed();
    --snapshot.pending_count; snapshot.pending = snapshot.pending_count != 0;
    result.revision = snapshot.revision; snapshot.last_result = result; Bump();
}
inline void Fill(unsigned count) {
    snapshot.initialized = true;
    for (unsigned i = 0; i < count; ++i) snapshot.alarms.push_back({10 + i, 7 + int(i % 17), int(i), true, "", "", ""});
    Changed();
}
}
struct StopwatchAlarmService::Impl {};
StopwatchAlarmService::StopwatchAlarmService() = default;
StopwatchAlarmService::~StopwatchAlarmService() = default;
stopwatch_alarm::Snapshot StopwatchAlarmService::GetSnapshot() const { ++test::reads; return test::snapshot; }
uint32_t StopwatchAlarmService::Version() const { ++test::versions; return test::snapshot.version; }
stopwatch_alarm::Result StopwatchAlarmService::GetRequest(uint32_t id) const {
    ++test::status_reads; const auto it = test::results.find(id);
    return it == test::results.end() ? stopwatch_alarm::Result{false, false, id, 0, 0, "unknown_request"} : it->second;
}
stopwatch_alarm::Result StopwatchAlarmService::Add(int hour, int minute, const std::string& reminder, const std::string& date) {
    assert(reminder.empty() && date.empty());
    return test::Accept({0, 0, 0, true, hour, minute});
}
stopwatch_alarm::Result StopwatchAlarmService::SetEnabled(uint32_t id, bool enabled, uint32_t revision) {
    return test::Accept({1, id, revision, enabled, 0, 0});
}
stopwatch_alarm::Result StopwatchAlarmService::Remove(uint32_t id, uint32_t revision) {
    return test::Accept({2, id, revision, false, 0, 0});
}
'''

HARNESS = r'''
#include "stopwatch_alarm.h"
#include "alarm_test_service.h"
#include <iostream>

extern const lv_font_t font_noto_sans_basic_20_4{20};
extern const lv_font_t font_noto_sans_basic_30_4{30};
extern "C" {
extern const lv_font_t MontserratSemiBold26{26};
extern const lv_font_t CommissionerMedium108{108};
}

lv_obj_t* Find(lv_obj_t* root, const std::string& kind, const std::string& text = "") {
    if (!TestVisible(root)) return nullptr;
    if (root->kind == kind && (text.empty() || root->text == text)) return root;
    if (root->kind == "button" && kind == "button" && !root->children.empty() && root->children[0]->text == text)
        return root;
    for (auto* child : root->children) if (auto* match = Find(child, kind, text)) return match;
    return nullptr;
}
void Press(lv_obj_t* object) { assert(TestVisible(object)); pointer.state = LV_INDEV_STATE_PRESSED; Emit(object, LV_EVENT_PRESSED); }
void Release(lv_obj_t* object, int code = LV_EVENT_CLICKED) { pointer.state = LV_INDEV_STATE_RELEASED; Emit(object, code); }
bool Click(lv_obj_t* object) {
    assert(object); if (lv_obj_has_state(object, LV_STATE_DISABLED)) return false;
    Press(object); Release(object); return true;
}
struct Fixture {
    StopwatchAlarmService service;
    StopwatchAlarm alarm{nullptr, &service};
    lv_obj_t* screen = lv_obj_create(nullptr);
    Fixture() { lv_obj_set_size(screen, 466, 466); alarm.Setup(screen); alarm.Open(); }
    ~Fixture() { alarm.Close(); }
    void Tick() { for (auto& timer : timers) if (timer->alive && !timer->paused) timer->callback(timer.get()); }
    lv_obj_t* Label(const std::string& text) {
        auto* object = Find(screen, "label", text);
        if (!object) std::cerr << "Missing label: " << text << std::endl;
        assert(object); return object;
    }
    lv_obj_t* Button(const std::string& text) { auto* object = Find(screen, "button", text); assert(object); return object; }
    bool Click(const std::string& text) { return ::Click(Button(text)); }
    lv_obj_t* Row(int index) {
        for (const auto& object : objects) if (object->alive && object->text == "No alarms")
            return object->parent->children.at(index + 1);
        assert(false); return nullptr;
    }
    lv_obj_t* Switch(int index) { return Row(index)->children.at(2); }
    lv_obj_t* Remove(int index) { return Row(index)->children.at(3); }
    void Toggle(int index) {
        auto* toggle = Switch(index); assert(!lv_obj_has_state(toggle, LV_STATE_DISABLED));
        Press(toggle); toggle->state ^= LV_STATE_CHECKED; Release(toggle, LV_EVENT_VALUE_CHANGED);
    }
    std::vector<lv_obj_t*> Rollers() {
        std::vector<lv_obj_t*> result;
        for (const auto& object : objects) if (object->alive && object->kind == "roller" && TestVisible(object.get())) result.push_back(object.get());
        assert(result.size() == 2); return result;
    }
    void Select(lv_obj_t* roller, int value) {
        Press(roller); roller->value = value; Emit(roller, LV_EVENT_VALUE_CHANGED);
        pointer.state = LV_INDEV_STATE_RELEASED;
    }
};

int main(int argc, char** argv) {
    assert(argc == 2); const std::string name = argv[1];
    if (name != "loading") test::Fill(name == "full" || name == "buttons_all_rows" ? 16 : name == "empty_add" || name == "add_cancel" || name == "add_failure" || name == "expired" || name == "hidden" ? 0 : 2);
    Fixture f;
    if (name == "loading") {
        f.Label("Loading alarms..."); f.Label("Clock not synced"); assert(!f.Click("Add"));
        test::snapshot.initialized = true; test::Bump(); f.Tick();
        assert(f.Click("Add")); f.Label("Not saved - OK to add"); assert(test::calls == 0);
    } else if (name == "empty_add") {
        auto* initial = f.Label("No alarms"); f.Click("Add");
        const auto rollers = f.Rollers(); assert(rollers[0]->value == 7 && rollers[1]->value == 0);
        assert(rollers[0]->options.starts_with("00\n01\n") && rollers[0]->options.ends_with("\n23"));
        assert(rollers[1]->options.ends_with("\n59"));
        f.Click("OK"); f.Label("Accepted - saving, not saved yet");
        assert(test::calls == 1 && test::snapshot.alarms.empty());
        assert(!f.Click("OK")); f.alarm.HandleButton(2); assert(test::calls == 1);
        // Another request's completion is not our success.
        test::snapshot.last_result = {true, false, 99, 99, 20, ""}; test::Bump(); f.Tick();
        f.Label("Accepted - saving, not saved yet");
        test::Complete(1); f.Tick(); f.Label("Saved");
        f.Label("07:00"); f.Label("Daily"); assert(initial->alive && test::snapshot.alarms.size() == 1);
    } else if (name == "add_cancel") {
        f.Click("Add"); auto rollers = f.Rollers(); f.Select(rollers[0], 23); f.Select(rollers[1], 59);
        f.Click("Cancel"); assert(test::calls == 0);
        f.Click("Add"); rollers = f.Rollers(); assert(rollers[0]->value == 7 && rollers[1]->value == 0);
        f.Select(rollers[0], 14); auto* saved_button = f.Button("OK"); f.alarm.Close();
        Release(saved_button); // stale callback rejected by inactive/generation guard
        f.alarm.Open(); assert(test::calls == 0);
        f.Click("Add"); rollers = f.Rollers(); assert(rollers[0]->value == 7);
    } else if (name == "add_failure") {
        f.Click("Add"); f.Click("OK"); test::Complete(1, "nvs_full"); f.Tick();
        f.Label("Not saved: nvs_full"); assert(test::snapshot.alarms.empty());
        assert(f.Click("OK")); test::Complete(2); f.Tick(); assert(test::snapshot.alarms.size() == 1);
    } else if (name == "toggle") {
        auto* toggle = f.Switch(0); const auto allocations = objects.size();
        f.Toggle(0); assert(lv_obj_has_state(toggle, LV_STATE_CHECKED));
        assert(test::commands[1].id == 10 && test::commands[1].revision == test::snapshot.revision);
        f.Label("Saving..."); test::Complete(1, "write_failed"); f.Tick();
        assert(lv_obj_has_state(toggle, LV_STATE_CHECKED)); f.Label("Not saved: write_failed");
        f.Toggle(0); test::Complete(2); f.Tick(); assert(!lv_obj_has_state(toggle, LV_STATE_CHECKED));
        assert(objects.size() == allocations && toggle->alive);
    } else if (name == "delete") {
        auto* row = f.Row(0); const auto allocations = objects.size();
        Press(row); Emit(row, LV_EVENT_LONG_PRESSED); pointer.state = LV_INDEV_STATE_RELEASED;
        f.Label("Delete this alarm?"); f.Label("07:00");
        f.alarm.HandleButton(2); f.Label("Alarms (2/16)"); assert(test::calls == 0); // Cancel default
        f.Tick(); Click(f.Remove(1)); f.Label("08:01"); f.Click("Delete");
        assert(test::commands[1].id == 11); f.Label("Accepted - saving, not saved yet");
        assert(test::snapshot.alarms.size() == 2); test::Complete(1); f.Tick();
        f.Label("Alarm deleted"); assert(test::snapshot.alarms.size() == 1 && test::snapshot.alarms[0].id == 10);
        assert(row->alive && objects.size() == allocations);
    } else if (name == "delete_conflict") {
        Click(f.Remove(0)); test::snapshot.alarms.erase(test::snapshot.alarms.begin()); test::Changed();
        // No intervening timer: click itself must notice the changed revision.
        f.Click("Delete"); f.Label("Alarms changed. Cancel and retry.");
        assert(test::calls == 0 && test::snapshot.alarms[0].id == 11);
        f.Click("Cancel"); f.Tick(); Click(f.Remove(0)); f.Click("Delete");
        assert(test::commands[1].id == 11);
        test::snapshot.alarms.push_back({55, 10, 0, true, "", "", ""}); test::Changed();
        test::Complete(1); f.Tick(); f.Label("Alarms changed. Cancel and retry.");
        assert(test::snapshot.alarms.size() == 2);
    } else if (name == "held_rebind") {
        auto* row = f.Row(0); const auto allocations = objects.size(); Press(row);
        test::snapshot.alarms.erase(test::snapshot.alarms.begin()); test::Changed(); f.Tick();
        assert(f.Row(0)->children[0]->text == "07:00"); // cannot rebind under a held pointer
        Emit(row, LV_EVENT_LONG_PRESSED); pointer.state = LV_INDEV_STATE_RELEASED;
        assert(!Find(f.screen, "label", "Delete this alarm?")); assert(test::calls == 0);
        f.Tick(); assert(f.Row(0) == row && f.Row(0)->children[0]->text == "08:01");
        Emit(row, LV_EVENT_LONG_PRESSED); // late release does not acquire replacement id
        assert(!Find(f.screen, "label", "Delete this alarm?"));
        assert(objects.size() == allocations);
    } else if (name == "close_reopen_release") {
        auto* toggle = f.Switch(0); Press(toggle); f.alarm.Close(); f.alarm.Open();
        toggle->state ^= LV_STATE_CHECKED; Release(toggle, LV_EVENT_VALUE_CHANGED);
        assert(lv_obj_has_state(toggle, LV_STATE_CHECKED));
        assert(test::calls == 0); f.Tick();
        f.Toggle(0); assert(test::calls == 1);
    } else if (name == "close_pending") {
        f.Click("Add"); f.Click("OK"); f.alarm.Close();
        const auto reads = test::reads; const auto status = test::status_reads;
        test::Complete(1); f.Tick(); assert(reads == test::reads && status == test::status_reads);
        f.alarm.Open(); f.Label("07:00"); assert(!Find(f.screen, "label", "Add Alarm")); assert(test::calls == 1);
    } else if (name == "hidden") {
        const auto reads = test::reads, versions = test::versions;
        lv_obj_add_flag(f.alarm.root(), LV_OBJ_FLAG_HIDDEN); f.Tick(); assert(timers.back()->paused);
        for (int i = 0; i < 10; ++i) f.Tick();
        assert(reads == test::reads && versions == test::versions);
        f.alarm.Open(); assert(!timers.back()->paused);
        const auto count = objects.size();
        for (int i = 0; i < 20; ++i) f.Tick();
        assert(objects.size() == count); assert(test::reads == reads + 1);
    } else if (name == "full") {
        assert(!f.Click("Add")); f.alarm.HandleButton(0); f.alarm.HandleButton(2); assert(test::calls == 0);
        test::snapshot.alarms.pop_back(); test::Changed(); f.Tick(); assert(f.Click("Add"));
        test::snapshot.alarms.push_back({60, 7, 0, true, "", "", ""}); test::Changed(); f.Tick();
        assert(!f.Click("OK")); assert(test::calls == 0);
    } else if (name == "buttons") {
        // Focus starts at the first enable; B next traverses its Delete, then next enable.
        assert(lv_obj_has_state(f.Switch(0), LV_STATE_FOCUSED));
        f.alarm.HandleButton(1); assert(lv_obj_has_state(f.Remove(0), LV_STATE_FOCUSED));
        f.alarm.HandleButton(2); f.Label("Delete this alarm?");
        f.alarm.HandleButton(2); f.Tick(); // default Cancel
        f.alarm.HandleButton(0); f.alarm.HandleButton(2); // wrap to Add
        f.Label("Add Alarm"); auto rollers = f.Rollers();
        f.alarm.HandleButton(2); // edit hour
        for (int i = 0; i < 8; ++i) f.alarm.HandleButton(0);
        assert(rollers[0]->value == 23); f.alarm.HandleButton(2); // done hour
        f.alarm.HandleButton(1); f.alarm.HandleButton(2); f.alarm.HandleButton(0);
        assert(rollers[1]->value == 59); f.alarm.HandleButton(2);
        f.alarm.HandleButton(1); f.alarm.HandleButton(1); f.alarm.HandleButton(2);
        assert(test::calls == 1 && test::commands[1].hour == 23 && test::commands[1].minute == 59);
    } else if (name == "buttons_all_rows") {
        for (int i = 0; i < 16; ++i) {
            if (i) f.alarm.HandleButton(1);
            assert(lv_obj_has_state(f.Switch(i), LV_STATE_FOCUSED));
            f.alarm.HandleButton(1); assert(lv_obj_has_state(f.Remove(i), LV_STATE_FOCUSED));
        }
        assert(f.Row(15)->parent->scroll_y > 0);
        f.alarm.HandleButton(2); f.Label("Delete this alarm?"); f.Label("22:15");
        f.alarm.HandleButton(1); f.alarm.HandleButton(2); assert(test::commands[1].id == 25);
    } else if (name == "external_and_metadata") {
        auto* row = f.Row(0); const auto count = objects.size();
        test::snapshot.alarms[0].date = "2026-10-27"; test::snapshot.alarms[0].reminder = "中文";
        test::snapshot.alarms[0].enabled = false; test::snapshot.time_valid = true; test::Changed(); f.Tick();
        f.Label("Once 2026-10-27 / Voice"); f.Label("24-hour / Local time");
        assert(!lv_obj_has_state(f.Switch(0), LV_STATE_CHECKED)); assert(row == f.Row(0) && count == objects.size());
        test::snapshot.pending = true; test::Bump(); f.Tick(); assert(!f.Click("Add"));
        test::snapshot.pending = false; test::Bump(); f.Tick(); assert(f.Click("Add"));
    } else if (name == "expired") {
        f.Click("Add"); f.Click("OK"); test::results.erase(1); test::snapshot.pending = false; test::Bump();
        f.Tick(); f.Label("Save status expired. Check the list."); assert(!f.Click("OK"));
        f.Click("Cancel"); f.Click("Add"); assert(f.Click("OK")); assert(test::calls == 2);
    } else if (name == "rejection") {
        test::rejection = "queue_full"; f.Toggle(0); f.Label("Busy. Please try again.");
        assert(lv_obj_has_state(f.Switch(0), LV_STATE_CHECKED));
        test::rejection.clear(); f.Toggle(0); assert(test::calls == 2);
    } else if (name == "null_service") {
        StopwatchAlarm unavailable(nullptr, nullptr); auto* parent = lv_obj_create(nullptr);
        unavailable.Setup(parent); unavailable.Open();
        assert(Find(parent, "label", "Alarm service unavailable"));
        auto* button = Find(parent, "button", "Add"); assert(!Click(button));
        unavailable.HandleButton(2); unavailable.Close();
    } else {
        std::cerr << "Unknown case: " << name << std::endl; return 2;
    }
    assert(keypad.waits == 0);
}
'''

CASES = (
    "loading", "empty_add", "add_cancel", "add_failure", "toggle", "delete",
    "delete_conflict", "held_rebind", "close_reopen_release", "close_pending",
    "hidden", "full", "buttons", "buttons_all_rows", "external_and_metadata",
    "expired", "rejection", "null_service",
)


@unittest.skipUnless(COMPILER, "C++ compiler required")
class StopwatchAlarmUiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="stopwatch-alarm-ui-")
        cls.addClassCleanup(cls.tmp.cleanup)
        directory = pathlib.Path(cls.tmp.name)
        (directory / "sdkconfig.h").write_text("#define CONFIG_STOPWATCH_LOCAL_APPS 1\n", encoding="utf-8")
        (directory / "lvgl.h").write_text(LVGL, encoding="utf-8")
        (directory / "alarm_test_service.h").write_text(SERVICE, encoding="utf-8")
        (directory / "test.cc").write_text(HARNESS, encoding="utf-8")
        cls.binary = str(directory / "alarm_ui_test.exe")
        subprocess.run(
            [COMPILER, "-std=c++23", "-Wall", "-Wextra", "-Werror", "-fno-exceptions", "-fno-rtti",
             "-I", str(directory), "-I", str(BOARD), str(BOARD / "stopwatch_alarm.cc"),
             str(directory / "test.cc"), "-o", cls.binary], check=True, timeout=90,
        )
        disabled = directory / "disabled"
        disabled.mkdir()
        (disabled / "sdkconfig.h").write_text("#define CONFIG_STOPWATCH_LOCAL_APPS 0\n", encoding="utf-8")
        (disabled / "test.cc").write_text('#include "stopwatch_alarm.h"\nint main() {}\n', encoding="utf-8")
        cls.disabled = str(disabled / "alarm_ui_disabled.exe")
        subprocess.run(
            [COMPILER, "-std=c++23", "-Wall", "-Wextra", "-Werror", "-fno-exceptions", "-fno-rtti",
             "-I", str(disabled), "-I", str(BOARD), str(BOARD / "stopwatch_alarm.cc"),
             str(disabled / "test.cc"), "-o", cls.disabled], check=True, timeout=90,
        )

    def test_ui_regressions(self):
        for name in CASES:
            with self.subTest(name=name):
                subprocess.run([self.binary, name], check=True, timeout=10)

    def test_disabled_without_lvgl_or_service(self):
        subprocess.run([self.disabled], check=True, timeout=10)


if __name__ == "__main__":
    unittest.main()
