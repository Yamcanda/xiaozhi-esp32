// Alarm layout adapted from M5Stack M5StopWatch-UserDemo (MIT).
// SPDX-FileCopyrightText: 2026 M5Stack Technology CO LTD
// SPDX-License-Identifier: MIT

#include "sdkconfig.h"

#if CONFIG_STOPWATCH_LOCAL_APPS

#include "stopwatch_alarm.h"

#include <algorithm>
#include <array>
#include <cstdio>
#include <cstring>
#include <string>
#include <utility>

#include "stopwatch_alarm_service.h"

LV_FONT_DECLARE(font_noto_sans_basic_20_4);
LV_FONT_DECLARE(font_noto_sans_basic_30_4);
extern "C" {
LV_FONT_DECLARE(MontserratSemiBold26);
LV_FONT_DECLARE(CommissionerMedium108);
}

namespace {
constexpr uint32_t kGreen = 0x4AD78C;
constexpr uint32_t kGrey = 0x4C4C4C;
constexpr uint32_t kDark = 0x343434;

void Text(lv_obj_t* label, const std::string& text) {
    if (std::strcmp(lv_label_get_text(label), text.c_str()) != 0) {
        lv_label_set_text(label, text.c_str());
    }
}

void Enabled(lv_obj_t* object, bool enabled) {
    if (enabled) {
        lv_obj_remove_state(object, LV_STATE_DISABLED);
    } else {
        lv_obj_add_state(object, LV_STATE_DISABLED);
    }
}

void Checked(lv_obj_t* object, bool checked) {
    if (checked) {
        lv_obj_add_state(object, LV_STATE_CHECKED);
    } else {
        lv_obj_remove_state(object, LV_STATE_CHECKED);
    }
}

bool Visible(lv_obj_t* object) {
    for (; object != nullptr; object = lv_obj_get_parent(object)) {
        if (lv_obj_has_flag(object, LV_OBJ_FLAG_HIDDEN)) {
            return false;
        }
    }
    return true;
}

void WaitForPointerRelease() {
    for (auto* input = lv_indev_get_next(nullptr); input != nullptr;
         input = lv_indev_get_next(input)) {
        if (lv_indev_get_type(input) == LV_INDEV_TYPE_POINTER) {
            lv_indev_wait_release(input);
        }
    }
}

bool PointerDown() {
    for (auto* input = lv_indev_get_next(nullptr); input != nullptr;
         input = lv_indev_get_next(input)) {
        if (lv_indev_get_type(input) == LV_INDEV_TYPE_POINTER &&
            lv_indev_get_state(input) == LV_INDEV_STATE_PRESSED) {
            return true;
        }
    }
    return false;
}

std::string Time(const stopwatch_alarm::Alarm& alarm) {
    char time[6];
    std::snprintf(time, sizeof(time), "%02d:%02d", alarm.hour, alarm.minute);
    return time;
}

std::string Detail(const stopwatch_alarm::Alarm& alarm) {
    // Do not render arbitrary reminder text: it may require unavailable glyphs.
    return (alarm.date.empty() ? std::string("Daily") : "Once " + alarm.date) +
           (alarm.reminder.empty() ? "" : " / Voice");
}

std::string Error(const std::string& error) {
    if (error == "conflict") {
        return "Alarms changed. Try again.";
    }
    if (error == "not_ready") {
        return "Alarms are still loading";
    }
    if (error == "queue_full" || error == "busy") {
        return "Busy. Please try again.";
    }
    if (error == "limit_reached" || error == "full") {
        return "Alarm limit reached (16)";
    }
    if (error == "not_found") {
        return "Alarm no longer exists";
    }
    return "Not saved: " + (error.empty() ? std::string("service unavailable") : error);
}

lv_obj_t* Panel(lv_obj_t* parent) {
    auto* panel = lv_obj_create(parent);
    lv_obj_remove_style_all(panel);
    lv_obj_set_size(panel, 466, 466);
    lv_obj_set_style_bg_color(panel, lv_color_black(), 0);
    lv_obj_set_style_bg_opa(panel, LV_OPA_COVER, 0);
    lv_obj_set_style_text_color(panel, lv_color_white(), 0);
    lv_obj_remove_flag(panel, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_remove_flag(panel, LV_OBJ_FLAG_GESTURE_BUBBLE);
    return panel;
}

lv_obj_t* Label(lv_obj_t* parent, const char* text, int x, int y, int width,
                const lv_font_t* font) {
    auto* label = lv_label_create(parent);
    lv_obj_set_width(label, width);
    lv_obj_set_pos(label, x, y);
    lv_obj_set_style_text_font(label, font, 0);
    lv_obj_set_style_text_color(label, lv_color_white(), 0);
    lv_obj_set_style_text_align(label, LV_TEXT_ALIGN_CENTER, 0);
    lv_label_set_text(label, text);
    lv_obj_remove_flag(label, LV_OBJ_FLAG_CLICKABLE);
    return label;
}
}  // namespace

struct StopwatchAlarm::Ui {
    enum class Page { List, Add, Delete };
    enum class Action { Add, Toggle, AskDelete, Hour, Minute, Save, Cancel, Delete };
    enum class Request { None, Add, Toggle, Delete };

