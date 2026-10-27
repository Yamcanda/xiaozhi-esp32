"""Extracted-production Home layout, animation and local-clock regressions.

A small LVGL object/animation model records native widget calls; it does not
render pixels or validate touch hardware, clipping, font rasterization or timing
on the device. No firmware dependencies or generated assets are required.
"""
import hashlib
import pathlib
import re
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
#include <cstdint>
#include <cstdio>
#include <ctime>
#include <functional>
#include <memory>
#include <string>
#include <vector>

constexpr unsigned LV_OBJ_FLAG_SCROLLABLE = 1, LV_OBJ_FLAG_CLICKABLE = 2,
                   LV_OBJ_FLAG_GESTURE_BUBBLE = 4, LV_OBJ_FLAG_HIDDEN = 8,
                   LV_OBJ_FLAG_OVERFLOW_VISIBLE = 16;
constexpr int LV_ALIGN_TOP_LEFT = 0, LV_ALIGN_TOP_MID = 1, LV_ALIGN_CENTER = 2,
              LV_ALIGN_BOTTOM_MID = 3, LV_TEXT_ALIGN_CENTER = 4,
              LV_EVENT_CLICKED = 5, LV_EVENT_GESTURE = 6,
              LV_EVENT_LONG_PRESSED = 10, LV_LABEL_LONG_WRAP = 11,
              LV_DIR_LEFT = 7, LV_DIR_RIGHT = 8, LV_DIR_TOP = 9,
              LV_RADIUS_CIRCLE = 32767, LV_OPA_TRANSP = 0,
              LV_OPA_50 = 128, LV_OPA_COVER = 255,
              LV_PART_MAIN = 0, LV_STATE_DEFAULT = 0;
using lv_color_t = uint32_t;
lv_color_t lv_color_hex(uint32_t value) { return value; }
lv_color_t lv_color_black() { return 0; }
lv_color_t lv_color_white() { return 0xffffff; }
struct lv_font_t { int height; };
struct lv_image_dsc_t { int width, height; };
const lv_image_dsc_t icon_badge{200, 200}, icon_setup{200, 200}, icon_clock{200, 200};
lv_font_t font_noto_sans_basic_20_4{20}, font_noto_sans_basic_30_4{30};
struct lv_obj_t;
struct lv_event_t { lv_obj_t* target; void* user_data; };
using lv_event_cb_t = void (*)(lv_event_t*);
struct Event { lv_event_cb_t callback; int code; void* user_data; };
struct lv_point_precise_t { int32_t x, y; };
using lv_point_t = lv_point_precise_t;
struct lv_obj_t {
    lv_obj_t* parent;
    std::string kind, text;
    unsigned flags = LV_OBJ_FLAG_CLICKABLE | LV_OBJ_FLAG_SCROLLABLE;
    int width = 0, height = 20, x = 0, y = 0, align = LV_ALIGN_TOP_LEFT;
    int align_x = 0, align_y = 0, radius = 0, border_width = 0;
    int bg_opa = LV_OPA_TRANSP, opa = LV_OPA_COVER, text_opa = LV_OPA_COVER;
    int line_width = 0, label_writes = 0, rotation = 0, pivot_x = 0, pivot_y = 0;
    uint32_t bg_color = 0, text_color = 0;
    bool has_text_color = false;
    const lv_font_t* font = nullptr;
    const lv_image_dsc_t* image_source = nullptr;
    std::vector<lv_obj_t*> children;
    std::vector<Event> events;
    std::vector<lv_point_precise_t> points;
};
std::vector<std::unique_ptr<lv_obj_t>> objects;
lv_obj_t* MakeObject(lv_obj_t* parent, const char* kind) {
    objects.emplace_back(new lv_obj_t{});
    auto* object = objects.back().get();
    object->parent = parent;
    object->kind = kind;
    if (parent) {
        object->flags |= LV_OBJ_FLAG_GESTURE_BUBBLE;
        parent->children.push_back(object);
    }
    return object;
}
lv_obj_t* lv_obj_create(lv_obj_t* parent) { return MakeObject(parent, "object"); }
lv_obj_t* lv_label_create(lv_obj_t* parent) { return MakeObject(parent, "label"); }
lv_obj_t* lv_line_create(lv_obj_t* parent) { return MakeObject(parent, "line"); }
lv_obj_t* lv_button_create(lv_obj_t* parent) { return MakeObject(parent, "button"); }
lv_obj_t* lv_image_create(lv_obj_t* parent) { return MakeObject(parent, "image"); }
void lv_image_set_src(lv_obj_t* image, const lv_image_dsc_t* source) {
    image->image_source = source;
    image->width = source->width;
    image->height = source->height;
}
void lv_obj_remove_style_all(lv_obj_t*) {}
void lv_obj_set_size(lv_obj_t* object, int width, int height) {
    object->width = width; object->height = height;
}
void lv_obj_set_width(lv_obj_t* object, int width) { object->width = width; }
void lv_obj_set_height(lv_obj_t* object, int height) { object->height = height; }
void lv_obj_set_pos(lv_obj_t* object, int x, int y) { object->x = x; object->y = y; }
void lv_obj_set_x(lv_obj_t* object, int x) { object->x = x; }
void lv_obj_set_y(lv_obj_t* object, int y) { object->y = y; }
void lv_obj_align(lv_obj_t* object, int align, int x, int y) {
    object->align = align; object->align_x = x; object->align_y = y;
    const int width = object->parent ? object->parent->width : 466;
    const int height = object->parent ? object->parent->height : 466;
    object->x = x;
    object->y = y;
    if (align == LV_ALIGN_TOP_MID || align == LV_ALIGN_CENTER || align == LV_ALIGN_BOTTOM_MID)
        object->x += (width - object->width) / 2;
    if (align == LV_ALIGN_CENTER) object->y += (height - object->height) / 2;
    if (align == LV_ALIGN_BOTTOM_MID) object->y += height - object->height;
}
void lv_obj_center(lv_obj_t* object) { lv_obj_align(object, LV_ALIGN_CENTER, 0, 0); }
void lv_obj_set_style_pad_all(lv_obj_t*, int, int) {}
void lv_obj_set_style_bg_color(lv_obj_t* object, lv_color_t color, int) { object->bg_color = color; }
void lv_obj_set_style_bg_opa(lv_obj_t* object, int opacity, int) { object->bg_opa = opacity; }
void lv_obj_set_style_opa(lv_obj_t* object, int opacity, int) { object->opa = opacity; }
void lv_obj_set_style_text_opa(lv_obj_t* object, int opacity, int) { object->text_opa = opacity; }
void lv_obj_set_style_text_color(lv_obj_t* object, lv_color_t color, int) {
    object->text_color = color; object->has_text_color = true;
}
lv_color_t lv_obj_get_style_text_color(const lv_obj_t* object, int) {
    return !object->has_text_color && object->parent
        ? lv_obj_get_style_text_color(object->parent, 0) : object->text_color;
}
void lv_obj_set_style_text_font(lv_obj_t* object, const lv_font_t* font, int) { object->font = font; }
void lv_obj_set_style_text_align(lv_obj_t*, int, int) {}
void lv_obj_set_style_transform_rotation(lv_obj_t* object, int rotation, int) { object->rotation = rotation; }
void lv_obj_set_style_transform_pivot_x(lv_obj_t* object, int pivot, int) { object->pivot_x = pivot; }
void lv_obj_set_style_transform_pivot_y(lv_obj_t* object, int pivot, int) { object->pivot_y = pivot; }
lv_obj_t* lv_obj_get_child(lv_obj_t* object, int index) { return object->children.at(index); }
void lv_obj_set_style_radius(lv_obj_t* object, int radius, int) { object->radius = radius; }
void lv_obj_set_style_border_width(lv_obj_t* object, int width, int) { object->border_width = width; }
void lv_obj_set_style_border_color(lv_obj_t*, lv_color_t, int) {}
void lv_obj_set_style_shadow_width(lv_obj_t*, int, int) {}
void lv_obj_set_style_line_width(lv_obj_t* object, int width, int) { object->line_width = width; }
void lv_obj_set_style_line_color(lv_obj_t*, lv_color_t, int) {}
void lv_obj_set_style_line_rounded(lv_obj_t*, bool, int) {}
void lv_line_set_points(lv_obj_t* object, const lv_point_precise_t* points, uint32_t count) {
    object->points.assign(points, points + count);
}
void lv_label_set_text(lv_obj_t* object, const char* text) {
    object->text = text; ++object->label_writes;
}
void lv_obj_add_flag(lv_obj_t* object, unsigned flags) { object->flags |= flags; }
void lv_obj_remove_flag(lv_obj_t* object, unsigned flags) { object->flags &= ~flags; }
void lv_obj_move_foreground(lv_obj_t*) {}
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

