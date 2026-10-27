// Settings layout adapted from M5Stack M5StopWatch-UserDemo (MIT).
// SPDX-FileCopyrightText: 2026 M5Stack Technology CO LTD
// SPDX-License-Identifier: MIT

#include "sdkconfig.h"

#if CONFIG_STOPWATCH_LOCAL_APPS

#include "stopwatch_settings.h"

#include <esp_app_desc.h>
#include <wifi_manager.h>

#include <algorithm>
#include <cmath>
#include <utility>

#include "application.h"
#include "audio_codec.h"
#include "board.h"
#include "lvgl_theme.h"
#include "stopwatch_weather_service.h"

LV_FONT_DECLARE(font_noto_sans_basic_20_4);
LV_FONT_DECLARE(font_noto_sans_basic_30_4);
extern "C" {
LV_FONT_DECLARE(MontserratSemiBold26);
LV_FONT_DECLARE(CommissionerMedium108);
LV_FONT_DECLARE(font_stopwatch_weather_20);
}

namespace {
constexpr uint32_t kGreen = 0x4AD78C;
constexpr uint32_t kGrey = 0x4C4C4C;
constexpr int kMenuOrder[] = {0, 1, 2, 6, 3, 4, 5};
constexpr const char* kKeyboardPages[] = {"ABCDEFGHIJKL", "MNOPQRSTUVWX", "YZ0123456789"};

bool PrintableAscii(const std::string& value) {
    return !value.empty() && std::all_of(value.begin(), value.end(), [](unsigned char c) {
        return c >= 32 && c <= 126;
    });
}

std::string CityName(const stopwatch_weather::Location& location) {
    // display_name is the service's glyph-safe alias / ASCII fallback. Never use
    // the arbitrary provider-native city name as the only on-screen identity.
    if (!location.display_name.empty()) {
        return location.display_name;
    }
    return PrintableAscii(location.id) ? location.id : "Not located";
}

std::string CityIdentity(const stopwatch_weather::Location& location) {
    std::string identity;
    if (PrintableAscii(location.region)) {
        identity = location.region;
    }
    const auto country = PrintableAscii(location.country) ? location.country : location.country_code;
    if (PrintableAscii(country)) {
        identity += (identity.empty() ? "" : " / ") + country;
    }
    // Keep the stable identity even for same-named cities in the same region.
    if (PrintableAscii(location.id)) {
        identity += (identity.empty() ? "" : " / ") + location.id;
    }
    return identity;
}

bool SameCityChoice(const stopwatch_weather::Location& current,
                    const stopwatch_weather::Location& builtin) {
    // UI-only alias deduplication, matching the codec's controlled city aliases.
    // Keep the exact effective position as the selectable first row.
    return stopwatch_weather::SameLocation(current, builtin) ||
           (current.country_code == builtin.country_code &&
            (current.city == builtin.city || current.city == builtin.display_name) &&
            std::abs(current.latitude - builtin.latitude) < 0.5 &&
            std::abs(current.longitude - builtin.longitude) < 0.5);
}

bool EqualLocation(const stopwatch_weather::Location& left,
                   const stopwatch_weather::Location& right) {
    return left.id == right.id && left.city == right.city &&
           left.display_name == right.display_name && left.region == right.region &&
           left.country == right.country && left.country_code == right.country_code &&
           left.latitude == right.latitude && left.longitude == right.longitude;
}
}  // namespace

StopwatchSettings::StopwatchSettings(Display* display, std::function<void()> home,
                                     std::function<bool()> configure_wifi,
                                     StopwatchWeatherService* weather_service)
    : display_(display), home_(std::move(home)), configure_wifi_(std::move(configure_wifi)),
      weather_service_(weather_service) {}