    struct Control {
        Ui* owner = nullptr;
        lv_obj_t* object = nullptr;
        lv_obj_t* scroll_target = nullptr;
        Action action = Action::Cancel;
        Page page = Page::List;
        uint32_t id = 0;
        uint32_t revision = 0;
        unsigned generation = 0;
        unsigned press_generation = 0;
        uint32_t press_id = 0;
        uint32_t press_revision = 0;
        bool pressed = false;
    };
    struct Row {
        Control body, toggle, remove;
        lv_obj_t* time = nullptr;
        lv_obj_t* detail = nullptr;
        std::string summary;
        bool enabled = false;
    };

    explicit Ui(StopwatchAlarmService* service) : service(service) {}
    ~Ui() {
        if (timer != nullptr) {
            lv_timer_delete(timer);
        }
        if (root != nullptr) {
            lv_obj_delete(root);
        }
    }

    StopwatchAlarmService* service;
    stopwatch_alarm::Snapshot snapshot;
    bool have_snapshot = false;
    bool active = false;
    bool rows_dirty = true;
    bool delete_conflict = false;
    bool uncertain = false;
    unsigned generation = 0;
    Page page = Page::List;
    lv_obj_t* root = nullptr;
    lv_obj_t* list = nullptr;
    lv_obj_t* add = nullptr;
    lv_obj_t* confirm = nullptr;
    lv_obj_t* list_view = nullptr;
    lv_obj_t* title = nullptr;
    lv_obj_t* clock = nullptr;
    lv_obj_t* status = nullptr;
    lv_obj_t* empty = nullptr;
    lv_obj_t* delete_time = nullptr;
    lv_obj_t* delete_detail = nullptr;
    lv_timer_t* timer = nullptr;
    std::array<Row, stopwatch_alarm::kMaxAlarms> rows{};
    Control add_button, hour, minute, save, cancel_add, delete_button, cancel_delete;
    std::array<Control*, stopwatch_alarm::kMaxAlarms * 2 + 1> focus{};
    int focus_count = 0;
    int focused = 0;
    Control* editing = nullptr;
    int draft_hour = 7;
    int draft_minute = 0;
    stopwatch_alarm::Alarm delete_alarm;
    uint32_t delete_revision = 0;
    uint32_t request_id = 0;
    uint32_t request_alarm_id = 0;
    unsigned request_generation = 0;
    Request request = Request::None;
    std::string notice;

    void Bind(Control& control, lv_obj_t* object, Action action, Page page,
              lv_obj_t* scroll_target = nullptr) {
        control.owner = this;
        control.object = object;
        control.scroll_target = scroll_target;
        control.action = action;
        control.page = page;
        lv_obj_remove_flag(object, LV_OBJ_FLAG_GESTURE_BUBBLE);
        lv_obj_remove_flag(object, LV_OBJ_FLAG_EVENT_BUBBLE);
        lv_obj_set_style_border_color(object, lv_color_hex(kGreen), LV_STATE_FOCUSED);
        lv_obj_set_style_border_width(object, 3, LV_STATE_FOCUSED);
        lv_obj_add_event_cb(object, Event, LV_EVENT_PRESSED, &control);
        if (action == Action::Toggle || action == Action::Hour || action == Action::Minute) {
            lv_obj_add_event_cb(object, Event, LV_EVENT_VALUE_CHANGED, &control);
        }
        if (action == Action::AskDelete && object == scroll_target) {
            lv_obj_add_event_cb(object, Event, LV_EVENT_LONG_PRESSED, &control);
        } else if (action != Action::Toggle && action != Action::Hour && action != Action::Minute) {
            lv_obj_add_event_cb(object, Event, LV_EVENT_CLICKED, &control);
        }
    }

