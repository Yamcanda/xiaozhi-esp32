#include <driver/i2c_master.h>
#include <driver/spi_master.h>
#include <esp_log.h>
#include <wifi_manager.h>
#include <algorithm>
#include <cstring>
#include <mutex>
#include "M5IOE1.h"
#include "M5PM1.h"
#include "application.h"
#include "assets/lang_config.h"
#include "backlight.h"
#include "button.h"
#include "codecs/es8311_audio_codec.h"
#include "config.h"
#include "display/lcd_display.h"
#include "esp_lcd_co5300.h"
#include "lvgl_theme.h"
#include "wifi_board.h"

#if CONFIG_STOPWATCH_LOCAL_APPS
#include "mcp_server.h"
#include "stopwatch_alarm_player.h"
#include "stopwatch_alarm_runtime.h"
#include "stopwatch_alarm_service.h"
#include "stopwatch_alarm_tools.h"
#include "stopwatch_apps.h"
#include "stopwatch_weather_service.h"
#include "stopwatch_weather_tools.h"

#include <atomic>
#include <cstdio>
#include <ctime>

LV_FONT_DECLARE(CommissionerMedium108);
LV_FONT_DECLARE(MontserratSemiBold26);
#endif

#define TAG "M5StackStopwatch"
#define LCD_OPCODE_WRITE_CMD (0x02ULL)

LV_FONT_DECLARE(font_noto_sans_basic_20_4);

// CO5300 AMOLED: initialize at full brightness, then restore the saved setting.
static const co5300_lcd_init_cmd_t vendor_specific_init[] = {
    // {cmd, { data }, data_size, delay_ms}
    {0xFE, (uint8_t[]){0x00}, 0, 0},
    {0xC4, (uint8_t[]){0x80}, 1, 0},
    {0x3A, (uint8_t[]){0x55}, 0, 10},  // RGB565
    {0x35, (uint8_t[]){0x00}, 0, 10},
    {0x53, (uint8_t[]){0x20}, 1, 10},
    {0x51, (uint8_t[]){0xFF}, 1, 10},
    {0x63, (uint8_t[]){0xFF}, 1, 10},
    {0x2A, (uint8_t[]){0x00, 0x00, 0x01, 0xD1}, 4, 0},  // Column address: 0-465
    {0x2B, (uint8_t[]){0x00, 0x00, 0x01, 0xD1}, 4, 0},  // Row address: 0-465
    {0x11, (uint8_t[]){0x00}, 0, 120},                  // Exit sleep
    {0x29, (uint8_t[]){0x00}, 0, 20},                   // Display on
};

class RoundLcdDisplay : public SpiLcdDisplay {
public:
    static void rounder_event_cb(lv_event_t* e) {
        lv_area_t* area = static_cast<lv_area_t*>(lv_event_get_param(e));
        area->x1 = (area->x1 >> 1) << 1;
        area->y1 = (area->y1 >> 1) << 1;
        area->x2 = ((area->x2 >> 1) << 1) + 1;
        area->y2 = ((area->y2 >> 1) << 1) + 1;
    }

    RoundLcdDisplay(esp_lcd_panel_io_handle_t io_handle, esp_lcd_panel_handle_t panel_handle,
                    int width, int height, int offset_x, int offset_y, bool mirror_x, bool mirror_y,
                    bool swap_xy)
        : SpiLcdDisplay(io_handle, panel_handle, width, height, offset_x, offset_y, mirror_x,
                        mirror_y, swap_xy) {}

