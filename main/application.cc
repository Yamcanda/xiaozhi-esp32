#include "application.h"
#include "assets.h"
#include "assets/lang_config.h"
#include "audio_codec.h"
#include "board.h"
#include "cjson_utils.h"
#include "display.h"
#include "mcp_server.h"
#include "mqtt_protocol.h"
#include "settings.h"
#include "system_info.h"
#include "text_glyph_payload.h"
#include "websocket_protocol.h"

#include <driver/gpio.h>
#include <esp_log.h>
#include <arpa/inet.h>
#include <cJSON.h>
#include <algorithm>
#include <cstring>
#include <limits>
#include <new>

#define TAG "Application"

Application::Application() : notify_player_(audio_service_) {
    event_group_ = xEventGroupCreate();

#if CONFIG_USE_DEVICE_AEC && CONFIG_USE_SERVER_AEC
#error "CONFIG_USE_DEVICE_AEC and CONFIG_USE_SERVER_AEC cannot be enabled at the same time"
#elif CONFIG_USE_DEVICE_AEC
    aec_mode_ = kAecOnDeviceSide;
#elif CONFIG_USE_SERVER_AEC
    aec_mode_ = kAecOnServerSide;
#else
    aec_mode_ = kAecOff;
#endif

    esp_timer_create_args_t clock_timer_args = {.callback =
                                                    [](void* arg) {
                                                        Application* app = (Application*)arg;
                                                        xEventGroupSetBits(app->event_group_,
                                                                           MAIN_EVENT_CLOCK_TICK);
                                                    },
                                                .arg = this,
                                                .dispatch_method = ESP_TIMER_TASK,
                                                .name = "clock_timer",
                                                .skip_unhandled_events = true};
    esp_timer_create(&clock_timer_args, &clock_timer_handle_);
}

Application::~Application() {
    notify_player_.Stop();
    if (clock_timer_handle_ != nullptr) {
        esp_timer_stop(clock_timer_handle_);
        esp_timer_delete(clock_timer_handle_);
    }
    if (startup_wake_word_hint_timer_ != nullptr) {
        esp_timer_stop(startup_wake_word_hint_timer_);
        esp_timer_delete(startup_wake_word_hint_timer_);
    }
    vEventGroupDelete(event_group_);
}

bool Application::SetDeviceState(DeviceState state) {
    if (!state_machine_.TransitionTo(state)) {
        return false;
    }
    if (state != kDeviceStateConnecting) {
        wake_word_invocation_policy_ = 0;
    }
    return true;
}

void Application::Initialize() {
    auto& board = Board::GetInstance();
    SetDeviceState(kDeviceStateStarting);

    // Setup the display
    auto display = board.GetDisplay();
    display->SetupUI();
    // Print board name/version info
    display->SetChatMessage("system", SystemInfo::GetUserAgent().c_str());

    // Setup the audio service
    auto codec = board.GetAudioCodec();
    audio_service_.Initialize(codec);
    audio_service_.Start();
    ESP_LOGI(TAG, "After board/audio init");
    SystemInfo::PrintHeapStats();

    AudioServiceCallbacks callbacks;
    callbacks.on_send_queue_available = [this]() {
        xEventGroupSetBits(event_group_, MAIN_EVENT_SEND_AUDIO);
    };
    callbacks.on_wake_word_detected = [this](const std::string& wake_word, uint32_t policy) {
        if (!IsWakeWordDetectionCurrent(policy)) {
            return;
        }
        wake_word_event_policy_.store(policy);
        xEventGroupSetBits(event_group_, MAIN_EVENT_WAKE_WORD_DETECTED);
    };
    callbacks.on_vad_change = [this](bool speaking) {
        xEventGroupSetBits(event_group_, MAIN_EVENT_VAD_CHANGE);
    };
    callbacks.on_playback_drained = [this]() {
        xEventGroupSetBits(event_group_, MAIN_EVENT_PLAYBACK_DRAINED);
    };
    callbacks.on_playback_progress = [this](uint32_t playback_id, uint32_t media_position_ms) {
        notify_player_.OnPlaybackProgress(playback_id, media_position_ms);
    };
    audio_service_.SetCallbacks(callbacks);

    // Add state change listeners
    state_machine_.AddStateChangeListener([this](DeviceState old_state, DeviceState new_state) {
        xEventGroupSetBits(event_group_, MAIN_EVENT_STATE_CHANGED);
    });

    // Start the clock timer to update the status bar
    esp_timer_start_periodic(clock_timer_handle_, 1000000);

    // Add MCP common tools (only once during initialization)
    auto& mcp_server = McpServer::GetInstance();
    mcp_server.AddCommonTools();
    mcp_server.AddUserOnlyTools();
    // Registration is complete here, so this is the only place the whole inventory exists.
    // It is logged because a server that accepts a bounded number of tools drops the rest
    // quietly: the only sign on this device was an alert at boot and a sad face, which is
    // easy to see and hard to connect to a tool list.
    mcp_server.LogToolInventory();

    // Set network event callback for UI updates and network state handling
    board.SetNetworkEventCallback([this](NetworkEvent event, const std::string& data) {
        auto display = Board::GetInstance().GetDisplay();

        switch (event) {
            case NetworkEvent::Scanning:
                display->ShowNotification(Lang::Strings::SCANNING_WIFI, 30000);
                xEventGroupSetBits(event_group_, MAIN_EVENT_NETWORK_DISCONNECTED);
                break;
            case NetworkEvent::Connecting: {
                if (data.empty()) {
                    // Cellular network - registering without carrier info yet
                    display->SetStatus(Lang::Strings::REGISTERING_NETWORK);
                } else {
                    // WiFi or cellular with carrier info
                    std::string msg = Lang::Strings::CONNECT_TO;
                    msg += data;
                    msg += "...";
                    display->ShowNotification(msg.c_str(), 30000);
                }
                break;
            }
            case NetworkEvent::Connected: {
                std::string msg = Lang::Strings::CONNECTED_TO;
                msg += data;
                display->ShowNotification(msg.c_str(), 30000);
                xEventGroupSetBits(event_group_, MAIN_EVENT_NETWORK_CONNECTED);
                break;
            }
            case NetworkEvent::Disconnected:
                xEventGroupSetBits(event_group_, MAIN_EVENT_NETWORK_DISCONNECTED);
                break;
            case NetworkEvent::WifiConfigModeEnter:
                // WiFi config mode enter is handled by WifiBoard internally
                break;
            case NetworkEvent::WifiConfigModeExit:
                // WiFi config mode exit is handled by WifiBoard internally
                break;
            // Cellular modem specific events
            case NetworkEvent::ModemDetecting:
                display->SetStatus(Lang::Strings::DETECTING_MODULE);
                break;
            case NetworkEvent::ModemErrorNoSim:
                Alert(Lang::Strings::ERROR, Lang::Strings::PIN_ERROR, "warning",
                      Lang::Sounds::OGG_ERR_PIN);
                break;
            case NetworkEvent::ModemErrorRegDenied:
                Alert(Lang::Strings::ERROR, Lang::Strings::REG_ERROR, "warning",
                      Lang::Sounds::OGG_ERR_REG);
                break;
            case NetworkEvent::ModemErrorInitFailed:
                Alert(Lang::Strings::ERROR, Lang::Strings::MODEM_INIT_ERROR, "warning",
                      Lang::Sounds::OGG_EXCLAMATION);
                break;
            case NetworkEvent::ModemErrorTimeout:
                display->SetStatus(Lang::Strings::REGISTERING_NETWORK);
                break;
        }
    });

    // Start network asynchronously
    board.StartNetwork();

    // Update the status bar immediately to show the network state
    display->UpdateStatusBar(true);
}

void Application::Run() {
    // Set the priority of the main task to 10
    vTaskPrioritySet(nullptr, 10);

    const EventBits_t ALL_EVENTS =
        MAIN_EVENT_SCHEDULE | MAIN_EVENT_SEND_AUDIO | MAIN_EVENT_WAKE_WORD_DETECTED |
        MAIN_EVENT_VAD_CHANGE | MAIN_EVENT_CLOCK_TICK | MAIN_EVENT_ERROR |
        MAIN_EVENT_NETWORK_CONNECTED | MAIN_EVENT_NETWORK_DISCONNECTED | MAIN_EVENT_TOGGLE_CHAT |
        MAIN_EVENT_START_LISTENING | MAIN_EVENT_STOP_LISTENING | MAIN_EVENT_ACTIVATION_DONE |
        MAIN_EVENT_STATE_CHANGED | MAIN_EVENT_PLAYBACK_DRAINED | MAIN_EVENT_STARTUP_HINT_TIMEOUT;

    while (true) {
        auto bits = xEventGroupWaitBits(event_group_, ALL_EVENTS, pdTRUE, pdFALSE, portMAX_DELAY);

        if (bits & MAIN_EVENT_ERROR) {
            FinishTextInteraction(false);
            if (GetDeviceState() == kDeviceStateNotifying) {
                StopNotification();
            }
            SetDeviceState(kDeviceStateIdle);
            Alert(Lang::Strings::ERROR, last_error_message_.c_str(), "cancel",
                  Lang::Sounds::OGG_EXCLAMATION);
        }

        if (bits & MAIN_EVENT_NETWORK_CONNECTED) {
            HandleNetworkConnectedEvent();
        }

        if (bits & MAIN_EVENT_NETWORK_DISCONNECTED) {
            HandleNetworkDisconnectedEvent();
        }

        if (bits & MAIN_EVENT_ACTIVATION_DONE) {
            HandleActivationDoneEvent();
        }

        if (bits & MAIN_EVENT_STATE_CHANGED) {
            HandleStateChangedEvent();
        }

        if (bits & MAIN_EVENT_PLAYBACK_DRAINED) {
            if (audio_service_.IsPlaybackIdle()) {
                notify_player_.OnPlaybackDrained();
            }
            FinishConversationDrain();
            ResumeListeningAudio();
        }

        if (bits & MAIN_EVENT_TOGGLE_CHAT) {
            HandleToggleChatEvent();
        }

        if (bits & MAIN_EVENT_START_LISTENING) {
            HandleStartListeningEvent();
        }

        if (bits & MAIN_EVENT_STOP_LISTENING) {
            HandleStopListeningEvent();
        }

        if (bits & MAIN_EVENT_SEND_AUDIO) {
            while (auto packet = audio_service_.PopPacketFromSendQueue()) {
                if (protocol_opening_.load() || text_interaction_muted_ ||
                    !IsConversationCurrent(packet->conversation_policy)) {
                    continue;
                }
                if (protocol_ && !protocol_->SendAudio(std::move(packet))) {
                    // Drop the remaining packets. Leaving them in the queue would
                    // stall the Opus codec task (it waits for queue space), which in
                    // turn deadlocks the whole audio input pipeline, as no new
                    // MAIN_EVENT_SEND_AUDIO event would ever be triggered again.
                    while (audio_service_.PopPacketFromSendQueue())
                        ;
                    break;
                }
            }
        }

        if (bits & MAIN_EVENT_WAKE_WORD_DETECTED) {
            HandleWakeWordDetectedEvent();
        }

        if (bits & MAIN_EVENT_VAD_CHANGE) {
            if (GetDeviceState() == kDeviceStateListening) {
                auto led = Board::GetInstance().GetLed();
                led->OnStateChanged();
            }
        }

        if (bits & MAIN_EVENT_SCHEDULE) {
            std::unique_lock<std::mutex> lock(mutex_);
            auto tasks = std::move(main_tasks_);
            lock.unlock();
            for (auto& task : tasks) {
                task();
            }
        }

        if (bits & MAIN_EVENT_CLOCK_TICK) {
            ResumeTextInteraction();
            FinishConversationDrain();
            ResumeListeningAudio();
            if (!protocol_closing_.load() && conversation_ready_policy_.load() == 0 &&
                draining_channel_generation_.load() == 0) {
                CloseConversationChannel();
            }
            clock_ticks_++;
            auto display = Board::GetInstance().GetDisplay();
            display->UpdateStatusBar();

            // Print debug info every 10 seconds
            if (clock_ticks_ % 10 == 0) {
                SystemInfo::PrintHeapStats();
                // SystemInfo::PrintTaskList();
                // SystemInfo::PrintTaskCpuUsage(pdMS_TO_TICKS(1000));
            }
        }
        if (bits & MAIN_EVENT_STARTUP_HINT_TIMEOUT) {
            HideStartupWakeWordHint();
        }
        ShowStartupWakeWordHint();
    }
}

