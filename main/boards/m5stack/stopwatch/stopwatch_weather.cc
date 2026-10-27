#include "sdkconfig.h"

#if CONFIG_STOPWATCH_LOCAL_APPS

#include "stopwatch_weather.h"

#include <algorithm>
#include <cmath>
#include <cstdio>
#include <cstring>
#include <ctime>
#include <string>

#include "stopwatch_weather_service.h"

LV_FONT_DECLARE(font_noto_sans_basic_20_4);
LV_FONT_DECLARE(font_noto_sans_basic_30_4);
extern "C" {
LV_FONT_DECLARE(font_stopwatch_weather_20);
LV_FONT_DECLARE(font_stopwatch_weather_30);
LV_FONT_DECLARE(CommissionerMedium108);
}

namespace {
using namespace stopwatch_weather;

// Design A has intentionally selected light/dark surfaces, not inverted colors.
struct Palette {
    uint32_t primary, secondary, accent, cloud, surface, line;
};
constexpr Palette kLight = {0x20291F, 0x5C6659, 0x64792E, 0x4E5C4B, 0xF4F6EF, 0xD4DACD};
constexpr Palette kDark = {0xF4F5F0, 0xA4ADA4, 0xE7EFAF, 0xF4F5F0, 0x101412, 0x353C34};

void Hidden(lv_obj_t* object, bool hidden) {
    if (hidden) {
        lv_obj_add_flag(object, LV_OBJ_FLAG_HIDDEN);
    } else {
        lv_obj_remove_flag(object, LV_OBJ_FLAG_HIDDEN);
    }
}

void SetText(lv_obj_t* label, const char* text) {
    // LVGL otherwise frees/reallocates even equal label text. In particular a
    // state-only update must not restart the long city's scrolling animation.
    if (std::strcmp(lv_label_get_text(label), text) != 0) {
        lv_label_set_text(label, text);
    }
}

bool Printable(const std::string& text) {
    return !text.empty() && text.size() <= 192 &&
           std::all_of(text.begin(), text.end(), [](unsigned char c) { return c >= 32 && c < 127; });
}

std::string CityName(const Location& location) {
    // Never depend on chat's transient glyph cache for an arbitrary provider name.
    for (const auto& city : kCities) {
        if (location.display_name == city.chinese || location.city == city.chinese ||
            location.city == city.name || location.display_name == city.name) {
            return city.chinese;
        }
    }
    if (Printable(location.display_name)) {
        return location.display_name;
    }
    if (Printable(location.city)) {
        return location.city;
    }
    if (Printable(location.id)) {
        return location.id;
    }
    return "位置未知";
}

std::string Number(float value, const char* suffix = "") {
    if (!std::isfinite(value)) {
        return std::string("--") + suffix;
    }
    char buffer[32];
    // Service validates ranges; retain an explicit UI bound for injected snapshots.
    if (value < -999 || value > 999) {
        return std::string("--") + suffix;
    }
    std::snprintf(buffer, sizeof(buffer), "%ld%s", std::lround(value), suffix);
    return buffer;
}

const char* StateText(const Snapshot& snapshot) {
    switch (snapshot.state) {
        case State::Starting:
            return "未加载";
        case State::Locating:
            return "定位中";
        case State::Updating:
            return "更新中";
        case State::Ready:
            return snapshot.pending ? "更新中" : "已更新";
        case State::Offline:
            return "离线";
        case State::Error:
            return "更新失败";
    }
    return "未知";
}

std::string DayCaption(int64_t timestamp, int offset, int64_t observed_at, bool relative) {
    const auto date = FormatLocalTime(timestamp, offset, "%Y/%m/%d");
    if (relative && date.size() == 10) {
        // Relative days are safe only when provider observation and device dates agree.
        const int64_t local_day = (timestamp + offset) / 86400;
        const int64_t observed_day = (observed_at + offset) / 86400;
        constexpr const char* names[] = {"今天", "明天", "后天"};
        const int64_t difference = local_day - observed_day;
        if (difference >= 0 && difference < 3) {
            return names[difference];
        }
    }
    return date.size() == 10 ? date.substr(5) : "--/--";
}

template <typename Entries>
std::string RainSummary(const Entries& entries, const char* period) {
    float maximum = -1;
    size_t known = 0;
    for (const auto& entry : entries) {
        if (entry.time > 0 && std::isfinite(entry.precipitation) && entry.precipitation >= 0 &&
            entry.precipitation <= 100) {
            maximum = std::max(maximum, entry.precipitation);
            ++known;
        }
    }
    if (known == 0) {
        return "暂无降雨概率";
    }
    return std::string(known == entries.size() ? period : "已知预报") + "，最高降雨 " +
           Number(maximum, "%");
}

// Each detail page stays inside the round safe area. URLs are complete, wrapped
// at explicit separators rather than silently truncated or used to launch HTTP.
constexpr const char* kSourceTitles[] = {"Weather data", "City search", "License", "Display changes",
                                        "Privacy / limits", "Data status"};
constexpr const char* kSourceBodies[] = {
    "Open-Meteo\nhttps://open-meteo.com\nWeather API / documentation\nhttps://open-meteo.com/\nen/docs",
    "Open-Meteo Geocoding\nhttps://open-meteo.com/\nen/docs/geocoding-api\nCity data: GeoNames\nhttps://www.geonames.org/",
    "Data: CC BY 4.0\nhttps://creativecommons.org/\nlicenses/by/4.0/\nhttps://open-meteo.com/\nen/licence",
    "Values rounded for display.\nConditions translated / grouped.\nIcons are simplified.\nClock: device local time.\nForecast: provider UTC offset.\nNot an official weather alert.",
    "IP location: https://ipwho.is\nhttps://ipwhois.io/documentation\nProviders see public IP / location.\nFree API: non-commercial use.\nNo availability guarantee.",
};
}  // namespace