    void SetupUI() override {
        if (setup_ui_called_) {
            return;
        }
        SpiLcdDisplay::SetupUI();
        DisplayLockGuard lock(this);
        if (!lock) {
            return;
        }

        // Keep Wi-Fi, time/status and battery on one row inside the round mask.
        lv_obj_set_width(status_bar_, LV_HOR_RES - 2 * DISPLAY_ROUND_EDGE_INSET);
        lv_obj_set_style_pad_left(status_bar_, 0, 0);
        lv_obj_set_style_pad_right(status_bar_, 0, 0);
        lv_obj_align(status_bar_, LV_ALIGN_TOP_MID, 0, DISPLAY_STATUS_BAR_TOP_OFF);
        lv_label_set_long_mode(notification_label_, LV_LABEL_LONG_SCROLL_CIRCULAR);
        lv_obj_align(status_label_, LV_ALIGN_CENTER, 0, 0);
        lv_obj_align(notification_label_, LV_ALIGN_CENTER, 0, 0);

        // Move mute beside Wi-Fi to leave room for the battery percentage on the right.
        auto* left_icons = lv_obj_create(status_bar_);
        lv_obj_set_size(left_icons, LV_SIZE_CONTENT, LV_SIZE_CONTENT);
        lv_obj_set_style_bg_opa(left_icons, LV_OPA_TRANSP, 0);
        lv_obj_set_style_border_width(left_icons, 0, 0);
        lv_obj_set_style_pad_all(left_icons, 0, 0);
        lv_obj_set_style_pad_column(left_icons, 4, 0);
        lv_obj_set_flex_flow(left_icons, LV_FLEX_FLOW_ROW);
        lv_obj_set_flex_align(left_icons, LV_FLEX_ALIGN_START, LV_FLEX_ALIGN_CENTER,
                              LV_FLEX_ALIGN_CENTER);
        lv_obj_set_parent(network_label_, left_icons);
        lv_obj_set_parent(mute_label_, left_icons);
        lv_obj_align(left_icons, LV_ALIGN_LEFT_MID, 0, 0);

        auto* right_icons = lv_obj_get_parent(battery_label_);
        lv_obj_set_parent(right_icons, status_bar_);
        lv_obj_set_style_pad_column(right_icons, 4, 0);
        lv_obj_set_style_margin_left(battery_label_, 0, 0);
        battery_percentage_label_ = lv_label_create(right_icons);
        lv_obj_set_style_text_font(battery_percentage_label_, &font_noto_sans_basic_20_4, 0);
        lv_obj_set_style_text_align(battery_percentage_label_, LV_TEXT_ALIGN_RIGHT, 0);
        lv_label_set_text(battery_percentage_label_, "--%");
        lv_obj_move_to_index(battery_percentage_label_, 0);
        lv_obj_align(right_icons, LV_ALIGN_RIGHT_MID, 0, 0);
        lv_obj_add_flag(top_bar_, LV_OBJ_FLAG_HIDDEN);
        UpdateStatusLayout();
        ApplyBatteryColor();

        // Chat/subtitle text: a bit higher, away from bottom arc
        if (bottom_bar_ != nullptr) {
            lv_obj_align(bottom_bar_, LV_ALIGN_BOTTOM_MID, 0, -DISPLAY_CHAT_BAR_BOTTOM_OFF);
            lv_obj_set_width(chat_message_label_, LV_HOR_RES * 0.75);
        }

        // emoji_box_: keep default LV_ALIGN_CENTER from SpiLcdDisplay::SetupUI()

        lv_display_add_event_cb(display_, rounder_event_cb, LV_EVENT_INVALIDATE_AREA, NULL);
#if CONFIG_STOPWATCH_LOCAL_APPS
        // System warnings stay above local pages, regardless of overlay sibling order.
        if (low_battery_popup_ != nullptr) {
            lv_obj_set_parent(low_battery_popup_, lv_display_get_layer_top(display_));
        }
        apps_ = std::make_unique<StopwatchApps>(
            this, [](bool enabled) { Application::GetInstance().SetConversationEnabled(enabled); },
            [this]() { return configure_wifi_ && configure_wifi_(); }, weather_service_,
            alarm_service_);
        apps_->Setup(lv_screen_active());
        SetupAlarmOverlay();
        Application::GetInstance().SetWakeWordActivationHandler(
            [this]() { return ActivateVoiceByWakeWord(); });
#endif
    }

#if CONFIG_STOPWATCH_LOCAL_APPS
    void SetWeatherService(StopwatchWeatherService* service) { weather_service_ = service; }
    void SetAlarmService(StopwatchAlarmService* service) { alarm_service_ = service; }
    void SetAlarmRuntime(StopwatchAlarmRuntime* runtime) { alarm_runtime_ = runtime; }

    bool CanPresentAlarm() {
        DisplayLockGuard lock(this);
        return lock && apps_ != nullptr && apps_->CanPresentAlarm();
    }

    bool IsVoiceForeground() {
        DisplayLockGuard lock(this);
        return lock && apps_ != nullptr && apps_->IsVoiceForeground();
    }

    void RestoreConversationForAlarm() {
        DisplayLockGuard lock(this);
        if (lock && apps_ != nullptr) {
            // Keep the foreground snapshot and policy update atomic with touch navigation.
            Application::GetInstance().SetConversationEnabled(apps_->IsVoiceForeground());
        }
    }

    bool ActivateVoiceForAlarm() {
        DisplayLockGuard lock(this);
        return lock && apps_ != nullptr && apps_->ActivateVoiceForAlarm();
    }

    void PresentAlarm(const std::vector<stopwatch_alarm::Alarm>& alarms, const char* status) {
        DisplayLockGuard lock(this);
        if (!lock || alarm_overlay_ == nullptr || alarms.empty()) {
            return;
        }
        const auto& alarm = alarms.front();
        lv_label_set_text_fmt(alarm_time_, "%02d:%02d", alarm.hour, alarm.minute);
        lv_label_set_text_fmt(alarm_count_, alarms.size() == 1 ? "Alarm" : "%u alarms",
                              static_cast<unsigned>(alarms.size()));
        lv_label_set_text(alarm_status_, status);
        lv_obj_remove_flag(alarm_overlay_, LV_OBJ_FLAG_HIDDEN);
        lv_obj_move_foreground(alarm_overlay_);
    }

    void HideAlarm() {
        DisplayLockGuard lock(this);
        if (lock && alarm_overlay_ != nullptr) {
            lv_obj_add_flag(alarm_overlay_, LV_OBJ_FLAG_HIDDEN);
        }
    }

    bool TakeAlarmAcknowledgement() { return alarm_acknowledged_.exchange(false); }

    void SetWifiConfigCallback(std::function<bool()> callback) {
        DisplayLockGuard lock(this);
        if (lock) {
            configure_wifi_ = std::move(callback);
        }
    }

    bool HandleAppButton(int button) {
        if (alarm_runtime_ != nullptr && alarm_runtime_->IsActive()) {
            alarm_runtime_->Acknowledge();
            return true;
        }
        DisplayLockGuard lock(this);
        return lock && apps_ != nullptr && apps_->HandleButton(button);
    }