void Application::HandleNetworkConnectedEvent() {
    ESP_LOGI(TAG, "Network connected");
    auto state = GetDeviceState();

    if (state == kDeviceStateStarting || state == kDeviceStateWifiConfiguring) {
        // Network is ready, start activation
        SetDeviceState(kDeviceStateActivating);
        if (activation_task_handle_ != nullptr) {
            ESP_LOGW(TAG, "Activation task already running");
            return;
        }

        xTaskCreate(
            [](void* arg) {
                Application* app = static_cast<Application*>(arg);
                app->ActivationTask();
                app->activation_task_handle_ = nullptr;
                vTaskDelete(NULL);
            },
            "activation", 4096 * 2, this, 2, &activation_task_handle_);
    }

    // Update the status bar immediately to show the network state
    auto display = Board::GetInstance().GetDisplay();
    display->UpdateStatusBar(true);
}

void Application::HandleNetworkDisconnectedEvent() {
    FinishTextInteraction(false);
    // Close current conversation when network disconnected
    auto state = GetDeviceState();
    if (state == kDeviceStateNotifying) {
        StopNotification();
    }
    if (state == kDeviceStateConnecting || state == kDeviceStateListening ||
        state == kDeviceStateSpeaking) {
        ESP_LOGI(TAG, "Closing audio channel due to network disconnection");
        if (protocol_opening_.load() || conversation_policy_.load() != 1 ||
            protocol_closing_.load() || (protocol_ && protocol_->UsesConversationPolicy())) {
            draining_channel_generation_.store(0);
            CloseConversationChannel();
            SetDeviceState(kDeviceStateIdle);
        } else if (protocol_) {
            CloseAudioChannel();
        }
    }

    // Update the status bar immediately to show the network state
    auto display = Board::GetInstance().GetDisplay();
    display->UpdateStatusBar(true);
}

void Application::HandleActivationDoneEvent() {
    ESP_LOGI(TAG, "Activation done");
    protocol_initialization_pending_ = false;

    SystemInfo::PrintHeapStats();
    SetDeviceState(kDeviceStateIdle);

    has_server_time_ = ota_->HasServerTime();

    // Protocol start may have already raised MAIN_EVENT_ERROR. Do not replace
    // that alert with the "ready" UI/sound — the main loop can process both
    // events back-to-back because the activation task is lower priority.
    const bool has_error = !last_error_message_.empty();
    if (!startup_completed_) {
        startup_completed_ = true;
        pending_startup_wake_word_hint_ = !has_error;
    }
    if (!has_error) {
        auto display = Board::GetInstance().GetDisplay();
        std::string message = std::string(Lang::Strings::VERSION) + ota_->GetCurrentVersion();
        display->ShowNotification(message.c_str());
        display->SetChatMessage("system", "");
    }

    // Release OTA object after activation is complete
    ota_.reset();
    auto& board = Board::GetInstance();
    board.SetPowerSaveLevel(PowerSaveLevel::LOW_POWER);

    if (!has_error && text_interaction_id_ == 0) {
        Schedule([this]() {
            // Do not queue a local cue over an assistant-first reply.
            if (text_interaction_muted_) {
                return;
            }
            // Play the success sound to indicate the device is ready
            audio_service_.PlaySound(Lang::Sounds::OGG_SUCCESS);
        });
    }
    // Policy/reset changes during initialization must retire the completed transport,
    // never the object still being published and started by the worker.
    if (conversation_ready_policy_.load() == 0 || reset_protocol_pending_.load()) {
        CloseConversationChannel();
    } else {
        ResumeTextInteraction();
        FlushDeferredMcpMessages();
        ResumeWakeWordActivation();
    }
}

void Application::ActivationTask() {
    // Create OTA object for activation process
    ota_ = std::make_unique<Ota>();

    // Check for new assets version
    CheckAssetsVersion();

    // Check for new firmware version
    CheckNewVersion();

    // Keep OTA configuration alive until the old transport has been destroyed.
    // Registration and resume are serialized on main, so a close completion
    // cannot race a worker into missing its wakeup. Only this worker waits.
    const auto worker = xTaskGetCurrentTaskHandle();
    do {
        Schedule([this, worker]() {
            protocol_initialization_pending_ = false;
            protocol_initialization_task_ = worker;
            if (reset_protocol_pending_.load()) {
                CloseConversationChannel();
            } else {
                ResumeProtocolInitialization();
            }
        });
        ulTaskNotifyTake(pdTRUE, portMAX_DELAY);
    } while (!InitializeProtocol());

    // Signal completion to main loop only after initialization actually ran.
    xEventGroupSetBits(event_group_, MAIN_EVENT_ACTIVATION_DONE);
}

void Application::CheckAssetsVersion() {
    // Only allow CheckAssetsVersion to be called once
    if (assets_version_checked_) {
        return;
    }
    assets_version_checked_ = true;

    auto& board = Board::GetInstance();
    auto display = board.GetDisplay();
    auto& assets = Assets::GetInstance();

    if (!assets.partition_valid()) {
        ESP_LOGW(TAG, "Assets partition is disabled for board %s", BOARD_NAME);
        return;
    }

    Settings settings("assets", true);
    // Check if there is a new assets need to be downloaded
    std::string download_url = settings.GetString("download_url");

    if (!download_url.empty()) {
        settings.EraseKey("download_url");

        char message[256];
        snprintf(message, sizeof(message), Lang::Strings::FOUND_NEW_ASSETS, download_url.c_str());
        Alert(Lang::Strings::LOADING_ASSETS, message, "cloud_download", Lang::Sounds::OGG_UPGRADE);

        // Wait for the audio service to be idle for 3 seconds
        vTaskDelay(pdMS_TO_TICKS(3000));
        SetDeviceState(kDeviceStateUpgrading);
        board.SetPowerSaveLevel(PowerSaveLevel::PERFORMANCE);
        display->SetChatMessage("system", Lang::Strings::PLEASE_WAIT);

        bool success =
            assets.Download(download_url, [this, display](int progress, size_t speed) -> void {
                char buffer[32];
                snprintf(buffer, sizeof(buffer), "%d%% %uKB/s", progress, speed / 1024);
                Schedule([display, message = std::string(buffer)]() {
                    display->SetChatMessage("system", message.c_str());
                });
            });

        board.SetPowerSaveLevel(PowerSaveLevel::LOW_POWER);
        vTaskDelay(pdMS_TO_TICKS(1000));

        if (!success) {
            Alert(Lang::Strings::ERROR, Lang::Strings::DOWNLOAD_ASSETS_FAILED, "cancel",
                  Lang::Sounds::OGG_EXCLAMATION);
            vTaskDelay(pdMS_TO_TICKS(2000));
            SetDeviceState(kDeviceStateActivating);
            return;
        }
    }

    // Apply assets
    assets.Apply();
    display->SetChatMessage("system", "");
    // "robot_2" is declared only by a board shipping its own pack: esp-vocat maps it to
    // neutral.eaf, and the rgb-matrix display special-cases the string. None of the shared
    // esp_emote_assets packs (320_240, 360_360, 1024_600) declares it, so on every board that
    // uses one this call fails, logs two errors and leaves whatever face was already up.
    // Fall back to an emotion all three packs do declare.
    if (!display->SetEmotion("robot_2")) {
        display->SetEmotion("neutral");
    }
}

void Application::CheckNewVersion() {
    const int MAX_RETRY = 10;
    int retry_count = 0;
    int retry_delay = 10;  // Initial retry delay in seconds

    auto& board = Board::GetInstance();
    while (true) {
        auto display = board.GetDisplay();
        display->SetStatus(Lang::Strings::CHECKING_NEW_VERSION);

        auto check = ota_->CheckVersion();
        if (!check) {
            retry_count++;
            if (retry_count >= MAX_RETRY) {
                ESP_LOGE(TAG, "Too many retries, exit version check");
                return;
            }

            const auto& err = check.error();
            char error_message[160];
            int error_message_length =
                snprintf(error_message, sizeof(error_message), "%s", err.ToString().c_str());
            if (error_message_length < 0 ||
                error_message_length >= static_cast<int>(sizeof(error_message))) {
                snprintf(error_message, sizeof(error_message), "%s", err.Message());
            }

            char buffer[320];
            int alert_message_length =
                snprintf(buffer, sizeof(buffer), Lang::Strings::CHECK_NEW_VERSION_FAILED,
                         retry_delay, error_message);
            if (alert_message_length < 0 ||
                alert_message_length >= static_cast<int>(sizeof(buffer))) {
                snprintf(buffer, sizeof(buffer), "%s", err.Message());
            }
            Alert(Lang::Strings::ERROR, buffer, "cloud_off", Lang::Sounds::OGG_EXCLAMATION);

            ESP_LOGW(TAG, "Check new version failed, retry in %d seconds (%d/%d)", retry_delay,
                     retry_count, MAX_RETRY);
            for (int i = 0; i < retry_delay; i++) {
                vTaskDelay(pdMS_TO_TICKS(1000));
                if (GetDeviceState() == kDeviceStateIdle) {
                    break;
                }
            }
            retry_delay *= 2;  // Double the retry delay
            continue;
        }
        retry_count = 0;
        retry_delay = 10;  // Reset retry delay

        if (ota_->HasNewVersion()) {
            if (UpgradeFirmware(ota_->GetFirmwareUrl(), ota_->GetFirmwareVersion())) {
                return;  // This line will never be reached after reboot
            }
            // If upgrade failed, continue to normal operation
        }

        // No new version, mark the current version as valid
        ota_->MarkCurrentVersionValid();
        if (!ota_->HasActivationCode() && !ota_->HasActivationChallenge()) {
            // Exit the loop if done checking new version
            break;
        }

        display->SetStatus(Lang::Strings::ACTIVATION);
        // Activation code is shown to the user and waiting for the user to input
        if (ota_->HasActivationCode()) {
            ShowActivationCode(ota_->GetActivationCode(), ota_->GetActivationMessage());
        }

        // This will block the loop until the activation is done or timeout
        for (int i = 0; i < 10; ++i) {
            ESP_LOGI(TAG, "Activating... %d/%d", i + 1, 10);
            esp_err_t err = ota_->Activate();
            if (err == ESP_OK) {
                break;
            } else if (err == ESP_ERR_TIMEOUT) {
                vTaskDelay(pdMS_TO_TICKS(3000));
            } else {
                vTaskDelay(pdMS_TO_TICKS(10000));
            }
            if (GetDeviceState() == kDeviceStateIdle) {
                break;
            }
        }
    }
}

