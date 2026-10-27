#pragma once

#include "sdkconfig.h"

#if CONFIG_STOPWATCH_LOCAL_APPS

class StopwatchWeatherService;

// Register once for the board lifetime, never for an individual LVGL page.
void RegisterStopwatchWeatherTools(StopwatchWeatherService& service);

#endif