    void ShowHome() {
        if (alarm_runtime_ != nullptr) {
            alarm_runtime_->CancelForUser();
        }
        DisplayLockGuard lock(this);
        if (lock && apps_ != nullptr) {
            apps_->ShowHome();
        }
    }

    bool ActivateVoiceByWakeWord() {
        if (alarm_runtime_ != nullptr) {
            alarm_runtime_->CancelForUser();
        }
        DisplayLockGuard lock(this);
        return lock && apps_ != nullptr && apps_->ActivateVoiceByWakeWord();
    }

    bool ScheduleAppNavigation(int index) {
        DisplayLockGuard lock(this);
        if (!lock || apps_ == nullptr) {
            return false;
        }
        const auto generation = apps_->navigation_generation();
        // Tool results are scheduled after their callback returns. Two hops put
        // that send ahead of Home, whose conversation policy may close WebSocket.
        Application::GetInstance().Schedule([this, index, generation]() {
            Application::GetInstance().Schedule([this, index, generation]() {
                {
                    DisplayLockGuard lock(this);
                    if (!lock || apps_ == nullptr || apps_->navigation_generation() != generation) {
                        return;
                    }
                }
                // Cancellation hides the alert through a separately locked hook.
                if (alarm_runtime_ != nullptr) {
                    alarm_runtime_->CancelForUser();
                }
                DisplayLockGuard lock(this);
                if (!lock || apps_ == nullptr || apps_->navigation_generation() != generation) {
                    return;
                }
                const auto result = apps_->NavigateToApp(index);
                if (result == StopwatchApps::NavigationResult::Busy ||
                    result == StopwatchApps::NavigationResult::Unavailable) {
                    ESP_LOGW(TAG, "App navigation not started: %d", static_cast<int>(result));
                }
            });
        });
        return true;
    }

    void SetStatus(const char* status) override {
        SpiLcdDisplay::SetStatus(status);
        ObserveVoiceState();
    }

    bool SetEmotion(const char* emotion) override {
        ObserveVoiceState(emotion != nullptr && strcmp(emotion, "neutral") == 0);
        return SpiLcdDisplay::SetEmotion(emotion);
    }

    void RegisterTouch(i2c_master_dev_handle_t touch) {
        DisplayLockGuard lock(this);
        if (!lock || touch_input_ != nullptr) {
            return;
        }
        touch_device_ = touch;
        touch_input_ = lv_indev_create();
        lv_indev_set_type(touch_input_, LV_INDEV_TYPE_POINTER);
        lv_indev_set_display(touch_input_, display_);
        lv_indev_set_user_data(touch_input_, this);
        lv_indev_set_read_cb(touch_input_, [](lv_indev_t* input, lv_indev_data_t* data) {
            auto* self = static_cast<RoundLcdDisplay*>(lv_indev_get_user_data(input));
            uint8_t reg = 0;
            uint8_t bytes[7] = {};
            bool pressed = false;
            if (i2c_master_transmit_receive(self->touch_device_, &reg, 1, bytes, sizeof(bytes),
                                            20) == ESP_OK) {
                const int event = bytes[3] >> 6;
                const int x = ((bytes[3] & 0x0f) << 8) | bytes[4];
                const int y = ((bytes[5] & 0x0f) << 8) | bytes[6];
                pressed = bytes[2] > 0 && (event == 0 || event == 2) && x < DISPLAY_WIDTH &&
                          y < DISPLAY_HEIGHT;
                if (pressed) {
                    self->touch_point_ = {x, y};
                }
            }
            data->point = self->touch_point_;
            data->state = pressed ? LV_INDEV_STATE_PRESSED : LV_INDEV_STATE_RELEASED;
        });
    }
#endif

    void UpdateStatusBar(bool update_all = false) override {
        SpiLcdDisplay::UpdateStatusBar(update_all);
#if CONFIG_STOPWATCH_LOCAL_APPS
        ObserveVoiceState();
#endif
        int level = 0;
        bool charging = false;
        bool discharging = false;
        bool valid = Board::GetInstance().GetBatteryLevel(level, charging, discharging);

        DisplayLockGuard lock(this);
        if (!lock || battery_percentage_label_ == nullptr) {
            return;
        }
        level = valid ? std::clamp(level, 0, 100) : -1;
        if (level != battery_level_) {
            battery_level_ = level;
            if (valid) {
                lv_label_set_text_fmt(battery_percentage_label_, "%d%%", level);
            } else {
                lv_label_set_text(battery_percentage_label_, "--%");
            }
            ApplyBatteryColor();
        }
    }