void Application::ResumeProtocolInitialization() {
    if (protocol_initialization_task_ != nullptr && !protocol_initialization_pending_ &&
        applied_conversation_policy_ == conversation_policy_.load() && !protocol_opening_.load() &&
        !protocol_closing_.load() && !reset_protocol_pending_.load()) {
        const auto worker = protocol_initialization_task_;
        protocol_initialization_task_ = nullptr;
        protocol_initialization_pending_ = true;
        xTaskNotifyGive(worker);
    }
}

bool Application::InitializeProtocol() {
    if (protocol_opening_.load() || protocol_closing_.load() || reset_protocol_pending_.load()) {
        return false;
    }
    auto& board = Board::GetInstance();
    auto display = board.GetDisplay();

    display->SetStatus(Lang::Strings::LOADING_PROTOCOL);

    if (ota_->HasMqttConfig()) {
        protocol_ = std::make_unique<MqttProtocol>();
    } else if (ota_->HasWebsocketConfig()) {
        protocol_ = std::make_unique<WebsocketProtocol>();
    } else {
        ESP_LOGW(TAG, "No protocol specified in the OTA config, using MQTT");
        protocol_ = std::make_unique<MqttProtocol>();
    }

    protocol_->SetConversationPolicySource(&conversation_policy_);
    const auto protocol_instance = protocol_instance_.fetch_add(1) + 1;
    protocol_->OnConnected([this]() { DismissAlert(); });

    protocol_->OnNetworkError([this, protocol_instance](const std::string& message) {
        const auto conversation = conversation_policy_.load();
        Schedule([this, protocol_instance, conversation, message]() {
            if (protocol_instance != protocol_instance_.load() ||
                !IsConversationCurrent(conversation)) {
                return;
            }
            if (text_interaction_id_ != 0) {
                FinishTextInteraction(false);
                return;
            }
            last_error_message_ = message;
            xEventGroupSetBits(event_group_, MAIN_EVENT_ERROR);
        });
    });

    protocol_->OnIncomingAudio([this](std::unique_ptr<AudioStreamPacket> packet) {
        if (GetDeviceState() == kDeviceStateSpeaking && CanReceiveReply(packet->channel_generation)) {
            if (audio_service_.PushPacketToDecodeQueue(std::move(packet)) &&
                draining_channel_generation_.load() != 0) {
                // This is an inactivity watchdog, not a maximum reply duration.
                reply_drain_deadline_.store(esp_timer_get_time() + 30000000);
            }
        }
    });

    protocol_->OnAudioChannelOpened([this, protocol_instance](uint32_t generation) {
        const auto conversation = conversation_policy_.load();
        Schedule([this, protocol_instance, generation, conversation]() {
            // An async open is committed only after its worker has relinquished
            // ownership. Never publish a stale OnOpened into a new foreground.
            if (protocol_opening_.load() || protocol_instance != protocol_instance_.load() ||
                !IsConversationCurrent(conversation) || !protocol_ ||
                !IsChannelCurrent(generation) || generation != protocol_->channel_generation()) {
                return;
            }
            PublishAudioChannelOpened(generation);
        });
    });

    protocol_->OnAudioChannelClosed([this, protocol_instance](uint32_t generation) {
        const auto conversation = conversation_policy_.load();
        Schedule([this, protocol_instance, generation, conversation]() {
            if (protocol_instance != protocol_instance_.load() ||
                conversation != conversation_policy_.load() || !IsChannelCurrent(generation)) {
                return;
            }
            if (text_interaction_id_ != 0) {
                FinishTextInteraction(false);
                return;
            }
            if (generation == draining_channel_generation_.load()) {
                // Remote end is the reply boundary, not playback completion.
                // Retain the buffered tail and let the normal idle check retire it.
                reply_ended_.store(true);
                FinishConversationDrain();
                return;
            }
            if (conversation_policy_.load() != 1 || protocol_->UsesConversationPolicy()) {
                CloseConversationChannel();
            }
            auto& board = Board::GetInstance();
            board.SetPowerSaveLevel(PowerSaveLevel::LOW_POWER);
            board.GetDisplay()->SetChatMessage("system", "");
            if (GetDeviceState() != kDeviceStateNotifying) {
                SetDeviceState(kDeviceStateIdle);
            }
        });
    });

    protocol_->OnIncomingJson([this, display](const cJSON* root, uint32_t generation) {
        const auto conversation = conversation_policy_.load();
        // Parse JSON data
        auto type = cJSON_GetObjectItem(root, "type");
        if (!cJSON_IsString(type)) {
            ESP_LOGW(TAG, "Incoming JSON message has no type");
            return;
        }
        if (strcmp(type->valuestring, "notify") == 0) {
            auto audio_url = cJSON_GetObjectItem(root, "audio_url");
            if (!cJSON_IsString(audio_url) || audio_url->valuestring[0] == '\0') {
                ESP_LOGW(TAG, "Notify message requires audio_url");
                return;
            }

            std::vector<NotifySubtitle> subtitles;
            auto subtitles_json = cJSON_GetObjectItem(root, "subtitles");
            if (subtitles_json != nullptr && !cJSON_IsArray(subtitles_json)) {
                ESP_LOGW(TAG, "Notify subtitles must be an array");
                return;
            }
            if (cJSON_IsArray(subtitles_json)) {
                cJSON* item = nullptr;
                cJSON_ArrayForEach (item, subtitles_json) {
                    auto start_ms = cJSON_GetObjectItem(item, "start_ms");
                    auto text = cJSON_GetObjectItem(item, "text");
                    if (!cJSON_IsNumber(start_ms) || start_ms->valuedouble < 0 ||
                        start_ms->valuedouble > std::numeric_limits<uint32_t>::max() ||
                        !cJSON_IsString(text)) {
                        ESP_LOGW(TAG, "Ignoring invalid notify subtitle");
                        continue;
                    }
                    subtitles.push_back({.start_ms = static_cast<uint32_t>(start_ms->valuedouble),
                                         .text = text->valuestring});
                }
            }

            Schedule([this, url = std::string(audio_url->valuestring),
                      subtitles = std::move(subtitles)]() mutable {
                StartNotification(std::move(url), std::move(subtitles));
            });
        } else if (strcmp(type->valuestring, "tts") == 0) {
            auto state = cJSON_GetObjectItem(root, "state");
            if (!cJSON_IsString(state)) {
                return;
            }
            if (!IsChannelCurrent(generation)) {
                return;
            }
            if (strcmp(state->valuestring, "start") == 0) {
                if (!IsConversationCurrent(conversation)) {
                    return;
                }
                Schedule([this, conversation, generation]() {
                    if (!IsConversationCurrent(conversation) || !IsChannelCurrent(generation)) {
                        return;
                    }
                    if (text_interaction_id_ != 0 &&
                        esp_timer_get_time() >= text_interaction_deadline_) {
                        FinishTextInteraction(false);
                        return;
                    }
                    if (text_interaction_id_ != 0 &&
                        (!text_interaction_sent_ || generation != text_interaction_generation_)) {
                        return;
                    }
                    reply_ended_.store(false);
                    aborted_ = false;
                    if (SetDeviceState(kDeviceStateSpeaking) && text_interaction_id_ != 0) {
                        text_reply_generation_ = generation;
                        FinishTextInteraction(true);
                    }
                });
            } else if (strcmp(state->valuestring, "stop") == 0) {
                Schedule([this, conversation, generation]() {
                    if (!IsChannelCurrent(generation)) {
                        return;
                    }
                    reply_ended_.store(true);
                    if (!IsConversationAvailable()) {
                        FinishConversationDrain();
                        return;
                    }
                    if (!IsConversationCurrent(conversation)) {
                        return;
                    }
                    if (text_interaction_id_ != 0) {
                        // A stop without start is not a successful spoken reply.
                        FinishTextInteraction(false);
                        return;
                    }
                    if (generation == text_reply_generation_) {
                        text_reply_generation_ = 0;
                        text_interaction_muted_ = false;
                    }
                    if (GetDeviceState() == kDeviceStateSpeaking) {
                        if (listening_mode_ == kListeningModeManualStop) {
                            SetDeviceState(kDeviceStateIdle);
                        } else {
                            SetDeviceState(kDeviceStateListening);
                        }
                    }
                });
            } else if (strcmp(state->valuestring, "sentence_start") == 0) {
                if (!CanReceiveReply(generation)) {
                    return;
                }
                auto text = cJSON_GetObjectItem(root, "text");
                if (cJSON_IsString(text)) {
                    std::vector<TextGlyph> glyphs;
                    uint8_t bpp = 0;
                    if (!TextGlyphPayload::Parse(root, glyphs, bpp)) {
                        glyphs.clear();
                    }
                    ESP_LOGI(TAG, "<< %s", text->valuestring);
                    Schedule([this, display, generation, message = std::string(text->valuestring),
                              glyphs = std::move(glyphs), bpp]() {
                        if (!CanReceiveReply(generation)) {
                            return;
                        }
                        display->AddTextGlyphs(glyphs, bpp);
                        display->SetChatMessage("assistant", message.c_str());
                    });
                }
            }
        } else if (strcmp(type->valuestring, "stt") == 0) {
            if (!IsConversationCurrent(conversation) || !IsChannelCurrent(generation)) {
                return;
            }
            auto text = cJSON_GetObjectItem(root, "text");
            if (cJSON_IsString(text)) {
                std::vector<TextGlyph> glyphs;
                uint8_t bpp = 0;
                if (!TextGlyphPayload::Parse(root, glyphs, bpp)) {
                    glyphs.clear();
                }
                ESP_LOGI(TAG, ">> %s", text->valuestring);
                Schedule([this, display, conversation, generation, message = std::string(text->valuestring),
                          glyphs = std::move(glyphs), bpp]() {
                    if (!IsConversationCurrent(conversation) || !IsChannelCurrent(generation)) {
                        return;
                    }
                    display->AddTextGlyphs(glyphs, bpp);
                    display->SetChatMessage("user", message.c_str());
                });
            }
        } else if (strcmp(type->valuestring, "llm") == 0) {
            auto emotion = cJSON_GetObjectItem(root, "emotion");
            if (cJSON_IsString(emotion)) {
                Schedule([this, display, conversation, generation,
                          emotion_str = std::string(emotion->valuestring)]() {
                    if (!IsConversationCurrent(conversation) || !IsChannelCurrent(generation)) {
                        return;
                    }
                    display->SetEmotion(emotion_str.c_str());
                });
            }
        } else if (strcmp(type->valuestring, "mcp") == 0) {
            auto payload = cJSON_GetObjectItem(root, "payload");
            if (cJSON_IsObject(payload)) {
                McpServer::GetInstance().ParseMessage(payload);
            }
        } else if (strcmp(type->valuestring, "system") == 0) {
            auto command = cJSON_GetObjectItem(root, "command");
            if (cJSON_IsString(command)) {
                ESP_LOGI(TAG, "System command: %s", command->valuestring);
                if (strcmp(command->valuestring, "reboot") == 0) {
                    // Do a reboot if user requests a OTA update
                    Schedule([this]() { Reboot(); });
                } else {
                    ESP_LOGW(TAG, "Unknown system command: %s", command->valuestring);
                }
            }
        } else if (strcmp(type->valuestring, "alert") == 0) {
            auto status = cJSON_GetObjectItem(root, "status");
            auto message = cJSON_GetObjectItem(root, "message");
            auto emotion = cJSON_GetObjectItem(root, "emotion");
            if (cJSON_IsString(status) && cJSON_IsString(message) && cJSON_IsString(emotion)) {
                Alert(status->valuestring, message->valuestring, emotion->valuestring,
                      Lang::Sounds::OGG_VIBRATION);
            } else {
                ESP_LOGW(TAG, "Alert command requires status, message and emotion");
            }
#if CONFIG_RECEIVE_CUSTOM_MESSAGE
        } else if (strcmp(type->valuestring, "custom") == 0) {
            auto payload = cJSON_GetObjectItem(root, "payload");
            CJsonStringUniquePtr root_json(cJSON_PrintUnformatted(root));
            ESP_LOGI(TAG, "Received custom message: %s", root_json ? root_json.get() : "");
            if (cJSON_IsObject(payload)) {
                CJsonStringUniquePtr payload_json(cJSON_PrintUnformatted(payload));
                if (payload_json) {
                    Schedule(
                        [this, display, payload_str = std::string(payload_json.get())]() {
                            display->SetChatMessage("system", payload_str.c_str());
                        });
                }
            } else {
                ESP_LOGW(TAG, "Invalid custom message format: missing payload");
            }
#endif
        } else {
            ESP_LOGW(TAG, "Unknown message type: %s", type->valuestring);
        }
    });

    protocol_->Start();
    return true;
}

