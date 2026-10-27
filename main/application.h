#ifndef _APPLICATION_H_
#define _APPLICATION_H_

#include <freertos/FreeRTOS.h>
#include <freertos/event_groups.h>
#include <freertos/task.h>
#include <esp_timer.h>

#include <atomic>
#include <string>
#include <mutex>
#include <deque>
#include <memory>
#include <functional>
#include <cstdint>
#include <vector>

#include "protocol.h"
#include "ota.h"
#include "audio_service.h"
#include "device_state.h"
#include "device_state_machine.h"
#include "notify/notify_player.h"

// Main event bits
#define MAIN_EVENT_SCHEDULE             (1 << 0)
#define MAIN_EVENT_SEND_AUDIO           (1 << 1)
#define MAIN_EVENT_WAKE_WORD_DETECTED   (1 << 2)
#define MAIN_EVENT_VAD_CHANGE           (1 << 3)
#define MAIN_EVENT_ERROR                (1 << 4)
#define MAIN_EVENT_ACTIVATION_DONE      (1 << 5)
#define MAIN_EVENT_CLOCK_TICK           (1 << 6)
#define MAIN_EVENT_NETWORK_CONNECTED    (1 << 7)
#define MAIN_EVENT_NETWORK_DISCONNECTED (1 << 8)
#define MAIN_EVENT_TOGGLE_CHAT          (1 << 9)
#define MAIN_EVENT_START_LISTENING      (1 << 10)
#define MAIN_EVENT_STOP_LISTENING       (1 << 11)
#define MAIN_EVENT_STATE_CHANGED        (1 << 12)
#define MAIN_EVENT_PLAYBACK_DRAINED     (1 << 13)
#define MAIN_EVENT_STARTUP_HINT_TIMEOUT (1 << 14)

enum AecMode {
    kAecOff,
    kAecOnDeviceSide,
    kAecOnServerSide,
};

class Application {
public:
    static Application& GetInstance() {
        static Application instance;
        return instance;
    }
    // Delete copy constructor and assignment operator
    Application(const Application&) = delete;
    Application& operator=(const Application&) = delete;

    /**
     * Initialize the application
     * This sets up display, audio, network callbacks, etc.
     * Network connection starts asynchronously.
     */
    void Initialize();

    /**
     * Run the main event loop
     * This function runs in the main task and never returns.
     * It handles all events including network, state changes, and user interactions.
     */
    void Run();

    DeviceState GetDeviceState() const { return state_machine_.GetState(); }
    bool IsVoiceDetected() const { return audio_service_.IsVoiceDetected(); }
    
    /**
     * Request state transition
     * Returns true if transition was successful
     */
    bool SetDeviceState(DeviceState state);

    /**
     * Schedule a callback to be executed in the main task
     */
    void Schedule(std::function<void()>&& callback);

    /**
     * Alert with status, message, emotion and optional sound
     */
    void Alert(const char* status, const char* message, const char* emotion = "", const std::string_view& sound = "");
    void DismissAlert();

    void AbortSpeaking(AbortReason reason);

    /**
     * Toggle chat state (event-based, thread-safe)
     * Sends MAIN_EVENT_TOGGLE_CHAT to be handled in Run()
     */
    void ToggleChatState();

    /**
     * Start listening (event-based, thread-safe)
     * Sends MAIN_EVENT_START_LISTENING to be handled in Run()
     */
    void StartListening();
    // Main task only. The optional nonblocking guard delays listen/start and
    // microphone processing until local playback resources are safe to release.
    // An empty handler preserves the existing listening behavior.
    void SetListeningStartGuard(std::function<bool()> guard);

    /**
     * Stop listening (event-based, thread-safe)
     * Sends MAIN_EVENT_STOP_LISTENING to be handled in Run()
     */
    void StopListening();