using lv_anim_exec_xcb_t = void (*)(void*, int32_t);
struct lv_anim_t;
using lv_anim_path_cb_t = int32_t (*)(const lv_anim_t*);
struct lv_anim_t {
    void* var = nullptr;
    lv_anim_exec_xcb_t exec = nullptr;
    lv_anim_path_cb_t path = nullptr;
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
void lv_anim_set_time(lv_anim_t* animation, uint32_t duration) { animation->duration = duration; }
void lv_anim_set_path_cb(lv_anim_t* animation, lv_anim_path_cb_t path) { animation->path = path; }
bool lv_anim_delete(void* var, lv_anim_exec_xcb_t exec) {
    ++animation_deletes;
    const bool match = animation_active && running_animation.var == var &&
                       (!exec || running_animation.exec == exec);
    if (match) animation_active = false;
    return match;
}
lv_anim_t* lv_anim_start(lv_anim_t* animation) {
    assert(!animation_active);  // A repeated input must not enqueue another slide.
    running_animation = *animation;
    animation_active = true;
    ++animation_starts;
    assert(animation->exec);
    animation->exec(animation->var, animation->from);
    return &running_animation;
}
void AdvanceAnimation(int32_t progress) {
    assert(animation_active);
    const auto animation = running_animation;
    animation.exec(animation.var, progress);
    if (progress >= animation.to) animation_active = false;
}

struct LvglTheme {
    uint32_t background = 0xf0e0d0, foreground = 0x123456;
    uint32_t background_color() const { return background; }
    uint32_t text_color() const { return foreground; }
};
struct Display {
    LvglTheme theme;
    LvglTheme* GetTheme() { return &theme; }
};
struct StopwatchSettings {
    lv_obj_t* root_ = nullptr;
    int setups = 0, opens = 0, closes = 0, theme_refreshes = 0, menu_depth = 0;
    void Setup(lv_obj_t* parent) {
        assert(!root_);
        ++setups;
        root_ = lv_obj_create(parent);
        lv_obj_set_style_bg_color(root_, lv_color_black(), 0);
        lv_obj_set_style_text_color(root_, lv_color_white(), 0);
    }
    lv_obj_t* root() const { return root_; }
    void Open() { ++opens; menu_depth = 0; }
    void Close() {
        ++closes; menu_depth = 0;
        if (root_) lv_obj_add_flag(root_, LV_OBJ_FLAG_HIDDEN);
    }
    void RefreshTheme() { ++theme_refreshes; }
    void HandleButton(int) {}
};
struct StopwatchWeather {
    lv_obj_t* root_ = nullptr;
    int setups = 0, opens = 0, closes = 0, theme_refreshes = 0;
    bool active = false;
    void Setup(lv_obj_t* parent) {
        assert(!root_);
        ++setups;
        root_ = lv_obj_create(parent);
    }
    lv_obj_t* root() const { return root_; }
    void Open() { ++opens; active = true; }
    void Close() {
        ++closes; active = false;
        if (root_) lv_obj_add_flag(root_, LV_OBJ_FLAG_HIDDEN);
    }
    void RefreshTheme() { ++theme_refreshes; }
    void HandleButton(int) {}
};
struct StopwatchAlarm : StopwatchWeather {};
uint32_t lv_tick_get() { return 0; }
uint32_t lv_tick_elaps(uint32_t since) { return lv_tick_get() - since; }
struct lv_timer_t { void* user_data; };
void* lv_timer_get_user_data(lv_timer_t* timer) { return timer->user_data; }
lv_timer_t timer;
lv_timer_t* lv_timer_create(void (*)(lv_timer_t*), int period, void* user_data) {
    assert(period > 0);
    timer.user_data = user_data;
    return &timer;
}
void lv_label_set_long_mode(lv_obj_t*, int) {}
struct Queue {} queue;
using TaskHandle_t = void*;
constexpr int pdPASS = 1;
Queue* xQueueCreate(int size, int item_size) {
    assert(size == 8 && item_size > 0);
    return &queue;
}
void vQueueDelete(Queue*) {}
int xTaskCreate(void (*)(void*), const char*, int, void*, int, TaskHandle_t*) {
    return pdPASS;  // Never launch an asynchronous worker in this layout harness.
}
#define ESP_LOGE(...) ((void)0)

std::time_t clock_now = 0;
int clock_localtime_calls = 0;
std::time_t test_time(std::time_t* output) {
    if (output) *output = clock_now;
    return clock_now;
}
std::tm* test_localtime_r(const std::time_t* value, std::tm* output) {
    ++clock_localtime_calls;
    *output = {};
    output->tm_year = *value < 1700000000 ? 70 : 124;
    output->tm_mon = 0;
    output->tm_mday = 1;
    output->tm_hour = 9;
    output->tm_min = static_cast<int>((*value / 60) % 60);
    return output;
}
'''


CHECKS = r'''
// Expectations stay independent of production order and semantic-indexed metadata.
constexpr int kExpectedHomeOrder[] = {0, 4, 1, 3, 2};
constexpr const char* kExpectedNames[] = {"XiaoZhi", "Badge", "Settings", "Weather", "Alarms"};
constexpr StopwatchAppState::Page kExpectedPages[] = {
    StopwatchAppState::Page::Voice, StopwatchAppState::Page::Badge,
    StopwatchAppState::Page::Settings, StopwatchAppState::Page::Weather,
    StopwatchAppState::Page::Alarm,
};
int HomePosition(int app) {
    for (int position = 0; position < 5; ++position) {
        if (kExpectedHomeOrder[position] == app) return position;
    }
    assert(false);
    return -1;
}
int AdjacentApp(int app, int direction) {
    return kExpectedHomeOrder[(HomePosition(app) + (direction < 0 ? 4 : 1)) % 5];
}
bool Hidden(const lv_obj_t* object) { return object->flags & LV_OBJ_FLAG_HIDDEN; }
std::string ClockText(const StopwatchApps& apps) {
    std::string text;
    for (const auto* digit : apps.home_clock_->children) text += digit->text;
    return text;
}
int ClockWrites(const StopwatchApps& apps) {
    int writes = 0;
    for (const auto* digit : apps.home_clock_->children) writes += digit->label_writes;
    return writes;
}
int Opacity(const lv_obj_t* object) { return object->opa * object->text_opa / 255; }
bool HasClick(const lv_obj_t* object) {
    return std::any_of(object->events.begin(), object->events.end(),
                       [](const Event& event) { return event.code == LV_EVENT_CLICKED; });
}
void Click(lv_obj_t* object) {
    assert(!Hidden(object) && HasClick(object));
    for (const auto& event : object->events) {
        if (event.code == LV_EVENT_CLICKED) {
            lv_event_t data{object, event.user_data};
            event.callback(&data);
        }
    }
}
lv_obj_t* Swipe(lv_obj_t* hit, int direction) {
    assert(hit);
    input.direction = direction;
    input.wait_release = false;
    while (hit && !(hit->flags & LV_OBJ_FLAG_CLICKABLE)) hit = hit->parent;
    auto* target = hit;
    while (target && (target->flags & LV_OBJ_FLAG_GESTURE_BUBBLE)) target = target->parent;
    assert(target);
    for (const auto& event : target->events) {
        if (event.code == LV_EVENT_GESTURE) {
            lv_event_t data{target, event.user_data};
            event.callback(&data);
        }
    }
    if (!input.wait_release && HasClick(hit)) Click(hit);
    return target;
}
void AssertDots(const StopwatchApps& apps) {
    const int selected = apps.state_.selection();
    for (int index = 0; index < 5; ++index) {
        const auto* dot = apps.home_dots_[index];
        assert(dot->width == (index == selected ? 14 : 8));
        assert(dot->height == dot->width);
        assert(dot->bg_opa == (index == selected ? 255 : 128));
        assert(dot->x + dot->width / 2 == 201 + 16 * HomePosition(index));
        assert(dot->y + dot->height / 2 == 433);
    }
}
void AssertIcon(const StopwatchApps& apps, int index) {
    const auto* icon = apps.home_icons_[index];
    assert(icon->children.size() == (index == 1 || index == 2 || index == 4 ? 1 : 9));
    if (index == 1 || index == 2 || index == 4) {
        assert(icon->children.front()->kind == "image");
        assert(icon->children.front()->image_source == (index == 1 ? &icon_badge : index == 2 ? &icon_setup : &icon_clock));
    } else {
        const auto* first = icon->children.front();
        assert(first->kind == "object" && first->image_source == nullptr);
        assert(first->x == (index == 0 ? 96 : 43));
        assert(first->y == (index == 0 ? 18 : 32));
        assert(first->width == (index == 0 ? 8 : 78));
        assert(first->height == (index == 0 ? 30 : 78));
        assert(first->bg_color == (index == 0 ? 0x9CE9D2 : 0xE7EFAF));
    }
}
void AssertSnapped(const StopwatchApps& apps) {
    const int selected = apps.state_.selection();
    assert(!apps.home_animating_ && !Hidden(apps.home_icons_[selected]));
    assert(apps.home_icons_[selected]->x == 133 && apps.home_icons_[selected]->y == 118);
    assert(apps.home_name_->text == kExpectedNames[selected] && Opacity(apps.home_name_) == 255);
    for (int index = 0; index < 5; ++index) {
        assert(Hidden(apps.home_icons_[index]) == (index != selected));
        AssertIcon(apps, index);
    }
    AssertDots(apps);
}
void AssertOpened(const StopwatchApps& apps, int selected) {
    assert(apps.state_.selection() == selected && apps.state_.page() == kExpectedPages[selected]);
    assert(Hidden(apps.overlay_) == (selected == 0));
    assert(Hidden(apps.settings_.root()) == (selected != 2));
    assert(Hidden(apps.weather_.root()) == (selected != 3));
    assert(Hidden(apps.alarm_.root()) == (selected != 4));
    if (selected == 1) assert(!Hidden(apps.badge_) && Hidden(apps.editor_panel_));
    assert(apps.wake_requests.back() == (selected == 0));
    assert(apps.home_name_->text == kExpectedNames[selected]);
    AssertIcon(apps, selected);
    AssertDots(apps);
}
void AssertSlidingIcons(const StopwatchApps& apps, int outgoing, int incoming) {
    for (int index = 0; index < StopwatchAppState::kAppCount; ++index) {
        assert(Hidden(apps.home_icons_[index]) == (index != outgoing && index != incoming));
    }
}
int main(int argc, char** argv) {
    assert(argc == 2);
    const std::string test = argv[1];
    using Page = StopwatchAppState::Page;
    auto* screen = lv_obj_create(nullptr);
    lv_obj_set_size(screen, 466, 466);
    StopwatchApps apps;
    apps.ApplyTheme();  // No widget tree exists yet.
    assert(apps.settings_.theme_refreshes == 0 && apps.weather_.theme_refreshes == 0);
    apps.Setup(screen);
    assert(apps.wake_requests.empty() && apps.boot_home_pending_ && Hidden(apps.overlay_));
    assert(apps.settings_.setups == 1 && apps.weather_.setups == 1 && apps.alarm_.setups == 1);
    assert(apps.settings_.root()->parent == apps.overlay_);
    assert(apps.weather_.root()->parent == apps.overlay_);
    assert(apps.alarm_.root()->parent == apps.overlay_);
    assert(Hidden(apps.settings_.root()) && Hidden(apps.weather_.root()) && Hidden(apps.alarm_.root()));
    const auto setup_object_count = objects.size();
    const auto setup_work = apps.work;
    apps.Setup(screen);  // Setup is idempotent; it cannot recreate either app tree.
    assert(objects.size() == setup_object_count && apps.work == setup_work);
    assert(apps.settings_.setups == 1 && apps.weather_.setups == 1 && apps.alarm_.setups == 1);
    apps.ShowHome();
    apps.work.clear();
    const auto object_count = objects.size();
    if (test == "layout") {
        static_assert(StopwatchAppState::kAppCount == 5);
        static_assert(sizeof(apps.home_icons_) / sizeof(apps.home_icons_[0]) ==
                      StopwatchAppState::kAppCount);
        static_assert(sizeof(apps.home_dots_) / sizeof(apps.home_dots_[0]) ==
                      StopwatchAppState::kAppCount);
        assert(std::string(kNames[0]) == "XiaoZhi");
        assert(std::string(kNames[1]) == "Badge");
        assert(std::string(kNames[2]) == "Settings");
        assert(std::string(kNames[3]) == "Weather");
        assert(std::string(kNames[4]) == "Alarms");
        assert(apps.home_->parent == apps.overlay_);
        assert(apps.home_->width == 466 && apps.home_->height == 466);
        assert(apps.home_->bg_color == 0 && apps.home_->bg_opa == 255);
        assert(!(apps.home_->flags & LV_OBJ_FLAG_SCROLLABLE));
        assert(!(apps.home_->flags & LV_OBJ_FLAG_GESTURE_BUBBLE));
        assert(apps.home_clock_->parent == apps.home_);
        assert(apps.home_clock_->width == 114 && apps.home_clock_->height == 36);
        assert(apps.home_clock_->align == LV_ALIGN_TOP_MID && apps.home_clock_->align_y == 4);
        assert(apps.home_clock_->children.size() == 5);
        constexpr int rotations[] = {-100, -46, 0, 32, 84};
        for (int index = 0; index < 5; ++index) {
            const auto* digit = apps.home_clock_->children[index];
            assert(digit->kind == "label" && lv_obj_get_style_text_color(digit, 0) == 0xffffff);
            assert(digit->rotation == rotations[index] && digit->pivot_x == 0 && digit->pivot_y == 233);
        }
        assert(apps.home_name_->parent == apps.home_ && lv_obj_get_style_text_color(apps.home_name_, 0) == 0xffffff);
        assert(apps.home_name_->align == LV_ALIGN_CENTER && apps.home_name_->align_y == 155);
        int arrows = 0, lines = 0;
        for (const auto* object : apps.home_->children) {
            if (object->width == 52 && object->height == 160) {
                assert(object->y == 153 && (object->x == 7 || object->x == 407));
                assert(HasClick(object) && object->bg_opa == 0);
                ++arrows;
            }
        }
        for (int index = 0; index < StopwatchAppState::kAppCount; ++index) {
            const auto* icon = apps.home_icons_[index];
            assert(icon->parent == apps.home_ && icon->width == 200 && icon->height == 200);
            assert(HasClick(icon) && (icon->flags & LV_OBJ_FLAG_GESTURE_BUBBLE));
            assert(!icon->children.empty());
            if (index == 0) {
                assert(icon->children.size() == 9);
                for (const auto* shape : icon->children) {
                    assert(shape->kind == "object" && shape->image_source == nullptr);
                }
            } else if (index == 1 || index == 2 || index == 4) {
                assert(icon->children.size() == 1);
                const auto* image = icon->children.front();
                assert(image->kind == "image");
                assert(image->image_source == (index == 1 ? &icon_badge : index == 2 ? &icon_setup : &icon_clock));
                assert(image->width == 200 && image->height == 200 && image->x == 0 && image->y == 0);
                assert(!(image->flags & (LV_OBJ_FLAG_CLICKABLE | LV_OBJ_FLAG_SCROLLABLE)));
                assert(image->flags & LV_OBJ_FLAG_GESTURE_BUBBLE);
            } else {
                assert(index == 3 && icon->children.size() > 1);
                for (const auto* shape : icon->children) {
                    assert(shape->kind == "object" && shape->image_source == nullptr);
                    assert(shape->bg_opa == LV_OPA_COVER);
                    assert(!(shape->flags & (LV_OBJ_FLAG_CLICKABLE | LV_OBJ_FLAG_SCROLLABLE)));
                    assert(shape->flags & LV_OBJ_FLAG_GESTURE_BUBBLE);
                    assert(shape->x >= 0 && shape->y >= 0);
                    assert(shape->x + shape->width <= 200 && shape->y + shape->height <= 200);
                }
            }
            const auto* dot = apps.home_dots_[index];
            assert(dot->parent == apps.home_);
            assert(dot->x + dot->width / 2 == 201 + 16 * HomePosition(index));
            assert(dot->y + dot->height / 2 == 433);
            assert(dot->bg_color == 0xffffff && dot->radius == LV_RADIUS_CIRCLE);
        }
        for (const auto& object : objects) {
            if (object->kind == "line") ++lines;
            assert(object->kind != "button" || object->parent == apps.editor_panel_);
            // The real Setup also creates the editor's Done button, not launcher chrome.
            assert(object->text != "HOME");
            for (int count : {2, 3, 4}) {
                for (int index = 1; index <= count; ++index) {
                    assert(object->text != std::to_string(index) + " / " + std::to_string(count));
                }
            }
            assert(object->text.find("A/B:") == std::string::npos);
            assert(object->text.find("double B") == std::string::npos);
        }
        assert(arrows == 2 && lines >= 2);
        AssertSnapped(apps);
    } else if (test == "theme") {
        const auto* name_font = apps.home_name_->font;
        const auto* clock_font = apps.home_clock_->font;
        for (int selected : kExpectedHomeOrder) {
            apps.state_.Open(selected);
            apps.ShowHome();
            for (int theme = 0; theme < 3; ++theme) {
                const int theme_refreshes = apps.settings_.theme_refreshes;
                const int weather_theme_refreshes = apps.weather_.theme_refreshes;
                const int weather_opens = apps.weather_.opens, weather_closes = apps.weather_.closes;
                apps.display.theme.background = theme == 0 ? 0xffffff : 0x123456;
                apps.display.theme.foreground = theme == 0 ? 0 : 0xabcdef;
                apps.ApplyTheme();
                assert(apps.overlay_->bg_color == apps.display.theme.background);
                assert(apps.editor_panel_->bg_color == apps.display.theme.background);
                assert(apps.home_->bg_color == 0 && apps.home_->bg_opa == 255);
                assert(apps.settings_.theme_refreshes == theme_refreshes + 1);
                assert(apps.weather_.theme_refreshes == weather_theme_refreshes + 1);
                assert(apps.weather_.opens == weather_opens && apps.weather_.closes == weather_closes);
                assert(Hidden(apps.weather_.root()) && !apps.weather_.active);
                assert(apps.settings_.root()->bg_color == 0);
                assert(lv_obj_get_style_text_color(apps.settings_.root(), 0) == 0xffffff);
                assert(lv_obj_get_style_text_color(apps.home_name_, 0) == 0xffffff);
                for (const auto* digit : apps.home_clock_->children) assert(lv_obj_get_style_text_color(digit, 0) == 0xffffff);
                assert(apps.home_name_->font == name_font && apps.home_clock_->font == clock_font);
                AssertSnapped(apps);
                // Reopening and refreshing a foreground app must retain the same
                // semantic name/artwork/Open target and its Home dot position.
                apps.OpenSelected();
                AssertOpened(apps, selected);
                const int settings_opens = apps.settings_.opens;
                const int opens = apps.weather_.opens, closes = apps.weather_.closes;
                const auto wake_requests = apps.wake_requests;
                apps.ApplyTheme();
                AssertOpened(apps, selected);
                assert(apps.settings_.opens == settings_opens);
                assert(apps.weather_.opens == opens && apps.weather_.closes == closes);
                assert(apps.wake_requests == wake_requests);
                apps.ShowHome();
                AssertSnapped(apps);
            }
        }
        assert(objects.size() == object_count);
    } else if (test == "animation") {
        for (int selected = 0; selected < StopwatchAppState::kAppCount; ++selected) {
            for (int direction : {-1, 1}) {
                apps.state_.Open(selected);
                apps.ShowHome();
                const int starts = animation_starts;
                AssertSnapped(apps);
                apps.Step(direction);
                const int incoming = AdjacentApp(selected, direction);
                assert(apps.home_animating_ && apps.home_from_ == selected);
                assert(apps.home_direction_ == direction && apps.state_.selection() == incoming);
                assert(apps.state_.page() == Page::Home && !Hidden(apps.home_));
                assert(animation_starts == starts + 1 && animation_active);
                assert(running_animation.var == &apps && running_animation.exec == StopwatchApps::HomeAnimation);
                assert(running_animation.from == 0 && running_animation.to == 466);
                assert(running_animation.duration == 360 && running_animation.path == lv_anim_path_ease_out);
                assert(apps.home_icons_[selected]->x == 133);
                assert(apps.home_icons_[incoming]->x == 133 + direction * 466);
                AssertSlidingIcons(apps, selected, incoming);
                AssertDots(apps);
                const int name_x = apps.home_name_->x, name_y = apps.home_name_->y;
                AdvanceAnimation(70);
                assert(apps.home_name_->text == kNames[selected] && Opacity(apps.home_name_) == 255);
                AdvanceAnimation(116);
                AssertSlidingIcons(apps, selected, incoming);
                assert(apps.home_icons_[selected]->x == 133 - direction * 116);
                assert(apps.home_icons_[incoming]->x == 133 + direction * 350);
                assert(apps.home_name_->text == kNames[selected] && Opacity(apps.home_name_) == 108);
                AdvanceAnimation(150);
                assert(apps.home_name_->text == kNames[selected] && Opacity(apps.home_name_) == 0);
                AdvanceAnimation(232);
                assert(apps.home_name_->text == kNames[selected] && Opacity(apps.home_name_) == 0);
                AdvanceAnimation(233);
                AssertSlidingIcons(apps, selected, incoming);
                assert(apps.home_icons_[selected]->x == 133 - direction * 233);
                assert(apps.home_icons_[incoming]->x == 133 + direction * 233);
                assert(apps.home_name_->text == kNames[incoming] && Opacity(apps.home_name_) == 0);
                AdvanceAnimation(316);
                assert(Opacity(apps.home_name_) == 0);
                AdvanceAnimation(350);
                assert(apps.home_name_->text == kNames[incoming] && Opacity(apps.home_name_) == 108);
                AdvanceAnimation(396);
                AssertSlidingIcons(apps, selected, incoming);
                assert(Opacity(apps.home_name_) == 255);
                assert(apps.home_name_->x == name_x && apps.home_name_->y == name_y);
                AdvanceAnimation(466);
                assert(!animation_active && apps.state_.selection() == incoming);
                AssertSnapped(apps);
                apps.OpenSelected();
                AssertOpened(apps, incoming);
                apps.ShowHome();
                AssertSnapped(apps);
            }
        }
        assert(objects.size() == object_count);  // No per-step allocation or object churn.
    } else if (test == "bounded_input") {
        apps.Step(1);
        const int starts = animation_starts;
        for (int index = 0; index < 1000; ++index) {
            apps.Step(index % 2 ? -1 : 1);
            apps.OpenSelected();
        }
        assert(animation_starts == starts && apps.state_.page() == Page::Home);
        assert(apps.state_.selection() == 4 && apps.home_animating_ && apps.work.empty());
        AdvanceAnimation(466);
        AssertSnapped(apps);
        apps.OpenSelected();
        assert(apps.state_.page() == Page::Alarm && !Hidden(apps.alarm_.root()));
        assert(apps.alarm_.opens == 1 && apps.alarm_.active && !apps.wake_requests.back());
        apps.ShowHome();
        assert((apps.work == std::vector<StopwatchApps::Work>{StopwatchApps::Work::StopEditor}));
        apps.work.clear();  // Finish ShowHome's cleanup before checking input adds no work.
        apps.Step(1);
        const int alarm_starts = animation_starts;
        for (int index = 0; index < 1000; ++index) {
            apps.Step(index % 2 ? -1 : 1);
            apps.OpenSelected();
        }
        assert(animation_starts == alarm_starts && apps.state_.page() == Page::Home);
        assert(apps.state_.selection() == 1 && apps.home_animating_ && apps.work.empty());
        AdvanceAnimation(466);
        AssertSnapped(apps);
        apps.OpenSelected();
        assert(apps.state_.page() == Page::Badge && !Hidden(apps.badge_));
        apps.ShowHome();
        apps.Step(1);
        assert(apps.home_animating_ && apps.state_.selection() == 3);
        for (int index = 0; index < 1000; ++index) {
            apps.Step(index % 2 ? -1 : 1);
            apps.OpenSelected();
        }
        assert(apps.state_.page() == Page::Home && apps.weather_.opens == 0);
        AdvanceAnimation(466);
        AssertSnapped(apps);
        apps.OpenSelected();
        assert(apps.state_.page() == Page::Weather && !Hidden(apps.weather_.root()));
        assert(apps.weather_.opens == 1 && apps.weather_.active && !apps.wake_requests.back());
        apps.ShowHome();
        apps.Step(1);
        assert(apps.home_animating_ && apps.state_.selection() == 2);
        for (int index = 0; index < 1000; ++index) {
            apps.Step(index % 2 ? -1 : 1);
            apps.OpenSelected();
        }
        assert(apps.state_.page() == Page::Home && apps.settings_.opens == 0);
        AdvanceAnimation(466);
        AssertSnapped(apps);
        apps.OpenSelected();
        assert(apps.state_.page() == Page::Settings && !Hidden(apps.settings_.root()));
        assert(apps.settings_.opens == 1 && !apps.wake_requests.back());
        apps.ShowHome();
        apps.Step(1);
        AdvanceAnimation(466);
        apps.OpenSelected();
        assert(apps.state_.page() == Page::Voice && Hidden(apps.overlay_));
        assert(objects.size() == object_count);
    } else if (test == "cancel") {
        apps.Step(1);
        AdvanceAnimation(116);
        const int deletes = animation_deletes;
        apps.ShowHome();
        assert(!animation_active && animation_deletes > deletes);
        assert(apps.state_.page() == Page::Home && apps.state_.selection() == 4);
        AssertSnapped(apps);
        apps.Step(-1);
        AdvanceAnimation(466);
        assert(apps.state_.selection() == 0);
        AssertSnapped(apps);
    } else if (test == "leave_home") {
        const Page pages[] = {Page::Voice, Page::Badge, Page::Settings, Page::Weather, Page::Alarm};
        for (int selected = 0; selected < StopwatchAppState::kAppCount; ++selected) {
            apps.state_.Open(selected);
            apps.ShowHome();
            apps.Step(1);
            AdvanceAnimation(116);
            const int target = AdjacentApp(selected, 1);
            apps.state_.Open(target);  // Model a system/UI page change outside the click guard.
            apps.RenderPage();
            assert(!apps.home_animating_ && !animation_active);
            assert(apps.state_.page() == pages[target]);
            assert(Hidden(apps.overlay_) == (target == 0));
            const int x = apps.home_icons_[target]->x;
            const auto text = apps.home_name_->text;
            StopwatchApps::HomeAnimation(&apps, 350);  // A stale callback cannot move/reset a page.
            assert(apps.home_icons_[target]->x == x && apps.home_name_->text == text);
        }
    } else if (test == "settings_page") {
        auto* root = apps.settings_.root();
        assert(root->parent == apps.overlay_ && Hidden(root));
        apps.Step(-1);  // Settings is last visually but keeps semantic app ID 2.
        AdvanceAnimation(466);
        AssertSnapped(apps);
        assert(apps.state_.selection() == 2);
        apps.settings_.menu_depth = 4;
        apps.OpenSelected();
        assert(apps.state_.page() == Page::Settings && apps.settings_.opens == 1);
        assert(apps.settings_.menu_depth == 0 && apps.settings_.root() == root);
        assert(!Hidden(root) && !Hidden(apps.overlay_));
        assert(Hidden(apps.home_) && Hidden(apps.badge_) && Hidden(apps.editor_panel_));
        assert(Hidden(apps.weather_.root()) && apps.weather_.opens == 0);
        assert(!apps.wake_requests.back());
        apps.settings_.menu_depth = 2;
        const auto wake_requests = apps.wake_requests;
        const auto work = apps.work;
        for (int index = 0; index < 3; ++index) {
            apps.RenderPage();
            apps.Step(index % 2 ? -1 : 1);
        }
        assert(apps.settings_.opens == 1 && apps.settings_.menu_depth == 2);
        assert(!Hidden(root) && apps.work == work && apps.wake_requests == wake_requests);
        const int closes = apps.settings_.closes;
        apps.ShowHome();
        assert(apps.state_.page() == Page::Home && apps.state_.selection() == 2);
        assert(apps.settings_.closes == closes + 1 && apps.settings_.menu_depth == 0);
        assert(Hidden(root) && apps.settings_.root() == root && !apps.wake_requests.back());
        AssertSnapped(apps);
        apps.OpenSelected();
        assert(apps.state_.page() == Page::Settings && apps.settings_.opens == 2 && !Hidden(root));
        // A direct render onto Badge must hide Settings without relying on Close().
        apps.state_.Open(1);
        apps.RenderPage();
        assert(Hidden(root) && !Hidden(apps.badge_));
        apps.state_.Open(2);
        apps.RenderPage();
        assert(!Hidden(root));
        apps.state_.ShowVoice();
        apps.RenderPage();
        assert(Hidden(root) && Hidden(apps.overlay_));
        assert(apps.settings_.root() == root && objects.size() == object_count);
    } else if (test == "weather_page") {
        auto* root = apps.weather_.root();
        assert(root->parent == apps.overlay_ && Hidden(root));
        apps.Step(-1);  // Weather precedes Settings visually but keeps app ID 3.
        AdvanceAnimation(466);
        apps.Step(-1);
        AdvanceAnimation(466);
        AssertSnapped(apps);
        assert(apps.state_.selection() == 3);
        apps.OpenSelected();
        assert(apps.state_.page() == Page::Weather && apps.weather_.opens == 1);
        assert(apps.weather_.active && !Hidden(root) && !Hidden(apps.overlay_));
        assert(Hidden(apps.home_) && Hidden(apps.badge_) && Hidden(apps.editor_panel_));
        assert(Hidden(apps.settings_.root()) && !apps.wake_requests.back());
        const auto wake_requests = apps.wake_requests;
        const auto work = apps.work;
        const int closes = apps.weather_.closes;
        const int theme_refreshes = apps.weather_.theme_refreshes;
        const int settings_theme_refreshes = apps.settings_.theme_refreshes;
        for (int theme = 0; theme < 3; ++theme) {
            apps.RenderPage();
            apps.Step(theme % 2 ? -1 : 1);  // Weather must not navigate Badge slots.
            apps.display.theme.background = theme % 2 ? 0xffffff : 0;
            apps.display.theme.foreground = theme % 2 ? 0 : 0xffffff;
            apps.ApplyTheme();
            assert(apps.weather_.theme_refreshes == theme_refreshes + theme + 1);
            assert(apps.settings_.theme_refreshes == settings_theme_refreshes + theme + 1);
            assert(apps.weather_.opens == 1 && apps.weather_.closes == closes);
            assert(apps.weather_.root() == root && apps.weather_.active && !Hidden(root));
            assert(apps.state_.page() == Page::Weather && apps.state_.selection() == 3);
            assert(apps.work == work && apps.wake_requests == wake_requests);
        }
        apps.ShowHome();
        assert(apps.weather_.closes == closes + 1 && !apps.weather_.active && Hidden(root));
        assert(apps.state_.page() == Page::Home && apps.state_.selection() == 3);
        AssertSnapped(apps);
        apps.OpenSelected();
        assert(apps.state_.page() == Page::Weather && apps.weather_.opens == 2);
        assert(apps.weather_.root() == root && apps.weather_.active && !Hidden(root));
        const int render_closes = apps.weather_.closes;
        const int render_opens = apps.weather_.opens;
        // Raw page changes exercise RenderPage's own hide logic, independently
        // of ShowHome/Close and OpenSelected/Open lifecycle callbacks.
        for (int target = 0; target < StopwatchAppState::kAppCount; ++target) {
            apps.state_.Open(3);
            apps.RenderPage();
            assert(!Hidden(root));
            apps.state_.Open(target);
            apps.RenderPage();
            assert(Hidden(root) == (target != 3));
            assert(Hidden(apps.overlay_) == (target == 0));
            if (target == 1) assert(!Hidden(apps.badge_));
            if (target == 2) assert(!Hidden(apps.settings_.root()));
        }
        apps.state_.Open(1);
        apps.editing_ = true;
        apps.RenderPage();
        assert(Hidden(root) && !Hidden(apps.editor_panel_) && Hidden(apps.badge_));
        apps.state_.ShowHome();
        apps.RenderPage();
        assert(Hidden(root) && !Hidden(apps.home_));
        assert(apps.weather_.opens == render_opens && apps.weather_.closes == render_closes);
        assert(apps.weather_.root() == root && objects.size() == object_count);
    } else if (test == "clock") {
        const int initial = ClockWrites(apps);
        apps.UpdateHomeClock();
        assert(ClockText(apps) == "--:--");
        assert(ClockWrites(apps) == initial);
        clock_now = 1700000040;  // Exactly on a minute boundary; local hour is stubbed.
        apps.UpdateHomeClock();
        char expected[6];
        std::snprintf(expected, sizeof(expected), "09:%02d", int((clock_now / 60) % 60));
        assert(ClockText(apps) == expected);
        const int writes = ClockWrites(apps);
        const int localtime_calls = clock_localtime_calls;
        clock_now += 59;
        apps.UpdateHomeClock();
        assert(ClockWrites(apps) == writes && clock_localtime_calls == localtime_calls);
        ++clock_now;
        apps.UpdateHomeClock();
        assert(ClockWrites(apps) == writes + 5 && clock_localtime_calls == localtime_calls + 1);
        assert(apps.state_.page() == Page::Home && apps.state_.selection() == 0 && apps.work.empty());
    } else if (test == "tick") {
        apps.state_.Open(1);
        apps.editing_ = true;
        apps.editor_requested_.store(true);
        apps.RenderPage();
        const auto page = apps.state_.page();
        lv_timer_t timer{&apps};
        clock_now = 1700000040;
        apps.Tick(&timer);
        assert(apps.state_.page() == page && apps.state_.selection() == 1);
        assert(apps.editing_ && apps.editor_requested_.load() && apps.work.empty());
        assert(!Hidden(apps.editor_panel_) && !Hidden(apps.overlay_));
        const int writes = ClockWrites(apps);
        for (int index = 0; index < 100; ++index) apps.Tick(&timer);
        assert(ClockWrites(apps) == writes && apps.work.empty());
        apps.badge_changed_.store(true);
        apps.queue_accepts = false;
        apps.Tick(&timer);
        assert(apps.badge_changed_.load() && apps.work.empty());
        apps.queue_accepts = true;
        apps.Tick(&timer);
        assert(!apps.badge_changed_.load());
        assert((apps.work == std::vector<StopwatchApps::Work>{StopwatchApps::Work::Load}));
        assert(apps.editing_ && apps.editor_requested_.load() && apps.state_.page() == page);
    } else if (test == "native_swipes") {
        // Use the actual SetupHome tree, including artwork and arrow line children.
        lv_obj_t* previous = nullptr;
        for (auto* child : apps.home_->children) {
            if (child->width == 52 && child->height == 160 && child->x == 7) previous = child;
        }
        assert(previous && !previous->children.empty());
        const int orders[2][5] = {{2, 3, 1, 4, 0}, {4, 1, 3, 2, 0}};
        for (int direction : {-1, 1}) {
            for (int hit_kind = 0; hit_kind < 4; ++hit_kind) {
                for (int expected : orders[direction < 0 ? 0 : 1]) {
                    const int selected = apps.state_.selection();
                    lv_obj_t* hit = apps.home_icons_[selected];
                    if (hit_kind == 1) hit = hit->children.back();
                    if (hit_kind == 2) hit = apps.home_name_;
                    if (hit_kind == 3) hit = previous->children.front();
                    assert(Swipe(hit, direction < 0 ? LV_DIR_RIGHT : LV_DIR_LEFT) == apps.home_);
                    assert(input.wait_release && apps.home_animating_);
                    assert(apps.home_direction_ == direction && apps.state_.selection() == expected);
                    assert(apps.state_.page() == Page::Home);
                    AssertDots(apps);
                    AdvanceAnimation(466);
                    AssertSnapped(apps);
                    assert(apps.settings_.opens == 0 && apps.weather_.opens == 0);
                    assert(apps.wake_requests.size() == 1 && !apps.wake_requests.back());
                }
            }
        }
    } else if (test == "clicks" || test == "buttons") {
        lv_obj_t *previous = nullptr, *next = nullptr;
        for (auto* object : apps.home_->children) {
            if (object->width == 52 && object->height == 160) {
                if (object->x < 233) previous = object;
                else next = object;
            }
        }
        assert(previous && next);
        const int orders[2][5] = {{2, 3, 1, 4, 0}, {4, 1, 3, 2, 0}};
        for (int direction : {-1, 1}) {
            for (int expected : orders[direction < 0 ? 0 : 1]) {
                const auto wake_requests = apps.wake_requests;
                if (test == "clicks") Click(direction < 0 ? previous : next);
                else assert(apps.HandleButton(direction < 0 ? 0 : 1));
                assert(apps.home_animating_ && apps.home_direction_ == direction);
                assert(apps.state_.selection() == expected && apps.state_.page() == Page::Home);
                AssertDots(apps);
                const int settings_opens = apps.settings_.opens, weather_opens = apps.weather_.opens;
                if (test == "clicks") Click(apps.home_icons_[expected]);
                else assert(apps.HandleButton(2));
                assert(apps.state_.page() == Page::Home);  // No open while sliding.
                assert(apps.settings_.opens == settings_opens && apps.weather_.opens == weather_opens);
                assert(apps.wake_requests == wake_requests);
                AdvanceAnimation(466);
                AssertSnapped(apps);
                for (int reopen = 0; reopen < 2; ++reopen) {
                    if (test == "clicks") Click(apps.home_icons_[expected]);
                    else assert(apps.HandleButton(2));
                    AssertOpened(apps, expected);
                    apps.ShowHome();
                    assert(apps.state_.page() == Page::Home && !Hidden(apps.home_));
                    assert(!apps.weather_.active && !apps.wake_requests.back());
                    AssertSnapped(apps);
                }
            }
        }
        assert(apps.settings_.opens == 4 && apps.weather_.opens == 4 && apps.alarm_.opens == 4);
        assert(objects.size() == object_count);
    } else {
        assert(false);
    }
}
'''


@unittest.skipUnless(COMPILER, "requires a host C++ compiler")
class StopwatchHomeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="stopwatch-home-test-")
        cls.addClassCleanup(cls.tmp.cleanup)
        apps = (BOARD / "stopwatch_apps.cc").read_text(encoding="utf-8")
        header = (BOARD / "stopwatch_apps.h").read_text(encoding="utf-8")
        source = PLATFORM
        source += next(line for line in apps.splitlines()
                       if line.startswith("constexpr const char* kNames[]")) + "\n"
        source += r'''
class StopwatchApps {
public:
    enum class Work { Initialize, Load, Previous, Next, StopEditor };
    StopwatchAppState state_;
    StopwatchSettings settings_;
    StopwatchWeather weather_;
    StopwatchAlarm alarm_;
    Display display;
    Display* display_ = &display;
    lv_obj_t* overlay_ = nullptr;
    lv_obj_t* home_ = nullptr;
    lv_obj_t* home_name_ = nullptr;
    lv_obj_t* home_clock_ = nullptr;
    lv_obj_t* badge_ = nullptr;
    lv_obj_t* badge_picture_ = nullptr;
    lv_obj_t* badge_hint_ = nullptr;
    lv_obj_t* editor_panel_ = nullptr;
    lv_obj_t* editor_url_ = nullptr;
    Queue* work_queue_ = nullptr;
    std::atomic<bool> editor_requested_{false}, badge_changed_{false};
    std::vector<Work> work;
    std::vector<bool> wake_requests;
    std::function<void(bool)> wake_policy_ = [this](bool enabled) {
        wake_requests.push_back(enabled);
    };
    void SetWakeWordEnabled(bool enabled);
    bool queue_accepts = true;
    bool QueueWork(Work value) {
        if (!queue_accepts) return false;
        work.push_back(value); return true;
    }
    void Setup(lv_obj_t* screen);
    void SetupHome();
    void Worker() {}
    void StartEditor() {}
    void StopEditor() {}
    void ApplyTheme();
    void RenderPage();
    void RenderHome();
    void AnimateHome(int direction);
    void UpdateHomeClock();
    void Step(int direction);
    void OpenSelected();
    bool HandleButton(int button);
    void ShowHome();
    bool CanNavigate() const;
    void AdvanceNavigation();
    static void DrawHomeIcon(lv_obj_t* parent, int index);
    static void HomeAnimation(void* user_data, int32_t progress);
    static void Tick(lv_timer_t* timer);
    static void Gesture(lv_event_t* event);
    static lv_obj_t* Panel(lv_obj_t* parent);
    static lv_obj_t* Label(lv_obj_t* parent, const char* text, int width, int y);
    lv_obj_t* Action(lv_obj_t* parent, const char* text, int width, int height,
                     int x, int y, lv_event_cb_t callback);
'''
        for token in ("lv_obj_t* home_icons_[", "lv_obj_t* home_dots_[",
                      "bool boot_home_pending_", "bool editing_", "time_t home_clock_minute_",
                      "int home_from_", "int home_direction_", "bool home_animating_",
                      "DeviceState last_voice_state_", "int navigation_target_",
                      "uint32_t navigation_tick_", "uint32_t navigation_generation_"):
            source += next(line.strip() for line in header.splitlines()
                           if line.strip().startswith(token)) + "\n"
        source += "};\n#define time test_time\n#define localtime_r test_localtime_r\n"
        for signature in (
            "lv_obj_t* StopwatchApps::Panel(lv_obj_t* parent)",
            "lv_obj_t* StopwatchApps::Label(lv_obj_t* parent,",
            "lv_obj_t* StopwatchApps::Action(lv_obj_t* parent,",
            "void StopwatchApps::Setup(lv_obj_t* screen)",
            "void StopwatchApps::SetupHome()",
            "void StopwatchApps::DrawHomeIcon(lv_obj_t* parent, int index)",
            "void StopwatchApps::ApplyTheme()",
            "void StopwatchApps::RenderPage()",
            "void StopwatchApps::RenderHome()",
            "void StopwatchApps::AnimateHome(int direction)",
            "void StopwatchApps::HomeAnimation(void* user_data, int32_t progress)",
            "void StopwatchApps::UpdateHomeClock()",
            "void StopwatchApps::Step(int direction)",
            "void StopwatchApps::OpenSelected()",
            "bool StopwatchApps::HandleButton(int button)",
            "void StopwatchApps::ShowHome()",
            "void StopwatchApps::SetWakeWordEnabled(bool enabled)",
            "bool StopwatchApps::CanNavigate() const",
            "void StopwatchApps::AdvanceNavigation()",
            "void StopwatchApps::Tick(lv_timer_t* timer)",
            "void StopwatchApps::Gesture(lv_event_t* event)",
        ):
            source += function(apps, signature)
        source += "#undef time\n#undef localtime_r\n"
        source += CHECKS
        binary = pathlib.Path(cls.tmp.name) / "stopwatch-home-test.exe"
        subprocess.run(
            [COMPILER, "-std=c++17", "-Wall", "-Wextra", "-I", str(BOARD),
             "-I", str(ROOT / "main"), "-x", "c++", "-", "-o", str(binary)],
            input=source, text=True, check=True, timeout=60,
        )
        cls.binary = str(binary)

    def run_case(self, name):
        subprocess.run([self.binary, name], check=True, timeout=10)

    def test_factory_black_launcher_geometry_native_icons_clock_and_five_centered_dots(self):
        self.run_case("layout")

    def test_all_app_names_icons_open_targets_and_dots_survive_home_reopen_and_theme_refresh(self):
        self.run_case("theme")

    def test_all_ten_directed_slides_show_only_outgoing_and_incoming_icons_then_snap(self):
        self.run_case("animation")

    def test_repeated_steps_and_open_are_ignored_during_one_bounded_animation(self):
        self.run_case("bounded_input")

    def test_explicit_home_cancels_slide_and_snaps_to_selected_icon(self):
        self.run_case("cancel")

    def test_leaving_home_cancels_slide_and_stale_callbacks_cannot_mutate_page(self):
        self.run_case("leave_home")

    def test_settings_entry_resets_menu_but_render_preserves_navigation_and_hides_other_roots(self):
        self.run_case("settings_page")

    def test_weather_lifecycle_render_page_and_active_theme_refresh_reuse_one_root(self):
        self.run_case("weather_page")

    def test_clock_waits_for_sync_and_only_updates_when_minute_changes(self):
        self.run_case("clock")

    def test_tick_updates_clock_without_mutating_local_editor_or_badge_work(self):
        self.run_case("tick")

    def test_real_home_icon_artwork_label_and_arrow_swipes_never_open_an_app(self):
        self.run_case("native_swipes")

    def test_home_arrows_and_icon_taps_keep_names_art_targets_and_dots_in_both_directions(self):
        self.run_case("clicks")

    def test_home_a_b_and_double_b_keep_names_art_targets_and_dots_in_both_directions(self):
        self.run_case("buttons")


class StopwatchFactoryBadgeIconTests(unittest.TestCase):
    def test_native_factory_pixels_descriptor_and_complete_license_are_preserved(self):
        source = (BOARD / "stopwatch_home_badge_icon.c").read_text(encoding="utf-8")
        array = re.search(r"uint8_t icon_badge_map\[\] = \{(.*?)\};", source, re.S)
        self.assertIsNotNone(array)
        pixels = bytes(int(value, 16) for value in
                       re.findall(r"0x([0-9a-fA-F]{2})", array.group(1)))
        self.assertEqual(len(pixels), 200 * 200 * 2)
        self.assertEqual(hashlib.sha256(pixels).hexdigest(),
                         "ebc954d749256efe1d8b95d769b7106a5c0cbdca320a3ae7d13e27b958ade965")
        descriptor = source[source.index("const lv_image_dsc_t icon_badge = {"):]
        for field, value in (("header.cf", "LV_COLOR_FORMAT_RGB565"),
                             ("header.magic", "LV_IMAGE_HEADER_MAGIC"),
                             ("header.w", "200"), ("header.h", "200"),
                             ("data_size", "40000 * 2"), ("data", "icon_badge_map")):
            self.assertRegex(descriptor, rf"\.{re.escape(field)}\s*=\s*{re.escape(value)}\s*,")
        self.assertIn("118ff99b5b5fdc1aee472f9be3b1c709a55fc240", source)
        for notice in ("Copyright (c) 2026 M5Stack Technology CO LTD",
                       "Permission is hereby granted, free of charge",
                       "copies or substantial portions of the Software.",
                       'THE SOFTWARE IS PROVIDED "AS IS"',
                       "OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE",
                       "SOFTWARE."):
            self.assertIn(notice, source)

    @unittest.skipUnless(COMPILER, "requires a host C++ compiler")
    def test_stock_build_excludes_factory_icon_and_lvgl_dependency(self):
        with tempfile.TemporaryDirectory(prefix="stopwatch-factory-icon-") as directory:
            pathlib.Path(directory, "sdkconfig.h").write_text("", encoding="utf-8")
            result = subprocess.run(
                [COMPILER, "-E", "-P", "-x", "c++", "-I", directory,
                 "-DCONFIG_STOPWATCH_LOCAL_APPS=0", str(BOARD / "stopwatch_home_badge_icon.c")],
                check=True, capture_output=True, text=True, timeout=30,
            )
            self.assertEqual(result.stdout.strip(), "")

    @unittest.skipUnless(COMPILER, "requires a host C++ compiler")
    def test_board_callbacks_apply_voice_policy_and_propagate_wifi_success_or_failure(self):
        board = (BOARD / "m5stack_stopwatch.cc").read_text(encoding="utf-8")
        setup = function(board, "void SetupUI()")
        # Keep both real constructor callbacks, independently of line wrapping.
        start = setup.index("apps_ = std::make_unique<StopwatchApps>(")
        callbacks = setup[start:setup.index("apps_->Setup(", start)]
        setter = function(board, "void SetWifiConfigCallback(")
        registration = function(board, "display_->SetWifiConfigCallback(")
        source = r'''
#include <cassert>
#include <functional>
#include <memory>
#include <utility>
#include <vector>
struct Application {
    std::vector<std::pair<int, bool>> requests;
    static Application& GetInstance() { static Application app; return app; }
    void SetConversationEnabled(bool enabled) { requests.emplace_back(0, enabled); }
    void SetWakeWordDetectionEnabled(bool enabled) { requests.emplace_back(1, enabled); }
};
struct DisplayLockGuard {
    explicit DisplayLockGuard(void*) {}
    explicit operator bool() const { return true; }
};
struct StopwatchWeatherService {};
struct StopwatchAlarmService {};
struct StopwatchApps {
    std::function<void(bool)> policy;
    std::function<bool()> configure_wifi;
    StopwatchWeatherService* weather_service;
    StopwatchAlarmService* alarm_service;
    StopwatchApps(void*, std::function<void(bool)> callback, std::function<bool()> wifi,
                  StopwatchWeatherService* weather, StopwatchAlarmService* alarm)
        : policy(std::move(callback)), configure_wifi(std::move(wifi)),
          weather_service(weather), alarm_service(alarm) {}
};
struct Display {
    std::unique_ptr<StopwatchApps> apps_;
    std::function<bool()> configure_wifi_;
    StopwatchWeatherService* weather_service_ = nullptr;
    StopwatchAlarmService* alarm_service_ = nullptr;
    void Setup() {
'''
        source += callbacks + "\n}\n" + setter + "};\n"
        source += r'''
struct M5StackStopwatchBoard {
    Display* display_;
    int wifi_config_calls = 0;
    bool wifi_config_result = false;
    bool EnterWifiConfigMode() { ++wifi_config_calls; return wifi_config_result; }
    void InitializeCallback() {
'''
        source += registration + ");\n}\n};\n"
        source += r'''
int main() {
    Display display;
    StopwatchWeatherService weather;
    StopwatchAlarmService alarm;
    display.weather_service_ = &weather;
    display.alarm_service_ = &alarm;
    display.Setup();
    assert(display.apps_->weather_service == &weather);
    assert(display.apps_->alarm_service == &alarm);
    display.apps_->policy(false);
    display.apps_->policy(true);
    const std::vector<std::pair<int, bool>> expected{{0, false}, {0, true}};
    assert(Application::GetInstance().requests == expected);
    assert(display.apps_->configure_wifi && !display.apps_->configure_wifi());
    M5StackStopwatchBoard board{&display};
    board.InitializeCallback();
    int calls = 0;
    for (bool success : {true, false, true, false}) {
        board.wifi_config_result = success;
        assert(display.apps_->configure_wifi() == success);
        assert(board.wifi_config_calls == ++calls);
    }
    display.SetWifiConfigCallback({});
    assert(!display.apps_->configure_wifi());
    assert(board.wifi_config_calls == calls);
}
'''
        with tempfile.TemporaryDirectory(prefix="stopwatch-policy-wiring-") as directory:
            binary = str(pathlib.Path(directory, "policy-wiring.exe"))
            subprocess.run([COMPILER, "-std=c++17", "-Wall", "-Wextra", "-Werror",
                            "-x", "c++", "-", "-o", binary],
                           input=source, text=True, check=True, timeout=60)
            subprocess.run([binary], check=True, timeout=10)