    lv_obj_t* Button(Control& control, lv_obj_t* parent, const char* text, int x, int y, int width,
                     int height, Action action, Page page, bool primary = false,
                     lv_obj_t* scroll_target = nullptr) {
        auto* button = lv_button_create(parent);
        lv_obj_remove_style_all(button);
        lv_obj_set_size(button, width, height);
        lv_obj_set_pos(button, x, y);
        lv_obj_set_style_radius(button, height / 2, 0);
        lv_obj_set_style_bg_color(button, lv_color_hex(primary ? kGreen : kGrey), 0);
        lv_obj_set_style_bg_opa(button, LV_OPA_COVER, 0);
        lv_obj_set_style_bg_color(button, lv_color_hex(primary ? 0x53BD65 : 0x696969),
                                  LV_STATE_PRESSED);
        lv_obj_remove_flag(button, LV_OBJ_FLAG_SCROLLABLE);
        auto* label = Label(button, text, 8, 0, width - 16,
                            height < 48 ? &font_noto_sans_basic_20_4 : &MontserratSemiBold26);
        lv_obj_center(label);
        if (primary) {
            lv_obj_set_style_text_color(label, lv_color_hex(0x0F5831), 0);
        }
        Bind(control, button, action, page, scroll_target);
        return button;
    }

    void Roller(Control& control, int x, int maximum, Action action) {
        auto* roller = lv_roller_create(add);
        lv_obj_remove_style_all(roller);
        lv_obj_set_size(roller, 150, 180);
        lv_obj_set_pos(roller, x, 150);
        lv_obj_set_style_radius(roller, 38, LV_PART_MAIN);
        lv_obj_set_style_bg_color(roller, lv_color_hex(kDark), LV_PART_MAIN);
        lv_obj_set_style_bg_opa(roller, LV_OPA_COVER, LV_PART_MAIN);
        lv_obj_set_style_text_font(roller, &MontserratSemiBold26, LV_PART_MAIN);
        lv_obj_set_style_text_line_space(roller, 9, LV_PART_MAIN);
        lv_obj_set_style_text_color(roller, lv_color_white(), LV_PART_MAIN);
        lv_obj_set_style_text_align(roller, LV_TEXT_ALIGN_CENTER, LV_PART_MAIN);
        lv_obj_set_style_bg_color(roller, lv_color_hex(0x696969), LV_PART_SELECTED);
        lv_obj_set_style_bg_opa(roller, LV_OPA_COVER, LV_PART_SELECTED);
        lv_obj_set_style_text_font(roller, &MontserratSemiBold26, LV_PART_SELECTED);
        lv_obj_set_style_text_line_space(roller, 9, LV_PART_SELECTED);
        lv_obj_set_style_text_color(roller, lv_color_white(), LV_PART_SELECTED);
        lv_obj_set_style_text_align(roller, LV_TEXT_ALIGN_CENTER, LV_PART_SELECTED);
        std::string options;
        for (int value = 0; value <= maximum; ++value) {
            char number[12];
            std::snprintf(number, sizeof(number), "%02d", value);
            if (!options.empty()) {
                options += '\n';
            }
            options += number;
        }
        lv_roller_set_options(roller, options.c_str(), LV_ROLLER_MODE_INFINITE);
        lv_roller_set_visible_row_count(roller, 5);
        Bind(control, roller, action, Page::Add);
    }