    void SetTheme(Theme* theme) override {
        SpiLcdDisplay::SetTheme(theme);
        DisplayLockGuard lock(this);
        if (lock && battery_percentage_label_ != nullptr) {
            UpdateStatusLayout();
            ApplyBatteryColor();
        }
#if CONFIG_STOPWATCH_LOCAL_APPS
        if (lock && apps_ != nullptr) {
            apps_->ApplyTheme();
            apps_->LogThemeState();
        }
#endif
        if (lock && container_ != nullptr) {
            const unsigned bg = static_cast<unsigned>(
                lv_color_to_u32(lv_obj_get_style_bg_color(container_, LV_PART_MAIN)) & 0x00ffffffU);
            const void* image = lv_obj_get_style_bg_image_src(container_, LV_PART_MAIN);
            ESP_LOGI(TAG, "Theme actual: current=%.64s container_bg=#%06x bg_opa=%u bg_image=%p",
                     current_theme_ != nullptr ? current_theme_->name().c_str() : "<none>", bg,
                     static_cast<unsigned>(lv_obj_get_style_bg_opa(container_, LV_PART_MAIN)),
                     const_cast<void*>(image));
        }
    }

private:
#if CONFIG_STOPWATCH_LOCAL_APPS
    std::unique_ptr<StopwatchApps> apps_;
    StopwatchWeatherService* weather_service_ = nullptr;
    StopwatchAlarmService* alarm_service_ = nullptr;
    StopwatchAlarmRuntime* alarm_runtime_ = nullptr;
    lv_obj_t* alarm_overlay_ = nullptr;
    lv_obj_t* alarm_time_ = nullptr;
    lv_obj_t* alarm_count_ = nullptr;
    lv_obj_t* alarm_status_ = nullptr;
    std::atomic<bool> alarm_acknowledged_{false};
    std::function<bool()> configure_wifi_;
    i2c_master_dev_handle_t touch_device_ = nullptr;
    lv_indev_t* touch_input_ = nullptr;
    lv_point_t touch_point_ = {};

    void SetupAlarmOverlay() {
        alarm_overlay_ = lv_obj_create(lv_screen_active());
        lv_obj_remove_style_all(alarm_overlay_);
        lv_obj_set_size(alarm_overlay_, 466, 466);
        lv_obj_set_style_bg_color(alarm_overlay_, lv_color_black(), 0);
        lv_obj_set_style_bg_opa(alarm_overlay_, LV_OPA_COVER, 0);
        lv_obj_set_style_text_color(alarm_overlay_, lv_color_white(), 0);
        lv_obj_remove_flag(alarm_overlay_, LV_OBJ_FLAG_SCROLLABLE);
        lv_obj_add_flag(alarm_overlay_, LV_OBJ_FLAG_CLICKABLE);
        auto label = [this](const char* text, int width, int y, const lv_font_t* font) {
            auto* object = lv_label_create(alarm_overlay_);
            lv_obj_set_width(object, width);
            lv_obj_set_style_text_font(object, font, 0);
            lv_obj_set_style_text_align(object, LV_TEXT_ALIGN_CENTER, 0);
            lv_label_set_text(object, text);
            lv_obj_align(object, LV_ALIGN_TOP_MID, 0, y);
            return object;
        };
        alarm_count_ = label("Alarm", 330, 68, &MontserratSemiBold26);
        alarm_time_ = label("00:00", 420, 117, &CommissionerMedium108);
        alarm_status_ = label("", 350, 235, &font_noto_sans_basic_20_4);
        lv_label_set_long_mode(alarm_status_, LV_LABEL_LONG_WRAP);
        auto* button = lv_button_create(alarm_overlay_);
        lv_obj_set_size(button, 333, 91);
        lv_obj_align(button, LV_ALIGN_TOP_MID, 0, 302);
        lv_obj_set_style_radius(button, LV_RADIUS_CIRCLE, 0);
        lv_obj_set_style_bg_color(button, lv_color_hex(0x4AD78C), 0);
        auto* text = lv_label_create(button);
        lv_obj_set_style_text_font(text, &MontserratSemiBold26, 0);
        lv_obj_set_style_text_color(text, lv_color_hex(0x0F5831), 0);
        lv_label_set_text(text, "OK");
        lv_obj_center(text);
        lv_obj_add_event_cb(
            button,
            [](lv_event_t* event) {
                auto* self = static_cast<RoundLcdDisplay*>(lv_event_get_user_data(event));
                self->alarm_acknowledged_.store(true);
            },
            LV_EVENT_CLICKED, this);
        label("A / B: stop   Hold A+B: Home", 350, 402, &font_noto_sans_basic_20_4);
        lv_obj_add_flag(alarm_overlay_, LV_OBJ_FLAG_HIDDEN);
    }

    void ObserveVoiceState(bool allow_boot_home = false) {
        DisplayLockGuard lock(this);
        if (lock && apps_ != nullptr) {
            const auto state = Application::GetInstance().GetDeviceState();
            apps_->ObserveVoiceState(state, allow_boot_home);
            if (alarm_overlay_ != nullptr &&
                (state == kDeviceStateWifiConfiguring || state == kDeviceStateUpgrading ||
                 state == kDeviceStateFatalError)) {
                lv_obj_add_flag(alarm_overlay_, LV_OBJ_FLAG_HIDDEN);
            }
        }
    }
#endif
    lv_obj_t* battery_percentage_label_ = nullptr;
    int battery_level_ = -1;