void Application::ShowActivationCode(const std::string& code, const std::string& message) {
    struct digit_sound {
        char digit;
        const std::string_view& sound;
    };
    static const std::array<digit_sound, 10> digit_sounds{
        {digit_sound{'0', Lang::Sounds::OGG_0}, digit_sound{'1', Lang::Sounds::OGG_1},
         digit_sound{'2', Lang::Sounds::OGG_2}, digit_sound{'3', Lang::Sounds::OGG_3},
         digit_sound{'4', Lang::Sounds::OGG_4}, digit_sound{'5', Lang::Sounds::OGG_5},
         digit_sound{'6', Lang::Sounds::OGG_6}, digit_sound{'7', Lang::Sounds::OGG_7},
         digit_sound{'8', Lang::Sounds::OGG_8}, digit_sound{'9', Lang::Sounds::OGG_9}}};

    // This sentence uses 9KB of SRAM, so we need to wait for it to finish
    Alert(Lang::Strings::ACTIVATION, message.c_str(), "link", Lang::Sounds::OGG_ACTIVATION);

    for (const auto& digit : code) {
        auto it = std::find_if(digit_sounds.begin(), digit_sounds.end(),
                               [digit](const digit_sound& ds) { return ds.digit == digit; });
        if (it != digit_sounds.end()) {
            audio_service_.PlaySound(it->sound);
        }
    }
}

void Application::Alert(const char* status, const char* message, const char* emotion,
                        const std::string_view& sound) {
    ESP_LOGW(TAG, "Alert [%s] %s: %s", emotion, status, message);
    auto display = Board::GetInstance().GetDisplay();
    display->SetStatus(status);
    display->SetEmotion(emotion);
    display->SetChatMessage("system", message);
    if (!sound.empty()) {
        audio_service_.PlaySound(sound);
    }
}

void Application::DismissAlert() {
    last_error_message_.clear();
    if (GetDeviceState() == kDeviceStateIdle) {
        auto display = Board::GetInstance().GetDisplay();
        display->SetStatus(Lang::Strings::STANDBY);
        display->SetEmotion("neutral");
        display->SetChatMessage("system", "");
    }
}

void Application::PublishAudioChannelOpened(uint32_t generation) {
    active_channel_generation_.store(generation);
    audio_service_.SetReplyChannel(generation);
    auto& board = Board::GetInstance();
    board.SetPowerSaveLevel(PowerSaveLevel::PERFORMANCE);
    auto* codec = board.GetAudioCodec();
    if (codec != nullptr && protocol_->server_sample_rate() != codec->output_sample_rate()) {
        ESP_LOGW(TAG, "Server sample rate %d does not match device output sample rate %d",
                 protocol_->server_sample_rate(), codec->output_sample_rate());
    }
}

bool Application::RequestTextInteraction(const std::string& text,
                                         std::function<void(bool)> completion) {
    const auto policy = conversation_policy_.load();
    const auto state = GetDeviceState();
    if (!(policy & 1U) || !Protocol::IsTextRequestValid(text) || text_interaction_id_ != 0 ||
        protocol_opening_.load() || reset_protocol_pending_.load() ||
        (state != kDeviceStateIdle &&
         !(state == kDeviceStateActivating &&
           (protocol_initialization_pending_ || protocol_initialization_task_ != nullptr)))) {
        return false;
    }
    text_interaction_id_ = ++next_text_interaction_id_;
    if (text_interaction_id_ == 0) {
        text_interaction_id_ = ++next_text_interaction_id_;
    }
    text_interaction_policy_ = policy;
    text_interaction_generation_ = 0;
    text_interaction_deadline_ = esp_timer_get_time() + 30000000;
    text_interaction_needs_close_ = true;
    text_interaction_sent_ = false;
    text_interaction_muted_ = true;
    text_reply_generation_ = 0;
    pending_text_interaction_ = text;
    text_interaction_completion_ = std::move(completion);
    pending_listening_start_ = false;
    play_popup_on_listening_ = false;
    pending_wake_policy_ = 0;
    pending_wake_word_.clear();
    wake_word_invocation_policy_ = 0;
    audio_service_.EnableVoiceProcessing(false);
    audio_service_.EnableWakeWordDetection(false);
    while (audio_service_.PopPacketFromSendQueue()) {
    }
    // Never reuse an old generation: even an idle channel may have a delayed TTS.
    ResumeTextInteraction();
    return true;
}

void Application::ResumeTextInteraction() {
    if (text_interaction_id_ == 0) {
        return;
    }
    const auto state = GetDeviceState();
    if (text_interaction_policy_ != conversation_policy_.load() ||
        !(conversation_policy_.load() & 1U) || reset_protocol_pending_.load() ||
        esp_timer_get_time() >= text_interaction_deadline_ ||
        (state != kDeviceStateIdle && state != kDeviceStateConnecting &&
         !(state == kDeviceStateActivating &&
           (protocol_initialization_pending_ || protocol_initialization_task_ != nullptr)))) {
        FinishTextInteraction(false);
        return;
    }
    if (protocol_opening_.load() || protocol_initialization_pending_ ||
        applied_conversation_policy_ != text_interaction_policy_ ||
        !IsConversationCurrent(text_interaction_policy_) || !protocol_ || text_interaction_sent_) {
        return;
    }
    if (text_interaction_needs_close_) {
        text_interaction_needs_close_ = false;
        CloseConversationChannel();
        return;
    }
    if (state != kDeviceStateIdle) {
        FinishTextInteraction(false);
        return;
    }
    auto* job = new (std::nothrow) TextInteractionOpenJob{
        this, protocol_, text_interaction_id_, text_interaction_policy_, protocol_instance_.load()};
    if (job == nullptr) {
        FinishTextInteraction(false);
        return;
    }
    if (!SetDeviceState(kDeviceStateConnecting)) {
        delete job;
        FinishTextInteraction(false);
        return;
    }
    Board::GetInstance().SetPowerSaveLevel(PowerSaveLevel::PERFORMANCE);
    protocol_open_close_pending_ = false;
    protocol_opening_.store(true);
    protocol_->RequireConversationSession();
    protocol_->SetChannelOpening(true);
    if (xTaskCreate(
            [](void* arg) {
                auto* job = static_cast<TextInteractionOpenJob*>(arg);
                const bool opened = job->protocol->OpenAudioChannel();
                auto* app = job->app;
                const auto request = job->request;
                const auto policy = job->policy;
                const auto instance = job->instance;
                auto* protocol = job->protocol.get();
                // App retains ownership until main receives this completion. Drop
                // the worker's shared_ptr before main may start a serialized close.
                delete job;
                app->Schedule([app, request, policy, instance, protocol, opened]() {
                    app->CompleteTextInteractionOpen(request, policy, instance, protocol, opened);
                });
                vTaskDelete(nullptr);
            },
            "text_interaction_open", 8192, job, 3, nullptr) != pdPASS) {
        delete job;
        protocol_->SetChannelOpening(false);
        protocol_opening_.store(false);
        FinishTextInteraction(false);
    }
}

void Application::CompleteTextInteractionOpen(uint32_t request, uint32_t policy, uint32_t instance,
                                              Protocol* protocol, bool opened) {
    protocol_opening_.store(false);
    if (protocol_ && protocol_.get() == protocol) {
        protocol_->SetChannelOpening(false);
    }
    const bool current = text_interaction_id_ == request && text_interaction_policy_ == policy &&
                         IsConversationCurrent(policy) && applied_conversation_policy_ == policy &&
                         instance == protocol_instance_.load() && protocol_.get() == protocol &&
                         !protocol_open_close_pending_ && !reset_protocol_pending_.load() &&
                         !protocol_initialization_pending_ &&
                         GetDeviceState() == kDeviceStateConnecting &&
                         esp_timer_get_time() < text_interaction_deadline_;
    protocol_open_close_pending_ = false;
    if (!current || !opened || !protocol_->IsAudioChannelOpened()) {
        if (text_interaction_id_ == request) {
            FinishTextInteraction(false);
        } else {
            CloseConversationChannel();
        }
        return;
    }
    // Connecting -> Speaking is illegal. Wait in Idle, without sending listen/start.
    if (!SetDeviceState(kDeviceStateIdle)) {
        FinishTextInteraction(false);
        return;
    }
    listening_mode_ = GetDefaultListeningMode();
    text_interaction_generation_ = protocol_->channel_generation();
    PublishAudioChannelOpened(text_interaction_generation_);
    text_interaction_sent_ = true;
    if (!protocol_->SendTextRequest(pending_text_interaction_)) {
        FinishTextInteraction(false);
        return;
    }
    pending_text_interaction_.clear();
    FlushDeferredMcpMessages();
}

void Application::FinishTextInteraction(bool started) {
    if (text_interaction_id_ == 0) {
        return;
    }
    auto completion = std::move(text_interaction_completion_);
    text_interaction_completion_ = {};
    text_interaction_id_ = 0;
    text_interaction_policy_ = 0;
    text_interaction_deadline_ = 0;
    text_interaction_generation_ = 0;
    text_interaction_needs_close_ = false;
    text_interaction_sent_ = false;
    pending_text_interaction_.clear();
    if (!started) {
        text_reply_generation_ = 0;
        text_interaction_muted_ = false;
        pending_listening_start_ = false;
        play_popup_on_listening_ = false;
        audio_service_.EnableVoiceProcessing(false);
        audio_service_.ResetDecoder();
        if (GetDeviceState() == kDeviceStateConnecting ||
            GetDeviceState() == kDeviceStateSpeaking || GetDeviceState() == kDeviceStateListening) {
            SetDeviceState(kDeviceStateIdle);
        }
        CloseConversationChannel();
    }
    if (completion) {
        completion(started);
    }
}

void Application::ToggleChatState() {
    toggle_chat_policy_.store(conversation_policy_.load());
    xEventGroupSetBits(event_group_, MAIN_EVENT_TOGGLE_CHAT);
}

void Application::StartListening() {
    start_listening_policy_.store(conversation_policy_.load());
    xEventGroupSetBits(event_group_, MAIN_EVENT_START_LISTENING);
}