lv_obj_t* StopwatchWeather::Panel(lv_obj_t* parent) {
    auto* panel = lv_obj_create(parent);
    lv_obj_remove_style_all(panel);
    lv_obj_set_size(panel, 466, 466);
    lv_obj_remove_flag(panel, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_add_flag(panel, LV_OBJ_FLAG_GESTURE_BUBBLE);
    return panel;
}

lv_obj_t* StopwatchWeather::Text(lv_obj_t* parent, const char* text, int x, int y, int width,
                                const lv_font_t* font, Ink ink) {
    auto* label = lv_label_create(parent);
    lv_obj_set_width(label, width);
    lv_obj_set_pos(label, x, y);
    lv_label_set_long_mode(label, LV_LABEL_LONG_CLIP);
    SetText(label, text);
    lv_obj_set_style_text_font(label, font, 0);
    lv_obj_set_style_text_align(label, LV_TEXT_ALIGN_CENTER, 0);
    lv_obj_remove_flag(label, LV_OBJ_FLAG_CLICKABLE);
    lv_obj_add_flag(label, LV_OBJ_FLAG_GESTURE_BUBBLE);
    paint_.push_back({label, ink, true});
    return label;
}

lv_obj_t* StopwatchWeather::Shape(lv_obj_t* parent, int x, int y, int width, int height, int radius,
                                 Ink ink) {
    auto* object = lv_obj_create(parent);
    lv_obj_remove_style_all(object);
    lv_obj_set_pos(object, x, y);
    lv_obj_set_size(object, width, height);
    lv_obj_set_style_radius(object, radius, 0);
    lv_obj_set_style_bg_opa(object, LV_OPA_COVER, 0);
    lv_obj_remove_flag(object, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_remove_flag(object, LV_OBJ_FLAG_CLICKABLE);
    lv_obj_add_flag(object, LV_OBJ_FLAG_GESTURE_BUBBLE);
    paint_.push_back({object, ink, false});
    return object;
}

void StopwatchWeather::BuildIcon(IconSlot& slot, lv_obj_t* parent, int x, int y, int size) {
    for (auto& part : slot.parts) {
        part = Panel(parent);
        lv_obj_set_pos(part, x, y);
        lv_obj_set_size(part, size, size);
        lv_obj_remove_flag(part, LV_OBJ_FLAG_CLICKABLE);
    }
    auto shape = [&](IconPart part, int sx, int sy, int w, int h, Ink ink) {
        return Shape(slot.parts[part], sx * size / 64, sy * size / 64,
                     (w * size + 63) / 64, (h * size + 63) / 64, LV_RADIUS_CIRCLE, ink);
    };
    auto cloud = [&](IconPart part, int offset) {
        shape(part, 8, 29 + offset, 23, 23, Ink::Cloud);
        shape(part, 23, 19 + offset, 30, 31, Ink::Cloud);
        shape(part, 42, 32 + offset, 19, 20, Ink::Cloud);
        shape(part, 17, 38 + offset, 35, 14, Ink::Cloud);
    };
    shape(Sun, 20, 20, 24, 24, Ink::Accent);
    constexpr int rays[][2] = {{29, 2}, {29, 54}, {2, 29}, {54, 29},
                               {10, 10}, {49, 10}, {10, 49}, {49, 49}};
    for (const auto& ray : rays) {
        shape(Sun, ray[0], ray[1], 6, 6, Ink::Accent);
    }
    // Keep the original design-A sun/cloud primitives, including ceil scaling.
    shape(Partly, 12, 9, 25, 25, Ink::Accent);
    shape(Partly, 22, 0, 5, 6, Ink::Accent);
    shape(Partly, 2, 19, 6, 5, Ink::Accent);
    shape(Partly, 7, 4, 5, 5, Ink::Accent);
    shape(Partly, 38, 4, 5, 5, Ink::Accent);
    cloud(Partly, 0);
    shape(Moon, 13, 9, 43, 43, Ink::Accent);
    shape(Moon, 28, 1, 33, 36, Ink::Surface);
    cloud(Cloud, -5);
    cloud(Rain, -12);
    cloud(Snow, -12);
    cloud(Bolt, -12);
    for (int i = 0; i < 3; ++i) {
        auto* drop = shape(Rain, 17 + i * 16, 47, 4, 12, Ink::Primary);
        lv_obj_set_style_transform_rotation(drop, 220, 0);
        shape(Snow, 14 + i * 16, 52, 6, 6, Ink::Primary);
        shape(Fog, 7 + (i % 2) * 5, 39 + i * 9, 49 - (i % 2) * 10, 3, Ink::Secondary);
    }
    auto* bolt = shape(Bolt, 31, 39, 7, 22, Ink::Accent);
    lv_obj_set_style_transform_rotation(bolt, 260, 0);
    shape(Bolt, 24, 46, 14, 5, Ink::Accent);
    Text(slot.parts[Unknown], "?", 0, std::max(0, (size - 46) / 2), size,
         size < 40 ? &font_noto_sans_basic_20_4 : &font_noto_sans_basic_30_4, Ink::Secondary);
    ShowIcon(slot, -1);
}

void StopwatchWeather::DrawIndicator(lv_obj_t* parent, Indicator indicator, int x, int y, int size) {
    auto shape = [&](int sx, int sy, int w, int h) {
        Shape(parent, x + sx * size / 64, y + sy * size / 64, (w * size + 63) / 64,
              (h * size + 63) / 64, LV_RADIUS_CIRCLE, Ink::Secondary);
    };
    if (indicator == Indicator::Drop) {
        shape(13, 23, 38, 38);
        shape(23, 11, 18, 27);
        shape(29, 3, 6, 22);
    } else {
        shape(3, 17, 43, 5);
        shape(3, 30, 57, 5);
        shape(3, 43, 31, 5);
    }
}

void StopwatchWeather::ShowIcon(IconSlot& slot, int code, bool is_day) {
    for (auto* part : slot.parts) {
        Hidden(part, true);
    }
    auto show = [&](IconPart part) { Hidden(slot.parts[part], false); };
    switch (WeatherCondition(code)) {
        case Condition::Clear:
            show(is_day ? Sun : Moon);
            break;
        case Condition::Cloudy:
            show(is_day && (code == 1 || code == 2) ? Partly : Cloud);
            break;
        case Condition::Fog:
            show(Fog);
            break;
        case Condition::Rain:
            show(Rain);
            break;
        case Condition::Snow:
            show(Snow);
            break;
        case Condition::Thunderstorm:
            show(Bolt);
            break;
        case Condition::Unknown:
            show(Unknown);
            break;
    }
}

void StopwatchWeather::BuildCurrent() {
    auto* page = pages_[0];
    updated_[0] = Text(page, "--/-- · 星期--", 93, 40, 280, &font_stopwatch_weather_20,
                       Ink::Secondary);
    clock_ = Text(page, "--:--", 114, 79, 100, &font_noto_sans_basic_30_4);
    city_[0] = Text(page, "位置未知", 222, 85, 146, &font_stopwatch_weather_20, Ink::Secondary);
    lv_label_set_long_mode(city_[0], LV_LABEL_LONG_SCROLL_CIRCULAR);
    BuildIcon(current_icon_, page, 100, 164, 78);
    // Keep ordinary temperatures exactly in the original design-A number slot.
    temperature_ = Text(page, "", 192, 157, 140, &CommissionerMedium108);
    minus_ = Shape(page, 192, 210, 18, 5, 2, Ink::Primary);
    missing_[0] = Shape(page, 218, 210, 38, 5, 2, Ink::Secondary);
    missing_[1] = Shape(page, 270, 210, 38, 5, 2, Ink::Secondary);
    degree_ = Text(page, "°", 326, 154, 30, &font_noto_sans_basic_30_4);
    condition_ = Text(page, "未知 · 体感 --°", 70, 283, 326, &font_stopwatch_weather_20);
    range_ = Text(page, "最高 --°   最低 --°", 80, 329, 306, &font_stopwatch_weather_20,
                  Ink::Secondary);
    DrawIndicator(page, Indicator::Drop, 126, 376, 22);
    humidity_ = Text(page, "--%", 151, 370, 60, &font_noto_sans_basic_20_4, Ink::Secondary);
    DrawIndicator(page, Indicator::Wind, 239, 376, 22);
    wind_ = Text(page, "-- km/h", 268, 370, 80, &font_noto_sans_basic_20_4, Ink::Secondary);
}

void StopwatchWeather::BuildHourly() {
    auto* page = pages_[1];
    hour_subtitle_ = Text(page, "逐时预报", 255, 121, 132, &font_stopwatch_weather_20,
                          Ink::Secondary);
    for (int i = 0; i < 4; ++i) {
        const int x = 54 + i * 90;
        hour_times_[i] = Text(page, "--:--", x, 173, 88, &font_stopwatch_weather_20,
                              Ink::Secondary);
        BuildIcon(hour_icons_[i], page, x + 21, 220, 45);
        hour_temperatures_[i] = Text(page, "--°", x, 277, 88, &font_noto_sans_basic_30_4);
        DrawIndicator(page, Indicator::Drop, x + 9, 333, 15);
        hour_rain_[i] = Text(page, "--%", x + 27, 323, 56, &font_noto_sans_basic_20_4,
                             Ink::Secondary);
    }
    Shape(page, 88, 365, 290, 1, 0, Ink::Line);
    summaries_[0] = Text(page, "未加载", 80, 379, 306, &font_stopwatch_weather_20, Ink::Secondary);
}

void StopwatchWeather::BuildDaily() {
    auto* page = pages_[2];
    Text(page, "低温 / 高温", 255, 121, 132, &font_stopwatch_weather_20, Ink::Secondary);
    for (int i = 0; i < 3; ++i) {
        const int y = 174 + i * 64;
        day_dates_[i] = Text(page, "--/--", 79, y + 8, 58, &font_stopwatch_weather_20);
        BuildIcon(day_icons_[i], page, 145, y + 3, 37);
        DrawIndicator(page, Indicator::Drop, 193, y + 17, 14);
        day_rain_[i] = Text(page, "--%", 211, y + 8, 54, &font_noto_sans_basic_20_4,
                            Ink::Secondary);
        day_low_[i] = Text(page, "--°", 276, y + 8, 52, &font_noto_sans_basic_20_4, Ink::Secondary);
        day_high_[i] = Text(page, "--°", 335, y + 8, 52, &font_noto_sans_basic_20_4);
        if (i < 2) {
            Shape(page, 79, y + 58, 308, 1, 0, Ink::Line);
        }
    }
    Shape(page, 88, 365, 290, 1, 0, Ink::Line);
    summaries_[1] = Text(page, "未加载", 80, 379, 306, &font_stopwatch_weather_20, Ink::Secondary);
}

void StopwatchWeather::BuildSource() {
    source_ = Panel(root_);
    lv_obj_add_flag(source_, LV_OBJ_FLAG_CLICKABLE);
    source_title_ = Text(source_, "", 90, 64, 286, &font_noto_sans_basic_30_4);
    source_body_ = Text(source_, "", 73, 134, 320, &font_noto_sans_basic_20_4);
    lv_obj_set_height(source_body_, 195);
    lv_label_set_long_mode(source_body_, LV_LABEL_LONG_WRAP);
    source_number_ = Text(source_, "", 73, 345, 320, &font_noto_sans_basic_20_4, Ink::Secondary);
    constexpr const char* names[] = {"< A", "Back", "B >"};
    for (int i = 0; i < 3; ++i) {
        auto* button = Panel(source_);
        lv_obj_set_size(button, 88, 48);
        lv_obj_set_pos(button, 101 + i * 88, 384);
        lv_obj_add_flag(button, LV_OBJ_FLAG_CLICKABLE);
        lv_obj_add_event_cb(button, Clicked, LV_EVENT_CLICKED, this);
        Text(button, names[i], 0, 8, 88, &font_noto_sans_basic_20_4);
        source_controls_[i] = button;
    }
    UpdateSource();
    Hidden(source_, true);
}

void StopwatchWeather::Setup(lv_obj_t* parent) {
    if (root_ != nullptr) {
        return;
    }
    paint_.reserve(400);
    root_ = Panel(parent);
    lv_obj_remove_flag(root_, LV_OBJ_FLAG_GESTURE_BUBBLE);
    lv_obj_add_flag(root_, LV_OBJ_FLAG_CLICKABLE);
    lv_obj_set_style_bg_opa(root_, LV_OPA_COVER, 0);
    paint_.push_back({root_, Ink::Surface, false});
    for (auto& page : pages_) {
        page = Panel(root_);
        lv_obj_add_flag(page, LV_OBJ_FLAG_CLICKABLE);
    }
    BuildCurrent();
    for (int i = 1; i < kPageCount; ++i) {
        city_[i] = Text(pages_[i], "位置未知", 100, 36, 266, &font_stopwatch_weather_20);
        lv_label_set_long_mode(city_[i], LV_LABEL_LONG_SCROLL_CIRCULAR);
        updated_[i] = Text(pages_[i], "--/-- --:--", 80, 72, 306, &font_stopwatch_weather_20,
                           Ink::Secondary);
    }
    BuildHourly();
    BuildDaily();
    // Reuse the original header as an invisible details hitbox, not a new row.
    source_button_ = Panel(root_);
    lv_obj_set_size(source_button_, 300, 44);
    lv_obj_set_pos(source_button_, 83, 68);
    lv_obj_add_flag(source_button_, LV_OBJ_FLAG_CLICKABLE);
    lv_obj_add_event_cb(source_button_, Clicked, LV_EVENT_CLICKED, this);
    for (int i = 0; i < kPageCount; ++i) {
        auto* button = Panel(root_);
        lv_obj_set_size(button, 44, 44);
        lv_obj_set_pos(button, 167 + i * 44, 405);
        lv_obj_add_flag(button, LV_OBJ_FLAG_CLICKABLE);
        lv_obj_add_event_cb(button, Clicked, LV_EVENT_CLICKED, this);
        dot_buttons_[i] = button;
        dots_[i] = Shape(button, 19, 19, 6, 6, LV_RADIUS_CIRCLE, Ink::Secondary);
    }
    BuildSource();
    lv_obj_add_event_cb(root_, Gesture, LV_EVENT_GESTURE, this);
    timer_ = lv_timer_create(Tick, 1000, this);
    lv_timer_pause(timer_);
    RenderSnapshot({});
    ShowPage(0);
    RefreshTheme();
    Hidden(root_, true);
}

void StopwatchWeather::Open() {
    if (root_ == nullptr) {
        return;
    }
    active_ = true;
    source_open_ = false;
    UpdateClock();
    ShowPage(0);
    if (service_ != nullptr) {
        service_->SetVisible(true);
        service_->RequestRefresh();
    }
    UpdateSnapshot(true);
    RefreshTheme();
    lv_timer_resume(timer_);
}

void StopwatchWeather::WaitPointers() {
    for (auto* input = lv_indev_get_next(nullptr); input != nullptr;
         input = lv_indev_get_next(input)) {
        if (lv_indev_get_type(input) == LV_INDEV_TYPE_POINTER) {
            lv_indev_wait_release(input);
        }
    }
}

void StopwatchWeather::Close() {
    if (root_ == nullptr) {
        return;
    }
    active_ = false;
    if (service_ != nullptr) {
        service_->SetVisible(false);
    }
    lv_timer_pause(timer_);
    Hidden(root_, true);
    // Includes source controls: a retained release cannot affect an immediate reopen.
    WaitPointers();
}

void StopwatchWeather::RefreshTheme() {
    if (root_ == nullptr) {
        return;
    }
    const auto* theme = display_ != nullptr ? display_->GetTheme() : nullptr;
    const auto& palette = theme != nullptr && theme->name() == "dark" ? kDark : kLight;
    for (const auto& paint : paint_) {
        uint32_t color = palette.primary;
        switch (paint.ink) {
            case Ink::Primary:
                break;
            case Ink::Secondary:
                color = palette.secondary;
                break;
            case Ink::Accent:
                color = palette.accent;
                break;
            case Ink::Cloud:
                color = palette.cloud;
                break;
            case Ink::Surface:
                color = palette.surface;
                break;
            case Ink::Line:
                color = palette.line;
                break;
        }
        if (paint.text) {
            lv_obj_set_style_text_color(paint.object, lv_color_hex(color), 0);
        } else {
            lv_obj_set_style_bg_color(paint.object, lv_color_hex(color), 0);
        }
    }
    for (int i = 0; i < kPageCount; ++i) {
        lv_obj_set_style_bg_color(dots_[i],
                                  lv_color_hex(i == page_ ? palette.primary : palette.secondary), 0);
    }
}

void StopwatchWeather::ShowPage(int index) {
    page_ = (index % kPageCount + kPageCount) % kPageCount;
    for (int i = 0; i < kPageCount; ++i) {
        Hidden(pages_[i], source_open_ || i != page_);
        Hidden(dot_buttons_[i], source_open_);
        const int width = i == page_ ? 20 : 6;
        lv_obj_set_size(dots_[i], width, 6);
        lv_obj_set_pos(dots_[i], (44 - width) / 2, 19);
    }
    Hidden(source_button_, source_open_);
    Hidden(source_, !source_open_);
    RefreshTheme();
}

void StopwatchWeather::UpdateSource() {
    source_page_ = (source_page_ % kSourceCount + kSourceCount) % kSourceCount;
    SetText(source_title_, kSourceTitles[source_page_]);
    SetText(source_body_, source_page_ == kSourceCount - 1 ? data_status_.c_str()
                                                         : kSourceBodies[source_page_]);
    char page[64];
    std::snprintf(page, sizeof(page), "%d / %d   Double B: Back", source_page_ + 1, kSourceCount);
    SetText(source_number_, page);
}

void StopwatchWeather::ShowSource(bool open) {
    source_open_ = open;
    if (open) {
        source_page_ = 0;
        UpdateSource();
    }
    ShowPage(page_);
    WaitPointers();
}

void StopwatchWeather::Step(int direction) {
    if (source_open_) {
        source_page_ += direction < 0 ? -1 : 1;
        UpdateSource();
    } else {
        ShowPage(page_ + (direction < 0 ? -1 : 1));
    }
}

bool StopwatchWeather::IsVisible() const {
    if (!active_ || root_ == nullptr) {
        return false;
    }
    for (auto* object = root_; object != nullptr; object = lv_obj_get_parent(object)) {
        if (lv_obj_has_flag(object, LV_OBJ_FLAG_HIDDEN)) {
            return false;
        }
    }
    return true;
}

void StopwatchWeather::HandleButton(int button) {
    if (!IsVisible()) {
        return;
    }
    if (button == 0 || button == 1) {
        Step(button == 0 ? -1 : 1);
    } else if (button == 2) {
        ShowSource(!source_open_);
    }
}

bool StopwatchWeather::IsCurrentEvent(lv_event_t* event) const {
    if (!active_) {
        return false;
    }
    auto* object = static_cast<lv_obj_t*>(lv_event_get_target(event));
    bool belongs = false;
    for (; object != nullptr; object = lv_obj_get_parent(object)) {
        if (lv_obj_has_flag(object, LV_OBJ_FLAG_HIDDEN)) {
            return false;
        }
        belongs = belongs || object == root_;
    }
    return belongs;
}

void StopwatchWeather::Clicked(lv_event_t* event) {
    auto* self = static_cast<StopwatchWeather*>(lv_event_get_user_data(event));
    if (!self->IsCurrentEvent(event)) {
        return;
    }
    auto* target = static_cast<lv_obj_t*>(lv_event_get_target(event));
    if (target == self->source_button_) {
        self->ShowSource(true);
        return;
    }
    for (int i = 0; i < kPageCount; ++i) {
        if (target == self->dot_buttons_[i]) {
            self->ShowPage(i);
            return;
        }
    }
    for (int i = 0; i < 3; ++i) {
        if (target == self->source_controls_[i]) {
            if (i == 1) {
                self->ShowSource(false);
            } else {
                self->Step(i == 0 ? -1 : 1);
            }
            return;
        }
    }
}

void StopwatchWeather::Gesture(lv_event_t* event) {
    auto* self = static_cast<StopwatchWeather*>(lv_event_get_user_data(event));
    auto* input = lv_indev_active();
    if (input == nullptr || !self->IsCurrentEvent(event)) {
        return;
    }
    const auto direction = lv_indev_get_gesture_dir(input);
    if (direction == LV_DIR_LEFT || direction == LV_DIR_RIGHT) {
        self->Step(direction == LV_DIR_LEFT ? 1 : -1);
        lv_indev_wait_release(input);
    }
}

void StopwatchWeather::UpdateTemperature(float value) {
    const bool valid = std::isfinite(value) && value >= -999 && value <= 999;
    const long rounded = valid ? std::lround(value) : 0;
    const bool negative = rounded < 0;
    char digits[16] = "";
    if (valid) {
        std::snprintf(digits, sizeof(digits), "%ld", std::abs(rounded));
    }
    const lv_font_t* font = &CommissionerMedium108;
    lv_point_t measured{};
    lv_text_get_size(&measured, digits, font, 0, 0, LV_COORD_MAX, LV_TEXT_FLAG_NONE);
    SetText(temperature_, digits);
    if (!negative && measured.x <= 140) {
        // Restore these exact anchors after every boundary-value fallback too.
        lv_obj_set_style_text_font(temperature_, font, 0);
        lv_obj_set_width(temperature_, 140);
        lv_obj_set_pos(temperature_, 192, 157);
        lv_obj_set_pos(degree_, 326, 154);
    } else {
        const int sign_width = negative ? 23 : 0;
        if (measured.x + sign_width + 30 > 164) {
            font = &font_stopwatch_weather_30;
            lv_text_get_size(&measured, digits, font, 0, 0, LV_COORD_MAX, LV_TEXT_FLAG_NONE);
        }
        // Only exceptional values adapt within the original x=192..356 assembly.
        const int total = measured.x + sign_width + 30;
        const int left = 192 + (164 - total) / 2;
        const bool large = font == &CommissionerMedium108;
        lv_obj_set_style_text_font(temperature_, font, 0);
        lv_obj_set_width(temperature_, std::max(1, static_cast<int>(measured.x)));
        lv_obj_set_pos(temperature_, left + sign_width, large ? 157 : 190);
        lv_obj_set_pos(minus_, left, large ? 201 : 208);
        lv_obj_set_pos(degree_, left + sign_width + measured.x, large ? 154 : 188);
    }
    Hidden(temperature_, !valid);
    Hidden(minus_, !valid || !negative);
    Hidden(degree_, !valid);
    for (auto* dash : missing_) {
        Hidden(dash, valid);
    }
}

void StopwatchWeather::UpdateClock() {
    const time_t now = time(nullptr);
    const int64_t minute = now >= 1577836800 ? now / 60 : -2;
    if (minute == clock_minute_) {
        return;
    }
    char date[16] = "";
    char time_text[6] = "--:--";
    char timestamp[16] = "--/-- --:--";
    struct tm local_time {};
    const bool valid = minute >= 0 && localtime_r(&now, &local_time) != nullptr &&
                       local_time.tm_wday >= 0 && local_time.tm_wday < 7 &&
                       std::strftime(date, sizeof(date), "%Y/%m/%d", &local_time) != 0 &&
                       std::strftime(time_text, sizeof(time_text), "%H:%M", &local_time) != 0 &&
                       std::strftime(timestamp, sizeof(timestamp), "%m/%d %H:%M", &local_time) != 0;
    // A failed localtime conversion must be retried, even in the same minute.
    clock_minute_ = valid ? minute : minute == -2 ? -2 : -1;
    clock_now_ = valid ? now : 0;
    clock_date_ = valid ? date : "";
    std::string caption = "--/-- · 星期--";
    if (valid) {
        constexpr const char* weekdays[] = {"日", "一", "二", "三", "四", "五", "六"};
        caption = clock_date_.substr(5) + " · 星期" + weekdays[local_time.tm_wday];
    }
    SetText(updated_[0], caption.c_str());
    SetText(clock_, valid ? time_text : "--:--");
    for (int i = 1; i < kPageCount; ++i) {
        SetText(updated_[i], valid ? timestamp : "--/-- --:--");
    }
    UpdateForecastTimes();
}

void StopwatchWeather::UpdateForecastTimes() {
    const bool valid = !clock_date_.empty();
    const bool relative = valid && !historical_ &&
                          FormatLocalTime(observed_at_, forecast_offset_, "%Y/%m/%d") == clock_date_;
    bool all_future = valid && !historical_;
    for (int i = 0; i < 4; ++i) {
        const int64_t timestamp = hour_timestamps_[i];
        const bool future = valid && timestamp > clock_now_;
        const bool same_date = valid &&
                               FormatLocalTime(timestamp, forecast_offset_, "%Y/%m/%d") == clock_date_;
        const auto text = timestamp > 0
                              ? FormatLocalTime(timestamp, forecast_offset_,
                                                !historical_ && future && same_date ? "%H:%M"
                                                                                   : "%m/%d %H")
                              : "--:--";
        SetText(hour_times_[i], text.c_str());
        all_future = all_future && future;
    }
    for (int i = 0; i < 3; ++i) {
        const auto text = day_timestamps_[i] > 0
                              ? DayCaption(day_timestamps_[i], forecast_offset_, observed_at_, relative)
                              : "--/--";
        SetText(day_dates_[i], text.c_str());
    }
    SetText(hour_subtitle_, all_future ? "未来 4 小时" : "逐时预报");
    SetText(summaries_[0], (all_future ? hourly_future_summary_ : hourly_summary_).c_str());
}

void StopwatchWeather::RenderSnapshot(const Snapshot& snapshot) {
    const auto& forecast = snapshot.forecast;
    const bool data = snapshot.has_data;
    const bool metadata = forecast.observed_at > 0 &&
                          (data || SameLocation(snapshot.location, forecast.location));
    const bool historical = metadata && (snapshot.cached || snapshot.stale || !snapshot.age_known);
    const auto city = CityName(metadata ? forecast.location : snapshot.location);
    for (auto* label : city_) {
        SetText(label, city.c_str());
    }
    const auto state = std::string(StateText(snapshot));
    observed_at_ = forecast.observed_at;
    forecast_offset_ = forecast.utc_offset;
    historical_ = historical;

    UpdateTemperature(data ? forecast.temperature : kMissing);
    ShowIcon(current_icon_, data ? forecast.code : -1, forecast.is_day);
    std::string condition = std::string(data ? ConditionText(forecast.code, forecast.is_day) : "未知") +
                            " · 体感 " + Number(data ? forecast.feels_like : kMissing, "°");
    const bool exceptional = snapshot.state != State::Ready || snapshot.pending;
    if (!data) {
        condition = state + " · 体感 --°";
    } else if (exceptional) {
        condition = state + " · " + condition;
    }
    SetText(condition_, condition.c_str());
    const auto range = std::string("最高 ") + Number(data ? forecast.days[0].high : kMissing, "°") +
                       "   最低 " + Number(data ? forecast.days[0].low : kMissing, "°");
    SetText(range_, range.c_str());
    SetText(humidity_, Number(data ? forecast.humidity : kMissing, "%").c_str());
    SetText(wind_, Number(data ? forecast.wind : kMissing, " km/h").c_str());
    for (int i = 0; i < 4; ++i) {
        const auto& hour = forecast.hours[i];
        const bool present = data && hour.time > 0;
        hour_timestamps_[i] = present ? hour.time : 0;
        SetText(hour_temperatures_[i], Number(present ? hour.temperature : kMissing, "°").c_str());
        SetText(hour_rain_[i], Number(present ? hour.precipitation : kMissing, "%").c_str());
        ShowIcon(hour_icons_[i], present ? hour.code : -1);
    }
    for (int i = 0; i < 3; ++i) {
        const auto& day = forecast.days[i];
        const bool present = data && day.time > 0;
        day_timestamps_[i] = present ? day.time : 0;
        SetText(day_rain_[i], Number(present ? day.precipitation : kMissing, "%").c_str());
        SetText(day_low_[i], Number(present ? day.low : kMissing, "°").c_str());
        SetText(day_high_[i], Number(present ? day.high : kMissing, "°").c_str());
        ShowIcon(day_icons_[i], present ? day.code : -1);
    }
    const auto summary = data && historical ? std::string("历史预报 · ") + state : state;
    const bool summarize = data && !historical && !exceptional;
    hourly_summary_ = summarize ? RainSummary(forecast.hours, "逐时预报") : summary;
    hourly_future_summary_ = summarize ? RainSummary(forecast.hours, "未来4小时") : summary;
    UpdateForecastTimes();
    SetText(summaries_[1], summarize ? RainSummary(forecast.days, "三日预报").c_str()
                                     : summary.c_str());

    constexpr const char* states[] = {"Starting", "Locating", "Updating", "Ready", "Offline", "Error"};
    data_status_ = metadata
                       ? std::string(historical ? "Historical update:\n" : "Provider update:\n") +
                             FormatLocalTime(forecast.observed_at, forecast.utc_offset,
                                             "%Y/%m/%d %H:%M") + "\n" +
                             FormatUtcOffset(forecast.utc_offset)
                       : "No provider update available";
    data_status_ += std::string("\nState: ") + states[static_cast<int>(snapshot.state)];
    data_status_ += snapshot.automatic ? " / Automatic" : " / Manual";
    data_status_ += !data ? "\nNo values" : historical ? "\nHistorical" : "\nLive forecast";
    data_status_ += snapshot.age_known ? " / age known in this boot" : " / age unknown";
    data_status_ += "\nFixed offset; not a DST rule.";
    if (source_open_) {
        UpdateSource();
    }
}

void StopwatchWeather::UpdateSnapshot(bool force) {
    if (service_ == nullptr) {
        if (force) {
            RenderSnapshot({});
        }
        return;
    }
    const uint32_t version = service_->Version();
    if (!force && has_snapshot_ && version == version_) {
        return;
    }
    const auto snapshot = service_->GetSnapshot();
    version_ = snapshot.version;
    has_snapshot_ = true;
    RenderSnapshot(snapshot);
}

void StopwatchWeather::Tick(lv_timer_t* timer) {
    auto* self = static_cast<StopwatchWeather*>(lv_timer_get_user_data(timer));
    if (self->IsVisible()) {
        self->UpdateClock();
        self->UpdateSnapshot();
    }
}

#endif