    void UpdateStatusLayout() {
        const auto* icon_font = lv_obj_get_style_text_font(battery_label_, LV_PART_MAIN);
        lv_point_t percentage_size;
        lv_text_get_size(&percentage_size, "100%", &font_noto_sans_basic_20_4, 0, 0, LV_COORD_MAX,
                         LV_TEXT_FLAG_NONE);
        lv_obj_set_width(battery_percentage_label_, percentage_size.x);

        // Equal reservations keep the clock centered, even at 100% or while muted.
        const int side_width = std::max(2 * icon_font->line_height + 4,
                                        percentage_size.x + icon_font->line_height + 4) +
                               8;
        const int status_width = LV_HOR_RES - 2 * DISPLAY_ROUND_EDGE_INSET;
        const int center_width = std::max(1, status_width - 2 * side_width);
        lv_obj_set_width(status_label_, center_width);
        lv_obj_set_width(notification_label_, center_width);

#if CONFIG_USE_WECHAT_MESSAGE_STYLE
        // Keep scrolling chat content below the relocated status row.
        lv_obj_update_layout(status_bar_);
        lv_obj_set_style_pad_top(container_,
                                 DISPLAY_STATUS_BAR_TOP_OFF + lv_obj_get_height(status_bar_), 0);
#endif
    }

    void ApplyBatteryColor() {
        auto* theme = static_cast<LvglTheme*>(current_theme_);
        lv_obj_set_style_text_color(battery_percentage_label_, theme->text_color(), 0);
        lv_color_t color = theme->text_color();
        if (battery_level_ >= 0) {
            if (battery_level_ <= 20) {
                color = lv_color_hex(0xD03B3B);
            } else if (battery_level_ <= 50) {
                color = lv_color_hex(theme->name() == "light" ? 0x9A6700 : 0xFAB219);
            } else {
                color = lv_color_hex(0x0CA30C);
            }
        }
        lv_obj_set_style_text_color(battery_label_, color, 0);
    }
};

class StopwatchBacklight : public Backlight {
public:
    StopwatchBacklight(esp_lcd_panel_io_handle_t panel_io, Display* display)
        : panel_io_(panel_io), display_(display) {}

protected:
    void SetBrightnessImpl(uint8_t brightness) override {
        DisplayLockGuard lock(display_);
        uint8_t data[] = {static_cast<uint8_t>((255 * brightness) / 100)};
        int lcd_cmd = 0x51;
        lcd_cmd &= 0xff;
        lcd_cmd <<= 8;
        lcd_cmd |= LCD_OPCODE_WRITE_CMD << 24;

        esp_err_t err = esp_lcd_panel_io_tx_param(panel_io_, lcd_cmd, data, sizeof(data));
        if (err != ESP_OK) {
            ESP_LOGE(TAG, "Failed to set display brightness: %s", esp_err_to_name(err));
        }
    }

private:
    esp_lcd_panel_io_handle_t panel_io_;
    Display* display_;
};

class M5StackStopwatchBoard : public WifiBoard {
private:
    i2c_master_bus_handle_t i2c_bus_;
    M5PM1 pmic_;
    M5IOE1 ioe_;
    Button button1_;
    Button button2_;
#if CONFIG_STOPWATCH_LOCAL_APPS
    StopwatchHomeChord home_chord_;
    StopwatchWeatherService weather_service_;
    StopwatchAlarmService alarm_service_;
    std::unique_ptr<StopwatchAlarmPlayer> alarm_player_;
    std::unique_ptr<StopwatchAlarmRuntime> alarm_runtime_;
    esp_timer_handle_t alarm_timer_ = nullptr;
    std::atomic<bool> alarm_tick_pending_{false};
#endif
    RoundLcdDisplay* display_;
    StopwatchBacklight* backlight_;
    std::mutex battery_mutex_;
    int last_battery_level_ = -1;

    void InitializeI2c() {
        i2c_master_bus_config_t i2c_bus_cfg = {
            .i2c_port = I2C_NUM_0,
            .sda_io_num = AUDIO_CODEC_I2C_SDA_PIN,
            .scl_io_num = AUDIO_CODEC_I2C_SCL_PIN,
            .clk_source = I2C_CLK_SRC_DEFAULT,
            .glitch_ignore_cnt = 7,
            .intr_priority = 0,
            .trans_queue_depth = 0,
            .flags = {.enable_internal_pullup = 1},
        };
        ESP_ERROR_CHECK(i2c_new_master_bus(&i2c_bus_cfg, &i2c_bus_));

        if (ioe_.begin(i2c_bus_, M5IOE1_I2C_ADDR, M5IOE1_I2C_FREQ_100K, M5IOE1_INT_MODE_POLLING) !=
            M5IOE1_OK) {
            ESP_LOGE(TAG, "M5IOE1 begin failed");
            return;
        }

        if (pmic_.begin(i2c_bus_, M5PM1_DEFAULT_ADDR, M5PM1_I2C_FREQ_100K) != M5PM1_OK) {
            ESP_LOGE(TAG, "M5PM1 begin failed");
            return;
        }

        pmic_.setChargeEnable(true);
        pmic_.setBoostEnable(true);
        pmic_.pinMode(PMIC_PIN_CHARGE_STATE, INPUT);
        pmic_.pinMode(PMIC_PIN_CHARGE_PROG, OUTPUT);
        pmic_.digitalWrite(PMIC_PIN_CHARGE_PROG, LOW);  // Set charge current to 425mA

        ioe_.pinMode(IOE_PIN_LCD_POWER, OUTPUT);
        ioe_.setDriveMode(IOE_PIN_LCD_POWER, M5IOE1_DRIVE_PUSHPULL);
        ioe_.digitalWrite(IOE_PIN_LCD_POWER, HIGH);

        ioe_.pinMode(IOE_PIN_LCD_RST, OUTPUT);
        ioe_.setDriveMode(IOE_PIN_LCD_RST, M5IOE1_DRIVE_PUSHPULL);
        ioe_.digitalWrite(IOE_PIN_LCD_RST, HIGH);

        ioe_.pinMode(IOE_PIN_CODEC_POWER, OUTPUT);
        ioe_.setDriveMode(IOE_PIN_CODEC_POWER, M5IOE1_DRIVE_PUSHPULL);
        ioe_.digitalWrite(IOE_PIN_CODEC_POWER, HIGH);

        ioe_.pinMode(IOE_PIN_PA_EN, OUTPUT);
        ioe_.setDriveMode(IOE_PIN_PA_EN, M5IOE1_DRIVE_PUSHPULL);
        ioe_.digitalWrite(IOE_PIN_PA_EN, HIGH);

        ioe_.pinMode(IOE_PIN_MOTOR, OUTPUT);
        ioe_.setDriveMode(IOE_PIN_MOTOR, M5IOE1_DRIVE_PUSHPULL);
        ioe_.digitalWrite(IOE_PIN_MOTOR, LOW);

        vTaskDelay(pdMS_TO_TICKS(100));
    }