    void Setup(lv_obj_t* parent) {
        if (root != nullptr) {
            return;
        }
        root = Panel(parent);
        list = Panel(root);
        add = Panel(root);
        confirm = Panel(root);
        list_view = lv_obj_create(list);
        lv_obj_remove_style_all(list_view);
        lv_obj_set_pos(list_view, 46, 124);
        lv_obj_set_size(list_view, 374, 250);
        lv_obj_add_flag(list_view, LV_OBJ_FLAG_SCROLLABLE);
        lv_obj_remove_flag(list_view, LV_OBJ_FLAG_GESTURE_BUBBLE);
        lv_obj_set_scroll_dir(list_view, LV_DIR_VER);
        lv_obj_set_scrollbar_mode(list_view, LV_SCROLLBAR_MODE_ACTIVE);
        lv_obj_set_style_pad_bottom(list_view, 8, 0);
        empty = Label(list_view, "No alarms", 0, 70, 374, &font_noto_sans_basic_30_4);
        // A bounded, persistent row pool: a timer only rebinds released widgets.
        // Never destroy the widget currently executing a touch callback.
        for (size_t i = 0; i < rows.size(); ++i) {
            auto& row = rows[i];
            auto* body = lv_button_create(list_view);
            lv_obj_remove_style_all(body);
            lv_obj_set_size(body, 374, 119);
            lv_obj_set_pos(body, 0, static_cast<int>(i) * 136);
            lv_obj_set_style_radius(body, 52, 0);
            lv_obj_set_style_bg_color(body, lv_color_hex(kGrey), 0);
            lv_obj_set_style_bg_opa(body, LV_OPA_COVER, 0);
            lv_obj_remove_flag(body, LV_OBJ_FLAG_SCROLLABLE);
            Bind(row.body, body, Action::AskDelete, Page::List, body);
            row.time = Label(body, "", 28, 21, 196, &font_noto_sans_basic_30_4);
            row.detail = Label(body, "", 24, 77, 220, &font_noto_sans_basic_20_4);
            lv_label_set_long_mode(row.detail, LV_LABEL_LONG_SCROLL_CIRCULAR);
            auto* toggle = lv_switch_create(body);
            lv_obj_set_size(toggle, 80, 44);
            lv_obj_set_pos(toggle, 258, 20);
            lv_obj_set_style_bg_color(toggle, lv_color_hex(0x3A3A3A), LV_PART_MAIN);
            lv_obj_set_style_bg_opa(toggle, LV_OPA_COVER, LV_PART_MAIN);
            lv_obj_set_style_radius(toggle, LV_RADIUS_CIRCLE, LV_PART_MAIN);
            lv_obj_set_style_bg_color(toggle, lv_color_hex(kGreen),
                                      static_cast<uint32_t>(LV_PART_INDICATOR) | LV_STATE_CHECKED);
            lv_obj_set_style_bg_opa(toggle, LV_OPA_COVER, LV_PART_INDICATOR);
            lv_obj_set_style_radius(toggle, LV_RADIUS_CIRCLE, LV_PART_INDICATOR);
            lv_obj_set_style_bg_color(toggle, lv_color_white(), LV_PART_KNOB);
            lv_obj_set_style_bg_opa(toggle, LV_OPA_COVER, LV_PART_KNOB);
            lv_obj_set_style_radius(toggle, LV_RADIUS_CIRCLE, LV_PART_KNOB);
            Bind(row.toggle, toggle, Action::Toggle, Page::List, body);
            Button(row.remove, body, "Delete", 254, 73, 90, 34, Action::AskDelete, Page::List,
                   false, body);
            lv_obj_add_flag(body, LV_OBJ_FLAG_HIDDEN);
        }
        Button(add_button, list, "Add", 123, 390, 220, 54, Action::Add, Page::List, true);
        Label(add, "Hour", 68, 119, 150, &font_noto_sans_basic_20_4);
        Label(add, "Minute", 248, 119, 150, &font_noto_sans_basic_20_4);
        Roller(hour, 68, 23, Action::Hour);
        Roller(minute, 248, 59, Action::Minute);
        Label(add, ":", 213, 210, 40, &font_noto_sans_basic_30_4);
        Label(add, "24h / Daily repeat", 83, 330, 300, &font_noto_sans_basic_20_4);
        Button(cancel_add, add, "Cancel", 91, 365, 140, 64, Action::Cancel, Page::Add);
        Button(save, add, "OK", 235, 365, 140, 64, Action::Save, Page::Add, true);
        // The existing number font includes digits and ':', not arbitrary text.
        delete_time = Label(confirm, "", 53, 137, 360, &CommissionerMedium108);
        delete_detail = Label(confirm, "", 73, 263, 320, &font_noto_sans_basic_20_4);
        lv_label_set_long_mode(delete_detail, LV_LABEL_LONG_SCROLL_CIRCULAR);
        Label(confirm, "Cancel keeps this alarm", 83, 302, 300, &font_noto_sans_basic_20_4);
        Button(cancel_delete, confirm, "Cancel", 81, 355, 148, 64, Action::Cancel, Page::Delete);
        Button(delete_button, confirm, "Delete", 237, 355, 148, 64, Action::Delete, Page::Delete,
               true);
        title = Label(root, "Alarms", 73, 28, 320, &MontserratSemiBold26);
        clock = Label(root, "Clock not synced", 73, 65, 320, &font_noto_sans_basic_20_4);
        status = Label(root, "", 63, 91, 340, &font_noto_sans_basic_20_4);
        lv_label_set_long_mode(status, LV_LABEL_LONG_SCROLL_CIRCULAR);
        lv_obj_add_flag(add, LV_OBJ_FLAG_HIDDEN);
        lv_obj_add_flag(confirm, LV_OBJ_FLAG_HIDDEN);
        lv_obj_add_flag(root, LV_OBJ_FLAG_HIDDEN);
        timer = lv_timer_create(Tick, 250, this);
        lv_timer_pause(timer);
    }