void Application::StopListening() {
    stop_listening_policy_.store(conversation_policy_.load());
    xEventGroupSetBits(event_group_, MAIN_EVENT_STOP_LISTENING);
}

void Application::HandleToggleChatEvent() {
    if (protocol_opening_.load() || text_interaction_muted_) {
        toggle_chat_policy_.store(0);
        return;
    }
    if (!IsConversationCurrent(toggle_chat_policy_.exchange(0))) {
        return;
    }
    auto state = GetDeviceState();

    if (state == kDeviceStateNotifying) {
        StopNotification();
        state = kDeviceStateIdle;
    }

    if (state == kDeviceStateActivating) {
        SetDeviceState(kDeviceStateIdle);
        return;
    } else if (state == kDeviceStateWifiConfiguring) {
        audio_service_.EnableAudioTesting(true);
        SetDeviceState(kDeviceStateAudioTesting);
        return;
    } else if (state == kDeviceStateAudioTesting) {
        audio_service_.EnableAudioTesting(false);
        SetDeviceState(kDeviceStateWifiConfiguring);
        return;
    }

    if (!protocol_) {
        ESP_LOGE(TAG, "Protocol not initialized");
        return;
    }

    if (state == kDeviceStateIdle) {
        ListeningMode mode = GetDefaultListeningMode();
        if (!protocol_->IsAudioChannelOpened()) {
            open_conversation_policy_ = conversation_policy_.load();
            SetDeviceState(kDeviceStateConnecting);
            // Schedule to let the state change be processed first (UI update)
            const auto policy = open_conversation_policy_;
            Schedule([this, mode, policy]() { ContinueOpenAudioChannel(mode, policy); });
            return;
        }
        SetListeningMode(mode);
    } else if (state == kDeviceStateSpeaking) {
        AbortSpeaking(kAbortReasonNone);
    } else if (state == kDeviceStateListening) {
        CloseAudioChannel();
    }
}

void Application::ContinueOpenAudioChannel(ListeningMode mode, uint32_t conversation) {
    // Check state again in case it was changed during scheduling
    if (protocol_opening_.load() || text_interaction_muted_ || !protocol_ ||
        !IsConversationCurrent(conversation) || GetDeviceState() != kDeviceStateConnecting) {
        return;
    }

    // Switch to performance mode before connecting to reduce latency
    auto& board = Board::GetInstance();
    board.SetPowerSaveLevel(PowerSaveLevel::PERFORMANCE);

    if (!protocol_->IsAudioChannelOpened()) {
        if (!protocol_->OpenAudioChannel()) {
            // Return to idle so the device is not stuck in the connecting
            // state (not every failure path reports a network error)
            SetDeviceState(kDeviceStateIdle);
            return;
        }
    }

    if (!IsConversationCurrent(conversation)) {
        SetDeviceState(kDeviceStateIdle);
        CloseConversationChannel();
        return;
    }
    PublishAudioChannelOpened(protocol_->channel_generation());
    SetListeningMode(mode);
}

void Application::HandleStartListeningEvent() {
    if (protocol_opening_.load() || text_interaction_muted_) {
        start_listening_policy_.store(0);
        return;
    }
    if (!IsConversationCurrent(start_listening_policy_.exchange(0))) {
        return;
    }
    auto state = GetDeviceState();

    if (state == kDeviceStateNotifying) {
        StopNotification();
        state = kDeviceStateIdle;
    }

    if (state == kDeviceStateActivating) {
        SetDeviceState(kDeviceStateIdle);
        return;
    } else if (state == kDeviceStateWifiConfiguring) {
        audio_service_.EnableAudioTesting(true);
        SetDeviceState(kDeviceStateAudioTesting);
        return;
    }

    if (!protocol_) {
        ESP_LOGE(TAG, "Protocol not initialized");
        return;
    }

    if (state == kDeviceStateIdle) {
        if (!protocol_->IsAudioChannelOpened()) {
            open_conversation_policy_ = conversation_policy_.load();
            SetDeviceState(kDeviceStateConnecting);
            // Schedule to let the state change be processed first (UI update)
            const auto policy = open_conversation_policy_;
            Schedule([this, policy]() {
                ContinueOpenAudioChannel(kListeningModeManualStop, policy);
            });
            return;
        }
        SetListeningMode(kListeningModeManualStop);
    } else if (state == kDeviceStateSpeaking) {
        AbortSpeaking(kAbortReasonNone);
        SetListeningMode(kListeningModeManualStop);
    }
}

void Application::HandleStopListeningEvent() {
    if (text_interaction_id_ != 0) {
        stop_listening_policy_.store(0);
        FinishTextInteraction(false);
        return;
    }
    if (protocol_opening_.load() || text_interaction_muted_) {
        stop_listening_policy_.store(0);
        return;
    }
    if (!IsConversationCurrent(stop_listening_policy_.exchange(0))) {
        return;
    }
    auto state = GetDeviceState();

    if (state == kDeviceStateNotifying) {
        StopNotification();
    } else if (state == kDeviceStateAudioTesting) {
        audio_service_.EnableAudioTesting(false);
        SetDeviceState(kDeviceStateWifiConfiguring);
        return;
    } else if (state == kDeviceStateListening) {
        if (protocol_) {
            protocol_->SendStopListening();
        }
        SetDeviceState(kDeviceStateIdle);
    }
}

void Application::HandleWakeWordDetectedEvent() {
    if (protocol_opening_.load() || text_interaction_muted_) {
        wake_word_event_policy_.store(0);
        return;
    }
    // The event may already be in Run()'s local bit snapshot when policy changes.
    const auto policy = wake_word_event_policy_.exchange(0);
    if (!IsWakeWordDetectionCurrent(policy)) {
        return;
    }

    auto state = GetDeviceState();
    auto wake_word = audio_service_.GetLastWakeWord();
    ESP_LOGI(TAG, "Wake word detected: %s (state: %d)", wake_word.c_str(), (int)state);

    if (protocol_initialization_pending_ && (conversation_policy_.load() & 1U) != 0) {
        if (pending_wake_policy_ == 0) {
            pending_wake_word_ = std::move(wake_word);
            pending_wake_policy_ = policy;
        }
        return;
    }
    if (!IsConversationAvailable()) {
        // Never promote public/manual WakeWordInvoke calls, stale events, or a
        // session already being enabled. The board owns foreground UI selection.
        if ((conversation_policy_.load() & 1U) != 0 || !wake_word_activation_handler_ ||
            (state != kDeviceStateIdle && state != kDeviceStateSpeaking &&
             state != kDeviceStateListening && state != kDeviceStateNotifying)) {
            return;
        }
        if (!wake_word_activation_handler_() || (conversation_policy_.load() & 1U) == 0) {
            audio_service_.EnableWakeWordDetection(true);
            return;
        }
        // The accepted handoff deliberately changed the conversation/wake epochs.
        // Retain just this detector event until the old channel finishes retiring.
        pending_wake_word_ = std::move(wake_word);
        pending_wake_policy_ = wake_word_policy_.load();
        Schedule([this]() { ResumeWakeWordActivation(); });
        return;
    }
    // An accepted detector also cancels queued UI navigation while voice is already
    // foreground, without toggling its conversation policy or restarting the UI.
    if (wake_word_activation_handler_ &&
        (state == kDeviceStateIdle || state == kDeviceStateNotifying ||
         state == kDeviceStateSpeaking || state == kDeviceStateListening)) {
        wake_word_activation_handler_();
    }
    if (protocol_opening_.load() || text_interaction_muted_ || !protocol_ ||
        !IsWakeWordInvocationCurrent(policy)) {
        return;
    }
    if (state == kDeviceStateIdle) {
        BeginWakeWordInvoke(wake_word, policy);
    } else if (state == kDeviceStateNotifying) {
        StopNotification();
        BeginWakeWordInvoke(wake_word, policy);
    } else if (state == kDeviceStateSpeaking || state == kDeviceStateListening) {
        AbortSpeaking(kAbortReasonWakeWordDetected);
        // Clear send queue to avoid sending residues to server
        while (audio_service_.PopPacketFromSendQueue())
            ;
        if (!IsWakeWordInvocationCurrent(policy)) {
            return;
        }

        if (state == kDeviceStateListening) {
            if (listening_start_guard_) {
                // A repeated detector must not bypass a deferred listening start.
                listening_mode_ = GetDefaultListeningMode();
                play_popup_on_listening_ = true;
                pending_listening_start_ = true;
                ResumeListeningAudio();
            } else {
                protocol_->SendStartListening(GetDefaultListeningMode());
                audio_service_.ResetDecoder();
                audio_service_.PlaySound(Lang::Sounds::OGG_POPUP);
            }
            // Re-enable wake word detection as it was stopped by the detection itself
            audio_service_.EnableWakeWordDetection(true);
        } else {
            // Play popup sound and start listening again
            play_popup_on_listening_ = true;
            SetListeningMode(GetDefaultListeningMode());
        }
    } else if (state == kDeviceStateActivating) {
        // Restart the activation check if the wake word is detected during activation
        SetDeviceState(kDeviceStateIdle);
    }
}

void Application::SetWakeWordActivationHandler(std::function<bool()> handler) {
    wake_word_activation_handler_ = std::move(handler);
}

bool Application::IsWakeWordDetectionCurrent(uint32_t policy) const {
    return (policy & 1U) != 0 && wake_word_policy_.load() == policy &&
           (IsConversationAvailable() || wake_word_activation_handler_);
}

bool Application::IsWakeWordInvocationCurrent(uint32_t policy) const {
    return IsConversationAvailable() && (policy & 1U) != 0 && wake_word_policy_.load() == policy;
}

void Application::ResumeWakeWordActivation() {
    if (pending_wake_policy_ == 0 || protocol_opening_.load() || text_interaction_muted_) {
        return;
    }
    const auto policy = pending_wake_policy_;
    if (!IsWakeWordDetectionCurrent(policy) || (conversation_policy_.load() & 1U) == 0) {
        pending_wake_policy_ = 0;
        pending_wake_word_.clear();
        return;
    }
    // Close completion can precede Apply in the same scheduled batch; initialization
    // can also still be publishing a replacement. Neither may consume the wake early.
    if (applied_conversation_policy_ != conversation_policy_.load() ||
        protocol_initialization_pending_ || !protocol_ || !IsConversationAvailable()) {
        return;
    }
    pending_wake_policy_ = 0;
    auto wake_word = std::move(pending_wake_word_);
    pending_wake_word_.clear();
    if (GetDeviceState() == kDeviceStateNotifying) {
        StopNotification();
    }
    if (GetDeviceState() == kDeviceStateIdle) {
        BeginWakeWordInvoke(wake_word, policy);
    }
}

void Application::CancelWakeWordInvoke(uint32_t policy) {
    if (wake_word_invocation_policy_ != policy) {
        return;
    }
    wake_word_invocation_policy_ = 0;
    // Only unwind a connection owned by this wake request, never an active conversation.
    if (GetDeviceState() == kDeviceStateConnecting) {
        SetDeviceState(kDeviceStateIdle);
    }
}