    void InitializeSpi() {
        spi_bus_config_t buscfg = {};
        buscfg.sclk_io_num = DISPLAY_QSPI_SCK;
        buscfg.data0_io_num = DISPLAY_QSPI_D0;
        buscfg.data1_io_num = DISPLAY_QSPI_D1;
        buscfg.data2_io_num = DISPLAY_QSPI_D2;
        buscfg.data3_io_num = DISPLAY_QSPI_D3;
        buscfg.max_transfer_sz = DISPLAY_WIDTH * DISPLAY_HEIGHT * sizeof(uint16_t);
        buscfg.flags = SPICOMMON_BUSFLAG_QUAD;
        ESP_ERROR_CHECK(spi_bus_initialize(DISPLAY_QSPI_HOST, &buscfg, SPI_DMA_CH_AUTO));
    }

    void InitializeDisplay() {
        ioe_.digitalWrite(IOE_PIN_LCD_RST, LOW);
        vTaskDelay(pdMS_TO_TICKS(10));
        ioe_.digitalWrite(IOE_PIN_LCD_RST, HIGH);
        vTaskDelay(pdMS_TO_TICKS(120));

        esp_lcd_panel_io_handle_t panel_io = nullptr;
        esp_lcd_panel_handle_t panel = nullptr;

        esp_lcd_panel_io_spi_config_t io_config = {};
        io_config.cs_gpio_num = DISPLAY_QSPI_CS;
        io_config.dc_gpio_num = GPIO_NUM_NC;
        io_config.spi_mode = 0;
        io_config.pclk_hz = 40 * 1000 * 1000;
        io_config.trans_queue_depth = 10;
        io_config.lcd_cmd_bits = 32;
        io_config.lcd_param_bits = 8;
        io_config.flags.quad_mode = true;
        ESP_ERROR_CHECK(esp_lcd_new_panel_io_spi(DISPLAY_QSPI_HOST, &io_config, &panel_io));

        co5300_vendor_config_t vendor_config = {
            .init_cmds = &vendor_specific_init[0],
            .init_cmds_size = sizeof(vendor_specific_init) / sizeof(co5300_lcd_init_cmd_t),
            .flags = {.use_qspi_interface = 1},
        };

        esp_lcd_panel_dev_config_t panel_config = {};
        panel_config.reset_gpio_num = GPIO_NUM_NC;
        panel_config.rgb_ele_order = LCD_RGB_ELEMENT_ORDER_RGB;
        panel_config.bits_per_pixel = 16;
        panel_config.vendor_config = &vendor_config;
        ESP_ERROR_CHECK(esp_lcd_new_panel_co5300(panel_io, &panel_config, &panel));

        esp_lcd_panel_set_gap(panel, 0x06, 0);
        esp_lcd_panel_reset(panel);
        esp_lcd_panel_init(panel);
        esp_lcd_panel_invert_color(panel, false);
        esp_lcd_panel_mirror(panel, DISPLAY_MIRROR_X, DISPLAY_MIRROR_Y);
        esp_lcd_panel_disp_on_off(panel, true);

        display_ = new RoundLcdDisplay(panel_io, panel, DISPLAY_WIDTH, DISPLAY_HEIGHT,
                                       DISPLAY_OFFSET_X, DISPLAY_OFFSET_Y, DISPLAY_MIRROR_X,
                                       DISPLAY_MIRROR_Y, DISPLAY_SWAP_XY);
        backlight_ = new StopwatchBacklight(panel_io, display_);
        backlight_->RestoreBrightness();
    }

#if CONFIG_STOPWATCH_LOCAL_APPS
    void InitializeAlarm() {
        alarm_service_.Initialize();
        alarm_player_ = std::make_unique<StopwatchAlarmPlayer>(
            Application::GetInstance().GetAudioService(),
            [this](bool on) { ioe_.digitalWrite(IOE_PIN_MOTOR, on ? HIGH : LOW); });
        StopwatchAlarmRuntime::Hooks hooks;
        hooks.ready = [this]() { return display_->CanPresentAlarm(); };
        hooks.present = [this](const auto& alarms, const char* status) {
            display_->PresentAlarm(alarms, status);
        };
        hooks.hide = [this]() { display_->HideAlarm(); };
        hooks.suspend_voice = []() { Application::GetInstance().SetConversationEnabled(false); };
        hooks.restore_voice = [this]() { display_->RestoreConversationForAlarm(); };
        hooks.activate_voice = [this]() { return display_->ActivateVoiceForAlarm(); };
        hooks.voice_foreground = [this]() { return display_->IsVoiceForeground(); };
        hooks.audio_quiet = []() {
            auto& app = Application::GetInstance();
            const auto state = app.GetDeviceState();
            return state != kDeviceStateSpeaking && state != kDeviceStateNotifying &&
                   !app.GetAudioService().IsAudioProcessorRunning() &&
                   app.GetAudioService().IsPlaybackIdle();
        };
        hooks.request_text = [](const std::string& text, std::function<void(bool)> completion) {
            return Application::GetInstance().RequestTextInteraction(text, std::move(completion));
        };
        alarm_runtime_ = std::make_unique<StopwatchAlarmRuntime>(alarm_service_, *alarm_player_,
                                                                 std::move(hooks));
        display_->SetAlarmRuntime(alarm_runtime_.get());
        Application::GetInstance().SetListeningStartGuard(
            [this]() { return alarm_runtime_->IsAudioReleased(); });
        const esp_timer_create_args_t timer = {
            .callback =
                [](void* argument) {
                    auto* self = static_cast<M5StackStopwatchBoard*>(argument);
                    // One queued tick at most, even if main is temporarily busy.
                    if (!self->alarm_tick_pending_.exchange(true)) {
                        Application::GetInstance().Schedule([self]() {
                            if (self->display_->TakeAlarmAcknowledgement()) {
                                self->alarm_runtime_->Acknowledge();
                            }
                            self->alarm_runtime_->Tick(time(nullptr), esp_timer_get_time());
                            self->alarm_tick_pending_.store(false);
                        });
                    }
                },
            .arg = this,
            .dispatch_method = ESP_TIMER_TASK,
            .name = "sw_alarm_tick",
            .skip_unhandled_events = true,
        };
        const auto error = esp_timer_create(&timer, &alarm_timer_);
        if (error != ESP_OK || esp_timer_start_periodic(alarm_timer_, 100000) != ESP_OK) {
            ESP_LOGE(TAG, "Cannot start alarm scheduler");
        }
    }