    void Arm(Control& control) {
        control.generation = generation;
        control.pressed = false;
    }

    void ArmPage() {
        for (auto& row : rows) {
            Arm(row.body);
            Arm(row.toggle);
            Arm(row.remove);
        }
        for (auto* control :
             {&add_button, &hour, &minute, &save, &cancel_add, &delete_button, &cancel_delete}) {
            Arm(*control);
        }
    }

    void SetPage(Page next) {
        ++generation;
        WaitForPointerRelease();
        page = next;
        editing = nullptr;
        ArmPage();
        for (auto* panel : {list, add, confirm}) {
            lv_obj_add_flag(panel, LV_OBJ_FLAG_HIDDEN);
        }
        lv_obj_remove_flag(page == Page::List  ? list
                           : page == Page::Add ? add
                                               : confirm,
                           LV_OBJ_FLAG_HIDDEN);
        focus_count = 0;
        focused = 0;
        if (page == Page::List) {
            rows_dirty = true;
        } else if (page == Page::Add) {
            focus[focus_count++] = &hour;
            focus[focus_count++] = &minute;
            focus[focus_count++] = &cancel_add;
            focus[focus_count++] = &save;
        } else {
            // Safe default: Double B alone cannot immediately delete an alarm.
            focus[focus_count++] = &cancel_delete;
            focus[focus_count++] = &delete_button;
        }
        Focus(0);
        RenderStatus();
    }

    void ReadSnapshot(bool force = false) {
        if (service == nullptr ||
            (!force && have_snapshot && service->Version() == snapshot.version)) {
            return;
        }
        auto next = service->GetSnapshot();
        rows_dirty = rows_dirty || !have_snapshot || next.revision != snapshot.revision ||
                     next.initialized != snapshot.initialized;
        if (page == Page::Delete && next.revision != delete_revision) {
            delete_conflict = true;
        }
        snapshot = std::move(next);
        have_snapshot = true;
    }

    const stopwatch_alarm::Alarm* Find(uint32_t id) const {
        for (const auto& alarm : snapshot.alarms) {
            if (alarm.id == id) {
                return &alarm;
            }
        }
        return nullptr;
    }

    bool Ready() const {
        return service != nullptr && snapshot.initialized && request_id == 0 && !snapshot.pending;
    }

    void RenderList() {
        if (!rows_dirty || page != Page::List || PointerDown()) {
            return;
        }
        uint32_t focus_id = focus_count == 0 ? 0 : focus[focused]->id;
        Action focus_action = focus_count == 0 ? Action::Toggle : focus[focused]->action;
        const int old_focus = focused;
        ++generation;
        WaitForPointerRelease();
        ArmPage();
        focus_count = 0;
        const auto count = std::min(rows.size(), snapshot.alarms.size());
        for (size_t i = 0; i < rows.size(); ++i) {
            auto& row = rows[i];
            if (i >= count) {
                lv_obj_add_flag(row.body.object, LV_OBJ_FLAG_HIDDEN);
                continue;
            }
            const auto& alarm = snapshot.alarms[i];
            for (auto* control : {&row.body, &row.toggle, &row.remove}) {
                control->id = alarm.id;
                control->revision = snapshot.revision;
            }
            row.enabled = alarm.enabled;
            row.summary = Detail(alarm);
            Text(row.time, Time(alarm));
            Checked(row.toggle.object, alarm.enabled);
            lv_obj_remove_flag(row.body.object, LV_OBJ_FLAG_HIDDEN);
            focus[focus_count++] = &row.toggle;
            focus[focus_count++] = &row.remove;
        }
        focus[focus_count++] = &add_button;
        if (count == 0) {
            lv_obj_remove_flag(empty, LV_OBJ_FLAG_HIDDEN);
        } else {
            lv_obj_add_flag(empty, LV_OBJ_FLAG_HIDDEN);
        }
        rows_dirty = false;
        int next_focus = std::min(old_focus, focus_count - 1);
        for (int i = 0; i < focus_count; ++i) {
            if (focus[i]->id == focus_id && focus[i]->action == focus_action) {
                next_focus = i;
                break;
            }
        }
        Focus(next_focus);
    }

