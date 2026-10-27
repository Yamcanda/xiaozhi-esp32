#pragma once

#include "sdkconfig.h"

#if CONFIG_STOPWATCH_LOCAL_APPS

class StopwatchAlarmService;

// Register once for the board lifetime. Tools enqueue on the shared service;
// callbacks never write NVS, navigate the display or start audio themselves.
void RegisterStopwatchAlarmTools(StopwatchAlarmService& service);

#endif  // CONFIG_STOPWATCH_LOCAL_APPS