    void Reboot();
    // Persistent wake-word policy, independent of the current conversation state.
    // Safe under a display lock: invalidates wake requests immediately and schedules audio changes.
    void SetWakeWordDetectionEnabled(bool enabled);
    // Foreground availability: stop uplink immediately, allow only the current
    // spoken reply to drain, and reopen into a fresh idle session.
    void SetConversationEnabled(bool enabled);
    static constexpr size_t kMaxTextInteractionBytes = Protocol::kMaxTextInteractionBytes;
    // Main task only, without a display lock. The caller owns foreground UI and
    // must enable conversation first. At most one request; no wake hook or PCM.
    // True accepts local work, NOT server acceptance or audible playback.
    // For accepted work, completion runs exactly once on main: true only after a
    // matching TTS start; false on cancellation/failure or a 30-second deadline.
    // A rejected request returns false without invoking completion.
    bool RequestTextInteraction(const std::string& text, std::function<void(bool)> completion = {});
    // Register during UI setup, before audio callbacks start. A real detector event
    // uses this main-task hook to foreground voice or cancel pending UI navigation.
    void SetWakeWordActivationHandler(std::function<bool()> handler);
    void WakeWordInvoke(const std::string& wake_word);
    bool UpgradeFirmware(const std::string& url, const std::string& version = "");
    bool CanEnterSleepMode();
    void SendMcpMessage(const std::string& payload);
    void RegisterMcpBroadcastCallback(std::function<void(const std::string&)> callback);
    void SetAecMode(AecMode mode);
    AecMode GetAecMode() const { return aec_mode_; }
    void PlaySound(const std::string_view& sound);
    AudioService& GetAudioService() { return audio_service_; }
    
    /**
     * Reset protocol resources (thread-safe)
     * Can be called from any task to release resources allocated after network connected
     * This includes closing audio channel, resetting protocol and ota objects
     */
    void ResetProtocol();

private:
    Application();
    ~Application();

    std::mutex mutex_;
    std::deque<std::function<void()>> main_tasks_;
    std::shared_ptr<Protocol> protocol_;
    std::atomic<uint32_t> conversation_policy_{1};
    std::atomic<uint32_t> conversation_ready_policy_{1};
    uint32_t applied_conversation_policy_ = 1;
    std::atomic<uint32_t> active_channel_generation_{0};
    std::atomic<uint32_t> protocol_instance_{0};
    std::atomic<uint32_t> draining_channel_generation_{0};
    std::atomic<bool> reply_ended_{false};
    std::atomic<bool> protocol_closing_{false};
    std::atomic<bool> protocol_opening_{false};
    bool protocol_open_close_pending_ = false;
    uint32_t next_text_interaction_id_ = 0;
    uint32_t text_interaction_id_ = 0;
    uint32_t text_interaction_policy_ = 0;
    uint32_t text_interaction_generation_ = 0;
    uint32_t text_reply_generation_ = 0;
    int64_t text_interaction_deadline_ = 0;
    bool text_interaction_needs_close_ = false;
    bool text_interaction_sent_ = false;
    bool text_interaction_muted_ = false;
    std::string pending_text_interaction_;
    std::function<void(bool)> text_interaction_completion_;
    static constexpr size_t kMaxDeferredMcpBytes = 16 * 1024;
    std::deque<std::string> deferred_mcp_messages_;
    size_t deferred_mcp_bytes_ = 0;
    std::atomic<uint32_t> toggle_chat_policy_{0};
    std::atomic<uint32_t> start_listening_policy_{0};
    std::atomic<uint32_t> stop_listening_policy_{0};
    uint32_t open_conversation_policy_ = 0;
    std::atomic<bool> reset_protocol_pending_{false};
    TaskHandle_t protocol_initialization_task_ = nullptr;
    bool protocol_initialization_pending_ = false;
    std::atomic<int64_t> reply_drain_deadline_{0};
    EventGroupHandle_t event_group_ = nullptr;
    esp_timer_handle_t clock_timer_handle_ = nullptr;
    esp_timer_handle_t startup_wake_word_hint_timer_ = nullptr;
    DeviceStateMachine state_machine_;
    ListeningMode listening_mode_ = kListeningModeAutoStop;
    AecMode aec_mode_ = kAecOff;
    std::string last_error_message_;
    std::string startup_wake_word_hint_message_;
    AudioService audio_service_;
    // Bit 0 is the policy (enabled by default); upper bits invalidate earlier wake work.
    std::atomic<uint32_t> wake_word_policy_{1};
    std::atomic<uint32_t> wake_word_event_policy_{0};
    std::atomic<uint32_t> wake_word_invocation_policy_{0};  // Pending wake-owned connection.
    std::function<bool()> wake_word_activation_handler_;
    std::string pending_wake_word_;
    uint32_t pending_wake_policy_ = 0;
    NotifyPlayer notify_player_;
    uint32_t notification_playback_id_ = 0;
    std::unique_ptr<Ota> ota_;