void Application::BeginWakeWordInvoke(const std::string& wake_word, uint32_t policy) {
    // Must run in the main task with the device in idle state.
    if (protocol_opening_.load() || text_interaction_muted_ || !protocol_ ||
        !IsWakeWordInvocationCurrent(policy)) {
        return;
    }
    audio_service_.EncodeWakeWord();
    if (!IsWakeWordInvocationCurrent(policy)) {
        return;
    }

    // Always pass through the connecting state, even if the audio channel is
    // already opened. ContinueWakeWordInvoke() rejects any other state, so
    // skipping this transition would silently drop the wake word invocation.
    if (!SetDeviceState(kDeviceStateConnecting)) {
        // Wake word detection was stopped by the detection itself; restore it
        // so the device does not become unresponsive to wake words.
        audio_service_.EnableWakeWordDetection(true);
        return;
    }
    wake_word_invocation_policy_ = policy;
    if (!IsWakeWordInvocationCurrent(policy)) {
        CancelWakeWordInvoke(policy);
        return;
    }

    if (!protocol_->IsAudioChannelOpened()) {
        // Schedule to let the state change be processed first (UI update),
        // then continue with OpenAudioChannel which may block for ~1 second.
        Schedule([this, wake_word, policy]() { ContinueWakeWordInvoke(wake_word, policy); });
        return;
    }
    // Channel already opened, continue directly.
    ContinueWakeWordInvoke(wake_word, policy);
}

void Application::ContinueWakeWordInvoke(const std::string& wake_word, uint32_t policy) {
    if (!IsWakeWordInvocationCurrent(policy)) {
        CancelWakeWordInvoke(policy);
        return;
    }
    // Check state again in case it was changed during scheduling.
    if (protocol_opening_.load() || text_interaction_muted_ || !protocol_ ||
        GetDeviceState() != kDeviceStateConnecting || wake_word_invocation_policy_ != policy) {
        return;
    }

    const auto conversation = conversation_policy_.load();
    // Switch to performance mode before connecting to reduce latency.
    auto& board = Board::GetInstance();
    board.SetPowerSaveLevel(PowerSaveLevel::PERFORMANCE);

    if (!protocol_->IsAudioChannelOpened()) {
        if (!protocol_->OpenAudioChannel()) {
            // Return to idle so the device is not stuck in the connecting
            // state (not every failure path reports a network error), and
            // wake word detection is re-enabled by the idle state handler.
            CancelWakeWordInvoke(policy);
            return;
        }
    }
    // Policy may have changed while OpenAudioChannel blocked. A disable/enable
    // cycle invalidates the request even though the current policy is enabled.
    if (!IsWakeWordInvocationCurrent(policy)) {
        CancelWakeWordInvoke(policy);
        if (!IsConversationCurrent(conversation)) {
            CloseConversationChannel();
        }
        return;
    }
    PublishAudioChannelOpened(protocol_->channel_generation());
    if (GetDeviceState() != kDeviceStateConnecting) {
        return;
    }

    ESP_LOGI(TAG, "Wake word detected: %s", wake_word.c_str());
#if CONFIG_SEND_WAKE_WORD_DATA
    // Encode and send the wake word data to the server.
    while (auto packet = audio_service_.PopWakeWordPacket()) {
        if (!IsWakeWordInvocationCurrent(policy)) {
            CancelWakeWordInvoke(policy);
            return;
        }
        packet->conversation_policy = conversation;
        protocol_->SendAudio(std::move(packet));
    }
    if (!IsWakeWordInvocationCurrent(policy)) {
        CancelWakeWordInvoke(policy);
        return;
    }
    protocol_->SendWakeWordDetected(wake_word);
    if (!IsWakeWordInvocationCurrent(policy)) {
        CancelWakeWordInvoke(policy);
        return;
    }
#else
    // Set flag to play popup sound after state changes to listening.
    // (PlaySound here would be cleared by ResetDecoder in EnableVoiceProcessing.)
    play_popup_on_listening_ = true;
#endif
    wake_word_invocation_policy_ = 0;
    SetListeningMode(GetDefaultListeningMode());
}

void Application::HandleStateChangedEvent() {
    DeviceState new_state = state_machine_.GetState();
    if (text_interaction_id_ != 0 && new_state != kDeviceStateIdle &&
        new_state != kDeviceStateConnecting &&
        !(new_state == kDeviceStateActivating && protocol_initialization_pending_)) {
        FinishTextInteraction(false);
        return;
    }
    clock_ticks_ = 0;
    // Any state change invalidates a pending deferred listening start;
    // the Listening case below re-arms it when needed.
    pending_listening_start_ = false;

    if (!IsConversationAvailable() && text_interaction_id_ == 0 &&
        (new_state == kDeviceStateListening || new_state == kDeviceStateConnecting)) {
        audio_service_.EnableVoiceProcessing(false);
        audio_service_.EnableWakeWordDetection(false);
        pending_listening_start_ = false;
        play_popup_on_listening_ = false;
        SetDeviceState(kDeviceStateIdle);
        return;
    }
    auto& board = Board::GetInstance();
    auto display = board.GetDisplay();
    auto led = board.GetLed();
    led->OnStateChanged();

    switch (new_state) {
        case kDeviceStateUnknown:
        case kDeviceStateIdle:
            // Keep a just-raised network error visible. SetDeviceState(idle)
            // queues STATE_CHANGED after Alert(), and the idle handler would
            // otherwise wipe the status, emotion, and chat message.
            if (last_error_message_.empty()) {
                display->SetStatus(Lang::Strings::STANDBY);
                display->ClearChatMessages();  // Clear messages first
                display->SetEmotion(
                    "neutral");  // Then set emotion (wechat mode checks child count)
            }
            audio_service_.EnableVoiceProcessing(false);
            audio_service_.EnableWakeWordDetection(!text_interaction_muted_);
            break;
        case kDeviceStateConnecting:
            display->SetStatus(Lang::Strings::CONNECTING);
            display->SetEmotion("neutral");
            display->SetChatMessage("system", "");
            break;
        case kDeviceStateListening:
            display->SetStatus(Lang::Strings::LISTENING);
            display->SetEmotion("neutral");

            // Make sure the audio processor is running
            if (play_popup_on_listening_ || !audio_service_.IsAudioProcessorRunning()) {
                // For auto mode, wait for the playback queue to drain before enabling
                // voice processing. This prevents audio truncation when STOP arrives
                // late due to network jitter. Instead of blocking the main loop here,
                // defer the start until MAIN_EVENT_PLAYBACK_DRAINED arrives.
                if (listening_mode_ == kListeningModeAutoStop && !audio_service_.IsPlaybackIdle()) {
                    pending_listening_start_ = true;
                } else {
                    StartListeningAudio();
                }
            } else {
                ConfigureWakeWordForListening();
            }
            break;
        case kDeviceStateSpeaking:
            display->SetStatus(Lang::Strings::SPEAKING);

            if (listening_mode_ != kListeningModeRealtime) {
                audio_service_.EnableVoiceProcessing(false);
                // Only AFE wake word can be detected in speaking mode
                audio_service_.EnableWakeWordDetection(audio_service_.IsAfeWakeWord());
            }
            if (IsConversationAvailable()) {
                audio_service_.ResetDecoder();
            }
            break;
        case kDeviceStateNotifying:
            display->SetStatus(Lang::Strings::SPEAKING);
            audio_service_.EnableVoiceProcessing(false);
            audio_service_.EnableWakeWordDetection(audio_service_.IsAfeWakeWord());
            break;
        case kDeviceStateWifiConfiguring:
            audio_service_.EnableVoiceProcessing(false);
            audio_service_.EnableWakeWordDetection(false);
            break;
        default:
            // Do nothing
            break;
    }
}

void Application::ShowStartupWakeWordHint() {
    // A coalesced activation/state event can queue another idle UI cleanup.
    // Wait for it before adding the hint, or ClearChatMessages() would erase it.
    if (!pending_startup_wake_word_hint_ ||
        (xEventGroupGetBits(event_group_) & MAIN_EVENT_STATE_CHANGED)) {
        return;
    }
    pending_startup_wake_word_hint_ = false;
    const auto state = GetDeviceState();
    const bool wake_word_running = audio_service_.IsWakeWordRunning();
    if (state != kDeviceStateIdle || !last_error_message_.empty() || !wake_word_running) {
        ESP_LOGI(TAG, "Startup wake word hint skipped (state=%d, wake word running=%d, error=%d)",
                 static_cast<int>(state), wake_word_running, !last_error_message_.empty());
        return;
    }

    const auto wake_words = audio_service_.GetWakeWords();
    std::string words;
    for (auto it = wake_words.begin(); it != wake_words.end(); ++it) {
        if (it->empty() || std::find(wake_words.begin(), it, *it) != it) {
            continue;
        }
        if (!words.empty()) {
            words += " / ";
        }
        words += *it;
    }
    if (words.empty()) {
        ESP_LOGW(TAG, "Startup wake word hint skipped: no available wake words");
        return;
    }
    std::string message = Lang::Strings::WAKE_WORD_HINT;
    const auto placeholder = message.find("%s");
    if (placeholder == std::string::npos) {
        ESP_LOGE(TAG, "Startup wake word hint skipped: invalid message template");
        return;
    }
    message.replace(placeholder, 2, words);
    if (startup_wake_word_hint_timer_ == nullptr) {
        esp_timer_create_args_t timer_args = {
            .callback =
                [](void* arg) {
                    auto app = static_cast<Application*>(arg);
                    xEventGroupSetBits(app->event_group_, MAIN_EVENT_STARTUP_HINT_TIMEOUT);
                },
            .arg = this,
            .dispatch_method = ESP_TIMER_TASK,
            .name = "startup_hint",
            .skip_unhandled_events = false,
        };
        ESP_ERROR_CHECK(esp_timer_create(&timer_args, &startup_wake_word_hint_timer_));
    }
    ESP_LOGI(TAG, "Startup wake word hint: %s", message.c_str());
    Board::GetInstance().GetDisplay()->SetChatMessage("assistant", message.c_str());
    startup_wake_word_hint_message_ = std::move(message);
    ESP_ERROR_CHECK(esp_timer_start_once(startup_wake_word_hint_timer_, 5000 * 1000));
}

void Application::HideStartupWakeWordHint() {
    if (startup_wake_word_hint_message_.empty()) {
        return;
    }
    // A conversation or alert may have replaced the hint while its timer was running.
    Board::GetInstance().GetDisplay()->RemoveChatMessage("assistant",
                                                         startup_wake_word_hint_message_.c_str());
    startup_wake_word_hint_message_.clear();
}

void Application::SetListeningStartGuard(std::function<bool()> guard) {
    listening_start_guard_ = std::move(guard);
}

void Application::ResumeListeningAudio() {
    if (!pending_listening_start_) {
        return;
    }
    if (!IsConversationAvailable() || !protocol_ || GetDeviceState() != kDeviceStateListening) {
        pending_listening_start_ = false;
        return;
    }
    // Only auto mode requires the complete playback queue to drain. Other modes
    // retain their existing overlap behavior and consult the optional guard.
    if (listening_mode_ == kListeningModeAutoStop && !audio_service_.IsPlaybackIdle()) {
        return;
    }
    StartListeningAudio();
}

void Application::StartListeningAudio() {
    if (protocol_opening_.load() || text_interaction_muted_) {
        return;
    }
    // Runs on main, directly from state handling or retried by playback/clock events.
    if (!IsConversationAvailable() || !protocol_ || GetDeviceState() != kDeviceStateListening) {
        pending_listening_start_ = false;
        return;
    }
    if (listening_start_guard_ && !listening_start_guard_()) {
        pending_listening_start_ = true;
        return;
    }
    pending_listening_start_ = false;

    // Send the start listening command
    protocol_->SendStartListening(listening_mode_);
    audio_service_.EnableVoiceProcessing(true);

    ConfigureWakeWordForListening();

    // Play popup sound after ResetDecoder (in EnableVoiceProcessing) has been called
    if (play_popup_on_listening_) {
        play_popup_on_listening_ = false;
        audio_service_.PlaySound(Lang::Sounds::OGG_POPUP);
    }
}

