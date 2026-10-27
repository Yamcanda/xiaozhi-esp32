#pragma once

#include "sdkconfig.h"

#if CONFIG_STOPWATCH_LOCAL_APPS

#include <freertos/FreeRTOS.h>
#include <freertos/queue.h>
#include <lvgl.h>

#include <atomic>
#include <ctime>
#include <functional>
#include <memory>

#include "device_state.h"
#include "display/display.h"
#include "lvgl_image.h"
#include "stopwatch_alarm.h"
#include "stopwatch_app_state.h"
#include "stopwatch_badge.h"
#include "stopwatch_settings.h"
#include "stopwatch_weather.h"

// All widgets and page state are protected by the owning display's LVGL lock.
class StopwatchApps {
public:
    explicit StopwatchApps(Display* display, std::function<void(bool)> wake_policy = {},
                           std::function<bool()> configure_wifi = {},
                           StopwatchWeatherService* weather_service = nullptr,
                           StopwatchAlarmService* alarm_service = nullptr);
    void Setup(lv_obj_t* screen);
    bool HandleButton(int button);
    void ShowHome();
    bool ActivateVoiceByWakeWord();
    bool ActivateVoiceForAlarm();
    bool CanPresentAlarm() const { return CanNavigate(); }
    bool IsVoiceForeground() const {
        return CanNavigate() && state_.page() == StopwatchAppState::Page::Voice;
    }
    enum class NavigationResult { Accepted, AlreadyOpen, Busy, Unavailable };
    NavigationResult NavigateToApp(int index);
    uint32_t navigation_generation() const { return navigation_generation_; }
    void ObserveVoiceState(DeviceState state, bool allow_boot_home = false);
    void ApplyTheme();
    void LogThemeState() const;

private:
    enum class Work { Initialize, Load, Previous, Next, StartEditor, StopEditor };
    Display* display_;
    std::function<void(bool)> wake_policy_;
    StopwatchAppState state_;
    StopwatchSettings settings_;
    StopwatchWeather weather_;
    StopwatchAlarm alarm_;
    StopwatchBadgeStore store_;
    StopwatchBadgeEditor editor_;
    QueueHandle_t work_queue_ = nullptr;
    std::atomic<bool> badge_changed_{false};
    std::atomic<bool> editor_requested_{false};
    std::unique_ptr<LvglImage> badge_image_;
    DeviceState last_voice_state_ = kDeviceStateUnknown;
    bool boot_home_pending_ = true;
    bool editing_ = false;
    lv_obj_t* overlay_ = nullptr;
    lv_obj_t* home_ = nullptr;
    lv_obj_t* home_icons_[StopwatchAppState::kAppCount] = {};
    lv_obj_t* home_dots_[StopwatchAppState::kAppCount] = {};
    lv_obj_t* home_name_ = nullptr;
    lv_obj_t* home_clock_ = nullptr;
    time_t home_clock_minute_ = -1;
    int home_from_ = 0;
    int home_direction_ = 1;
    bool home_animating_ = false;
    int navigation_target_ = -1;
    uint32_t navigation_tick_ = 0;
    uint32_t navigation_generation_ = 1;
    lv_obj_t* badge_ = nullptr;
    lv_obj_t* badge_picture_ = nullptr;
    lv_obj_t* badge_hint_ = nullptr;
    lv_obj_t* editor_panel_ = nullptr;
    lv_obj_t* editor_url_ = nullptr;

    bool QueueWork(Work work);
    void Worker();
    void RefreshBadge();
    void SetWakeWordEnabled(bool enabled);
    void OpenSelected();
    void AdvanceNavigation();
    bool CanNavigate() const;
    void Step(int direction);
    void RenderPage();
    void SetupHome();
    void RenderHome();
    void AnimateHome(int direction);
    void UpdateHomeClock();
    static void DrawHomeIcon(lv_obj_t* parent, int index);
    static void HomeAnimation(void* user_data, int32_t progress);
    void StartEditor();
    void StopEditor();
    static void Tick(lv_timer_t* timer);
    static void Gesture(lv_event_t* event);
    static lv_obj_t* Panel(lv_obj_t* parent);
    static lv_obj_t* Label(lv_obj_t* parent, const char* text, int width, int y);
    lv_obj_t* Action(lv_obj_t* parent, const char* text, int width, int height, int x, int y,
                     lv_event_cb_t callback);
};

#endif
