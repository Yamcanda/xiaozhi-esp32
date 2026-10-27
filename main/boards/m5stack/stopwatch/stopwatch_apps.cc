#include "sdkconfig.h"

#if CONFIG_STOPWATCH_LOCAL_APPS

#include "stopwatch_apps.h"

#include <esp_log.h>
#include <freertos/task.h>
#include <src/misc/cache/instance/lv_image_cache.h>

#include <algorithm>
#include <cstdio>
#include <ctime>

#include "lvgl_theme.h"

LV_FONT_DECLARE(font_noto_sans_basic_20_4);
LV_FONT_DECLARE(font_noto_sans_basic_30_4);
extern "C" {
LV_IMAGE_DECLARE(icon_badge);
LV_IMAGE_DECLARE(icon_setup);
LV_IMAGE_DECLARE(icon_clock);
}

namespace {
constexpr const char* kTag = "StopwatchApps";
constexpr const char* kNames[] = {"XiaoZhi", "Badge", "Settings", "Weather", "Alarms"};
}  // namespace

StopwatchApps::StopwatchApps(Display* display, std::function<void(bool)> wake_policy,
                             std::function<bool()> configure_wifi,
                             StopwatchWeatherService* weather_service,
                             StopwatchAlarmService* alarm_service)
    : display_(display),
      wake_policy_(std::move(wake_policy)),
      settings_(
          display, [this]() { ShowHome(); }, std::move(configure_wifi), weather_service),
      weather_(display, weather_service),
      alarm_(display, alarm_service),
      editor_(store_, [this]() { badge_changed_.store(true); }) {}

