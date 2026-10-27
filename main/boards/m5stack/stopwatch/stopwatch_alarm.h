#pragma once

#include "sdkconfig.h"

#if CONFIG_STOPWATCH_LOCAL_APPS

#include <lvgl.h>

#include <memory>

class Display;
class StopwatchAlarmService;

// Board-local, microphone-free UI. The caller holds the display lock for every
// public method (and destruction); the page timer runs in LVGL's locked context.
class StopwatchAlarm {
public:
    StopwatchAlarm(Display* display, StopwatchAlarmService* service);
    ~StopwatchAlarm();
    StopwatchAlarm(const StopwatchAlarm&) = delete;
    StopwatchAlarm& operator=(const StopwatchAlarm&) = delete;

    void Setup(lv_obj_t* parent);
    lv_obj_t* root() const;
    void Open();
    void Close();
    // A/B traverse controls; Double B activates the focused control.
    void HandleButton(int button);

private:
    struct Ui;
    std::unique_ptr<Ui> ui_;
};

#endif