void Application::ConfigureWakeWordForListening() {
#ifdef CONFIG_WAKE_WORD_DETECTION_IN_LISTENING
    // Enable wake word detection in listening mode (configured via Kconfig)
    audio_service_.EnableWakeWordDetection(audio_service_.IsAfeWakeWord());
#else
    // Disable wake word detection in listening mode
    audio_service_.EnableWakeWordDetection(false);
#endif
}

void Application::StartNotification(std::string audio_url, std::vector<NotifySubtitle> subtitles) {
    FinishTextInteraction(false);
    if (GetDeviceState() != kDeviceStateIdle || notify_player_.IsBusy()) {
        ESP_LOGW(TAG, "Ignoring notify message while device is busy");
        return;
    }

    auto& board = Board::GetInstance();
    board.SetPowerSaveLevel(PowerSaveLevel::PERFORMANCE);
    audio_service_.EnableVoiceProcessing(false);
    audio_service_.EnableWakeWordDetection(audio_service_.IsAfeWakeWord());
    audio_service_.ReleaseWakeWordResources();
    while (audio_service_.PopPacketFromSendQueue()) {
        // Discard microphone audio left over from a previous conversation.
    }

    if (!SetDeviceState(kDeviceStateNotifying)) {
        board.SetPowerSaveLevel(PowerSaveLevel::LOW_POWER);
        return;
    }

    audio_service_.ResetDecoder();
    uint32_t playback_id = ++notification_playback_id_;
    if (playback_id == 0) {
        playback_id = ++notification_playback_id_;
    }
    audio_service_.PlaySound(Lang::Sounds::OGG_POPUP);

    bool started = notify_player_.Start(
        std::move(audio_url), std::move(subtitles), playback_id,
        [this](uint32_t id, const std::string& text) {
            Schedule([this, id, text]() {
                if (GetDeviceState() == kDeviceStateNotifying && notification_playback_id_ == id) {
                    Board::GetInstance().GetDisplay()->SetChatMessage("assistant", text.c_str());
                }
            });
        },
        [this](uint32_t id, bool success) {
            Schedule([this, id, success]() { HandleNotificationFinished(id, success); });
        });

    if (!started) {
        ESP_LOGE(TAG, "Failed to start notification playback");
        StopNotification();
    }
}

void Application::StopNotification() {
    notify_player_.Stop();
    audio_service_.ResetDecoder();
    auto& board = Board::GetInstance();
    board.GetDisplay()->SetChatMessage("assistant", "");
    board.SetPowerSaveLevel(PowerSaveLevel::LOW_POWER);
    if (GetDeviceState() == kDeviceStateNotifying) {
        SetDeviceState(kDeviceStateIdle);
    }
}

void Application::HandleNotificationFinished(uint32_t playback_id, bool success) {
    if (GetDeviceState() != kDeviceStateNotifying || notification_playback_id_ != playback_id) {
        return;
    }
    ESP_LOGI(TAG, "Notification playback %lu %s", static_cast<unsigned long>(playback_id),
             success ? "completed" : "failed");
    StopNotification();
}

void Application::Schedule(std::function<void()>&& callback) {
    {
        std::lock_guard<std::mutex> lock(mutex_);
        main_tasks_.push_back(std::move(callback));
    }
    xEventGroupSetBits(event_group_, MAIN_EVENT_SCHEDULE);
}

void Application::AbortSpeaking(AbortReason reason) {
    if (protocol_opening_.load() || text_interaction_muted_ || !IsConversationAvailable() ||
        !protocol_) {
        return;
    }
    ESP_LOGI(TAG, "Abort speaking");
    aborted_ = true;
    if (protocol_) {
        protocol_->SendAbortSpeaking(reason);
    }
}

void Application::SetListeningMode(ListeningMode mode) {
    if (protocol_opening_.load() || text_interaction_muted_ || !IsConversationAvailable()) {
        return;
    }
    listening_mode_ = mode;
    SetDeviceState(kDeviceStateListening);
}

ListeningMode Application::GetDefaultListeningMode() const {
    return aec_mode_ == kAecOff ? kListeningModeAutoStop : kListeningModeRealtime;
}

void Application::Reboot() {
    FinishTextInteraction(false);
    ESP_LOGI(TAG, "Rebooting...");
    if (GetDeviceState() == kDeviceStateNotifying) {
        StopNotification();
    }
    // Disconnect the audio channel
    if (!protocol_initialization_pending_ && protocol_ && !protocol_opening_.load() &&
        !protocol_closing_.load() && protocol_->IsAudioChannelOpened()) {
        CloseAudioChannel();
    }
    if (protocol_opening_.load() || protocol_initialization_pending_ ||
        conversation_policy_.load() != 1 || protocol_closing_.load() ||
        (protocol_ && protocol_->UsesConversationPolicy())) {
        reset_protocol_pending_ = true;
        conversation_ready_policy_.store(0);
        CloseConversationChannel();
    } else {
        protocol_.reset();
    }
    audio_service_.Stop();

    vTaskDelay(pdMS_TO_TICKS(1000));
    esp_restart();
}

bool Application::UpgradeFirmware(const std::string& url, const std::string& version) {
    FinishTextInteraction(false);
    auto& board = Board::GetInstance();
    auto display = board.GetDisplay();

    std::string upgrade_url = url;
    std::string version_info = version.empty() ? "(Manual upgrade)" : version;

    if (GetDeviceState() == kDeviceStateNotifying) {
        StopNotification();
    }

    // Close audio channel if it's open
    if (protocol_opening_.load()) {
        CloseConversationChannel();
    } else if (protocol_ && !protocol_closing_.load() && protocol_->IsAudioChannelOpened()) {
        ESP_LOGI(TAG, "Closing audio channel before firmware upgrade");
        CloseAudioChannel();
    }
    ESP_LOGI(TAG, "Starting firmware upgrade from URL: %s", upgrade_url.c_str());

    Alert(Lang::Strings::OTA_UPGRADE, Lang::Strings::UPGRADING, "download",
          Lang::Sounds::OGG_UPGRADE);
    vTaskDelay(pdMS_TO_TICKS(3000));

    SetDeviceState(kDeviceStateUpgrading);

    std::string message = std::string(Lang::Strings::NEW_VERSION) + version_info;
    display->SetChatMessage("system", message.c_str());

    board.SetPowerSaveLevel(PowerSaveLevel::PERFORMANCE);
    audio_service_.Stop();
    vTaskDelay(pdMS_TO_TICKS(1000));

    bool upgrade_success = Ota::Upgrade(upgrade_url, [this, display](int progress, size_t speed) {
        char buffer[32];
        snprintf(buffer, sizeof(buffer), "%d%% %uKB/s", progress, speed / 1024);
        Schedule([display, message = std::string(buffer)]() {
            display->SetChatMessage("system", message.c_str());
        });
    });

    if (!upgrade_success) {
        // Upgrade failed, restart audio service and continue running
        ESP_LOGE(TAG,
                 "Firmware upgrade failed, restarting audio service and continuing operation...");
        audio_service_.Start();                              // Restart audio service
        board.SetPowerSaveLevel(PowerSaveLevel::LOW_POWER);  // Restore power save level
        Alert(Lang::Strings::ERROR, Lang::Strings::UPGRADE_FAILED, "cancel",
              Lang::Sounds::OGG_EXCLAMATION);
        vTaskDelay(pdMS_TO_TICKS(3000));
        return false;
    } else {
        // Upgrade success, reboot immediately
        ESP_LOGI(TAG, "Firmware upgrade successful, rebooting...");
        display->SetChatMessage("system", "Upgrade successful, rebooting...");
        vTaskDelay(pdMS_TO_TICKS(1000));  // Brief pause to show message
        Reboot();
        return true;
    }
}

bool Application::IsConversationCurrent(uint32_t policy) const {
    return (policy & 1U) != 0 && policy == conversation_policy_.load() &&
           policy == conversation_ready_policy_.load() && !protocol_closing_.load();
}

bool Application::IsConversationAvailable() const {
    return IsConversationCurrent(conversation_policy_.load());
}

bool Application::IsChannelCurrent(uint32_t generation) const {
    return generation != 0 && !protocol_closing_.load() &&
           generation == active_channel_generation_.load();
}

bool Application::CanReceiveReply(uint32_t generation) const {
    if (!IsChannelCurrent(generation)) {
        return false;
    }
    if (IsConversationAvailable()) {
        return true;
    }
    return (conversation_policy_.load() & 1U) == 0 &&
           generation == draining_channel_generation_.load() && !reply_ended_.load();
}

void Application::SetConversationEnabled(bool enabled) {
    auto previous = conversation_policy_.load();
    uint32_t policy;
    do {
        if (((previous & 1U) != 0) == enabled) {
            return;
        }
        policy = ((previous + 2U) & ~1U) | (enabled ? 1U : 0U);
    } while (!conversation_policy_.compare_exchange_weak(previous, policy));

    // Atomic-only work here: callers may hold the display lock. All PCM/Opus
    // work retains the old token, so even a complete exit/reentry cannot relabel it.
    audio_service_.SetConversationPolicy(policy);
    // Keep wake enable intent unchanged, but invalidate every wake continuation
    // even when the caller uses this API without the separate wake policy API.
    auto wake_policy = wake_word_policy_.load();
    while (!wake_word_policy_.compare_exchange_weak(wake_policy, wake_policy + 2U)) {
    }
    conversation_ready_policy_.store(0);
    const auto state = GetDeviceState();
    if (!enabled && (state == kDeviceStateSpeaking || state == kDeviceStateListening)) {
        draining_channel_generation_.store(active_channel_generation_.load());
        if (state == kDeviceStateListening) {
            // Only buffered playback may drain here, never a future server reply.
            reply_ended_.store(true);
        }
    } else {
        draining_channel_generation_.store(0);
        audio_service_.SetReplyChannel(0);
    }
    Schedule([this, policy]() { ApplyConversationPolicy(policy); });
}

void Application::ApplyConversationPolicy(uint32_t policy) {
    if (policy != conversation_policy_.load()) {
        return;
    }
    applied_conversation_policy_ = policy;
    if (text_interaction_id_ != 0 && text_interaction_policy_ != policy) {
        FinishTextInteraction(false);
    }
    text_reply_generation_ = 0;
    if (text_interaction_id_ == 0) {
        text_interaction_muted_ = false;
    }
    pending_listening_start_ = false;
    play_popup_on_listening_ = false;
    open_conversation_policy_ = 0;
    wake_word_invocation_policy_ = 0;
    audio_service_.ApplyConversationPolicy();
    const bool wake_only = (policy & 1U) == 0 && wake_word_activation_handler_;
    audio_service_.SetWakeWordDetectionInhibited(
        (wake_word_policy_.load() & 1U) == 0 || !wake_only, wake_word_policy_.load());
    if (wake_only) {
        audio_service_.EnableWakeWordDetection(true);
    }
    const auto state = GetDeviceState();
    if ((policy & 1U) == 0 &&
        (state == kDeviceStateSpeaking || state == kDeviceStateListening) &&
        draining_channel_generation_.load() != 0) {
        if (state == kDeviceStateListening) {
            SetDeviceState(kDeviceStateIdle);
        }
        // Empty playback is merely an underrun until the matching TTS end arrives.
        reply_drain_deadline_ = esp_timer_get_time() + 30000000;
        FinishConversationDrain();
        return;
    }
    draining_channel_generation_.store(0);
    reply_drain_deadline_ = 0;
    if (state == kDeviceStateSpeaking || state == kDeviceStateListening ||
        state == kDeviceStateConnecting) {
        audio_service_.ResetDecoder();
        SetDeviceState(kDeviceStateIdle);
    }
    CloseConversationChannel();
}