lv_obj_t* StopwatchApps::Panel(lv_obj_t* parent) {
    auto* panel = lv_obj_create(parent);
    lv_obj_remove_style_all(panel);
    lv_obj_set_size(panel, 466, 466);
    // Stop gesture target discovery at the page; child controls bubble to it.
    lv_obj_remove_flag(panel, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_remove_flag(panel, LV_OBJ_FLAG_GESTURE_BUBBLE);
    lv_obj_add_flag(panel, LV_OBJ_FLAG_CLICKABLE);
    return panel;
}

lv_obj_t* StopwatchApps::Label(lv_obj_t* parent, const char* text, int width, int y) {
    auto* label = lv_label_create(parent);
    lv_obj_set_width(label, width);
    lv_label_set_text(label, text);
    lv_obj_set_style_text_align(label, LV_TEXT_ALIGN_CENTER, 0);
    lv_obj_set_style_text_font(label, &font_noto_sans_basic_20_4, 0);
    lv_obj_align(label, LV_ALIGN_TOP_MID, 0, y);
    lv_obj_remove_flag(label, LV_OBJ_FLAG_CLICKABLE);
    return label;
}

lv_obj_t* StopwatchApps::Action(lv_obj_t* parent, const char* text, int width, int height, int x,
                                int y, lv_event_cb_t callback) {
    auto* button = lv_button_create(parent);
    lv_obj_set_size(button, width, height);
    lv_obj_align(button, LV_ALIGN_TOP_MID, x, y);
    lv_obj_set_style_pad_all(button, 0, 0);
    lv_obj_add_flag(button, LV_OBJ_FLAG_GESTURE_BUBBLE);
    auto* label = Label(button, text, width - 8, 0);
    lv_obj_center(label);
    lv_obj_add_event_cb(button, callback, LV_EVENT_CLICKED, this);
    return button;
}

void StopwatchApps::DrawHomeIcon(lv_obj_t* parent, int index) {
    // Keep XiaoZhi's artwork and the existing Badge/Settings factory images.
    auto shape = [parent](int x, int y, int width, int height, int radius, uint32_t color) {
        auto* object = lv_obj_create(parent);
        lv_obj_remove_style_all(object);
        lv_obj_set_pos(object, x, y);
        lv_obj_set_size(object, width, height);
        lv_obj_set_style_radius(object, radius, 0);
        lv_obj_set_style_bg_color(object, lv_color_hex(color), 0);
        lv_obj_set_style_bg_opa(object, LV_OPA_COVER, 0);
        lv_obj_remove_flag(object, LV_OBJ_FLAG_SCROLLABLE);
        lv_obj_remove_flag(object, LV_OBJ_FLAG_CLICKABLE);
        lv_obj_add_flag(object, LV_OBJ_FLAG_GESTURE_BUBBLE);
        return object;
    };
    if (index == 0) {
        shape(96, 18, 8, 30, 4, 0x9CE9D2);
        shape(88, 8, 24, 24, LV_RADIUS_CIRCLE, 0x9CE9D2);
        shape(8, 86, 18, 46, 9, 0x65BBA8);
        shape(174, 86, 18, 46, 9, 0x65BBA8);
        shape(24, 44, 152, 130, 40, 0x9CE9D2);
        shape(40, 65, 120, 82, 28, 0x122D2B);
        shape(62, 88, 16, 30, 8, 0xB7FFF0);
        shape(122, 88, 16, 30, 8, 0xB7FFF0);
        shape(80, 157, 40, 6, 3, 0x3F8E7C);
    } else if (index == 1 || index == 2 || index == 4) {
        auto* image = lv_image_create(parent);
        lv_image_set_src(image, index == 1 ? &icon_badge : index == 2 ? &icon_setup : &icon_clock);
        lv_obj_center(image);
        lv_obj_remove_flag(image, LV_OBJ_FLAG_SCROLLABLE);
        lv_obj_remove_flag(image, LV_OBJ_FLAG_CLICKABLE);
        lv_obj_add_flag(image, LV_OBJ_FLAG_GESTURE_BUBBLE);
    } else if (index == 3) {
        shape(43, 32, 78, 78, LV_RADIUS_CIRCLE, 0xE7EFAF);
        shape(76, 9, 10, 16, 5, 0xE7EFAF);
        shape(19, 65, 16, 10, 5, 0xE7EFAF);
        shape(34, 20, 12, 12, 6, 0xE7EFAF);
        shape(124, 23, 12, 12, 6, 0xE7EFAF);
        shape(43, 91, 63, 63, LV_RADIUS_CIRCLE, 0xF4F5F0);
        shape(84, 67, 80, 84, LV_RADIUS_CIRCLE, 0xF4F5F0);
        shape(133, 100, 44, 54, LV_RADIUS_CIRCLE, 0xF4F5F0);
        shape(68, 120, 87, 34, 17, 0xF4F5F0);
    }
}

void StopwatchApps::SetupHome() {
    home_ = Panel(overlay_);
    lv_obj_set_style_bg_color(home_, lv_color_black(), 0);
    lv_obj_set_style_bg_opa(home_, LV_OPA_COVER, 0);
    lv_obj_set_style_text_color(home_, lv_color_white(), 0);
    for (int i = 0; i < StopwatchAppState::kAppCount; ++i) {
        auto* icon = lv_obj_create(home_);
        lv_obj_remove_style_all(icon);
        lv_obj_set_size(icon, 200, 200);
        lv_obj_set_pos(icon, 133, 118);
        lv_obj_remove_flag(icon, LV_OBJ_FLAG_SCROLLABLE);
        lv_obj_add_flag(icon, LV_OBJ_FLAG_CLICKABLE);
        lv_obj_add_flag(icon, LV_OBJ_FLAG_GESTURE_BUBBLE);
        lv_obj_add_event_cb(
            icon,
            [](lv_event_t* event) {
                static_cast<StopwatchApps*>(lv_event_get_user_data(event))->OpenSelected();
            },
            LV_EVENT_CLICKED, this);
        home_icons_[i] = icon;
        DrawHomeIcon(icon, i);
    }
    home_name_ = Label(home_, "", 300, 0);
    lv_obj_set_style_text_font(home_name_, &font_noto_sans_basic_30_4, 0);
    lv_obj_align(home_name_, LV_ALIGN_CENTER, 0, 155);

    static const lv_point_precise_t left[] = {{26, 2}, {2, 33}, {26, 65}};
    static const lv_point_precise_t right[] = {{2, 2}, {26, 33}, {2, 65}};
    for (int i = 0; i < 2; ++i) {
        auto* arrow = lv_obj_create(home_);
        lv_obj_remove_style_all(arrow);
        lv_obj_set_size(arrow, 52, 160);
        lv_obj_set_pos(arrow, i == 0 ? 7 : 407, 153);
        lv_obj_remove_flag(arrow, LV_OBJ_FLAG_SCROLLABLE);
        lv_obj_add_flag(arrow, LV_OBJ_FLAG_CLICKABLE);
        lv_obj_add_flag(arrow, LV_OBJ_FLAG_GESTURE_BUBBLE);
        lv_obj_add_event_cb(arrow, i == 0 ? +[](lv_event_t* event) {
            static_cast<StopwatchApps*>(lv_event_get_user_data(event))->Step(-1);
        } : +[](lv_event_t* event) {
            static_cast<StopwatchApps*>(lv_event_get_user_data(event))->Step(1);
        }, LV_EVENT_CLICKED, this);
        auto* line = lv_line_create(arrow);
        lv_line_set_points(line, i == 0 ? left : right, 3);
        lv_obj_set_size(line, 28, 67);
        lv_obj_center(line);
        lv_obj_set_style_line_color(line, lv_color_white(), 0);
        lv_obj_set_style_line_width(line, 4, 0);
        lv_obj_set_style_line_rounded(line, true, 0);
        lv_obj_remove_flag(line, LV_OBJ_FLAG_CLICKABLE);
        lv_obj_add_flag(line, LV_OBJ_FLAG_GESTURE_BUBBLE);
    }
    for (int i = 0; i < StopwatchAppState::kAppCount; ++i) {
        auto* dot = lv_obj_create(home_);
        lv_obj_remove_style_all(dot);
        lv_obj_set_style_radius(dot, LV_RADIUS_CIRCLE, 0);
        lv_obj_set_style_bg_color(dot, lv_color_white(), 0);
        lv_obj_remove_flag(dot, LV_OBJ_FLAG_SCROLLABLE);
        lv_obj_remove_flag(dot, LV_OBJ_FLAG_CLICKABLE);
        home_dots_[i] = dot;
    }
    home_clock_ = lv_obj_create(home_);
    lv_obj_remove_style_all(home_clock_);
    lv_obj_set_size(home_clock_, 114, 36);
    lv_obj_align(home_clock_, LV_ALIGN_TOP_MID, 0, 4);
    lv_obj_remove_flag(home_clock_, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_remove_flag(home_clock_, LV_OBJ_FLAG_CLICKABLE);
    lv_obj_add_flag(home_clock_, LV_OBJ_FLAG_OVERFLOW_VISIBLE);
    constexpr int rotations[] = {-100, -46, 0, 32, 84};
    constexpr int offsets_x[] = {3, 3, 1, 4, 4};
    constexpr int offsets_y[] = {0, 0, -2, -1, -1};
    for (int i = 0; i < 5; ++i) {
        auto* digit = lv_label_create(home_clock_);
        lv_label_set_text(digit, "-");
        lv_obj_set_style_text_font(digit, &font_noto_sans_basic_30_4, 0);
        lv_obj_set_style_transform_pivot_x(digit, 0, 0);
        lv_obj_set_style_transform_pivot_y(digit, 233, 0);
        lv_obj_align(digit, LV_ALIGN_CENTER, offsets_x[i], offsets_y[i]);
        lv_obj_set_style_transform_rotation(digit, rotations[i], 0);
        lv_obj_remove_flag(digit, LV_OBJ_FLAG_CLICKABLE);
    }
    lv_obj_add_event_cb(home_, Gesture, LV_EVENT_GESTURE, this);
    UpdateHomeClock();
    RenderHome();
}

void StopwatchApps::Setup(lv_obj_t* screen) {
    if (overlay_ != nullptr) {
        return;
    }
    // Keep core chat objects alive on the same screen underneath this opaque sibling.
    overlay_ = Panel(screen);
    lv_obj_set_style_bg_opa(overlay_, LV_OPA_COVER, 0);
    SetupHome();

    badge_ = Panel(overlay_);
    lv_obj_set_style_bg_color(badge_, lv_color_black(), 0);
    lv_obj_set_style_bg_opa(badge_, LV_OPA_COVER, 0);
    badge_picture_ = lv_image_create(badge_);
    lv_obj_center(badge_picture_);
    lv_obj_remove_flag(badge_picture_, LV_OBJ_FLAG_CLICKABLE);
    badge_hint_ = Label(badge_, "Loading badge...", 330, 175);
    lv_obj_set_style_text_color(badge_hint_, lv_color_white(), 0);
    lv_obj_add_event_cb(
        badge_,
        [](lv_event_t* event) {
            static_cast<StopwatchApps*>(lv_event_get_user_data(event))->StartEditor();
        },
        LV_EVENT_LONG_PRESSED, this);
    lv_obj_add_event_cb(badge_, Gesture, LV_EVENT_GESTURE, this);

    editor_panel_ = Panel(overlay_);
    lv_obj_set_style_bg_opa(editor_panel_, LV_OPA_COVER, 0);
    Label(editor_panel_, "BADGE EDITOR", 300, 72);
    Label(editor_panel_, "Phone and watch: same Wi-Fi\nOpen this URL in a browser", 330, 115);
    editor_url_ = Label(editor_panel_, "Starting editor...", 338, 183);
    lv_label_set_long_mode(editor_url_, LV_LABEL_LONG_WRAP);
    Action(editor_panel_, "Done", 140, 48, 0, 320, [](lv_event_t* event) {
        static_cast<StopwatchApps*>(lv_event_get_user_data(event))->StopEditor();
    });
    Label(editor_panel_, "Hold A+B together: Home", 300, 383);
    settings_.Setup(overlay_);
    weather_.Setup(overlay_);
    alarm_.Setup(overlay_);

    work_queue_ = xQueueCreate(8, sizeof(Work));
    TaskHandle_t worker = nullptr;
    if (work_queue_ == nullptr ||
        xTaskCreate([](void* arg) { static_cast<StopwatchApps*>(arg)->Worker(); }, "badge_worker",
                    8192, this, 2, &worker) != pdPASS) {
        ESP_LOGE(kTag, "Cannot start badge worker");
        if (work_queue_ != nullptr) {
            vQueueDelete(work_queue_);
            work_queue_ = nullptr;
        }
        lv_label_set_text(badge_hint_, "Badge worker unavailable");
    } else {
        QueueWork(Work::Initialize);
    }
    lv_timer_create(Tick, 50, this);
    ApplyTheme();
    RenderPage();
    // Leave activation, Wi-Fi setup and boot errors visible until first idle.
    lv_obj_add_flag(overlay_, LV_OBJ_FLAG_HIDDEN);
}

void StopwatchApps::ApplyTheme() {
    if (overlay_ == nullptr) {
        return;
    }
    auto* theme = static_cast<LvglTheme*>(display_->GetTheme());
    lv_obj_set_style_bg_color(overlay_, theme->background_color(), 0);
    lv_obj_set_style_bg_color(editor_panel_, theme->background_color(), 0);
    lv_obj_set_style_text_color(overlay_, theme->text_color(), 0);
    // The factory-style launcher stays black/white in either chat theme.
    lv_obj_set_style_bg_color(home_, lv_color_black(), 0);
    lv_obj_set_style_text_color(home_, lv_color_white(), 0);
    settings_.RefreshTheme();
    weather_.RefreshTheme();
}

void StopwatchApps::LogThemeState() const {
    const char* page = "unknown";
    switch (state_.page()) {
        case StopwatchAppState::Page::Home:
            page = "home";
            break;
        case StopwatchAppState::Page::Voice:
            page = "voice";
            break;
        case StopwatchAppState::Page::Badge:
            page = editing_ ? "badge-editor" : "badge";
            break;
        case StopwatchAppState::Page::Settings:
            page = "settings";
            break;
        case StopwatchAppState::Page::Weather:
            page = "weather";
            break;
        case StopwatchAppState::Page::Alarm:
            page = "alarm";
            break;
    }
    const int hidden =
        overlay_ == nullptr ? -1 : static_cast<int>(lv_obj_has_flag(overlay_, LV_OBJ_FLAG_HIDDEN));
    ESP_LOGI(kTag, "Theme foreground: page=%s overlay_hidden=%d boot_home_pending=%d", page, hidden,
             static_cast<int>(boot_home_pending_));
}

bool StopwatchApps::QueueWork(Work work) {
    if (work_queue_ == nullptr || xQueueSend(work_queue_, &work, 0) != pdTRUE) {
        ESP_LOGW(kTag, "Badge worker busy or unavailable");
        return false;
    }
    return true;
}

void StopwatchApps::Worker() {
    Work work;
    while (xQueueReceive(work_queue_, &work, portMAX_DELAY) == pdTRUE) {
        // A cancelled editor is stopped here, never in the main or LVGL tasks.
        if (!editor_requested_.load()) {
            editor_.Stop();
        }
        switch (work) {
            case Work::Initialize:
                store_.Initialize();
                RefreshBadge();
                break;
            case Work::Previous:
            case Work::Next:
                store_.StepSlot(work == Work::Previous ? -1 : 1);
                RefreshBadge();
                break;
            case Work::Load:
                RefreshBadge();
                break;
            case Work::StartEditor: {
                if (!editor_requested_.load()) {
                    break;
                }
                // Every entry is a new session, even if a reopen superseded a queued stop.
                editor_.Stop();
                if (!editor_requested_.load()) {
                    break;
                }
                auto url = editor_.Start();
                DisplayLockGuard lock(display_);
                if (lock && editing_) {
                    lv_label_set_text(
                        editor_url_,
                        url.empty() ? "Editor unavailable.\nConnect Wi-Fi and check badge storage."
                                    : url.c_str());
                }
                break;
            }
            case Work::StopEditor:
                // A subsequent StartEditor may already have superseded this request.
                if (!editor_requested_.load()) {
                    editor_.Stop();
                }
                break;
        }
    }
    vTaskDelete(nullptr);
}

void StopwatchApps::RefreshBadge() {
    // Filesystem access and JPEG decoding stay outside the LVGL/main event loops.
    auto image = store_.LoadActiveImage();
    const bool available = store_.available();
    DisplayLockGuard lock(display_);
    if (!lock) {
        return;
    }
    lv_image_set_src(badge_picture_, nullptr);
    if (badge_image_ != nullptr) {
        lv_image_cache_drop(badge_image_->image_dsc());
    }
    badge_image_ = std::move(image);
    if (badge_image_ != nullptr) {
        lv_image_set_src(badge_picture_, badge_image_->image_dsc());
        lv_obj_remove_flag(badge_picture_, LV_OBJ_FLAG_HIDDEN);
        lv_obj_add_flag(badge_hint_, LV_OBJ_FLAG_HIDDEN);
    } else {
        lv_obj_add_flag(badge_picture_, LV_OBJ_FLAG_HIDDEN);
        lv_label_set_text(
            badge_hint_,
            available ? "No badge image\n\nHold screen or double B to edit\nA/B: previous / next"
                      : "Badge storage unavailable\nCheck the apps partition table");
        lv_obj_remove_flag(badge_hint_, LV_OBJ_FLAG_HIDDEN);
    }
}

void StopwatchApps::RenderHome() {
    lv_anim_delete(this, HomeAnimation);
    home_animating_ = false;
    for (int position = 0; position < StopwatchAppState::kAppCount; ++position) {
        const int i = StopwatchAppState::kHomeOrder[position];
        const bool selected = i == state_.selection();
        lv_obj_set_pos(home_icons_[i], 133, 118);
        if (selected) {
            lv_obj_remove_flag(home_icons_[i], LV_OBJ_FLAG_HIDDEN);
        } else {
            lv_obj_add_flag(home_icons_[i], LV_OBJ_FLAG_HIDDEN);
        }
        const int size = selected ? 14 : 8;
        lv_obj_set_size(home_dots_[i], size, size);
        const int center = 233 + 16 * position - 8 * (StopwatchAppState::kAppCount - 1);
        lv_obj_set_pos(home_dots_[i], center - size / 2, 433 - size / 2);
        lv_obj_set_style_bg_opa(home_dots_[i], selected ? LV_OPA_COVER : LV_OPA_50, 0);
    }
    lv_label_set_text(home_name_, kNames[state_.selection()]);
    lv_obj_set_style_opa(home_name_, LV_OPA_COVER, 0);
}

void StopwatchApps::AnimateHome(int direction) {
    // Ignore repeat input in flight instead of building an unbounded animation queue.
    if (home_animating_) {
        return;
    }
    home_from_ = state_.selection();
    state_.Step(direction);
    RenderHome();
    home_direction_ = direction < 0 ? -1 : 1;
    home_animating_ = true;
    lv_obj_remove_flag(home_icons_[home_from_], LV_OBJ_FLAG_HIDDEN);
    lv_obj_remove_flag(home_icons_[state_.selection()], LV_OBJ_FLAG_HIDDEN);
    HomeAnimation(this, 0);
    lv_anim_t animation;
    lv_anim_init(&animation);
    lv_anim_set_var(&animation, this);
    lv_anim_set_exec_cb(&animation, HomeAnimation);
    lv_anim_set_values(&animation, 0, 466);
    lv_anim_set_duration(&animation, 360);
    lv_anim_set_path_cb(&animation, lv_anim_path_ease_out);
    lv_anim_start(&animation);
}

void StopwatchApps::HomeAnimation(void* user_data, int32_t progress) {
    auto* self = static_cast<StopwatchApps*>(user_data);
    if (!self->home_animating_) {
        return;
    }
    progress = std::clamp<int32_t>(progress, 0, 466);
    lv_obj_set_x(self->home_icons_[self->home_from_], 133 - self->home_direction_ * progress);
    lv_obj_set_x(self->home_icons_[self->state_.selection()],
                 133 + self->home_direction_ * (466 - progress));
    const int nearest = progress < 233 ? self->home_from_ : self->state_.selection();
    const int distance = std::min<int>(progress, 466 - progress);
    int opacity = 0;
    if (distance <= 70) {
        opacity = 255;
    } else if (distance < 150) {
        opacity = 255 * (150 - distance) / 80;
    }
    lv_label_set_text(self->home_name_, kNames[nearest]);
    lv_obj_set_style_opa(self->home_name_, opacity, 0);
    if (progress == 466) {
        self->navigation_tick_ = lv_tick_get();
        self->RenderHome();
    }
}

void StopwatchApps::UpdateHomeClock() {
    const time_t now = time(nullptr);
    const time_t minute = now >= 1577836800 ? now / 60 : -2;
    if (minute == home_clock_minute_) {
        return;
    }
    home_clock_minute_ = minute;
    char text[6] = "--:--";
    struct tm local_time;
    if (minute >= 0 && localtime_r(&now, &local_time) != nullptr) {
        strftime(text, sizeof(text), "%H:%M", &local_time);
    }
    for (int i = 0; i < 5; ++i) {
        const char digit[] = {text[i], '\0'};
        lv_label_set_text(lv_obj_get_child(home_clock_, i), digit);
    }
}

void StopwatchApps::RenderPage() {
    const auto page = state_.page();
    if (home_animating_ && page != StopwatchAppState::Page::Home) {
        RenderHome();
    }
    const bool voice = page == StopwatchAppState::Page::Voice;
    lv_obj_add_flag(settings_.root(), LV_OBJ_FLAG_HIDDEN);
    lv_obj_add_flag(weather_.root(), LV_OBJ_FLAG_HIDDEN);
    lv_obj_add_flag(alarm_.root(), LV_OBJ_FLAG_HIDDEN);
    if (voice) {
        lv_obj_add_flag(overlay_, LV_OBJ_FLAG_HIDDEN);
        return;
    }
    lv_obj_remove_flag(overlay_, LV_OBJ_FLAG_HIDDEN);
    lv_obj_move_foreground(overlay_);
    lv_obj_add_flag(home_, LV_OBJ_FLAG_HIDDEN);
    lv_obj_add_flag(badge_, LV_OBJ_FLAG_HIDDEN);
    lv_obj_add_flag(editor_panel_, LV_OBJ_FLAG_HIDDEN);
    if (page == StopwatchAppState::Page::Home) {
        RenderHome();
        lv_obj_remove_flag(home_, LV_OBJ_FLAG_HIDDEN);
    } else if (page == StopwatchAppState::Page::Badge) {
        lv_obj_remove_flag(editing_ ? editor_panel_ : badge_, LV_OBJ_FLAG_HIDDEN);
    } else if (page == StopwatchAppState::Page::Settings) {
        lv_obj_remove_flag(settings_.root(), LV_OBJ_FLAG_HIDDEN);
    } else if (page == StopwatchAppState::Page::Weather) {
        lv_obj_remove_flag(weather_.root(), LV_OBJ_FLAG_HIDDEN);
    } else if (page == StopwatchAppState::Page::Alarm) {
        lv_obj_remove_flag(alarm_.root(), LV_OBJ_FLAG_HIDDEN);
    }
}

void StopwatchApps::SetWakeWordEnabled(bool enabled) {
    if (wake_policy_) {
        wake_policy_(enabled);
    }
}

void StopwatchApps::ShowHome() {
    if (overlay_ == nullptr) {
        return;
    }
    boot_home_pending_ = false;
    ++navigation_generation_;
    navigation_target_ = -1;
    settings_.Close();
    weather_.Close();
    alarm_.Close();
    editing_ = false;
    editor_requested_.store(false);
    QueueWork(Work::StopEditor);
    state_.ShowHome();
    SetWakeWordEnabled(false);
    RenderPage();
}

bool StopwatchApps::CanNavigate() const {
    return overlay_ != nullptr && !boot_home_pending_ &&
           last_voice_state_ != kDeviceStateWifiConfiguring &&
           last_voice_state_ != kDeviceStateUpgrading && last_voice_state_ != kDeviceStateFatalError;
}

bool StopwatchApps::ActivateVoiceByWakeWord() {
    if (!CanNavigate()) {
        return false;
    }
    if (state_.page() == StopwatchAppState::Page::Voice) {
        ++navigation_generation_;
        navigation_target_ = -1;
        SetWakeWordEnabled(true);
        return true;
    }
    ShowHome();
    state_.ShowVoice();
    SetWakeWordEnabled(true);
    RenderPage();
    return true;
}

bool StopwatchApps::ActivateVoiceForAlarm() {
    if (!CanNavigate()) {
        return false;
    }
    ShowHome();
    state_.ShowVoice();
    SetWakeWordEnabled(true);
    RenderPage();
    return true;
}

StopwatchApps::NavigationResult StopwatchApps::NavigateToApp(int index) {
    // Remote navigation can leave voice, never activate it from a local page.
    if (index <= 0 || index >= StopwatchAppState::kAppCount || !CanNavigate()) {
        return NavigationResult::Unavailable;
    }
    if (state_.page() != StopwatchAppState::Page::Home && state_.selection() == index) {
        return NavigationResult::AlreadyOpen;
    }
    if (navigation_target_ >= 0) {
        return NavigationResult::Busy;
    }
    ShowHome();
    navigation_target_ = index;
    navigation_tick_ = lv_tick_get();
    return NavigationResult::Accepted;
}

void StopwatchApps::AdvanceNavigation() {
    if (navigation_target_ < 0) {
        return;
    }
    if (!CanNavigate() || state_.page() != StopwatchAppState::Page::Home) {
        navigation_target_ = -1;
        return;
    }
    if (home_animating_) {
        navigation_tick_ = lv_tick_get();
        return;
    }
    // Give Home and the selected tile time to appear before advancing/opening.
    if (lv_tick_elaps(navigation_tick_) < 200) {
        return;
    }
    navigation_tick_ = lv_tick_get();
    if (state_.selection() == navigation_target_) {
        navigation_target_ = -1;
        OpenSelected();
        return;
    }
    int position = 0, target = 0;
    for (int i = 0; i < StopwatchAppState::kAppCount; ++i) {
        if (StopwatchAppState::kHomeOrder[i] == state_.selection()) {
            position = i;
        }
        if (StopwatchAppState::kHomeOrder[i] == navigation_target_) {
            target = i;
        }
    }
    const int forward = (target - position + StopwatchAppState::kAppCount) %
                        StopwatchAppState::kAppCount;
    AnimateHome(forward <= StopwatchAppState::kAppCount / 2 ? 1 : -1);
}

void StopwatchApps::OpenSelected() {
    if (state_.page() != StopwatchAppState::Page::Home || home_animating_ || navigation_target_ >= 0) {
        return;
    }
    boot_home_pending_ = false;
    ++navigation_generation_;
    state_.OpenSelected();
    if (state_.page() == StopwatchAppState::Page::Settings) {
        settings_.Open();
    } else if (state_.page() == StopwatchAppState::Page::Weather) {
        weather_.Open();
    } else if (state_.page() == StopwatchAppState::Page::Alarm) {
        alarm_.Open();
    }
    SetWakeWordEnabled(state_.page() == StopwatchAppState::Page::Voice);
    RenderPage();
}

void StopwatchApps::Step(int direction) {
    if (state_.page() == StopwatchAppState::Page::Home && navigation_target_ < 0) {
        AnimateHome(direction);
    } else if (state_.page() == StopwatchAppState::Page::Badge && !editing_) {
        QueueWork(direction < 0 ? Work::Previous : Work::Next);
    }
}

bool StopwatchApps::HandleButton(int button) {
    if (overlay_ == nullptr || boot_home_pending_) {
        return false;
    }
    switch (state_.page()) {
        case StopwatchAppState::Page::Voice:
            return false;
        case StopwatchAppState::Page::Home:
            if (button == 2) {
                OpenSelected();
            } else {
                Step(button == 0 ? -1 : 1);
            }
            break;
        case StopwatchAppState::Page::Settings:
            settings_.HandleButton(button);
            break;
        case StopwatchAppState::Page::Weather:
            weather_.HandleButton(button);
            break;
        case StopwatchAppState::Page::Alarm:
            alarm_.HandleButton(button);
            break;
        case StopwatchAppState::Page::Badge:
            if (editing_) {
                StopEditor();
            } else if (button == 2) {
                StartEditor();
            } else {
                Step(button == 0 ? -1 : 1);
            }
            break;
    }
    return true;
}

void StopwatchApps::ObserveVoiceState(DeviceState state, bool allow_boot_home) {
    if (overlay_ == nullptr) {
        return;
    }
    // Idle alone also occurs before startup errors; wait for the normal idle UI.
    if (boot_home_pending_ && allow_boot_home && state == kDeviceStateIdle) {
        ShowHome();
    }
    if (state == last_voice_state_) {
        return;
    }
    last_voice_state_ = state;
    settings_.ObserveVoiceState(state);
    // Normal conversation states never replace a deliberately selected local page.
    // System setup/errors remain visible without implicitly enabling wake detection.
    if (state == kDeviceStateWifiConfiguring || state == kDeviceStateUpgrading ||
        state == kDeviceStateFatalError) {
        ++navigation_generation_;
        navigation_target_ = -1;
        settings_.Close();
        weather_.Close();
        alarm_.Close();
        editing_ = false;
        editor_requested_.store(false);
        QueueWork(Work::StopEditor);
        state_.ShowVoice();
        RenderPage();
    }
}

void StopwatchApps::StartEditor() {
    if (state_.page() != StopwatchAppState::Page::Badge || editing_) {
        return;
    }
    editing_ = true;
    editor_requested_.store(true);
    lv_label_set_text(editor_url_, "Starting editor...");
    if (!QueueWork(Work::StartEditor)) {
        editor_requested_.store(false);
        lv_label_set_text(editor_url_, "Badge worker busy.\nTap Done and try again.");
    }
    RenderPage();
}

void StopwatchApps::StopEditor() {
    editing_ = false;
    editor_requested_.store(false);
    QueueWork(Work::StopEditor);
    RenderPage();
}

void StopwatchApps::Tick(lv_timer_t* timer) {
    auto* self = static_cast<StopwatchApps*>(lv_timer_get_user_data(timer));
    self->UpdateHomeClock();
    self->AdvanceNavigation();
    if (self->badge_changed_.exchange(false) && !self->QueueWork(Work::Load)) {
        self->badge_changed_.store(true);
    }
}

void StopwatchApps::Gesture(lv_event_t* event) {
    auto* self = static_cast<StopwatchApps*>(lv_event_get_user_data(event));
    auto* input = lv_indev_active();
    if (input == nullptr) {
        return;
    }
    const auto direction = lv_indev_get_gesture_dir(input);
    if (direction == LV_DIR_LEFT || direction == LV_DIR_RIGHT) {
        self->Step(direction == LV_DIR_LEFT ? 1 : -1);
        // Consume the release so a swipe across the central tile cannot open it.
        lv_indev_wait_release(input);
    }
}

#endif