    void InitializeTools() {
        RegisterStopwatchWeatherTools(weather_service_);
        RegisterStopwatchAlarmTools(alarm_service_);
        McpServer::GetInstance().AddTool(
            "self.app.open",
            "Open a StopWatch local app: alarm, badge, settings or weather. Leaves XiaoZhi voice "
            "via Home, "
            "visibly selects the target tile, then enters it. If already in that app, do nothing. "
            "This tool cannot activate voice or start a microphone session. A scheduled request "
            "may be cancelled by Home, a wake word, or system setup/errors.",
            PropertyList({Property("app", kPropertyTypeString).SetMaxLength(8)}),
            [this](const PropertyList& properties) -> ToolResult {
                const auto& name = properties["app"].value<std::string>();
                const int index = name == "badge"      ? 1
                                  : name == "settings" ? 2
                                  : name == "weather"  ? 3
                                  : name == "alarm"    ? 4
                                                       : -1;
                if (index < 0) {
                    return std::unexpected("app must be alarm, badge, settings or weather");
                }
                if (!display_->ScheduleAppNavigation(index)) {
                    return std::unexpected("App UI unavailable");
                }
                return std::string("Navigation scheduled; an already-open target is left unchanged. "
                                   "Busy or unavailable UI will ignore the request.");
            });
    }

    void InitializeTouch() {
        ioe_.pinMode(IOE_PIN_TOUCH_RST, OUTPUT);
        ioe_.setDriveMode(IOE_PIN_TOUCH_RST, M5IOE1_DRIVE_PUSHPULL);
        ioe_.digitalWrite(IOE_PIN_TOUCH_RST, LOW);
        vTaskDelay(pdMS_TO_TICKS(10));
        ioe_.digitalWrite(IOE_PIN_TOUCH_RST, HIGH);
        vTaskDelay(pdMS_TO_TICKS(50));
        i2c_device_config_t config = {};
        config.dev_addr_length = I2C_ADDR_BIT_LEN_7;
        config.device_address = TOUCH_I2C_ADDR;
        config.scl_speed_hz = 100000;
        i2c_master_dev_handle_t touch = nullptr;
        auto error = i2c_master_bus_add_device(i2c_bus_, &config, &touch);
        if (error != ESP_OK) {
            ESP_LOGW(TAG, "Cannot add CST820: %s", esp_err_to_name(error));
            return;
        }
        uint8_t chip_id = 0, version = 0;
        uint8_t reg = 0xa7;
        error = i2c_master_transmit_receive(touch, &reg, 1, &chip_id, 1, 50);
        reg = 0xa9;
        if (error == ESP_OK) {
            error = i2c_master_transmit_receive(touch, &reg, 1, &version, 1, 50);
        }
        if (error != ESP_OK || chip_id == 0 || version == 0) {
            ESP_LOGW(TAG, "CST820 not ready; use A/B navigation");
            i2c_master_bus_rm_device(touch);
            return;
        }
        display_->RegisterTouch(touch);
    }
#endif