    std::function<void(const std::string&)> mcp_broadcast_callback_;

    bool has_server_time_ = false;
    bool startup_completed_ = false;
    bool pending_startup_wake_word_hint_ = false;
    bool aborted_ = false;
    bool assets_version_checked_ = false;
    bool play_popup_on_listening_ = false;  // Flag to play popup sound after state changes to listening
    bool pending_listening_start_ =
        false;  // Waiting for auto playback drain or the listening guard.
    std::function<bool()> listening_start_guard_;
    int clock_ticks_ = 0;
    TaskHandle_t activation_task_handle_ = nullptr;


    // Event handlers
    void HandleStateChangedEvent();
    void HandleToggleChatEvent();
    void HandleStartListeningEvent();
    void HandleStopListeningEvent();
    void HandleNetworkConnectedEvent();
    void HandleNetworkDisconnectedEvent();
    void HandleActivationDoneEvent();
    void HandleWakeWordDetectedEvent();
    bool IsConversationCurrent(uint32_t policy) const;
    bool IsConversationAvailable() const;
    bool IsChannelCurrent(uint32_t generation) const;
    bool CanReceiveReply(uint32_t generation) const;
    void ApplyConversationPolicy(uint32_t policy);
    void FinishConversationDrain();
    void CloseConversationChannel();
    void CloseAudioChannel();
    struct ConversationCloseJob {
        Application* app;
        std::shared_ptr<Protocol> protocol;
    };
    void ReleaseConversationClose(ConversationCloseJob* job);
    void CompleteConversationClose();
    void ResumeProtocolInitialization();
    struct TextInteractionOpenJob {
        Application* app;
        std::shared_ptr<Protocol> protocol;
        uint32_t request;
        uint32_t policy;
        uint32_t instance;
    };
    void ResumeTextInteraction();
    void CompleteTextInteractionOpen(uint32_t request, uint32_t policy, uint32_t instance,
                                     Protocol* protocol, bool opened);

    void FinishTextInteraction(bool started);
    void FlushDeferredMcpMessages();
    void PublishAudioChannelOpened(uint32_t generation);
    void ContinueOpenAudioChannel(ListeningMode mode, uint32_t policy);
    bool IsWakeWordDetectionCurrent(uint32_t policy) const;
    bool IsWakeWordInvocationCurrent(uint32_t policy) const;
    void ResumeWakeWordActivation();
    void CancelWakeWordInvoke(uint32_t policy);
    void BeginWakeWordInvoke(const std::string& wake_word, uint32_t policy);
    void ContinueWakeWordInvoke(const std::string& wake_word, uint32_t policy);
    void StartListeningAudio();
    void ResumeListeningAudio();
    void ConfigureWakeWordForListening();
    void ShowStartupWakeWordHint();
    void HideStartupWakeWordHint();
    void StartNotification(std::string audio_url, std::vector<NotifySubtitle> subtitles);
    void StopNotification();
    void HandleNotificationFinished(uint32_t playback_id, bool success);

    // Activation task (runs in background)
    void ActivationTask();

    // Helper methods
    void CheckAssetsVersion();
    void CheckNewVersion();
    bool InitializeProtocol();
    void ShowActivationCode(const std::string& code, const std::string& message);
    void SetListeningMode(ListeningMode mode);
    ListeningMode GetDefaultListeningMode() const;
    
    // State change handler called by state machine
    void OnStateChanged(DeviceState old_state, DeviceState new_state);
};


class TaskPriorityReset {
public:
    TaskPriorityReset(BaseType_t priority) {
        original_priority_ = uxTaskPriorityGet(NULL);
        vTaskPrioritySet(NULL, priority);
    }
    ~TaskPriorityReset() {
        vTaskPrioritySet(NULL, original_priority_);
    }

private:
    BaseType_t original_priority_;
};

#endif // _APPLICATION_H_