    void Focus(int index, bool scroll = false) {
        if (focus_count == 0) {
            return;
        }
        focused = (index + focus_count) % focus_count;
        for (int i = 0; i < focus_count; ++i) {
            if (i == focused) {
                lv_obj_add_state(focus[i]->object, LV_STATE_FOCUSED);
            } else {
                lv_obj_remove_state(focus[i]->object, LV_STATE_FOCUSED);
            }
        }
        if (scroll && focus[focused]->scroll_target != nullptr) {
            lv_obj_scroll_to_view(focus[focused]->scroll_target, LV_ANIM_OFF);
        }
    }

    void RenderStatus() {
        if (root == nullptr) {
            return;
        }
        if (page == Page::List) {
            char heading[32];
            std::snprintf(heading, sizeof(heading), "Alarms (%u/16)",
                          static_cast<unsigned>(snapshot.alarms.size()));
            Text(title, heading);
        } else {
            Text(title, page == Page::Add ? "Add Alarm" : "Delete this alarm?");
        }
        Text(clock, snapshot.time_valid ? "24-hour / Local time" : "Clock not synced");
        std::string text;
        if (service == nullptr) {
            text = "Alarm service unavailable";
        } else if (!snapshot.initialized) {
            text = snapshot.error.empty() ? "Loading alarms..." : Error(snapshot.error);
        } else if (request_id != 0) {
            text = "Accepted - saving, not saved yet";
        } else if (page == Page::Delete && delete_conflict) {
            text = "Alarms changed. Cancel and retry.";
        } else if (!notice.empty()) {
            text = notice;
        } else if (snapshot.pending) {
            text = "Another change is saving...";
        } else if (page == Page::Add) {
            text = editing != nullptr ? "A/B adjust - Double B done" : "Not saved - OK to add";
        } else if (page == Page::Delete) {
            text = "Confirm to remove this alarm";
        } else if (!snapshot.error.empty()) {
            text = Error(snapshot.error);
        } else {
            text = "Hold alarm to delete / A/B focus";
        }
        Text(status, text);
        const bool ready = Ready();
        Enabled(add_button.object, ready && snapshot.alarms.size() < rows.size());
        Enabled(hour.object, request_id == 0);
        Enabled(minute.object, request_id == 0);
        Enabled(save.object, ready && !uncertain && snapshot.alarms.size() < rows.size());
        Enabled(delete_button.object, ready && !delete_conflict && !uncertain);
        for (auto& row : rows) {
            const bool current = !rows_dirty && row.toggle.revision == snapshot.revision;
            Enabled(row.body.object, ready && current);
            Enabled(row.toggle.object, ready && current);
            Enabled(row.remove.object, ready && current);
            Text(row.detail,
                 request_id != 0 && request_alarm_id == row.toggle.id ? "Saving..." : row.summary);
        }
    }

    bool Current(Control& control) {
        if (!active || page != control.page || control.generation != generation ||
            !Visible(control.object) || lv_obj_has_state(control.object, LV_STATE_DISABLED)) {
            return false;
        }
        return true;
    }

    bool CurrentRow(const Control& control) {
        if (rows_dirty || control.revision != snapshot.revision || Find(control.id) == nullptr) {
            notice = "Alarms changed. Please try again.";
            rows_dirty = true;
            RenderStatus();
            return false;
        }
        return true;
    }