    void InitializeButtons() {
        button1_.OnClick([this]() {
            Application::GetInstance().Schedule([this]() {
#if CONFIG_STOPWATCH_LOCAL_APPS
                if (display_->HandleAppButton(0)) {
                    return;
                }
#endif
                auto& app = Application::GetInstance();
                if (app.GetDeviceState() == kDeviceStateStarting &&
                    !WifiManager::GetInstance().IsConnected()) {
                    EnterWifiConfigMode();
                    return;
                }
                app.ToggleChatState();
            });
        });

        button2_.OnClick([this]() {
            Application::GetInstance().Schedule([this]() {
#if CONFIG_STOPWATCH_LOCAL_APPS
                if (display_->HandleAppButton(1)) {
                    return;
                }
#endif
                // Voice app: volume 0 -> 10 -> ... -> 100 -> 0.
                auto* codec = GetAudioCodec();
                int volume = codec->output_volume() + 10;
                if (volume > 100) {
                    volume = 0;
                }
                codec->SetOutputVolume(volume);
                GetDisplay()->ShowNotification(std::string(Lang::Strings::VOLUME) + ":" +
                                               std::to_string(volume) + "%");
            });
        });
#if CONFIG_STOPWATCH_LOCAL_APPS
        // Both keys must reach the long-press threshold; either key alone does nothing.
        button1_.OnPressDown([this]() { home_chord_.Press(0); });
        button2_.OnPressDown([this]() { home_chord_.Press(1); });
        button1_.OnPressUp([this]() { home_chord_.Release(0); });
        button2_.OnPressUp([this]() { home_chord_.Release(1); });
        auto home = [this](int button) {
            if (home_chord_.LongPress(button)) {
                Application::GetInstance().Schedule([this]() { display_->ShowHome(); });
            }
        };
        button1_.OnLongPress([home]() { home(0); });
        button2_.OnLongPress([home]() { home(1); });
        button2_.OnDoubleClick([this]() {
            Application::GetInstance().Schedule([this]() { display_->HandleAppButton(2); });
        });
#endif
    }

public:
    M5StackStopwatchBoard()
        : i2c_bus_(nullptr),
#if CONFIG_STOPWATCH_LOCAL_APPS
          button1_(BUTTON1_GPIO, false, STOPWATCH_HOME_HOLD_MS),
          button2_(BUTTON2_GPIO, false, STOPWATCH_HOME_HOLD_MS),
#else
          button1_(BUTTON1_GPIO),
          button2_(BUTTON2_GPIO),
#endif
          display_(nullptr),
          backlight_(nullptr) {
        InitializeI2c();
        InitializeSpi();
        InitializeDisplay();
#if CONFIG_STOPWATCH_LOCAL_APPS
        display_->SetWeatherService(&weather_service_);
        display_->SetAlarmService(&alarm_service_);
        display_->SetWifiConfigCallback([this]() { return EnterWifiConfigMode(); });
        InitializeTouch();
#endif
        InitializeButtons();
#if CONFIG_STOPWATCH_LOCAL_APPS
        InitializeTools();
        InitializeAlarm();
#endif
    }

    AudioCodec* GetAudioCodec() override {
        static Es8311AudioCodec audio_codec(
            i2c_bus_, I2C_NUM_0, AUDIO_INPUT_SAMPLE_RATE, AUDIO_OUTPUT_SAMPLE_RATE,
            AUDIO_I2S_GPIO_MCLK, AUDIO_I2S_GPIO_BCLK, AUDIO_I2S_GPIO_WS, AUDIO_I2S_GPIO_DOUT,
            AUDIO_I2S_GPIO_DIN, AUDIO_CODEC_GPIO_PA, AUDIO_CODEC_ES8311_ADDR, false);
        return &audio_codec;
    }

    Display* GetDisplay() override { return display_; }

    Backlight* GetBacklight() override { return backlight_; }

    bool GetBatteryLevel(int& level, bool& charging, bool& discharging) override {
        std::lock_guard<std::mutex> lock(battery_mutex_);
        uint16_t voltage_mv = 0;
        if (pmic_.readVbat(&voltage_mv) != M5PM1_OK) {
            return false;
        }

        int charge_state_level = pmic_.digitalRead(PMIC_PIN_CHARGE_STATE);
        if (charge_state_level < 0) {
            // Charge status read failed; leave charging/discharging unknown
            charging = false;
            discharging = false;
        } else {
            // M5PM1 charge status is active low: 0 means charging, 1 means not charging
            charging = (charge_state_level == 0);
            discharging = !charging;
        }

        const int BATTERY_MIN_VOLTAGE = 3400;
        const int BATTERY_MAX_VOLTAGE = 4200;
        if (voltage_mv < BATTERY_MIN_VOLTAGE) {
            level = 0;
        } else if (voltage_mv > BATTERY_MAX_VOLTAGE) {
            level = 100;
        } else {
            level = ((voltage_mv - BATTERY_MIN_VOLTAGE) * 100) /
                    (BATTERY_MAX_VOLTAGE - BATTERY_MIN_VOLTAGE);
        }

        // Ignore voltage rebound unless charging is confirmed. Keep history across read errors.
        if (last_battery_level_ >= 0 && !charging) {
            level = std::min(level, last_battery_level_);
        }
        last_battery_level_ = level;
        return true;
    }
};

DECLARE_BOARD(M5StackStopwatchBoard);
