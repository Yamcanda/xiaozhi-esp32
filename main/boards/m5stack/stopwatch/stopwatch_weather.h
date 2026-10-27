#pragma once

#include "sdkconfig.h"

#if CONFIG_STOPWATCH_LOCAL_APPS

#include <lvgl.h>

#include <cstdint>
#include <string>
#include <vector>

#include "display/display.h"
#include "stopwatch_weather_data.h"

class StopwatchWeatherService;

// Board-local design A. The shell owns root visibility and the display lock;
// the service owns all network/storage work and never touches these LVGL objects.
class StopwatchWeather {
public:
    explicit StopwatchWeather(Display* display, StopwatchWeatherService* service = nullptr)
        : display_(display), service_(service) {}
    StopwatchWeather(Display* display, StopwatchWeatherService& service)
        : StopwatchWeather(display, &service) {}
    void Setup(lv_obj_t* parent);
    lv_obj_t* root() const { return root_; }
    void Open();
    void Close();
    void RefreshTheme();
    void HandleButton(int button);

private:
    enum class Ink { Primary, Secondary, Accent, Cloud, Surface, Line };
    enum IconPart { Sun, Partly, Moon, Cloud, Rain, Snow, Fog, Bolt, Unknown, PartCount };
    enum class Indicator { Drop, Wind };
    struct IconSlot {
        lv_obj_t* parts[PartCount] = {};
    };
    struct Paint {
        lv_obj_t* object;
        Ink ink;
        bool text;
    };
    static constexpr int kPageCount = 3;
    static constexpr int kSourceCount = 6;
    Display* display_;
    StopwatchWeatherService* service_;
    bool active_ = false;
    bool has_snapshot_ = false;
    bool source_open_ = false;
    uint32_t version_ = 0;
    int64_t clock_minute_ = -1;
    int64_t clock_now_ = 0;
    std::string clock_date_;
    // Only temporal metadata survives a render; the service still owns snapshots.
    int64_t observed_at_ = 0;
    int forecast_offset_ = 0;
    bool historical_ = false;
    int64_t hour_timestamps_[4] = {};
    int64_t day_timestamps_[3] = {};
    std::string hourly_summary_;
    std::string hourly_future_summary_;
    int page_ = 0;
    int source_page_ = 0;
    lv_obj_t* root_ = nullptr;
    lv_obj_t* pages_[kPageCount] = {};
    lv_obj_t* dot_buttons_[kPageCount] = {};
    lv_obj_t* dots_[kPageCount] = {};
    lv_obj_t* city_[kPageCount] = {};
    lv_obj_t* updated_[kPageCount] = {};
    lv_obj_t* clock_ = nullptr;
    lv_obj_t* summaries_[2] = {};
    std::string data_status_;
    IconSlot current_icon_;
    IconSlot hour_icons_[4];
    IconSlot day_icons_[3];
    lv_obj_t* temperature_ = nullptr;
    lv_obj_t* minus_ = nullptr;
    lv_obj_t* missing_[2] = {};
    lv_obj_t* degree_ = nullptr;
    lv_obj_t* condition_ = nullptr;
    lv_obj_t* range_ = nullptr;
    lv_obj_t* humidity_ = nullptr;
    lv_obj_t* wind_ = nullptr;
    lv_obj_t* hour_subtitle_ = nullptr;
    lv_obj_t* hour_times_[4] = {};
    lv_obj_t* hour_temperatures_[4] = {};
    lv_obj_t* hour_rain_[4] = {};
    lv_obj_t* day_dates_[3] = {};
    lv_obj_t* day_rain_[3] = {};
    lv_obj_t* day_low_[3] = {};
    lv_obj_t* day_high_[3] = {};
    lv_obj_t* source_button_ = nullptr;
    lv_obj_t* source_ = nullptr;
    lv_obj_t* source_title_ = nullptr;
    lv_obj_t* source_body_ = nullptr;
    lv_obj_t* source_number_ = nullptr;
    lv_obj_t* source_controls_[3] = {};
    lv_timer_t* timer_ = nullptr;
    // Populated only during Setup. Refreshes only reuse labels and icon groups.
    std::vector<Paint> paint_;

    void BuildCurrent();
    void BuildHourly();
    void BuildDaily();
    void BuildSource();
    void BuildIcon(IconSlot& slot, lv_obj_t* parent, int x, int y, int size);
    void DrawIndicator(lv_obj_t* parent, Indicator indicator, int x, int y, int size);
    static void ShowIcon(IconSlot& slot, int code, bool is_day = true);
    void UpdateTemperature(float value);
    void UpdateClock();
    void UpdateForecastTimes();
    void UpdateSnapshot(bool force = false);
    void RenderSnapshot(const stopwatch_weather::Snapshot& snapshot);
    void ShowPage(int index);
    void ShowSource(bool open);
    void UpdateSource();
    void Step(int direction);
    bool IsVisible() const;
    bool IsCurrentEvent(lv_event_t* event) const;
    static void WaitPointers();
    lv_obj_t* Text(lv_obj_t* parent, const char* text, int x, int y, int width,
                   const lv_font_t* font, Ink ink = Ink::Primary);
    lv_obj_t* Shape(lv_obj_t* parent, int x, int y, int width, int height, int radius, Ink ink);
    static lv_obj_t* Panel(lv_obj_t* parent);
    static void Tick(lv_timer_t* timer);
    static void Gesture(lv_event_t* event);
    static void Clicked(lv_event_t* event);
};

#endif