void Application::FinishConversationDrain() {
    if (draining_channel_generation_.load() == 0) {
        return;
    }
    const auto deadline = reply_drain_deadline_.load();
    const bool timed_out = deadline != 0 && esp_timer_get_time() >= deadline;
    if (!timed_out && (!reply_ended_.load() || !audio_service_.IsPlaybackIdle())) {
        return;
    }
    draining_channel_generation_.store(0);
    reply_drain_deadline_ = 0;
    if (timed_out) {
        audio_service_.ResetDecoder();
    }
    if (GetDeviceState() == kDeviceStateSpeaking) {
        SetDeviceState(kDeviceStateIdle);
    }
    CloseConversationChannel();
}

void Application::CloseAudioChannel() {
    if (protocol_opening_.load() || text_interaction_id_ != 0) {
        if (text_interaction_id_ != 0) {
            FinishTextInteraction(false);
        } else {
            CloseConversationChannel();
        }
        return;
    }
    if (protocol_closing_.load()) {
        return;
    }
    if (conversation_policy_.load() != 1 || (protocol_ && protocol_->UsesConversationPolicy())) {
        conversation_ready_policy_.store(0);
        CloseConversationChannel();
    } else if (protocol_) {
        protocol_->CloseAudioChannel();
    }
}

void Application::CloseConversationChannel() {
    conversation_ready_policy_.store(0);
    active_channel_generation_.store(0);
    audio_service_.SetReplyChannel(0);
    text_reply_generation_ = 0;
    if (text_interaction_id_ == 0) {
        text_interaction_muted_ = false;
    }
    if (protocol_opening_.load()) {
        // Invalidate immediately, but do not touch the transport or shared_ptr
        // while OpenAudioChannel owns it on the worker.
        protocol_open_close_pending_ = true;
        return;
    }
    if (protocol_initialization_pending_) {
        return;
    }
    // A policy-owned close also fulfils the pending request's fresh-channel fence.
    if (text_interaction_id_ != 0) {
        text_interaction_needs_close_ = false;
    }
    if (protocol_closing_.exchange(true)) {
        return;
    }
    if (!protocol_ || (protocol_->channel_generation() == 0 && !reset_protocol_pending_.load())) {
        // No audio open has been attempted. Keep the control transport available
        // for MCP discovery; there is no channel to retire or goodbye to send.
        // Protocol reset must still release ownership on the close worker below.
        CompleteConversationClose();
        return;
    }
    protocol_->SetChannelClosing(true);
    auto* job = new (std::nothrow) ConversationCloseJob{this, protocol_};
    if (job == nullptr || xTaskCreate([](void* arg) {
            auto* job = static_cast<ConversationCloseJob*>(arg);
            job->protocol->CloseAudioChannel();
            auto* app = job->app;
            auto worker = xTaskGetCurrentTaskHandle();
            app->Schedule([app, job, worker]() {
                app->ReleaseConversationClose(job);
                xTaskNotifyGive(worker);
            });
            // Main relinquishes ownership, but keeps the channel unavailable
            // until this worker has also completed potentially blocking teardown.
            ulTaskNotifyTake(pdTRUE, portMAX_DELAY);
            delete job;
            app->Schedule([app]() { app->CompleteConversationClose(); });
            vTaskDelete(nullptr);
        }, "conversation_close", 4096, job, 3, nullptr) != pdPASS) {
        delete job;
        // Fail closed; retry on the bounded clock tick, never destroy on main.
        protocol_->SetChannelClosing(false);
        protocol_closing_.store(false);
        ESP_LOGE(TAG, "Failed to create conversation close task");
    }
}

void Application::ReleaseConversationClose(ConversationCloseJob* job) {
    if (reset_protocol_pending_.load() && protocol_ == job->protocol) {
        // The close job retains the final owner. Destruction stays on its worker.
        protocol_.reset();
    }
}

void Application::CompleteConversationClose() {
    if (reset_protocol_pending_.load() && protocol_) {
        // A reset arrived after the first main acknowledgement. Retire that
        // retained object with the same serialized worker path, never on main.
        protocol_closing_.store(false);
        CloseConversationChannel();
        return;
    }
    reset_protocol_pending_.store(false);
    protocol_closing_.store(false);
    if (protocol_) {
        protocol_->SetChannelClosing(false);
    }
    const auto policy = conversation_policy_.load();
    if (applied_conversation_policy_ != policy) {
        conversation_ready_policy_.store(0);
        return;
    }
    conversation_ready_policy_.store(policy);
    audio_service_.SetWakeWordDetectionInhibited(
        (wake_word_policy_.load() & 1U) == 0 ||
            ((policy & 1U) == 0 && !wake_word_activation_handler_),
        wake_word_policy_.load());
    if (policy & 1U) {
        if (GetDeviceState() != kDeviceStateNotifying) {
            SetDeviceState(kDeviceStateIdle);
        }
    }
    ResumeProtocolInitialization();
    ResumeTextInteraction();
    FlushDeferredMcpMessages();
    ResumeWakeWordActivation();
}

void Application::SetWakeWordDetectionEnabled(bool enabled) {
    auto previous = wake_word_policy_.load();
    uint32_t policy;
    do {
        if (((previous & 1U) != 0) == enabled) {
            return;
        }
        policy = ((previous + 2U) & ~1U) | (enabled ? 1U : 0U);
    } while (!wake_word_policy_.compare_exchange_weak(previous, policy));

    // No engine or display work here: this API can be called while holding a display lock.
    Schedule([this, policy]() {
        if (wake_word_policy_.load() != policy) {
            return;
        }
        // The engine carries this token from detection processing through delivery,
        // so an asynchronous result cannot adopt a newer enabled policy.
        audio_service_.SetWakeWordDetectionInhibited(
            (policy & 1U) == 0 || (!IsConversationAvailable() && !wake_word_activation_handler_),
            policy);
        const auto invocation_policy = wake_word_invocation_policy_.load();
        if (invocation_policy != 0 && invocation_policy != policy) {
            CancelWakeWordInvoke(invocation_policy);
        }
    });
}

void Application::WakeWordInvoke(const std::string& wake_word) {
    const auto policy = wake_word_policy_.load();
    if (!IsWakeWordInvocationCurrent(policy)) {
        return;
    }

    // May be called from outside the main task (e.g. board button callbacks).
    // GetDeviceState() is atomic; inspect protocol only after reaching the main task.
    const auto state = GetDeviceState();
    if (state != kDeviceStateIdle && state != kDeviceStateNotifying &&
        state != kDeviceStateSpeaking && state != kDeviceStateListening) {
        return;
    }
    Schedule([this, wake_word, policy, state]() {
        if (!protocol_ || !IsWakeWordInvocationCurrent(policy) || GetDeviceState() != state) {
            return;
        }
        if (state == kDeviceStateIdle) {
            BeginWakeWordInvoke(wake_word, policy);
        } else if (state == kDeviceStateNotifying) {
            StopNotification();
            BeginWakeWordInvoke(wake_word, policy);
        } else if (state == kDeviceStateSpeaking) {
            AbortSpeaking(kAbortReasonNone);
        } else if (state == kDeviceStateListening) {
            CloseAudioChannel();
        }
    });
}

bool Application::CanEnterSleepMode() {
    if (protocol_opening_.load() || protocol_closing_.load() || protocol_initialization_pending_ ||
        text_interaction_muted_) {
        return false;
    }
    if (GetDeviceState() != kDeviceStateIdle) {
        return false;
    }

    if (protocol_ && protocol_->IsAudioChannelOpened()) {
        return false;
    }

    if (!audio_service_.IsIdle()) {
        return false;
    }

    // Now it is safe to enter sleep mode
    return true;
}

void Application::RegisterMcpBroadcastCallback(std::function<void(const std::string&)> callback) {
    mcp_broadcast_callback_ = std::move(callback);
}

void Application::SendMcpMessage(const std::string& payload) {
    // Always schedule to run in main task for thread safety
    Schedule([this, payload]() {
        // MCP discovery and control replies do not require a foreground voice session.
        if (protocol_opening_.load() || protocol_initialization_pending_) {
            // A bounded control queue avoids touching session_id_/mqtt_/socket
            // while a lifecycle worker is publishing them. Never replay a reset.
            if (!reset_protocol_pending_.load() && deferred_mcp_messages_.size() < 8 &&
                payload.size() <= kMaxDeferredMcpBytes - deferred_mcp_bytes_) {
                deferred_mcp_messages_.push_back(payload);
                deferred_mcp_bytes_ += payload.size();
            } else {
                ESP_LOGW(TAG, "MCP deferred queue full or protocol reset pending");
            }
        } else if (protocol_ && !protocol_closing_.load()) {
            protocol_->SendMcpMessage(payload);
        } else {
            ESP_LOGW(TAG, "MCP send skipped: protocol=%d closing=%d", protocol_ != nullptr,
                     protocol_closing_.load());
        }
        if (mcp_broadcast_callback_) {
            mcp_broadcast_callback_(payload);
        }
    });
}

void Application::FlushDeferredMcpMessages() {
    if (protocol_opening_.load() || protocol_initialization_pending_ || protocol_closing_.load()) {
        return;
    }
    auto messages = std::move(deferred_mcp_messages_);
    deferred_mcp_messages_.clear();
    deferred_mcp_bytes_ = 0;
    for (const auto& payload : messages) {
        if (!protocol_ || reset_protocol_pending_.load() || protocol_closing_.load()) {
            break;
        }
        protocol_->SendMcpMessage(payload);
    }
}

void Application::SetAecMode(AecMode mode) {
    aec_mode_ = mode;
    Schedule([this]() {
        auto& board = Board::GetInstance();
        auto display = board.GetDisplay();
        switch (aec_mode_) {
            case kAecOff:
                audio_service_.EnableDeviceAec(false);
                display->ShowNotification(Lang::Strings::RTC_MODE_OFF);
                break;
            case kAecOnServerSide:
                audio_service_.EnableDeviceAec(false);
                display->ShowNotification(Lang::Strings::RTC_MODE_ON);
                break;
            case kAecOnDeviceSide:
                audio_service_.EnableDeviceAec(true);
                display->ShowNotification(Lang::Strings::RTC_MODE_ON);
                break;
        }

        // If the AEC mode is changed, close the audio channel
        if (protocol_opening_.load() || text_interaction_id_ != 0) {
            CloseAudioChannel();
        } else if (protocol_ && !protocol_closing_.load() && protocol_->IsAudioChannelOpened()) {
            CloseAudioChannel();
        }
    });
}

void Application::PlaySound(const std::string_view& sound) { audio_service_.PlaySound(sound); }

void Application::ResetProtocol() {
    Schedule([this]() {
        if (GetDeviceState() == kDeviceStateNotifying) {
            StopNotification();
        }
        reset_protocol_pending_ = true;
        FinishTextInteraction(false);
        deferred_mcp_messages_.clear();
        deferred_mcp_bytes_ = 0;
        conversation_ready_policy_.store(0);
        CloseConversationChannel();
    });
}