    void Submitted(const stopwatch_alarm::Result& result, Request kind, uint32_t id = 0) {
        if (!result.ok || result.request_id == 0) {
            notice = Error(result.error);
            if (result.error == "conflict") {
                rows_dirty = true;
                delete_conflict = page == Page::Delete;
            }
        } else {
            // Acceptance is not a saved configuration. Only this request's
            // GetRequest completion may show success or leave the editor.
            request_id = result.request_id;
            request_alarm_id = id;
            request = kind;
            request_generation = generation;
            editing = nullptr;
            notice.clear();
        }
        ReadSnapshot(true);
        RenderStatus();
    }

    void PollRequest() {
        if (request_id == 0 || service == nullptr) {
            return;
        }
        const auto result = service->GetRequest(request_id);
        if (result.pending) {
            return;
        }
        if (result.request_id != request_id && result.error != "unknown_request") {
            return;
        }
        const auto kind = request;
        const bool same_page = generation == request_generation;
        request_id = 0;
        request_alarm_id = 0;
        request = Request::None;
        ReadSnapshot(true);
        if (result.ok) {
            notice = kind == Request::Delete ? "Alarm deleted" : "Saved";
            if (same_page && (page == Page::Add || page == Page::Delete)) {
                SetPage(Page::List);
            }
        } else if (result.error == "unknown_request") {
            // A very old accepted request may have committed before eviction.
            // Never claim failure or offer a one-click duplicate Add retry.
            uncertain = true;
            notice = "Save status expired. Check the list.";
        } else {
            notice = Error(result.error);
            delete_conflict = page == Page::Delete &&
                              (result.error == "conflict" || snapshot.revision != delete_revision);
        }
    }

    void Activate(Control& control, bool from_touch = false, bool wanted_enabled = false) {
        if (!Current(control)) {
            return;
        }
        ReadSnapshot(true);  // No row rebinding or page destruction in callbacks.
        switch (control.action) {
            case Action::Add:
                if (Ready() && snapshot.alarms.size() < rows.size()) {
                    draft_hour = 7;
                    draft_minute = 0;
                    uncertain = false;
                    notice.clear();
                    lv_roller_set_selected(hour.object, 7, LV_ANIM_OFF);
                    lv_roller_set_selected(minute.object, 0, LV_ANIM_OFF);
                    SetPage(Page::Add);
                }
                break;
            case Action::Toggle:
                if (Ready() && CurrentRow(control)) {
                    const bool enabled = from_touch ? wanted_enabled : !Find(control.id)->enabled;
                    Submitted(service->SetEnabled(control.id, enabled, control.revision),
                              Request::Toggle, control.id);
                }
                break;
            case Action::AskDelete:
                if (Ready() && CurrentRow(control)) {
                    delete_alarm = *Find(control.id);
                    delete_revision = control.revision;
                    delete_conflict = false;
                    uncertain = false;
                    notice.clear();
                    Text(delete_time, Time(delete_alarm));
                    Text(delete_detail, Detail(delete_alarm));
                    SetPage(Page::Delete);
                }
                break;
            case Action::Hour:
            case Action::Minute:
                if (request_id == 0) {
                    notice.clear();
                    editing = editing == &control ? nullptr : &control;
                }
                break;
            case Action::Save:
                if (Ready() && !uncertain && snapshot.alarms.size() < rows.size()) {
                    // Draft changes and leaving the page never call the service.
                    Submitted(service->Add(draft_hour, draft_minute), Request::Add);
                }
                break;
            case Action::Cancel:
                notice = request_id != 0 ? "Accepted request is still saving" : "";
                uncertain = false;
                SetPage(Page::List);
                break;
            case Action::Delete:
                if (Ready() && !delete_conflict && !uncertain &&
                    snapshot.revision == delete_revision && Find(delete_alarm.id) != nullptr) {
                    Submitted(service->Remove(delete_alarm.id, delete_revision), Request::Delete,
                              delete_alarm.id);
                }
                break;
        }
        RenderStatus();
    }

