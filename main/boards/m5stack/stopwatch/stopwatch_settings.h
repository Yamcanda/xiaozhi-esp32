#pragma once

#include "sdkconfig.h"

#if CONFIG_STOPWATCH_LOCAL_APPS

#include <lvgl.h>

#include <array>
#include <functional>
#include <string>

#include "device_state.h"
#include "display/display.h"
#include "stopwatch_weather_data.h"

class StopwatchWeatherService;

// Like StopwatchApps, all widgets and navigation are protected by the display lock.
class StopwatchSettings {
public:
    explicit StopwatchSettings(Display* display, std::function<void()> home = {},
                               std::function<bool()> configure_wifi = {},
                               StopwatchWeatherService* weather_service = nullptr);
    void Setup(lv_obj_t* parent);
    lv_obj_t* root() const { return root_; }
    void Open();
    void Close();
    void ObserveVoiceState(DeviceState state);
    void RefreshTheme();
    void HandleButton(int button);

private:
    enum class Page {
        Menu, Brightness, Volume, Theme, Wifi, About,
        Location, CityList, CitySearch, CityResults, CityConfirm
    };
    enum class CityAction {
        Automatic, Common, Recent, Search, Back, SelectCity,
        Character, KeyboardPage, Delete, Submit, Candidate, Confirm
    };
    struct CityControl {
        lv_obj_t* object = nullptr;
        CityAction action = CityAction::Back;
        int argument = 0;
        unsigned generation = 0;
    };
    // Keep the persistent menu's original semantic indices; Weather is displayed
    // under Device, not appended visually after Home.
    static constexpr int kMenuItems = 7;
    static constexpr int kHomeItem = 5;
    static constexpr int kLocationItem = 6;
    Display* display_;
    std::function<void()> home_;
    std::function<bool()> configure_wifi_;
    StopwatchWeatherService* weather_service_;
    Page page_ = Page::Menu;
    bool active_ = false;
    bool commit_pending_ = false;
    bool wifi_config_pending_ = false;
    unsigned generation_ = 0;
    int selection_ = 0;
    int value_ = 0;
    int initial_value_ = 0;
    lv_obj_t* root_ = nullptr;
    lv_obj_t* menu_ = nullptr;
    lv_obj_t* detail_ = nullptr;
    lv_obj_t* menu_items_[kMenuItems] = {};
    lv_obj_t* theme_summary_ = nullptr;
    lv_obj_t* value_label_ = nullptr;
    lv_obj_t* slider_ = nullptr;
    lv_obj_t* theme_options_[2] = {};
    lv_obj_t* wifi_status_ = nullptr;
    lv_obj_t* primary_ = nullptr;
    lv_timer_t* timer_ = nullptr;

    stopwatch_weather::Snapshot city_snapshot_;
    stopwatch_weather::SearchResult city_search_;
    stopwatch_weather::Location city_draft_;
    uint32_t city_version_ = 0;
    uint32_t city_revision_ = 0;
    uint32_t city_request_ = 0;
    uint32_t city_search_version_ = 0;
    unsigned city_request_generation_ = 0;
    unsigned city_search_generation_ = 0;
    bool city_automatic_ = true;
    bool city_edited_ = false;
    bool city_conflict_ = false;
    bool city_commit_pending_ = false;
    bool city_recent_ = false;
    int city_list_count_ = 0;
    uint32_t city_list_revision_ = 0;
    std::array<stopwatch_weather::Location, stopwatch_weather::kCityCount + 1> city_list_{};
    int city_keyboard_page_ = 0;
    int city_focus_ = 0;
    int city_control_count_ = 0;
    std::string city_query_;
    std::string city_notice_;
    std::array<CityControl, 24> city_controls_{};
    lv_obj_t* city_mode_label_ = nullptr;
    lv_obj_t* city_name_label_ = nullptr;
    lv_obj_t* city_identity_label_ = nullptr;
    lv_obj_t* city_status_label_ = nullptr;
    lv_obj_t* city_query_label_ = nullptr;
    lv_obj_t* city_confirm_ = nullptr;
    lv_obj_t* city_list_view_ = nullptr;

    bool IsCurrentEvent(lv_event_t* event) const;
    void BuildMenu();
    void ShowMenu();
    void SelectMenu(int index, bool scroll);
    void StepMenu(int direction);
    void ActivateMenu();
    void ShowDetail(Page page);
    void ClearDetailPointers();
    void SetValue(int value);
    void RenderTheme();
    void UpdateWifiStatus();
    void Commit();
    void RequestWifiConfig();
    void Defer(std::function<void()> action);

    bool IsCityPage() const;
    void BeginCityPage(Page page);
    void ShowLocation(const std::string& notice = "");
    void ShowCityList(bool recent);
    void ShowCitySearch();
    void ShowCityResults();
    void ShowCityConfirm(bool automatic, const stopwatch_weather::Location& location,
                         uint32_t revision);
    void RenderCityConfirm();
    void PollCity();
    void RenderCityStatus();
    void FocusCity(int index, bool scroll = false);
    void ActivateCity(CityAction action, int argument);
    void SubmitCitySearch();
    void CancelCitySearch();
    void CommitCity();
    static void WaitForPointerRelease();
    static void CityClicked(lv_event_t* event);
    lv_obj_t* CityButton(const char* text, int x, int y, int width, int height,
                         CityAction action, int argument = 0, bool primary = false,
                         lv_obj_t* parent = nullptr);
    lv_obj_t* CityRow(const stopwatch_weather::Location& location, int y,
                      CityAction action, int argument, lv_obj_t* parent = nullptr);
    static void Tick(lv_timer_t* timer);
    static void MenuClicked(lv_event_t* event);
    static lv_obj_t* Panel(lv_obj_t* parent);
    static lv_obj_t* Label(lv_obj_t* parent, const char* text, int width, int y,
                           const lv_font_t* font);
    lv_obj_t* Button(lv_obj_t* parent, const char* text, int y, int height, lv_event_cb_t callback,
                     bool primary = false);
};

#endif