lv_obj_t* StopwatchSettings::Panel(lv_obj_t* parent) {
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

lv_obj_t* StopwatchSettings::Label(lv_obj_t* parent, const char* text, int width, int y,
                                   const lv_font_t* font) {
    auto* label = lv_label_create(parent);
    lv_obj_set_width(label, width);
    lv_obj_set_style_text_font(label, font, 0);
    lv_obj_set_style_text_color(label, lv_color_white(), 0);
    lv_obj_set_style_text_align(label, LV_TEXT_ALIGN_CENTER, 0);
    lv_label_set_text(label, text);
    lv_obj_align(label, LV_ALIGN_TOP_MID, 0, y);
    lv_obj_remove_flag(label, LV_OBJ_FLAG_CLICKABLE);
    return label;
}

lv_obj_t* StopwatchSettings::Button(lv_obj_t* parent, const char* text, int y, int height,
                                    lv_event_cb_t callback, bool primary) {
    auto* button = lv_button_create(parent);
    lv_obj_remove_style_all(button);
    lv_obj_set_size(button, 374, height);
    lv_obj_align(button, LV_ALIGN_TOP_MID, 0, y);
    lv_obj_set_style_radius(button, 60, 0);
    lv_obj_set_style_bg_color(button, lv_color_hex(primary ? kGreen : kGrey), 0);
    lv_obj_set_style_bg_opa(button, LV_OPA_COVER, 0);
    lv_obj_set_style_bg_color(button, lv_color_hex(primary ? 0x53BD65 : 0x696969),
                              LV_STATE_PRESSED);
    lv_obj_set_style_border_color(button, lv_color_hex(kGreen), LV_STATE_FOCUSED);
    lv_obj_set_style_border_width(button, 3, LV_STATE_FOCUSED);
    lv_obj_remove_flag(button, LV_OBJ_FLAG_SCROLLABLE);
    auto* label = Label(button, text, 294, 0, &font_noto_sans_basic_30_4);
    lv_label_set_long_mode(label, LV_LABEL_LONG_SCROLL_CIRCULAR);
    lv_obj_center(label);
    if (primary) {
        lv_obj_set_style_text_color(label, lv_color_hex(0x0F5831), 0);
    }
    lv_obj_add_event_cb(button, callback, LV_EVENT_CLICKED, this);
    return button;
}

void StopwatchSettings::Setup(lv_obj_t* parent) {
    if (root_ != nullptr) {
        return;
    }
    root_ = Panel(parent);
    BuildMenu();
    detail_ = Panel(root_);
    lv_obj_add_flag(detail_, LV_OBJ_FLAG_HIDDEN);
    lv_obj_add_flag(root_, LV_OBJ_FLAG_HIDDEN);
    timer_ = lv_timer_create(Tick, 1000, this);
    lv_timer_pause(timer_);
}

bool StopwatchSettings::IsCurrentEvent(lv_event_t* event) const {
    if (!active_) {
        return false;
    }
    // LVGL can deliver a press-locked release after its page was hidden.
    auto* object = static_cast<lv_obj_t*>(lv_event_get_target(event));
    auto* page = page_ == Page::Menu ? menu_ : detail_;
    bool on_page = false;
    for (; object != nullptr; object = lv_obj_get_parent(object)) {
        if (lv_obj_has_flag(object, LV_OBJ_FLAG_HIDDEN)) {
            return false;
        }
        on_page = on_page || object == page;
        if (object == root_) {
            return on_page;
        }
    }
    return false;
}

void StopwatchSettings::BuildMenu() {
    menu_ = Panel(root_);
    lv_obj_add_flag(menu_, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_set_scroll_dir(menu_, LV_DIR_VER);
    lv_obj_set_scrollbar_mode(menu_, LV_SCROLLBAR_MODE_ACTIVE);
    lv_obj_set_style_pad_bottom(menu_, 72, 0);
    int y = 36;
    auto heading = [&](const char* text) {
        Label(menu_, text, 330, y, &MontserratSemiBold26);
        y += 52;
    };
    auto item = [&](int index, const char* text) {
        menu_items_[index] = Button(menu_, text, y, 119, MenuClicked);
        y += 140;
    };
    heading("Device");
    item(0, "Brightness");
    item(1, "Volume");
    item(2, "Wi-Fi");
    item(kLocationItem, "Weather location");
    y += 5;
    heading("XiaoZhi");
    item(3, "Theme");
    theme_summary_ = Label(menu_items_[3], "", 294, 78, &font_noto_sans_basic_20_4);
    lv_obj_align(lv_obj_get_child(menu_items_[3], 0), LV_ALIGN_TOP_MID, 0, 24);
    y += 5;
    heading("Firmware");
    item(4, "Version");
    item(5, "Home");
}

void StopwatchSettings::Open() {
    if (root_ == nullptr) {
        return;
    }
    ++generation_;
    CancelCitySearch();
    city_commit_pending_ = false;
    city_edited_ = false;
    active_ = true;
    commit_pending_ = false;
    selection_ = 0;
    ShowMenu();
    lv_obj_scroll_to_y(menu_, 0, LV_ANIM_OFF);
    lv_timer_resume(timer_);
}

void StopwatchSettings::Close() {
    if (!active_) {
        return;
    }
    ++generation_;
    active_ = false;
    commit_pending_ = false;
    city_commit_pending_ = false;
    CancelCitySearch();
    WaitForPointerRelease();
    lv_timer_pause(timer_);
    lv_obj_add_flag(root_, LV_OBJ_FLAG_HIDDEN);
}

void StopwatchSettings::ObserveVoiceState(DeviceState state) {
    if (state == kDeviceStateWifiConfiguring || state == kDeviceStateUpgrading ||
        state == kDeviceStateFatalError) {
        wifi_config_pending_ = false;
    }
}

void StopwatchSettings::ShowMenu() {
    if (!active_) {
        return;
    }
    ++generation_;
    commit_pending_ = false;
    city_commit_pending_ = false;
    CancelCitySearch();
    WaitForPointerRelease();
    page_ = Page::Menu;
    lv_obj_add_flag(detail_, LV_OBJ_FLAG_HIDDEN);
    lv_obj_remove_flag(menu_, LV_OBJ_FLAG_HIDDEN);
    SelectMenu(selection_, false);
    RefreshTheme();
}

void StopwatchSettings::SelectMenu(int index, bool scroll) {
    selection_ = (index + kMenuItems) % kMenuItems;
    for (int i = 0; i < kMenuItems; ++i) {
        if (i == selection_) {
            lv_obj_add_state(menu_items_[i], LV_STATE_FOCUSED);
        } else {
            lv_obj_remove_state(menu_items_[i], LV_STATE_FOCUSED);
        }
    }
    if (scroll) {
        lv_obj_scroll_to_view(menu_items_[selection_], LV_ANIM_OFF);
    }
}

void StopwatchSettings::StepMenu(int direction) {
    for (int i = 0; i < kMenuItems; ++i) {
        if (kMenuOrder[i] == selection_) {
            SelectMenu(kMenuOrder[(i + direction + kMenuItems) % kMenuItems], true);
            return;
        }
    }
}

void StopwatchSettings::MenuClicked(lv_event_t* event) {
    auto* self = static_cast<StopwatchSettings*>(lv_event_get_user_data(event));
    if (!self->IsCurrentEvent(event)) {
        return;
    }
    auto* target = static_cast<lv_obj_t*>(lv_event_get_target(event));
    for (int i = 0; i < kMenuItems; ++i) {
        if (self->menu_items_[i] == target) {
            self->SelectMenu(i, false);
            self->ActivateMenu();
            return;
        }
    }
}

void StopwatchSettings::ActivateMenu() {
    static constexpr Page pages[] = {Page::Brightness, Page::Volume, Page::Wifi, Page::Theme,
                                     Page::About};
    if (selection_ == kHomeItem) {
        Defer([this]() {
            if (home_) {
                home_();
            }
        });
    } else if (selection_ == kLocationItem) {
        CancelCitySearch();
        city_edited_ = false;
        city_conflict_ = false;
        city_query_.clear();
        city_keyboard_page_ = 0;
        ShowLocation();
    } else {
        ShowDetail(pages[selection_]);
    }
}

void StopwatchSettings::ClearDetailPointers() {
    value_label_ = nullptr;
    slider_ = nullptr;
    theme_options_[0] = theme_options_[1] = nullptr;
    wifi_status_ = nullptr;
    primary_ = nullptr;
    city_mode_label_ = nullptr;
    city_name_label_ = nullptr;
    city_identity_label_ = nullptr;
    city_status_label_ = nullptr;
    city_query_label_ = nullptr;
    city_confirm_ = nullptr;
    city_list_view_ = nullptr;
    city_list_count_ = 0;
    city_controls_ = {};
    city_control_count_ = 0;
}

void StopwatchSettings::ShowDetail(Page page) {
    ++generation_;
    commit_pending_ = false;
    city_commit_pending_ = false;
    CancelCitySearch();
    WaitForPointerRelease();
    page_ = page;
    ClearDetailPointers();
    lv_obj_clean(detail_);
    lv_obj_add_flag(menu_, LV_OBJ_FLAG_HIDDEN);
    lv_obj_remove_flag(detail_, LV_OBJ_FLAG_HIDDEN);

    // Staged values are committed once on OK, not on every slider drag/NVS write.
    if (page == Page::Brightness || page == Page::Volume) {
        auto& board = Board::GetInstance();
        auto* backlight = page == Page::Brightness ? board.GetBacklight() : nullptr;
        auto* codec = page == Page::Volume ? board.GetAudioCodec() : nullptr;
        const bool available = backlight != nullptr || codec != nullptr;
        initial_value_ = backlight != nullptr ? backlight->brightness()
                         : codec != nullptr   ? codec->output_volume()
                                              : 0;
        value_ = std::clamp(initial_value_, page == Page::Brightness ? 10 : 0, 100);
        Label(detail_, page == Page::Brightness ? "Brightness" : "Volume", 300, 28,
              &MontserratSemiBold26);
        value_label_ = Label(detail_, "", 300, 78, &CommissionerMedium108);
        slider_ = lv_slider_create(detail_);
        lv_obj_set_size(slider_, 374, 24);
        lv_obj_align(slider_, LV_ALIGN_CENTER, 0, -8);
        lv_slider_set_range(slider_, page == Page::Brightness ? 10 : 0, 100);
        lv_obj_set_style_bg_color(slider_, lv_color_hex(0x343434), LV_PART_MAIN);
        lv_obj_set_style_bg_opa(slider_, LV_OPA_COVER, LV_PART_MAIN);
        lv_obj_set_style_radius(slider_, LV_RADIUS_CIRCLE, LV_PART_MAIN);
        lv_obj_set_style_bg_color(slider_, lv_color_hex(kGreen), LV_PART_INDICATOR);
        lv_obj_set_style_bg_opa(slider_, LV_OPA_COVER, LV_PART_INDICATOR);
        lv_obj_set_style_radius(slider_, LV_RADIUS_CIRCLE, LV_PART_INDICATOR);
        lv_obj_set_style_bg_color(slider_, lv_color_white(), LV_PART_KNOB);
        lv_obj_set_style_bg_opa(slider_, LV_OPA_COVER, LV_PART_KNOB);
        lv_obj_set_style_border_width(slider_, 0, LV_PART_KNOB);
        lv_obj_set_style_radius(slider_, LV_RADIUS_CIRCLE, LV_PART_KNOB);
        lv_obj_set_style_pad_all(slider_, 10, LV_PART_KNOB);
        lv_obj_remove_flag(slider_, LV_OBJ_FLAG_GESTURE_BUBBLE);
        SetValue(value_);
        lv_obj_add_event_cb(
            slider_,
            [](lv_event_t* event) {
                auto* self = static_cast<StopwatchSettings*>(lv_event_get_user_data(event));
                if (self->IsCurrentEvent(event) && !self->commit_pending_) {
                    self->SetValue(lv_slider_get_value(self->slider_));
                }
            },
            LV_EVENT_VALUE_CHANGED, this);
        Label(detail_, available ? "A / B: adjust    Double B: save" : "Not available", 330, 273,
              &font_noto_sans_basic_20_4);
        primary_ = Button(
            detail_, "OK", 326, 106,
            [](lv_event_t* event) {
                auto* self = static_cast<StopwatchSettings*>(lv_event_get_user_data(event));
                if (self->IsCurrentEvent(event)) {
                    self->Commit();
                }
            },
            true);
        if (!available) {
            lv_obj_add_state(slider_, LV_STATE_DISABLED);
            lv_obj_add_state(primary_, LV_STATE_DISABLED);
        }
    } else if (page == Page::Theme) {
        auto* theme = display_->GetTheme();
        initial_value_ = theme != nullptr && theme->name() == "dark" ? 1 : 0;
        value_ = initial_value_;
        Label(detail_, "XiaoZhi Theme", 330, 36, &MontserratSemiBold26);
        theme_options_[0] = Button(detail_, "Light", 102, 80, [](lv_event_t* event) {
            auto* self = static_cast<StopwatchSettings*>(lv_event_get_user_data(event));
            if (self->IsCurrentEvent(event)) {
                self->SetValue(0);
            }
        });
        theme_options_[1] = Button(detail_, "Dark", 200, 80, [](lv_event_t* event) {
            auto* self = static_cast<StopwatchSettings*>(lv_event_get_user_data(event));
            if (self->IsCurrentEvent(event)) {
                self->SetValue(1);
            }
        });
        RenderTheme();
        primary_ = Button(
            detail_, "OK", 326, 106,
            [](lv_event_t* event) {
                auto* self = static_cast<StopwatchSettings*>(lv_event_get_user_data(event));
                if (self->IsCurrentEvent(event)) {
                    self->Commit();
                }
            },
            true);
    } else if (page == Page::Wifi) {
        Label(detail_, "Wi-Fi", 300, 36, &MontserratSemiBold26);
        wifi_status_ = Label(detail_, "", 330, 96, &font_noto_sans_basic_20_4);
        lv_obj_set_height(wifi_status_, 150);
        lv_label_set_long_mode(wifi_status_, LV_LABEL_LONG_WRAP);
        UpdateWifiStatus();
        primary_ = Button(
            detail_, "Configure", 258, 74,
            [](lv_event_t* event) {
                auto* self = static_cast<StopwatchSettings*>(lv_event_get_user_data(event));
                if (self->IsCurrentEvent(event)) {
                    self->RequestWifiConfig();
                }
            },
            true);
        Button(detail_, "Back", 350, 74, [](lv_event_t* event) {
            auto* self = static_cast<StopwatchSettings*>(lv_event_get_user_data(event));
            if (self->IsCurrentEvent(event)) {
                self->ShowMenu();
            }
        });
        if (!configure_wifi_) {
            lv_obj_add_state(primary_, LV_STATE_DISABLED);
        }
    } else if (page == Page::About) {
        Label(detail_, "Firmware", 330, 36, &MontserratSemiBold26);
        const auto* info = esp_app_get_description();
        Label(detail_, "XiaoZhi / M5Stack StopWatch", 330, 120, &font_noto_sans_basic_20_4);
        auto* version = Label(detail_, info->version, 330, 173, &font_noto_sans_basic_30_4);
        lv_label_set_long_mode(version, LV_LABEL_LONG_WRAP);
        Label(detail_, info->idf_ver, 330, 242, &font_noto_sans_basic_20_4);
        Button(
            detail_, "OK", 326, 106,
            [](lv_event_t* event) {
                auto* self = static_cast<StopwatchSettings*>(lv_event_get_user_data(event));
                if (self->IsCurrentEvent(event)) {
                    self->ShowMenu();
                }
            },
            true);
    }
}

void StopwatchSettings::SetValue(int value) {
    if (!active_ || commit_pending_ ||
        (page_ != Page::Brightness && page_ != Page::Volume && page_ != Page::Theme)) {
        return;
    }
    if (page_ == Page::Theme) {
        value_ = std::clamp(value, 0, 1);
        RenderTheme();
        return;
    }
    const int minimum = page_ == Page::Brightness ? 10 : 0;
    const int step = page_ == Page::Volume ? 5 : 1;
    value_ = std::clamp(((std::clamp(value, minimum, 100) + step / 2) / step) * step, minimum, 100);
    lv_label_set_text_fmt(value_label_, "%d", value_);
    lv_slider_set_value(slider_, value_, LV_ANIM_OFF);
}

void StopwatchSettings::RenderTheme() {
    for (int i = 0; i < 2; ++i) {
        if (theme_options_[i] != nullptr) {
            lv_obj_set_style_bg_color(theme_options_[i], lv_color_hex(i == value_ ? kGreen : kGrey),
                                      0);
            lv_obj_set_style_text_color(lv_obj_get_child(theme_options_[i], 0),
                                        i == value_ ? lv_color_hex(0x0F5831) : lv_color_white(), 0);
        }
    }
}

void StopwatchSettings::RefreshTheme() {
    if (root_ == nullptr) {
        return;
    }
    auto* theme = display_->GetTheme();
    lv_label_set_text(theme_summary_,
                      theme != nullptr && theme->name() == "dark" ? "Dark" : "Light");
    // Reconcile an external/MCP change only if the user has not edited a staged choice.
    if (active_ && page_ == Page::Theme && value_ == initial_value_ && !commit_pending_) {
        initial_value_ = theme != nullptr && theme->name() == "dark" ? 1 : 0;
        value_ = initial_value_;
        RenderTheme();
    }
}

void StopwatchSettings::UpdateWifiStatus() {
    auto& wifi = WifiManager::GetInstance();
    std::string status;
    if (wifi.IsConfigMode()) {
        status = "Setup hotspot\n" + wifi.GetApSsid() + "\n" + wifi.GetApWebUrl();
    } else if (wifi.IsConnected()) {
        status = "Connected\n" + wifi.GetSsid() + "\n" + wifi.GetIpAddress();
    } else {
        status = "Not connected\nUse Configure to set up Wi-Fi";
    }
    lv_label_set_text(wifi_status_, status.c_str());
}

void StopwatchSettings::Defer(std::function<void()> action) {
    if (!active_ || commit_pending_) {
        return;
    }
    commit_pending_ = true;
    const unsigned generation = generation_;
    // Release the LVGL callback before any NVS, board, or application mutation.
    Application::GetInstance().Schedule([this, generation, action = std::move(action)]() {
        DisplayLockGuard lock(display_);
        if (!lock) {
            return;
        }
        if (!active_ || generation != generation_) {
            return;
        }
        commit_pending_ = false;
        action();
    });
}

void StopwatchSettings::Commit() {
    if (!active_ || (page_ != Page::Brightness && page_ != Page::Volume && page_ != Page::Theme)) {
        return;
    }
    const Page page = page_;
    const int value = value_;
    Defer([this, page, value]() {
        auto& board = Board::GetInstance();
        if (page == Page::Brightness) {
            if (auto* backlight = board.GetBacklight()) {
                backlight->SetBrightness(value, true);
            }
        } else if (page == Page::Volume) {
            if (auto* codec = board.GetAudioCodec();
                codec != nullptr && codec->output_volume() != value) {
                codec->SetOutputVolume(value);
            }
        } else if (page == Page::Theme) {
            auto* theme = LvglThemeManager::GetInstance().GetTheme(value == 0 ? "light" : "dark");
            if (theme != nullptr && display_->GetTheme() != theme) {
                display_->SetTheme(theme);
            }
        }
        ShowMenu();
    });
}

void StopwatchSettings::RequestWifiConfig() {
    if (!active_ || page_ != Page::Wifi || !configure_wifi_ || wifi_config_pending_) {
        return;
    }
    Defer([this]() {
        const auto state = Application::GetInstance().GetDeviceState();
        if (wifi_config_pending_ || WifiManager::GetInstance().IsConfigMode() ||
            (state != kDeviceStateStarting && state != kDeviceStateIdle &&
             state != kDeviceStateListening && state != kDeviceStateSpeaking &&
             state != kDeviceStateNotifying)) {
            return;
        }
        // WifiBoard returns before its delayed handoff; navigation must not rearm it.
        wifi_config_pending_ = true;
        if (!configure_wifi_()) {
            wifi_config_pending_ = false;
        }
    });
}

void StopwatchSettings::HandleButton(int button) {
    if (!active_ || commit_pending_) {
        return;
    }
    if (IsCityPage()) {
        const unsigned generation = generation_;
        PollCity();
        if (generation != generation_) {
            return;
        }
        if (button == 2) {
            if (city_control_count_ != 0) {
                const auto control = city_controls_[city_focus_];
                if (!lv_obj_has_state(control.object, LV_STATE_DISABLED)) {
                    ActivateCity(control.action, control.argument);
                }
            }
        } else {
            FocusCity(city_focus_ + (button == 0 ? -1 : 1), true);
        }
    } else if (page_ == Page::Menu) {
        if (button == 2) {
            ActivateMenu();
        } else {
            StepMenu(button == 0 ? -1 : 1);
        }
    } else if (page_ == Page::Wifi) {
        if (button == 2) {
            RequestWifiConfig();
        } else {
            ShowMenu();
        }
    } else if (page_ == Page::About) {
        ShowMenu();
    } else if (button == 2) {
        Commit();
    } else {
        const int step = page_ == Page::Volume ? 5 : 1;
        SetValue(value_ + (button == 0 ? -step : step));
    }
}

bool StopwatchSettings::IsCityPage() const {
    return page_ == Page::Location || page_ == Page::CityList || page_ == Page::CitySearch ||
           page_ == Page::CityResults || page_ == Page::CityConfirm;
}

void StopwatchSettings::WaitForPointerRelease() {
    // A hidden/rebuilt page must not inherit a pointer's press-locked release,
    // including a rapid Close/Open that reuses the persistent menu widgets.
    for (auto* input = lv_indev_get_next(nullptr); input != nullptr;
         input = lv_indev_get_next(input)) {
        if (lv_indev_get_type(input) == LV_INDEV_TYPE_POINTER) {
            lv_indev_wait_release(input);
        }
    }
}

void StopwatchSettings::BeginCityPage(Page page) {
    ++generation_;
    commit_pending_ = false;
    city_commit_pending_ = false;
    page_ = page;
    WaitForPointerRelease();
    // Clear every retained LVGL pointer before destroying its object. In
    // particular RefreshTheme may run while another detail page is visible.
    ClearDetailPointers();
    lv_obj_clean(detail_);
    lv_obj_add_flag(menu_, LV_OBJ_FLAG_HIDDEN);
    lv_obj_remove_flag(detail_, LV_OBJ_FLAG_HIDDEN);
    city_focus_ = 0;
}

lv_obj_t* StopwatchSettings::CityButton(const char* text, int x, int y, int width, int height,
                                       CityAction action, int argument, bool primary,
                                       lv_obj_t* parent) {
    if (city_control_count_ >= static_cast<int>(city_controls_.size())) {
        return nullptr;
    }
    auto* button = lv_button_create(parent != nullptr ? parent : detail_);
    lv_obj_remove_style_all(button);
    lv_obj_set_size(button, width, height);
    lv_obj_set_pos(button, x, y);
    lv_obj_set_style_radius(button, 22, 0);
    lv_obj_set_style_bg_color(button, lv_color_hex(primary ? kGreen : kGrey), 0);
    lv_obj_set_style_bg_opa(button, LV_OPA_COVER, 0);
    lv_obj_set_style_bg_color(button, lv_color_hex(primary ? 0x53BD65 : 0x696969), LV_STATE_PRESSED);
    lv_obj_set_style_border_color(button, lv_color_hex(kGreen), LV_STATE_FOCUSED);
    lv_obj_set_style_border_width(button, 3, LV_STATE_FOCUSED);
    lv_obj_remove_flag(button, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_remove_flag(button, LV_OBJ_FLAG_GESTURE_BUBBLE);
    auto* label = Label(button, text, width - 16, 0, &font_stopwatch_weather_20);
    lv_label_set_long_mode(label, LV_LABEL_LONG_SCROLL_CIRCULAR);
    lv_obj_center(label);
    if (primary) {
        lv_obj_set_style_text_color(label, lv_color_hex(0x0F5831), 0);
    }
    city_controls_[city_control_count_++] = {button, action, argument, generation_};
    lv_obj_add_event_cb(button, CityClicked, LV_EVENT_CLICKED, this);
    return button;
}

lv_obj_t* StopwatchSettings::CityRow(const stopwatch_weather::Location& location, int y,
                                    CityAction action, int argument, lv_obj_t* parent) {
    auto* button = CityButton(CityName(location).c_str(), parent != nullptr ? 0 : 78, y, 310, 52,
                              action, argument, false, parent);
    auto* name = lv_obj_get_child(button, 0);
    lv_obj_align(name, LV_ALIGN_TOP_MID, 0, 2);
    auto* identity = Label(button, CityIdentity(location).c_str(), 294, 22,
                           &font_noto_sans_basic_20_4);
    lv_label_set_long_mode(identity, LV_LABEL_LONG_SCROLL_CIRCULAR);
    return button;
}

void StopwatchSettings::FocusCity(int index, bool scroll) {
    if (city_control_count_ == 0) {
        return;
    }
    city_focus_ = (index + city_control_count_) % city_control_count_;
    for (int i = 0; i < city_control_count_; ++i) {
        if (i == city_focus_) {
            lv_obj_add_state(city_controls_[i].object, LV_STATE_FOCUSED);
        } else {
            lv_obj_remove_state(city_controls_[i].object, LV_STATE_FOCUSED);
        }
    }
    auto* focused = city_controls_[city_focus_].object;
    if (scroll && city_list_view_ != nullptr && lv_obj_get_parent(focused) == city_list_view_) {
        lv_obj_scroll_to_view(focused, LV_ANIM_OFF);
    }
}

void StopwatchSettings::CityClicked(lv_event_t* event) {
    auto* self = static_cast<StopwatchSettings*>(lv_event_get_user_data(event));
    if (!self->IsCurrentEvent(event) || !self->IsCityPage()) {
        return;
    }
    // Poll first: a simultaneous voice update may rebuild a clean page or turn
    // an edited draft into a conflict. Never reuse the old control index then.
    const unsigned generation = self->generation_;
    self->PollCity();
    if (generation != self->generation_) {
        return;
    }
    auto* target = static_cast<lv_obj_t*>(lv_event_get_target(event));
    for (int i = 0; i < self->city_control_count_; ++i) {
        const auto control = self->city_controls_[i];
        if (control.object == target && control.generation == self->generation_ &&
            !lv_obj_has_state(control.object, LV_STATE_DISABLED)) {
            self->FocusCity(i);
            self->ActivateCity(control.action, control.argument);
            return;
        }
    }
}

void StopwatchSettings::ShowLocation(const std::string& notice) {
    CancelCitySearch();
    city_notice_ = notice;
    if (weather_service_ != nullptr) {
        city_snapshot_ = weather_service_->GetSnapshot();
        city_version_ = city_snapshot_.version;
    }
    city_revision_ = city_snapshot_.config_revision;
    city_automatic_ = city_snapshot_.automatic;
    city_draft_ = city_snapshot_.location;
    city_edited_ = false;
    city_conflict_ = false;
    BeginCityPage(Page::Location);
    Label(detail_, "Weather location", 300, 34, &MontserratSemiBold26);
    city_mode_label_ = Label(detail_, city_snapshot_.automatic ? "Automatic (IP)" : "Manual", 300,
                             76, &font_noto_sans_basic_20_4);
    city_name_label_ = Label(detail_, CityName(city_snapshot_.location).c_str(), 320, 103,
                             &font_stopwatch_weather_20);
    lv_label_set_long_mode(city_name_label_, LV_LABEL_LONG_SCROLL_CIRCULAR);
    city_status_label_ = Label(detail_, "", 330, 132, &font_noto_sans_basic_20_4);
    lv_label_set_long_mode(city_status_label_, LV_LABEL_LONG_SCROLL_CIRCULAR);
    auto* automatic = CityButton("Automatic (IP)", 76, 168, 314, 48, CityAction::Automatic);
    auto* common = CityButton("Common cities", 76, 224, 314, 48, CityAction::Common);
    auto* recent = CityButton("Recent cities (3)", 76, 280, 314, 48, CityAction::Recent);
    auto* search = CityButton("Search city", 76, 336, 314, 48, CityAction::Search);
    CityButton("Back", 163, 397, 140, 44, CityAction::Back);
    if (weather_service_ == nullptr) {
        for (auto* object : {automatic, common, recent, search}) {
            lv_obj_add_state(object, LV_STATE_DISABLED);
        }
    } else if (city_snapshot_.recent_count == 0) {
        lv_obj_add_state(recent, LV_STATE_DISABLED);
    }
    RenderCityStatus();
    FocusCity(0);
}

void StopwatchSettings::ShowCityList(bool recent) {
    CancelCitySearch();
    city_recent_ = recent;
    BeginCityPage(Page::CityList);
    city_list_revision_ = city_revision_;
    if (recent) {
        for (size_t i = 0; i < std::min<size_t>(city_snapshot_.recent_count, 3); ++i) {
            city_list_[city_list_count_++] = city_snapshot_.recent[i];
        }
    } else {
        const auto& current = city_snapshot_.location;
        const bool located = stopwatch_weather::ValidLocation(current);
        if (located) {
            city_list_[city_list_count_++] = current;
        }
        for (int i = 0; i < stopwatch_weather::kCityCount; ++i) {
            const auto location = stopwatch_weather::BuiltinLocation(i);
            if (!located || !SameCityChoice(current, location)) {
                city_list_[city_list_count_++] = location;
            }
        }
    }
    Label(detail_, recent ? "Recent cities" : "Common cities", 300, 34, &MontserratSemiBold26);
    city_status_label_ = Label(detail_, "", 310, 78, &font_noto_sans_basic_20_4);
    lv_label_set_long_mode(city_status_label_, LV_LABEL_LONG_SCROLL_CIRCULAR);
    if (recent) {
        if (city_list_count_ == 0) {
            Label(detail_, "No recent cities", 300, 165, &font_noto_sans_basic_20_4);
        }
        for (int i = 0; i < city_list_count_; ++i) {
            CityRow(city_list_[i], 112 + i * 54, CityAction::SelectCity, i);
        }
    } else {
        // Only the rows scroll. The reusable detail page and Back stay fixed.
        city_list_view_ = lv_obj_create(detail_);
        lv_obj_remove_style_all(city_list_view_);
        lv_obj_set_pos(city_list_view_, 78, 112);
        lv_obj_set_size(city_list_view_, 310, 270);
        lv_obj_add_flag(city_list_view_, LV_OBJ_FLAG_SCROLLABLE);
        lv_obj_set_scroll_dir(city_list_view_, LV_DIR_VER);
        lv_obj_set_scrollbar_mode(city_list_view_, LV_SCROLLBAR_MODE_ACTIVE);
        lv_obj_remove_flag(city_list_view_, LV_OBJ_FLAG_GESTURE_BUBBLE);
        for (int i = 0; i < city_list_count_; ++i) {
            CityRow(city_list_[i], i * 54, CityAction::SelectCity, i, city_list_view_);
        }
        lv_obj_scroll_to_y(city_list_view_, 0, LV_ANIM_OFF);
    }
    CityButton("Back", 163, 390, 140, 44, CityAction::Back);
    RenderCityStatus();
    FocusCity(0);
}

void StopwatchSettings::ShowCitySearch() {
    CancelCitySearch();
    BeginCityPage(Page::CitySearch);
    Label(detail_, "Search city", 300, 32, &MontserratSemiBold26);
    city_query_label_ = Label(detail_, city_query_.empty() ? "Type name / ZIP" : city_query_.c_str(),
                              310, 73, &font_noto_sans_basic_20_4);
    lv_label_set_long_mode(city_query_label_, LV_LABEL_LONG_SCROLL_CIRCULAR);
    city_status_label_ = Label(detail_, "", 310, 102, &font_noto_sans_basic_20_4);
    lv_label_set_long_mode(city_status_label_, LV_LABEL_LONG_SCROLL_CIRCULAR);
    // Twelve 44px keys per page, inside the round screen's safe chord. A/B
    // traverse the exact same controls as touch; Double B activates a key.
    const char* keys = kKeyboardPages[city_keyboard_page_];
    for (int i = 0; i < 12; ++i) {
        const char text[] = {keys[i], '\0'};
        CityButton(text, 91 + (i % 6) * 48, 142 + (i / 6) * 52, 44, 44,
                   CityAction::Character, keys[i]);
    }
    CityButton("Space", 91, 246, 92, 44, CityAction::Character, ' ');
    CityButton(",", 187, 246, 44, 44, CityAction::Character, ',');
    CityButton("'", 235, 246, 44, 44, CityAction::Character, '\'');
    CityButton("-", 283, 246, 92, 44, CityAction::Character, '-');
    CityButton("Page", 91, 298, 140, 44, CityAction::KeyboardPage);
    CityButton("Delete", 235, 298, 140, 44, CityAction::Delete);
    CityButton("Search", 91, 350, 140, 44, CityAction::Submit, 0, true);
    CityButton("Back", 235, 350, 140, 44, CityAction::Back);
    Label(detail_, "A/B focus - Double B select", 280, 403, &font_noto_sans_basic_20_4);
    RenderCityStatus();
    FocusCity(0);
}

void StopwatchSettings::ShowCityResults() {
    BeginCityPage(Page::CityResults);
    Label(detail_, "Choose a city", 300, 34, &MontserratSemiBold26);
    auto* query = Label(detail_, city_query_.c_str(), 310, 76, &font_noto_sans_basic_20_4);
    lv_label_set_long_mode(query, LV_LABEL_LONG_SCROLL_CIRCULAR);
    city_status_label_ = Label(detail_, "", 310, 102, &font_noto_sans_basic_20_4);
    lv_label_set_long_mode(city_status_label_, LV_LABEL_LONG_SCROLL_CIRCULAR);
    if (city_search_.state == stopwatch_weather::SearchState::Ready) {
        for (size_t i = 0; i < std::min<size_t>(city_search_.count, 5); ++i) {
            CityRow(city_search_.candidates[i].location, 126 + i * 52,
                    CityAction::Candidate, static_cast<int>(i));
        }
    }
    CityButton("Back", 163, 394, 140, 44, CityAction::Back);
    RenderCityStatus();
    FocusCity(0);
}

void StopwatchSettings::ShowCityConfirm(bool automatic, const stopwatch_weather::Location& location,
                                       uint32_t revision) {
    city_automatic_ = automatic;
    city_draft_ = location;
    city_revision_ = revision;
    city_edited_ = city_edited_ || automatic != city_snapshot_.automatic ||
                   (!automatic && !stopwatch_weather::SameLocation(location, city_snapshot_.location));
    city_conflict_ = city_conflict_ || revision != city_snapshot_.config_revision;
    city_notice_.clear();
    BeginCityPage(Page::CityConfirm);
    Label(detail_, "Confirm location", 300, 36, &MontserratSemiBold26);
    city_mode_label_ = Label(detail_, "", 310, 96, &font_noto_sans_basic_20_4);
    city_name_label_ = Label(detail_, "", 330, 140, &font_stopwatch_weather_20);
    lv_label_set_long_mode(city_name_label_, LV_LABEL_LONG_SCROLL_CIRCULAR);
    city_identity_label_ = Label(detail_, "", 330, 184, &font_noto_sans_basic_20_4);
    lv_label_set_long_mode(city_identity_label_, LV_LABEL_LONG_SCROLL_CIRCULAR);
    city_status_label_ = Label(detail_, "", 330, 239, &font_noto_sans_basic_20_4);
    lv_label_set_long_mode(city_status_label_, LV_LABEL_LONG_SCROLL_CIRCULAR);
    city_confirm_ = CityButton("OK", 91, 318, 284, 52, CityAction::Confirm, 0, true);
    CityButton("Back", 163, 389, 140, 44, CityAction::Back);
    RenderCityConfirm();
    FocusCity(0);
}

void StopwatchSettings::RenderCityConfirm() {
    lv_label_set_text(city_mode_label_, city_automatic_ ? "Automatic (IP)" : "Manual");
    lv_label_set_text(city_name_label_, city_automatic_ ? "Use approximate public IP location"
                                                       : CityName(city_draft_).c_str());
    lv_label_set_text(city_identity_label_, city_automatic_ ? "May differ with VPN / carrier"
                                                           : CityIdentity(city_draft_).c_str());
    RenderCityStatus();
}

void StopwatchSettings::RenderCityStatus() {
    if (city_status_label_ == nullptr) {
        return;
    }
    const bool candidate_expired = page_ == Page::CityConfirm && city_request_ != 0 &&
        city_search_.state != stopwatch_weather::SearchState::Ready;
    std::string status;
    if (weather_service_ == nullptr) {
        status = "Location service unavailable";
    } else if (city_commit_pending_) {
        status = "Saving configuration...";
    } else if (candidate_expired) {
        status = "Search expired. Back and search again.";
    } else if (city_conflict_) {
        status = "Configuration changed. Back and choose again.";
    } else if (!city_notice_.empty()) {
        status = city_notice_;
    } else if (page_ == Page::CityConfirm) {
        status = "Not saved - OK to confirm";
    } else if (page_ == Page::CityResults) {
        using stopwatch_weather::SearchState;
        switch (city_search_.state) {
            case SearchState::Pending: status = "Searching... Back to cancel"; break;
            case SearchState::Ready:
                status = city_search_.count == 0 ? "No matches. Back to edit." : "Select, then confirm";
                break;
            case SearchState::Expired: status = "Search expired. Back to retry."; break;
            case SearchState::Superseded: status = "Search cancelled. Back to retry."; break;
            case SearchState::Error: status = "Search failed: " + city_search_.error; break;
            default: status = "Search unavailable. Back to retry."; break;
        }
    } else if (page_ == Page::CitySearch) {
        status = "ASCII / ZIP; optional region, country";
    } else if (page_ == Page::CityList) {
        status = "Offline choice - confirm before saving";
    } else if (!city_snapshot_.error.empty()) {
        status = "Weather: " + city_snapshot_.error;
    } else {
        status = city_snapshot_.pending ? "Weather update pending" : "Shared with Weather / voice";
    }
    lv_label_set_text(city_status_label_, status.c_str());
    if (city_confirm_ != nullptr) {
        if (city_conflict_ || candidate_expired || city_commit_pending_ || weather_service_ == nullptr) {
            lv_obj_add_state(city_confirm_, LV_STATE_DISABLED);
        } else {
            lv_obj_remove_state(city_confirm_, LV_STATE_DISABLED);
        }
    }
}

void StopwatchSettings::CancelCitySearch() {
    if (city_request_ != 0 && weather_service_ != nullptr) {
        weather_service_->CancelSearch(city_request_);
    }
    city_request_ = 0;
    city_search_version_ = 0;
    ++city_request_generation_;
    city_search_ = {};
}

void StopwatchSettings::SubmitCitySearch() {
    if (weather_service_ == nullptr || city_commit_pending_) {
        return;
    }
    const auto begin = city_query_.find_first_not_of(' ');
    if (begin == std::string::npos) {
        city_notice_ = "Enter a city name or ZIP";
        RenderCityStatus();
        return;
    }
    CancelCitySearch();
    const auto end = city_query_.find_last_not_of(' ');
    // Explicit submission only: editing never performs HTTP or queues requests.
    const auto result = weather_service_->SearchCity(city_query_.substr(begin, end - begin + 1));
    if (!result.ok) {
        city_notice_ = "Search not started: " + result.error;
        RenderCityStatus();
        return;
    }
    city_notice_.clear();
    city_request_ = result.request_id;
    city_search_generation_ = city_request_generation_;
    city_search_version_ = weather_service_->SearchVersion(city_request_);
    city_search_ = weather_service_->GetSearch(city_request_);
    ShowCityResults();
}

void StopwatchSettings::PollCity() {
    if (!active_ || !IsCityPage() || weather_service_ == nullptr ||
        lv_obj_has_flag(root_, LV_OBJ_FLAG_HIDDEN)) {
        return;
    }
    if (weather_service_->Version() != city_version_) {
        const auto snapshot = weather_service_->GetSnapshot();
        city_version_ = snapshot.version;
        const bool changed = snapshot.config_revision != city_snapshot_.config_revision;
        const bool moved = !EqualLocation(snapshot.location, city_snapshot_.location);
        city_snapshot_ = snapshot;
        if ((changed || moved) && !city_commit_pending_) {
            if (city_edited_) {
                city_conflict_ = city_conflict_ || changed;
            } else {
                // A clean draft follows voice and effective IP location changes.
                // Typed/selected drafts retain their original revision.
                city_revision_ = snapshot.config_revision;
                city_automatic_ = snapshot.automatic;
                city_draft_ = snapshot.location;
                if (page_ == Page::CityConfirm) {
                    RenderCityConfirm();
                } else if (page_ == Page::CityList && (changed || !city_recent_)) {
                    ShowCityList(city_recent_);
                }
            }
        }
        if (page_ == Page::Location) {
            lv_label_set_text(city_mode_label_, snapshot.automatic ? "Automatic (IP)" : "Manual");
            lv_label_set_text(city_name_label_, CityName(snapshot.location).c_str());
            for (int i = 0; i < city_control_count_; ++i) {
                if (city_controls_[i].action == CityAction::Recent) {
                    if (snapshot.recent_count == 0) {
                        lv_obj_add_state(city_controls_[i].object, LV_STATE_DISABLED);
                    } else {
                        lv_obj_remove_state(city_controls_[i].object, LV_STATE_DISABLED);
                    }
                }
            }
        }
        RenderCityStatus();
    }
    if ((page_ == Page::CityResults || page_ == Page::CityConfirm) && city_request_ != 0 &&
        city_search_generation_ == city_request_generation_ &&
        weather_service_->SearchVersion(city_request_) != city_search_version_) {
        const auto result = weather_service_->GetSearch(city_request_);
        if (result.request_id != city_request_ || city_search_generation_ != city_request_generation_) {
            return;
        }
        city_search_version_ = result.version;
        city_search_ = result;
        if (page_ == Page::CityResults) {
            ShowCityResults();
        } else if (result.state != stopwatch_weather::SearchState::Ready) {
            RenderCityStatus();
        }
    }
}

void StopwatchSettings::ActivateCity(CityAction action, int argument) {
    if (!active_ || !IsCityPage() || (city_commit_pending_ && action != CityAction::Back)) {
        return;
    }
    switch (action) {
        case CityAction::Automatic:
            ShowCityConfirm(true, {}, city_revision_);
            break;
        case CityAction::Common:
            city_notice_.clear();
            ShowCityList(false);
            break;
        case CityAction::Recent:
            city_notice_.clear();
            ShowCityList(true);
            break;
        case CityAction::Search:
            city_notice_.clear();
            ShowCitySearch();
            break;
        case CityAction::Back:
            if (page_ == Page::Location) {
                ShowMenu();
            } else if (page_ == Page::CityResults) {
                ShowCitySearch();
            } else {
                ShowLocation();
            }
            break;
        case CityAction::SelectCity:
            if (argument >= 0 && argument < city_list_count_) {
                ShowCityConfirm(false, city_list_[argument], city_list_revision_);
            }
            break;
        case CityAction::Character:
            if (city_query_.size() < 96) {
                city_query_ += static_cast<char>(argument);
                city_edited_ = true;
                city_notice_.clear();
                lv_label_set_text(city_query_label_, city_query_.c_str());
                RenderCityStatus();
            } else {
                city_notice_ = "Maximum 96 ASCII characters";
                RenderCityStatus();
            }
            break;
        case CityAction::Delete:
            if (!city_query_.empty()) {
                city_query_.pop_back();
                city_edited_ = true;
                city_notice_.clear();
                lv_label_set_text(city_query_label_, city_query_.empty() ? "Type name / ZIP"
                                                                       : city_query_.c_str());
                RenderCityStatus();
            }
            break;
        case CityAction::KeyboardPage:
            city_keyboard_page_ = (city_keyboard_page_ + 1) % 3;
            ShowCitySearch();
            break;
        case CityAction::Submit:
            if (city_conflict_) {
                RenderCityStatus();
            } else {
                SubmitCitySearch();
            }
            break;
        case CityAction::Candidate:
            if (city_search_.state == stopwatch_weather::SearchState::Ready && argument >= 0 &&
                argument < static_cast<int>(std::min<size_t>(city_search_.count, 5))) {
                ShowCityConfirm(false, city_search_.candidates[argument].location,
                                city_search_.config_revision);
            }
            break;
        case CityAction::Confirm: CommitCity(); break;
    }
}

void StopwatchSettings::CommitCity() {
    if (!active_ || page_ != Page::CityConfirm || weather_service_ == nullptr ||
        city_commit_pending_) {
        return;
    }
    PollCity();
    if (city_conflict_ || (city_request_ != 0 &&
        city_search_.state != stopwatch_weather::SearchState::Ready)) {
        RenderCityStatus();
        return;
    }
    city_commit_pending_ = true;
    RenderCityStatus();
    const unsigned generation = generation_;
    Application::GetInstance().Schedule([this, generation]() {
        stopwatch_weather::Location location;
        bool automatic;
        uint32_t revision;
        {
            DisplayLockGuard lock(display_);
            if (!lock || !active_ || generation != generation_ ||
                page_ != Page::CityConfirm || !city_commit_pending_) {
                return;
            }
            automatic = city_automatic_;
            location = city_draft_;
            revision = city_revision_;
        }
        // Checked NVS and the shared revision guard run outside the LVGL lock.
        // Close cancels a queued command; once this setter executes, closing the
        // UI cannot pretend it rolled the committed configuration back.
        const auto result = weather_service_->SetLocation(automatic, location, revision);
        DisplayLockGuard lock(display_);
        if (!lock || !active_ || generation != generation_ || page_ != Page::CityConfirm) {
            return;
        }
        city_commit_pending_ = false;
        if (result.ok) {
            ShowLocation("Configuration saved - weather may be pending");
        } else {
            const auto snapshot = weather_service_->GetSnapshot();
            city_version_ = snapshot.version;
            city_snapshot_ = snapshot;
            city_conflict_ = snapshot.config_revision != revision;
            city_notice_ = "Not saved: " + result.error;
            RenderCityStatus();
        }
    });
}

void StopwatchSettings::Tick(lv_timer_t* timer) {
    auto* self = static_cast<StopwatchSettings*>(lv_timer_get_user_data(timer));
    if (self->active_ && self->IsCityPage()) {
        self->PollCity();
    }
    if (self->active_ && self->page_ == Page::Wifi) {
        self->UpdateWifiStatus();
    }
}

#endif