    static void Event(lv_event_t* event) {
        auto* control = static_cast<Control*>(lv_event_get_user_data(event));
        auto& self = *control->owner;
        if (lv_event_get_target(event) != control->object) {
            return;
        }
        const auto code = lv_event_get_code(event);
        bool enabled = false;
        if (control->action == Action::Toggle && code == LV_EVENT_VALUE_CHANGED) {
            enabled = lv_obj_has_state(control->object, LV_STATE_CHECKED);
            for (const auto& row : self.rows) {
                if (row.toggle.object == control->object) {
                    // LVGL toggles first, even on a retained release. Restore
                    // committed state before validating the event generation.
                    Checked(control->object, row.enabled);
                    break;
                }
            }
        }
        if (!self.Current(*control)) {
            return;
        }
        if (code == LV_EVENT_PRESSED) {
            control->pressed = true;
            control->press_generation = self.generation;
            control->press_id = control->id;
            control->press_revision = control->revision;
            return;
        }
        if (!control->pressed || control->press_generation != self.generation ||
            control->press_id != control->id || control->press_revision != control->revision) {
            return;
        }
        if (control->action == Action::Hour || control->action == Action::Minute) {
            if (code == LV_EVENT_VALUE_CHANGED && self.request_id == 0) {
                const int value = lv_roller_get_selected(control->object);
                if (control->action == Action::Hour) {
                    self.draft_hour = std::clamp(value, 0, 23);
                } else {
                    self.draft_minute = std::clamp(value, 0, 59);
                }
                self.notice.clear();
                self.RenderStatus();
                return;
            }
        }
        control->pressed = false;  // One activation per press, even after a failed save.
        for (int i = 0; i < self.focus_count; ++i) {
            if (self.focus[i] == control) {
                self.Focus(i);
                break;
            }
        }
        self.Activate(*control, true, enabled);
    }

    void Poll() {
        if (!active || root == nullptr) {
            return;
        }
        if (!Visible(root)) {
            lv_timer_pause(timer);
            return;
        }
        ReadSnapshot();
        PollRequest();
        RenderList();
        RenderStatus();
    }

    static void Tick(lv_timer_t* timer) { static_cast<Ui*>(lv_timer_get_user_data(timer))->Poll(); }

    void Open() {
        if (root == nullptr) {
            return;
        }
        active = true;
        uncertain = false;
        notice.clear();
        ReadSnapshot(true);
        SetPage(Page::List);
        lv_obj_scroll_to_y(list_view, 0, LV_ANIM_OFF);
        lv_obj_remove_flag(root, LV_OBJ_FLAG_HIDDEN);
        lv_timer_resume(timer);
        Poll();
    }

    void Close() {
        if (root == nullptr) {
            return;
        }
        ++generation;
        active = false;
        editing = nullptr;
        draft_hour = 7;
        draft_minute = 0;
        ArmPage();
        WaitForPointerRelease();
        lv_timer_pause(timer);
        lv_obj_add_flag(root, LV_OBJ_FLAG_HIDDEN);
        // An already accepted request belongs to the service; closing neither
        // submits a draft nor pretends to cancel an in-flight storage commit.
    }

    void HandleButton(int button) {
        if (!active || !Visible(root) || button < 0 || button > 2) {
            return;
        }
        const auto before = generation;
        Poll();
        if (before != generation || focus_count == 0) {
            return;
        }
        if (page == Page::Add && editing != nullptr && request_id == 0) {
            if (button == 2) {
                editing = nullptr;
            } else {
                auto& value = editing == &hour ? draft_hour : draft_minute;
                const int count = editing == &hour ? 24 : 60;
                value = (value + (button == 0 ? -1 : 1) + count) % count;
                lv_roller_set_selected(editing->object, value, LV_ANIM_OFF);
            }
            notice.clear();
            RenderStatus();
        } else if (button == 2) {
            Activate(*focus[focused]);
        } else {
            Focus(focused + (button == 0 ? -1 : 1), true);
        }
    }
};

StopwatchAlarm::StopwatchAlarm(Display* display, StopwatchAlarmService* service)
    : ui_(std::make_unique<Ui>(service)) {
    // The display is the caller's lock owner, not an optional capability to use.
    (void)display;
}
StopwatchAlarm::~StopwatchAlarm() = default;
void StopwatchAlarm::Setup(lv_obj_t* parent) { ui_->Setup(parent); }
lv_obj_t* StopwatchAlarm::root() const { return ui_->root; }
void StopwatchAlarm::Open() { ui_->Open(); }
void StopwatchAlarm::Close() { ui_->Close(); }
void StopwatchAlarm::HandleButton(int button) { ui_->HandleButton(button); }

#endif  // CONFIG_STOPWATCH_LOCAL_APPS
