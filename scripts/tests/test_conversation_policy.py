"""Run extracted foreground-policy code with deterministic lifecycle and epoch races."""

import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]


def definition(source, signature):
    start = source.index(signature)
    brace = source.index("{", start)
    depth, end = 1, brace + 1
    while depth:
        depth += (source[end] == "{") - (source[end] == "}")
        end += 1
    return source[start:end]


def fields(header, names):
    return "\n".join(next(line for line in header.splitlines()
                          if re.search(r"\b" + re.escape(name) + r"\b\s*(?:\{|=|;)", line)
                          and ";" in line) for name in names)


class ConversationPolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        compiler = shlex.split(os.environ.get("CXX", "c++"))
        if not compiler or shutil.which(compiler[0]) is None:
            raise unittest.SkipTest("requires a host C++23 compiler")
        cls.tmp = tempfile.TemporaryDirectory(prefix="conversation-policy-test-")
        cls.addClassCleanup(cls.tmp.cleanup)
        app = (ROOT / "main/application.cc").read_text(encoding="utf-8")
        header = (ROOT / "main/application.h").read_text(encoding="utf-8")
        audio_header = (ROOT / "main/audio/audio_service.h").read_text(encoding="utf-8")
        source = APP_PLATFORM.replace("APP_FIELDS", fields(header, (
            "conversation_policy_", "conversation_ready_policy_", "active_channel_generation_",
            "protocol_instance_", "draining_channel_generation_", "reply_ended_",
            "protocol_closing_", "toggle_chat_policy_", "start_listening_policy_",
            "stop_listening_policy_", "open_conversation_policy_", "reset_protocol_pending_",
            "protocol_initialization_task_", "protocol_initialization_pending_",
            "reply_drain_deadline_", "wake_word_policy_",
            "wake_word_invocation_policy_", "wake_word_activation_handler_",
            "pending_wake_word_", "pending_wake_policy_", "applied_conversation_policy_",
            "protocol_opening_", "protocol_open_close_pending_", "text_interaction_id_",
            "text_interaction_policy_", "text_interaction_generation_", "text_reply_generation_",
            "text_interaction_sent_", "text_interaction_muted_", "text_interaction_deadline_",
            "text_interaction_needs_close_", "deferred_mcp_messages_",
            "deferred_mcp_bytes_", "listening_start_guard_")))
        source = source.replace("std::atomic<bool> reply_ended_", "HookAtomic<bool> reply_ended_")
        source = source.replace("@CLOSE_JOB@", definition(header, "struct ConversationCloseJob") + ";")
        source = source.replace("AUDIO_POLICY_METHOD", definition(
            audio_header, "void SetConversationPolicy("))
        source = source.replace("AUDIO_CALLBACK", definition(app, "protocol_->OnIncomingAudio("))
        source = source.replace("CLOSED_CALLBACK", definition(app, "protocol_->OnAudioChannelClosed("))
        source = source.replace("TTS_HANDLER", definition(
            app, 'else if (strcmp(type->valuestring, "tts") == 0)').replace("else if", "if", 1))
        source = source.replace("RUN_SEND", definition(app, "if (bits & MAIN_EVENT_SEND_AUDIO)"))
        source = source.replace("RUN_PLAYBACK", definition(app, "if (bits & MAIN_EVENT_PLAYBACK_DRAINED)"))
        for signature in (
            "bool Application::IsConversationCurrent(", "bool Application::IsConversationAvailable(",
            "bool Application::IsChannelCurrent(", "bool Application::CanReceiveReply(",
            "void Application::SetConversationEnabled(", "void Application::ApplyConversationPolicy(",
            "void Application::FinishConversationDrain(", "void Application::CloseAudioChannel(",
            "void Application::CloseConversationChannel(", "void Application::ReleaseConversationClose(",
            "void Application::CompleteConversationClose(", "void Application::ResumeProtocolInitialization(",
            "void Application::SetWakeWordActivationHandler(", "bool Application::IsWakeWordDetectionCurrent(",
            "void Application::ResumeWakeWordActivation(",
            "void Application::ActivationTask(",
            "void Application::ToggleChatState(", "void Application::StartListening(",
            "void Application::StopListening(", "void Application::HandleToggleChatEvent(",
            "void Application::HandleStartListeningEvent(", "void Application::HandleStopListeningEvent(",
            "void Application::ContinueOpenAudioChannel(", "void Application::StartListeningAudio(",
            "void Application::ResumeListeningAudio(",
            "void Application::SetDeviceState(", "void Application::SetListeningMode(",
            "void Application::ResetProtocol(", "void Application::Schedule(",
        ):
            if signature == "void Application::SetDeviceState(":
                signature = "bool Application::SetDeviceState("
            method = definition(app, signature)
            if signature == "bool Application::IsChannelCurrent(":
                # Pause after the production atomic checks, before the caller acts
                # on that snapshot. Never reenter a serialized main-task mutation.
                method = method.replace("return generation != 0", "const bool current = generation != 0")
                method = method.replace("generation == active_channel_generation_.load();",
                                        "generation == active_channel_generation_.load();\n"
                                        "    if (on_channel_check) { auto hook = std::move(on_channel_check); "
                                        "on_channel_check = {}; hook(); }\n    return current;")
            source += method + "\n"
        # Execute the production initialization deferral guard; firmware setup
        # below it is represented by a transport factory that requires live OTA.
        initialize = definition(app, "bool Application::InitializeProtocol(")
        source += initialize[:initialize.index("    auto& board")] + "    return InitializeTransport();\n}\n"
        source += APP_CHECKS
        cls.binaries = {}
        cls.compile(compiler, "application", source)
        cls.compile(compiler, "audio", audio_source())
        cls.compile(compiler, "transports", transport_source())

    @classmethod
    def compile(cls, compiler, name, source):
        path = Path(cls.tmp.name) / (name + ".cc")
        path.write_text(source, encoding="utf-8")
        executable = Path(cls.tmp.name) / (name + ".exe")
        result = subprocess.run(compiler + ["-std=c++23", "-Wall", "-Wextra", "-Werror",
                                           "-Wno-unused-parameter", "-fno-exceptions", "-fno-rtti",
                                           str(path), "-o", str(executable)],
                                capture_output=True, text=True, timeout=60)
        if result.returncode:
            raise AssertionError(result.stdout + result.stderr)
        cls.binaries[name] = str(executable)

    def run_case(self, name, binary="application"):
        result = subprocess.run([self.binaries[binary], name], capture_output=True,
                                text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_default_and_idempotent_policy_preserve_stock_availability(self):
        self.run_case("default")

    def test_listening_exit_stops_uplink_without_waiting_for_future_reply(self):
        self.run_case("listening")

    def test_pending_listening_buffered_tail_drains_without_microphone_or_reset(self):
        self.run_case("listening_tail")

    def test_speaking_underrun_is_not_reply_completion(self):
        self.run_case("underrun")

    def test_tts_end_before_playback_empty_waits_for_tail(self):
        self.run_case("end_before_drain")

    def test_tts_end_after_empty_playback_finishes_without_listening(self):
        self.run_case("end_after_drain")

    def test_wake_only_speaking_exit_drains_buffered_tail_without_continuation(self):
        self.run_case("wake_only_buffered")

    def test_wake_only_speaking_exit_accepts_ongoing_reply_until_matching_end(self):
        self.run_case("wake_only_ongoing")

    def test_wake_only_remote_close_preserves_tail_without_continuation(self):
        self.run_case("wake_only_remote_close")

    def test_healthy_long_reply_refreshes_inactivity_watchdog(self):
        self.run_case("long_reply")

    def test_missing_tts_end_has_finite_inactivity_fallback(self):
        self.run_case("timeout")

    def test_reentry_discards_old_reply_and_waits_for_serialized_close(self):
        self.run_case("reentry")

    def test_first_home_and_voice_entry_do_not_close_a_never_opened_channel(self):
        self.run_case("unopened_home")

    def test_reset_of_a_never_opened_protocol_still_destroys_it_on_worker(self):
        self.run_case("unopened_reset")

    def test_failed_partial_open_still_closes_even_without_an_active_channel(self):
        self.run_case("partial_open")

    def test_close_worker_is_bounded_and_protocol_lifetime_is_retained(self):
        self.run_case("close_owner")

    def test_close_task_creation_failure_stays_unavailable_and_can_retry(self):
        self.run_case("close_failure")

    def test_queued_old_close_cannot_cancel_fresh_foreground(self):
        self.run_case("stale_close")

    def test_notification_playback_survives_foreground_exit_and_reentry(self):
        self.run_case("notification")

    def test_manual_event_and_open_continuations_reject_foreground_aba(self):
        self.run_case("manual_aba")

    def test_blocking_open_rechecks_foreground_epoch(self):
        self.run_case("open_aba")

    def test_stale_receive_stop_cannot_mutate_new_reply_end_flag(self):
        self.run_case("stop_mutation_aba")

    def test_reset_stays_unavailable_until_worker_destructor_completes(self):
        self.run_case("reset_destruction")

    def test_remote_close_during_exit_preserves_buffered_reply_tail(self):
        self.run_case("remote_close_tail")

    def test_deferred_and_failed_first_init_propagates_foreground_epoch(self):
        self.run_case("lazy_init", "audio")

    def test_deferred_activation_retains_ota_until_worker_destruction_and_initialization(self):
        self.run_case("deferred_activation")

    def test_initialization_retry_after_reset_without_publication_is_not_stranded(self):
        self.run_case("activation_retry_empty")

    def test_initialization_retry_retires_published_transport_before_renotifying(self):
        self.run_case("activation_retry_published")

    def test_queued_pong_checks_lifetime_safe_channel_before_acquiring_socket(self):
        self.run_case("pong_close", "transports")

    def test_original_pcm_epoch_and_encode_commit_reject_aba(self):
        self.run_case("encode_aba", "audio")

    def test_audio_send_queue_filters_stale_epochs_and_remains_bounded(self):
        self.run_case("send_queue", "audio")

    def test_lite_original_pcm_token_and_partial_frame_do_not_cross_epochs(self):
        self.run_case("lite", "audio")

    def test_afe_original_fetch_token_and_history_reset_fence_reentry(self):
        self.run_case("afe", "audio")

    def test_both_transport_send_boundaries_reject_stale_audio(self):
        self.run_case("send", "transports")

    def test_mqtt_conversational_session_isolation_is_opt_in(self):
        self.run_case("mqtt_sessions", "transports")

    def test_websocket_original_channel_metadata_rejects_old_callbacks(self):
        self.run_case("websocket_generation", "transports")


APP_PLATFORM = r'''
#include <atomic>
#include <cassert>
#include <condition_variable>
#include <cstdint>
#include <cstring>
#include <deque>
#include <functional>
#include <memory>
#include <mutex>
#include <new>
#include <string>
#include <utility>
#include <vector>
#define ESP_LOGE(...) ((void)0)
#define ESP_LOGI(...) ((void)0)
#define ESP_LOGW(...) ((void)0)
#define AS_EVENT_AUDIO_PROCESSOR_RUNNING 4U
#define AS_EVENT_WAKE_WORD_RUNNING 2U
#define MAIN_EVENT_SEND_AUDIO 2U
#define MAIN_EVENT_PLAYBACK_DRAINED 8192U
#define MAIN_EVENT_TOGGLE_CHAT 512U
#define MAIN_EVENT_START_LISTENING 1024U
#define MAIN_EVENT_STOP_LISTENING 2048U
#define MAIN_EVENT_SCHEDULE 1U
#define MAIN_EVENT_ACTIVATION_DONE 16U
constexpr int pdPASS = 1, pdTRUE = 1, portMAX_DELAY = -1;
using TaskHandle_t = void*;
int64_t now_us = 0;
int64_t esp_timer_get_time() { return now_us; }
uint32_t xEventGroupSetBits(uint32_t* group, uint32_t bits) { return *group |= bits; }
uint32_t xEventGroupClearBits(uint32_t* group, uint32_t bits) { return *group &= ~bits; }
template<class T> class HookAtomic {
    std::atomic<T> value_;
public:
    std::function<void(T)> before_store;
    HookAtomic(T value) : value_(value) {}
    T load() const { return value_.load(); }
    void store(T value) { if (before_store) { auto hook = std::move(before_store); before_store = {}; hook(value); } value_.store(value); }
};
bool on_worker = false, fail_task_create = false;
int tasks_created = 0, tasks_deleted = 0, close_calls = 0, destructions = 0;
std::deque<std::function<void()>> workers;
std::function<void()> acknowledge_worker, destructor_hook, activation_wait;
TaskHandle_t current_task = reinterpret_cast<void*>(1);
bool activation_notified = false;
int xTaskCreate(void (*entry)(void*), const char*, int, void* arg, int, void*) {
    if (fail_task_create) return 0;
    ++tasks_created;
    workers.push_back([entry, arg]() { entry(arg); });
    return pdPASS;
}
TaskHandle_t xTaskGetCurrentTaskHandle() { assert(on_worker); return current_task; }
void xTaskNotifyGive(TaskHandle_t task) { assert(!on_worker); if (task == reinterpret_cast<void*>(2)) activation_notified = true; }
void ulTaskNotifyTake(int, int) {
    assert(on_worker); on_worker = false;
    if (current_task == reinterpret_cast<void*>(2)) { activation_wait(); assert(activation_notified); activation_notified = false; }
    else acknowledge_worker();
    on_worker = true;
}
void vTaskDelete(void*) { assert(on_worker); ++tasks_deleted; }
void RunWorker() {
    assert(!workers.empty()); auto worker = std::move(workers.front()); workers.pop_front();
    const auto saved_task = current_task; current_task = reinterpret_cast<void*>(1);
    on_worker = true; worker(); on_worker = false; current_task = saved_task;
}
struct AudioStreamPacket { uint32_t conversation_policy = 1, channel_generation = 0; std::vector<uint8_t> payload; };
enum DeviceState { kDeviceStateIdle, kDeviceStateConnecting, kDeviceStateListening,
    kDeviceStateSpeaking, kDeviceStateNotifying, kDeviceStateActivating,
    kDeviceStateWifiConfiguring, kDeviceStateAudioTesting };
enum ListeningMode { kListeningModeAutoStop, kListeningModeManualStop, kListeningModeRealtime };
enum AbortReason { kAbortReasonNone };
enum class PowerSaveLevel { PERFORMANCE, LOW_POWER };
namespace Lang::Sounds { const std::string OGG_POPUP = "popup"; }
struct TextGlyph {};
struct cJSON {
    const char* valuestring = nullptr;
    std::string type, state, text;
    cJSON(const char* type, const char* state) : type(type), state(state) {}
};
cJSON* cJSON_GetObjectItem(const cJSON* root, const char* key) {
    static cJSON item("", "");
    if (!std::strcmp(key, "type")) item.valuestring = root->type.c_str();
    else if (!std::strcmp(key, "state")) item.valuestring = root->state.c_str();
    else item.valuestring = root->text.c_str();
    return &item;
}
bool cJSON_IsString(const cJSON* item) { return item && item->valuestring; }
struct TextGlyphPayload { static bool Parse(const cJSON*, std::vector<TextGlyph>&, uint8_t&) { return false; } };
struct Display { int changes = 0; void SetChatMessage(const char*, const char*) { ++changes; } void AddTextGlyphs(const std::vector<TextGlyph>&, uint8_t) {} };
struct Board {
    Display display;
    static Board& GetInstance() { static Board board; return board; }
    void SetPowerSaveLevel(PowerSaveLevel) {}
    Display* GetDisplay() { return &display; }
};
class AudioService {
public:
    std::atomic<uint32_t> conversation_policy_{1}, wake_word_running_policy_{1}, reply_channel_generation_{11};
    uint32_t events = AS_EVENT_AUDIO_PROCESSOR_RUNNING | AS_EVENT_WAKE_WORD_RUNNING;
    uint32_t* event_group_ = &events;
    bool playback_idle = true, wake_inhibited = false, wake_requested = true;
    int resets = 0, applied = 0, voice_enables = 0, sounds = 0, accepted_audio = 0;
    std::deque<std::unique_ptr<AudioStreamPacket>> send;
    AUDIO_POLICY_METHOD
    void SetReplyChannel(uint32_t generation) { reply_channel_generation_.store(generation); }
    void ApplyConversationPolicy() { ++applied; EnableVoiceProcessing(false); send.clear(); }
    void SetWakeWordDetectionInhibited(bool inhibited, uint32_t) { wake_inhibited = inhibited; }
    void EnableWakeWordDetection(bool requested) { wake_requested = requested; }
    void EnableVoiceProcessing(bool enabled) { if (enabled && (conversation_policy_.load() & 1U)) { ++voice_enables; events |= AS_EVENT_AUDIO_PROCESSOR_RUNNING; } else events &= ~AS_EVENT_AUDIO_PROCESSOR_RUNNING; }
    bool IsPlaybackIdle() const { return playback_idle; }
    bool IsAudioProcessorRunning() const { return events & AS_EVENT_AUDIO_PROCESSOR_RUNNING; }
    void ResetDecoder() { ++resets; playback_idle = true; }
    void PlaySound(const std::string&) { ++sounds; }
    void EnableAudioTesting(bool) {}
    bool PushPacketToDecodeQueue(std::unique_ptr<AudioStreamPacket> packet) {
        if (packet->channel_generation != reply_channel_generation_.load()) return false;
        ++accepted_audio; playback_idle = false; return true;
    }
    std::unique_ptr<AudioStreamPacket> PopPacketFromSendQueue() { if (send.empty()) return nullptr; auto p = std::move(send.front()); send.pop_front(); return p; }
};
class Protocol {
public:
    bool opened = true, closing = false;
    uint32_t generation = 11;
    int opens = 0, starts = 0, sends = 0;
    std::function<void()> on_open;
    std::function<void(uint32_t)> on_closed;
    std::function<void(std::unique_ptr<AudioStreamPacket>)> on_audio;
    ~Protocol() { assert(on_worker); if (destructor_hook) destructor_hook(); ++destructions; }
    void SetChannelClosing(bool value) { closing = value; }
    bool UsesConversationPolicy() const { return false; }
    bool IsAudioChannelOpened() const { assert(!closing); return opened; }
    uint32_t channel_generation() const { return generation; }
    void CloseAudioChannel() { assert(on_worker || !closing); ++close_calls; opened = false; if (on_closed) on_closed(generation); }
    bool OpenAudioChannel() { assert(!closing && !on_worker); ++opens; if (on_open) on_open(); ++generation; opened = true; return true; }
    bool SendAudio(std::unique_ptr<AudioStreamPacket>) { assert(!closing); ++sends; return true; }
    void SendStartListening(ListeningMode) { assert(!closing); ++starts; }
    void SendStopListening() { assert(!closing); }
    void OnIncomingAudio(std::function<void(std::unique_ptr<AudioStreamPacket>)> callback) { on_audio = std::move(callback); }
    void OnAudioChannelClosed(std::function<void(uint32_t)> callback) { on_closed = std::move(callback); }
};
struct StateMachine {
    DeviceState state = kDeviceStateIdle;
    bool TransitionTo(DeviceState next) { state = next; return true; }
};
struct Ota { int config = 42; };
class Application {
public:
    APP_FIELDS
    std::shared_ptr<Protocol> protocol_ = std::make_shared<Protocol>();
    AudioService audio_service_;
    StateMachine state_machine_;
    std::mutex mutex_;
    std::deque<std::function<void()>> main_tasks_;
    uint32_t events = 0;
    uint32_t* event_group_ = &events;
    ListeningMode listening_mode_ = kListeningModeAutoStop;
    bool pending_listening_start_ = false, play_popup_on_listening_ = false, aborted_ = false;
    mutable std::function<void()> on_channel_check;
    int stopped_notifications = 0, aborts = 0, wake_begins = 0;
    struct NotifyPlayer { int drains = 0; void OnPlaybackDrained() { ++drains; } } notify_player_;
    @CLOSE_JOB@
    Application() {
        active_channel_generation_.store(11); protocol_instance_.store(1);
        const auto protocol_instance = protocol_instance_.load();
        AUDIO_CALLBACK);
        CLOSED_CALLBACK);
        acknowledge_worker = [this]() { Flush(); };
    }
    ~Application() { on_worker = true; protocol_.reset(); on_worker = false; }
    DeviceState GetDeviceState() const { return state_machine_.state; }
    bool SetDeviceState(DeviceState);
    bool IsConversationCurrent(uint32_t) const;
    bool IsConversationAvailable() const;
    bool IsChannelCurrent(uint32_t) const;
    bool CanReceiveReply(uint32_t) const;
    void SetConversationEnabled(bool);
    void ApplyConversationPolicy(uint32_t);
    void FinishConversationDrain();
    void CloseAudioChannel();
    void CloseConversationChannel();
    void ReleaseConversationClose(ConversationCloseJob*);
    void CompleteConversationClose();
    void ResumeProtocolInitialization();
    // The text path is executed with real worker/close helpers in test_text_interaction.py.
    void FinishTextInteraction(bool) { assert(text_interaction_id_ == 0); }
    void ResumeTextInteraction() { assert(text_interaction_id_ == 0); }
    void FlushDeferredMcpMessages() { assert(deferred_mcp_messages_.empty()); }
    void PublishAudioChannelOpened(uint32_t generation) {
        active_channel_generation_.store(generation); audio_service_.SetReplyChannel(generation);
    }
    void SetWakeWordActivationHandler(std::function<bool()>);
    bool IsWakeWordDetectionCurrent(uint32_t) const;
    void ResumeWakeWordActivation();
    // Wake invocation is exercised end-to-end by test_wake_word_policy.py.
    void BeginWakeWordInvoke(const std::string&, uint32_t) { ++wake_begins; }
    void ActivationTask();
    bool InitializeProtocol();
    std::unique_ptr<Ota> ota_;
    int assets_checks = 0, version_checks = 0, initializations = 0;
    void CheckAssetsVersion() { ++assets_checks; }
    void CheckNewVersion() { ++version_checks; }
    bool InitializeTransport() {
        assert(on_worker && ota_ && ota_->config == 42);
        assert(!protocol_closing_.load() && !reset_protocol_pending_.load());
        ++initializations; protocol_ = std::make_shared<Protocol>(); return true;
    }
    void ToggleChatState();
    void StartListening();
    void StopListening();
    void HandleToggleChatEvent();
    void HandleStartListeningEvent();
    void HandleStopListeningEvent();
    void ContinueOpenAudioChannel(ListeningMode, uint32_t);
    void StartListeningAudio();
    void ResumeListeningAudio();
    void SetListeningMode(ListeningMode);
    void ResetProtocol();
    void Schedule(std::function<void()>&&);
    ListeningMode GetDefaultListeningMode() const { return kListeningModeAutoStop; }
    void StopNotification() { ++stopped_notifications; SetDeviceState(kDeviceStateIdle); }
    void AbortSpeaking(AbortReason) { ++aborts; }
    void ConfigureWakeWordForListening() {}
    void Flush() { auto tasks = std::move(main_tasks_); main_tasks_.clear(); for (auto& task : tasks) task(); }
    void Drain() { for (int i = 0; i < 20 && !main_tasks_.empty(); ++i) Flush(); }
    void ReceiveTts(const char* state, uint32_t generation) {
        cJSON data("tts", state); const auto* root = &data;
        const auto conversation = conversation_policy_.load();
        auto* type = cJSON_GetObjectItem(root, "type");
        auto* display = Board::GetInstance().GetDisplay();
        TTS_HANDLER
    }
    void RunSend() { const auto bits = MAIN_EVENT_SEND_AUDIO; RUN_SEND }
    void RunPlayback() { const auto bits = MAIN_EVENT_PLAYBACK_DRAINED; RUN_PLAYBACK }
    void Audio(uint32_t generation) { auto p = std::make_unique<AudioStreamPacket>(); p->channel_generation = generation; protocol_->on_audio(std::move(p)); }
    void Fresh(uint32_t generation) { active_channel_generation_.store(generation); audio_service_.SetReplyChannel(generation); protocol_->generation = generation; protocol_->opened = true; state_machine_.state = kDeviceStateSpeaking; reply_ended_.store(false); }
};
'''


APP_CHECKS = r'''
int main(int argc, char** argv) {
    assert(argc == 2);
    const std::string test = argv[1];
    Application app;
    auto& audio = app.audio_service_;
    auto& protocol = *app.protocol_;
    if (test == "default") {
        assert(app.IsConversationAvailable() && app.conversation_policy_ == 1);
        app.SetConversationEnabled(true);
        assert(app.main_tasks_.empty() && audio.applied == 0 && tasks_created == 0);
    } else if (test == "listening" || test == "listening_tail") {
        const bool tail = test == "listening_tail";
        app.state_machine_.state = kDeviceStateListening;
        app.pending_listening_start_ = tail;
        audio.playback_idle = !tail;
        app.StartListening();
        app.SetConversationEnabled(false);
        assert(!app.IsConversationAvailable() && audio.events == 0);
        app.HandleStartListeningEvent();
        app.StartListeningAudio();
        assert(protocol.starts == 0 && audio.voice_enables == 0);
        app.Drain();
        assert(app.GetDeviceState() == kDeviceStateIdle && audio.resets == 0);
        app.ReceiveTts("start", 11); app.Drain();
        assert(app.GetDeviceState() == kDeviceStateIdle && !app.CanReceiveReply(11));
        if (tail) {
            assert(tasks_created == 0 && app.draining_channel_generation_ == 11);
            app.RunPlayback(); assert(tasks_created == 0);
            audio.playback_idle = true; app.RunPlayback();
        }
        assert(tasks_created == 1); RunWorker(); app.Drain();
        assert(protocol.starts == 0 && audio.voice_enables == 0 && !app.IsConversationAvailable());
    } else if (test == "underrun" || test == "end_before_drain" || test == "end_after_drain") {
        app.state_machine_.state = kDeviceStateSpeaking;
        app.listening_mode_ = kListeningModeRealtime;
        audio.playback_idle = false;
        app.SetConversationEnabled(false);
        assert(audio.events == 0 && !app.IsConversationAvailable());
        app.Drain();
        assert(audio.resets == 0 && tasks_created == 0 && app.CanReceiveReply(11));
        if (test != "end_before_drain") {
            audio.playback_idle = true; app.RunPlayback();
            assert(tasks_created == 0 && app.GetDeviceState() == kDeviceStateSpeaking);
        }
        if (test == "underrun") {
            app.Audio(11); assert(audio.accepted_audio == 1);
            app.ReceiveTts("start", 11); app.Drain();
            assert(tasks_created == 0 && protocol.starts == 0);
        } else {
            app.ReceiveTts("stop", 11); app.Drain();
            assert(protocol.starts == 0 && audio.voice_enables == 0);
            if (test == "end_before_drain") {
                assert(tasks_created == 0); audio.playback_idle = true; app.RunPlayback();
            }
            assert(tasks_created == 1 && app.GetDeviceState() == kDeviceStateIdle);
            RunWorker(); app.Drain();
        }
    } else if (test == "wake_only_buffered" || test == "wake_only_ongoing" ||
               test == "wake_only_remote_close") {
        const bool ongoing = test == "wake_only_ongoing";
        const bool remote_close = test == "wake_only_remote_close";
        int activations = 0;
        app.SetWakeWordActivationHandler([&]() { ++activations; app.SetConversationEnabled(true); return true; });
        app.state_machine_.state = kDeviceStateSpeaking;
        app.listening_mode_ = kListeningModeRealtime;
        audio.playback_idle = false;
        app.SetConversationEnabled(false);
        assert(audio.events == 0 && !app.IsConversationAvailable());
        app.Drain();
        assert(!audio.wake_inhibited && audio.wake_requested);
        assert(app.IsWakeWordDetectionCurrent(app.wake_word_policy_));
        assert(!app.IsConversationAvailable() && audio.voice_enables == 0 && protocol.starts == 0);
        assert(app.draining_channel_generation_ == 11 && tasks_created == 0 && audio.resets == 0);
        app.StartListening(); app.ToggleChatState();
        app.HandleStartListeningEvent(); app.HandleToggleChatEvent(); app.StartListeningAudio();
        app.RunPlayback();  // A buffered/in-flight reply must not be cut short.
        assert(tasks_created == 0 && app.GetDeviceState() == kDeviceStateSpeaking);
        if (ongoing) {
            audio.playback_idle = true; app.RunPlayback();  // An underrun is not TTS completion.
            assert(tasks_created == 0 && app.CanReceiveReply(11));
            app.Audio(10); app.Audio(11); app.Audio(11);
            assert(audio.accepted_audio == 2 && !audio.playback_idle);
            app.ReceiveTts("start", 11); app.Drain();
            assert(app.GetDeviceState() == kDeviceStateSpeaking && audio.resets == 0);
            app.ReceiveTts("stop", 10); app.Drain();
            assert(!app.reply_ended_.load() && tasks_created == 0);
        }
        if (remote_close) protocol.on_closed(11);
        else app.ReceiveTts("stop", 11);
        app.Drain();
        assert(app.reply_ended_.load() && !app.CanReceiveReply(11));
        assert(app.GetDeviceState() == kDeviceStateSpeaking && tasks_created == 0);
        app.Audio(11); assert(audio.accepted_audio == (ongoing ? 2 : 0));
        assert(audio.resets == 0 && protocol.starts == 0 && audio.voice_enables == 0);
        audio.playback_idle = true; app.RunPlayback();
        assert(app.GetDeviceState() == kDeviceStateIdle && tasks_created == 1);
        assert(app.draining_channel_generation_ == 0 && audio.resets == 0);
        RunWorker(); app.Drain();
        // The opt-in permits future detector activation, not automatic reply continuation.
        assert(!app.IsConversationAvailable() && !audio.wake_inhibited && audio.wake_requested);
        assert(app.conversation_ready_policy_ == app.conversation_policy_);
        assert(protocol.starts == 0 && protocol.opens == 0 && close_calls == 1);
        assert(audio.voice_enables == 0 && audio.resets == 0 && app.aborts == 0);
        assert(activations == 0 && app.wake_begins == 0 && app.pending_wake_policy_ == 0);
    } else if (test == "long_reply" || test == "timeout") {
        app.state_machine_.state = kDeviceStateSpeaking; audio.playback_idle = true;
        app.SetConversationEnabled(false); app.Drain();
        if (test == "long_reply") {
            for (int i = 0; i < 6; ++i) {
                now_us += 29000000; app.Audio(11); app.FinishConversationDrain();
                assert(tasks_created == 0 && app.draining_channel_generation_ == 11);
            }
            assert(now_us > 150000000 && audio.accepted_audio == 6);
            app.ReceiveTts("stop", 11); app.Drain();
            assert(tasks_created == 0); audio.playback_idle = true; app.RunPlayback();
        } else {
            now_us = 30000001; app.FinishConversationDrain(); assert(audio.resets == 1);
        }
        assert(tasks_created == 1); RunWorker();
    } else if (test == "reentry") {
        app.state_machine_.state = kDeviceStateSpeaking; audio.playback_idle = false;
        app.SetConversationEnabled(false); app.Drain();
        app.SetConversationEnabled(true);
        assert(!app.CanReceiveReply(11) && !app.IsConversationAvailable());
        app.Drain(); assert(tasks_created == 1 && audio.resets == 1 && !app.IsConversationAvailable());
        app.Audio(11); assert(audio.accepted_audio == 0);
        RunWorker(); app.Drain();
        assert(app.IsConversationAvailable() && app.GetDeviceState() == kDeviceStateIdle && !protocol.opened);
    } else if (test == "unopened_home" || test == "unopened_reset" || test == "partial_open") {
        protocol.generation = test == "partial_open" ? 11 : 0;
        protocol.opened = false;
        app.active_channel_generation_.store(0);
        if (test == "unopened_reset") {
            app.ResetProtocol(); app.Drain();
            assert(tasks_created == 1 && protocol.closing && app.protocol_closing_.load());
            destructor_hook = [&]() { assert(on_worker && !app.protocol_); };
            RunWorker(); app.Drain(); destructor_hook = {};
            assert(destructions == 1 && !app.protocol_ && !app.protocol_closing_.load());
        } else {
            app.SetConversationEnabled(false); app.Drain();
            assert(!app.IsConversationAvailable() && audio.events == 0 && protocol.starts == 0);
            if (test == "partial_open") {
                assert(tasks_created == 1 && protocol.closing);
                RunWorker(); app.Drain();
                assert(close_calls == 1 && !app.protocol_closing_.load());
            } else {
                assert(tasks_created == 0 && close_calls == 0 && !protocol.closing);
                assert(!app.protocol_closing_.load() && app.conversation_ready_policy_ == 2);
                app.SetConversationEnabled(true); app.Drain();
                assert(tasks_created == 0 && close_calls == 0 && app.IsConversationAvailable());
                assert(protocol.opens == 0 && protocol.starts == 0 && audio.voice_enables == 0);
            }
        }
    } else if (test == "close_owner") {
        app.SetConversationEnabled(false); app.Drain();
        assert(tasks_created == 1 && close_calls == 0 && protocol.closing);
        app.CloseConversationChannel(); app.ResetProtocol(); app.Drain();
        assert(tasks_created == 1 && close_calls == 0 && app.protocol_.use_count() == 2);
        RunWorker(); app.Drain();
        assert(close_calls == 1 && tasks_deleted == 1 && destructions == 1 && !app.protocol_);
    } else if (test == "close_failure") {
        fail_task_create = true; app.SetConversationEnabled(false); app.Drain();
        assert(tasks_created == 0 && close_calls == 0 && !app.IsConversationAvailable());
        fail_task_create = false; app.CloseConversationChannel();
        assert(tasks_created == 1); RunWorker();
    } else if (test == "stale_close") {
        protocol.on_closed(11); auto old_tasks = std::move(app.main_tasks_); app.main_tasks_.clear();
        app.SetConversationEnabled(false); app.Drain(); app.SetConversationEnabled(true); app.Drain();
        RunWorker(); app.Drain(); app.Fresh(12);
        for (auto& task : old_tasks) task();
        protocol.on_closed(11); app.Drain();
        assert(app.GetDeviceState() == kDeviceStateSpeaking && app.IsConversationAvailable());
    } else if (test == "notification") {
        app.state_machine_.state = kDeviceStateNotifying; audio.playback_idle = false;
        app.SetConversationEnabled(false); app.Drain(); assert(audio.resets == 0);
        RunWorker(); app.SetConversationEnabled(true); app.Drain(); RunWorker(); app.Drain();
        assert(app.GetDeviceState() == kDeviceStateNotifying && !audio.playback_idle);
        assert(audio.resets == 0 && app.stopped_notifications == 0 && audio.voice_enables == 0);
    } else if (test == "manual_aba") {
        app.ToggleChatState(); app.StartListening(); app.StopListening();
        app.SetConversationEnabled(false); app.SetConversationEnabled(true);
        app.HandleToggleChatEvent(); app.HandleStartListeningEvent(); app.HandleStopListeningEvent();
        app.ContinueOpenAudioChannel(kListeningModeManualStop, 1);
        app.Drain(); assert(protocol.opens == 0 && protocol.starts == 0 && tasks_created == 1);
        RunWorker(); app.Drain(); assert(app.IsConversationAvailable());
    } else if (test == "open_aba") {
        protocol.opened = false; app.state_machine_.state = kDeviceStateConnecting;
        protocol.on_open = [&]() { app.SetConversationEnabled(false); app.SetConversationEnabled(true); };
        app.ContinueOpenAudioChannel(kListeningModeManualStop, 1);
        assert(protocol.opens == 1 && protocol.starts == 0 && tasks_created == 1);
        app.Drain(); RunWorker(); app.Drain(); assert(app.IsConversationAvailable());
    } else if (test == "reset_destruction") {
        app.SetConversationEnabled(false); app.SetConversationEnabled(true); app.Drain();
        app.ResetProtocol(); app.Drain();
        destructor_hook = [&]() {
            assert(app.protocol_closing_.load() && !app.IsConversationAvailable());
            assert(!app.protocol_);
        };
        RunWorker(); app.Drain(); destructor_hook = {};
        assert(destructions == 1 && !app.protocol_closing_.load() && app.IsConversationAvailable());
    } else if (test == "deferred_activation") {
        app.SetConversationEnabled(false); app.SetConversationEnabled(true); app.Drain();
        app.ResetProtocol(); app.Drain();
        assert(!app.InitializeProtocol() && !app.ota_ && app.initializations == 0);
        destructor_hook = [&]() {
            assert(app.protocol_closing_.load() && !app.IsConversationAvailable());
            assert(app.ota_ && app.initializations == 0 && !activation_notified);
            assert(!(app.events & MAIN_EVENT_ACTIVATION_DONE));
        };
        activation_wait = [&]() {
            app.Drain();
            assert(app.protocol_initialization_task_ == reinterpret_cast<void*>(2));
            assert(!app.protocol_initialization_pending_);
            assert(app.ota_ && app.initializations == 0 && !activation_notified);
            assert(!(app.events & MAIN_EVENT_ACTIVATION_DONE));
            RunWorker();
            assert(destructions == 1 && app.protocol_closing_.load() && !activation_notified);
            app.Drain();
            assert(!app.protocol_closing_.load() && activation_notified && app.ota_);
            assert(app.protocol_initialization_pending_);
        };
        current_task = reinterpret_cast<void*>(2); on_worker = true;
        app.ActivationTask(); on_worker = false; current_task = reinterpret_cast<void*>(1);
        destructor_hook = {};
        assert(app.initializations == 1 && app.assets_checks == 1 && app.version_checks == 1);
        assert(app.ota_ && (app.events & MAIN_EVENT_ACTIVATION_DONE));
        assert(!app.protocol_initialization_task_ && app.IsConversationAvailable());
        assert(app.protocol_initialization_pending_);  // Cleared only by main activation-done handling.
    } else if (test == "activation_retry_empty" || test == "activation_retry_published") {
        const bool published = test == "activation_retry_published";
        on_worker = true; app.protocol_.reset(); on_worker = false;
        destructions = 0;
        int waits = 0;
        activation_wait = [&]() {
            ++waits;
            app.Drain();
            if (waits == 1) {
                assert(activation_notified && app.protocol_initialization_pending_);
                if (published) app.protocol_ = std::make_shared<Protocol>();
                // Reset arrives after notification but before InitializeProtocol's entry guard.
                app.ResetProtocol(); app.Drain();
                assert(app.reset_protocol_pending_ && app.conversation_ready_policy_ == 0);
                assert(!app.protocol_closing_ && workers.empty());
            } else {
                assert(waits == 2 && app.initializations == 0);
                if (published) {
                    assert(!activation_notified && app.protocol_closing_);
                    assert(!app.protocol_initialization_pending_ && workers.size() == 1);
                    RunWorker(); app.Drain();
                    assert(destructions == 1);
                }
                assert(activation_notified && app.protocol_initialization_pending_);
                assert(!app.protocol_initialization_task_ && !app.reset_protocol_pending_);
                assert(!app.protocol_closing_ && app.IsConversationAvailable());
            }
        };
        current_task = reinterpret_cast<void*>(2); on_worker = true;
        app.ActivationTask(); on_worker = false; current_task = reinterpret_cast<void*>(1);
        assert(waits == 2 && app.initializations == 1 && app.ota_);
        assert(app.events & MAIN_EVENT_ACTIVATION_DONE);
        assert(app.protocol_initialization_pending_);
    } else if (test == "remote_close_tail") {
        app.state_machine_.state = kDeviceStateSpeaking; audio.playback_idle = false;
        app.SetConversationEnabled(false); app.Drain();
        protocol.on_closed(11); app.Drain();
        assert(tasks_created == 0 && app.draining_channel_generation_ == 11 && audio.resets == 0);
        audio.playback_idle = true; app.RunPlayback();
        assert(tasks_created == 1); RunWorker(); app.Drain();
        assert(app.GetDeviceState() == kDeviceStateIdle && protocol.starts == 0);
    } else if (test == "stop_mutation_aba") {
        app.state_machine_.state = kDeviceStateSpeaking;
        app.on_channel_check = [&]() {
            app.SetConversationEnabled(false); app.SetConversationEnabled(true); app.Drain();
            RunWorker(); app.Drain(); app.Fresh(12);
        };
        app.ReceiveTts("stop", 11); app.Drain();
        assert(!app.reply_ended_.load() && app.GetDeviceState() == kDeviceStateSpeaking);
    } else { assert(false); }
    while (!workers.empty()) { RunWorker(); app.Drain(); }
}
'''


def audio_source():
    audio = (ROOT / "main/audio/audio_service.cc").read_text(encoding="utf-8")
    header = (ROOT / "main/audio/audio_service.h").read_text(encoding="utf-8")
    packet = (ROOT / "main/protocols/protocol.h").read_text(encoding="utf-8")
    queue = (ROOT / "main/audio/fixed_queue.h").read_text(encoding="utf-8")
    source = AUDIO_PLATFORM.replace("@PACKET@", definition(packet, "struct AudioStreamPacket") + ";")
    source = source.replace("AUDIO_TASK", definition(header, "struct AudioTask") + ";")
    source = source.replace("FIXED_QUEUE", queue.replace("#include <esp_system.h>", ""))
    source = source.replace("AUDIO_FIELDS", fields(header, (
        "conversation_policy_", "reply_channel_generation_", "wake_word_running_policy_")))
    source = source.replace("POLICY_METHOD", definition(header, "void SetConversationPolicy("))
    source = source.replace("CURRENT_METHOD", definition(header, "bool IsConversationPolicyCurrent("))
    source = source.replace("ENCODE_STEP", definition(audio, "if (!audio_encode_queue_.empty())"))
    for signature in ("void AudioService::PushTaskToEncodeQueue(",
                      "std::unique_ptr<AudioStreamPacket> AudioService::PopPacketFromSendQueue(",
                      "void AudioService::ApplyConversationPolicy(",
                      "bool AudioService::InitializeAudioEngine("):
        source += definition(audio, signature) + "\n"
    lite = (ROOT / "main/audio/engines/lite_audio_engine.cc").read_text(encoding="utf-8")
    afe = (ROOT / "main/audio/engines/afe_audio_engine.cc").read_text(encoding="utf-8")
    for signature in ("void LiteAudioEngine::Feed(", "void LiteAudioEngine::EnableVoiceProcessing(",
                      "void LiteAudioEngine::OutputRawAudio("):
        source += definition(lite, signature) + "\n"
    for signature in ("void AfeAudioEngine::Feed(", "void AfeAudioEngine::EnableVoiceProcessing(",
                      "void AfeAudioEngine::ProcessingTask(", "void AfeAudioEngine::ApplyPendingReset(",
                      "void AfeAudioEngine::HandleVoiceResult("):
        method = definition(afe, signature)
        if "::ProcessingTask(" in method:
            method = method.replace("while (true)",
                                    "for (int host_step = 0; host_step < 1; ++host_step)", 1)
        source += method + "\n"
    return source + AUDIO_CHECKS


def transport_source():
    protocol = (ROOT / "main/protocols/protocol.h").read_text(encoding="utf-8")
    mqtt = (ROOT / "main/protocols/mqtt_protocol.cc").read_text(encoding="utf-8")
    websocket = (ROOT / "main/protocols/websocket_protocol.cc").read_text(encoding="utf-8")
    source = TRANSPORT_PLATFORM.replace("@PACKET@", definition(protocol, "struct AudioStreamPacket") + ";")
    source = source.replace("@BINARY@", definition(protocol, "struct BinaryProtocol2") +
                            " __attribute__((packed));\n" + definition(protocol, "struct BinaryProtocol3") +
                            " __attribute__((packed));")
    for method in ("void SetConversationPolicySource(", "void SetChannelClosing(",
                   "bool IsChannelClosing(", "bool IsChannelOpening(", "bool UsesConversationPolicy(",
                   "uint32_t BeginChannel(", "bool IsAudioPacketCurrent("):
        source = source.replace("@PROTOCOL_METHODS@", definition(protocol, method) + "\n@PROTOCOL_METHODS@")
    source = source.replace("@PROTOCOL_METHODS@", "")
    source = source.replace("@MQTT_CALLBACK@", definition(mqtt, "mqtt_->OnMessage("))
    source = source.replace("@WS_CALLBACK@", definition(websocket, "websocket_->OnData("))
    source = source.replace("@WS_CLOSED@", definition(websocket, "websocket_->OnDisconnected("))
    ping_start = websocket.index("    std::weak_ptr<WebSocket> weak_websocket = websocket_;")
    ping_end = websocket.index("    websocket_->OnDisconnected(", ping_start)
    source = source.replace("@WS_PING@", websocket[ping_start:ping_end])
    source += definition(mqtt, "bool MqttProtocol::SendAudio(") + "\n"
    source += definition(websocket, "bool WebsocketProtocol::SendAudio(") + "\n"
    source += definition(websocket, "void WebsocketProtocol::CloseAudioChannel(") + "\n"
    return source + TRANSPORT_CHECKS


AUDIO_PLATFORM = r'''
#include <atomic>
#include <cassert>
#include <condition_variable>
#include <cstdint>
#include <cstdlib>
#include <deque>
#include <functional>
#include <memory>
#include <mutex>
#include <string>
#include <vector>
#define ESP_LOGE(...) ((void)0)
#define ESP_LOGD(...) ((void)0)
#define ESP_LOGW(...) ((void)0)
#define AS_EVENT_AUDIO_PROCESSOR_RUNNING 4U
#define AS_EVENT_WAKE_WORD_RUNNING 2U
constexpr size_t MAX_ENCODE_TASKS_IN_QUEUE = 2, MAX_SEND_PACKETS_IN_QUEUE = 40;
constexpr int OPUS_FRAME_DURATION_MS = 60, ESP_AUDIO_ERR_OK = 0, ESP_FAIL = -1;
constexpr int pdFALSE = 0, pdTRUE = 1, portMAX_DELAY = -1, VAD_SPEECH = 1, VAD_SILENCE = 0;
constexpr bool kUseAfeForVoiceProcessing = true;
using EventBits_t = uint32_t;
uint32_t xEventGroupSetBits(uint32_t* group, uint32_t bits) { return *group |= bits; }
uint32_t xEventGroupClearBits(uint32_t* group, uint32_t bits) { return *group &= ~bits; }
uint32_t xEventGroupGetBits(uint32_t* group) { return *group; }
uint32_t xEventGroupWaitBits(uint32_t* group, uint32_t, int, int, int) { return *group; }
[[noreturn]] void esp_system_abort(const char*) { std::abort(); }
int64_t esp_timer_get_time() { return 2000000; }
FIXED_QUEUE
@PACKET@
enum AudioTaskType { kAudioTaskTypeEncodeToSendQueue, kAudioTaskTypeEncodeToTestingQueue, kAudioTaskTypeDecodeToPlaybackQueue };
AUDIO_TASK
struct esp_audio_enc_in_frame_t { uint8_t* buffer; uint32_t len; };
struct esp_audio_enc_out_frame_t { uint8_t* buffer; uint32_t len, encoded_bytes; };
std::function<void()> encode_hook;
int encoded = 0;
int esp_opus_enc_process(void*, esp_audio_enc_in_frame_t*, esp_audio_enc_out_frame_t* out) {
    ++encoded; if (encode_hook) { auto hook = std::move(encode_hook); encode_hook = {}; hook(); }
    out->buffer[0] = 17; out->encoded_bytes = 1; return 0;
}
struct LazyEngine {
    bool ok = false; uint32_t voice_policy = 1;
    bool Initialize(void*, int, void*) { return ok; }
    void EnableDeviceAec(bool) {}
    void EnableVoiceProcessing(bool enabled, uint32_t policy) { voice_policy = enabled ? policy | 1U : policy & ~1U; }
};
class AudioService {
public:
    AUDIO_FIELDS
    std::unique_ptr<LazyEngine> audio_engine_ = std::make_unique<LazyEngine>();
    bool audio_engine_initialized_ = false, device_aec_enabled_ = false;
    void* codec_ = nullptr;
    void* models_list_ = nullptr;
    bool InitializeAudioEngine();
    uint32_t events = AS_EVENT_AUDIO_PROCESSOR_RUNNING | AS_EVENT_WAKE_WORD_RUNNING;
    uint32_t* event_group_ = &events;
    std::mutex audio_queue_mutex_;
    std::condition_variable audio_queue_cv_;
    FixedQueue<AudioTask, MAX_ENCODE_TASKS_IN_QUEUE> audio_encode_queue_;
    FixedQueue<std::unique_ptr<AudioStreamPacket>, MAX_SEND_PACKETS_IN_QUEUE> audio_send_queue_;
    FixedQueue<std::unique_ptr<AudioStreamPacket>, 100> audio_testing_queue_;
    FixedQueue<uint32_t, 3> timestamp_queue_;
    struct { uint32_t encode_drop_count = 0, encode_count = 0; } debug_statistics_;
    int64_t last_encode_drop_log_time_ = 0;
    bool voice_processing_requested_ = false;
    void* opus_encoder_ = reinterpret_cast<void*>(1);
    size_t encoder_frame_size_ = 4;
    int encoder_outbuf_size_ = 16;
    struct { std::function<void()> on_send_queue_available, on_audio_testing_queue_full; } callbacks_;
    POLICY_METHOD
    CURRENT_METHOD
    void EnableVoiceProcessing(bool) {}
    void PushTaskToEncodeQueue(AudioTaskType, std::vector<int16_t>&&, uint32_t policy = 1);
    std::unique_ptr<AudioStreamPacket> PopPacketFromSendQueue();
    void ApplyConversationPolicy();
    void EncodeOnce() {
        std::unique_lock<std::mutex> lock(audio_queue_mutex_);
        for (int host_step = 0; host_step < 1; ++host_step) { ENCODE_STEP }
    }
};
struct Codec { size_t input_channels() const { return 1; } };
struct FakeWakeWord { void Feed(const std::vector<int16_t>&) {} };
class LiteAudioEngine {
public:
    std::atomic<uint32_t> voice_policy_{1};
    std::atomic<bool> wake_word_enabled_{false}, voice_processing_enabled_{false};
    std::unique_ptr<FakeWakeWord> wake_word_;
    std::mutex wake_word_mutex_, output_mutex_;
    Codec codec;
    Codec* codec_ = &codec;
    int frame_samples_ = 4;
    std::vector<int16_t> output_buffer_;
    uint32_t output_buffer_policy_ = 0;
    std::function<void(std::vector<int16_t>&&, uint32_t)> output_callback_;
    void Feed(std::vector<int16_t>&&, uint32_t policy = 1);
    void EnableVoiceProcessing(bool, uint32_t policy = 1);
    void OutputRawAudio(const std::vector<int16_t>&, uint32_t);
};
struct afe_fetch_result_t { int ret_value = 0, vad_state = VAD_SPEECH; int16_t* data = nullptr; int data_size = 0; };
struct FakeAfe {
    afe_fetch_result_t result;
    std::function<void()> on_fetch;
    int resets = 0, feeds = 0;
    int get_feed_chunksize(void*) { return 4; }
    void feed(void*, const int16_t*) { ++feeds; }
    void reset_buffer(void*) { ++resets; }
    afe_fetch_result_t* fetch_with_delay(void*, int) { if (on_fetch) { auto hook = std::move(on_fetch); on_fetch = {}; hook(); } return &result; }
};
class AfeAudioEngine {
public:
    static constexpr EventBits_t kWakeWordEnabled = 1, kVoiceProcessingEnabled = 2, kAfeActive = 4;
    std::atomic<uint32_t> voice_policy_{1}, wake_word_policy_{1}, control_generation_{0};
    uint32_t processing_voice_policy_ = 0, output_buffer_policy_ = 0;
    std::atomic<bool> reset_pending_{false}, output_reset_pending_{false}, afe_control_dirty_{false};
    uint32_t events = kVoiceProcessingEnabled | kAfeActive;
    uint32_t* event_group_ = &events;
    FakeAfe iface;
    FakeAfe* afe_iface_ = &iface;
    void* afe_data_ = reinterpret_cast<void*>(1);
    Codec codec;
    Codec* codec_ = &codec;
    std::vector<int16_t> input_buffer_, output_buffer_;
    std::mutex input_buffer_mutex_;
    int frame_samples_ = 4;
    bool is_speaking_ = false;
    std::function<void(std::vector<int16_t>&&, uint32_t)> output_callback_;
    std::function<void(bool)> vad_state_change_callback_;
    void Feed(std::vector<int16_t>&&, uint32_t policy = 1);
    void EnableVoiceProcessing(bool, uint32_t policy = 1);
    void ProcessingTask();
    void ApplyPendingReset();
    void HandleVoiceResult(const afe_fetch_result_t*, uint32_t);
    void UpdateActiveState() { output_reset_pending_ = !(events & kVoiceProcessingEnabled); }
    void OutputRawAudio(const std::vector<int16_t>&, uint32_t) { assert(false); }
    void HandleWakeWordResult(const afe_fetch_result_t*, uint32_t) {}
    void ApplyAfeControls() {}
};
'''


AUDIO_CHECKS = r'''
int main(int argc, char** argv) {
    assert(argc == 2); const std::string test = argv[1];
    AudioService audio;
    if (test == "encode_aba") {
        audio.PushTaskToEncodeQueue(kAudioTaskTypeEncodeToSendQueue, {1, 2, 3, 4}, 1);
        encode_hook = [&]() { audio.SetConversationPolicy(2); audio.SetConversationPolicy(5); };
        audio.EncodeOnce(); assert(encoded == 1 && audio.audio_send_queue_.empty());
        audio.PushTaskToEncodeQueue(kAudioTaskTypeEncodeToSendQueue, {1, 2, 3, 4}, 1);
        assert(audio.audio_encode_queue_.empty());
        audio.PushTaskToEncodeQueue(kAudioTaskTypeEncodeToSendQueue, {5, 6, 7, 8}, 5);
        audio.EncodeOnce(); auto fresh = audio.PopPacketFromSendQueue();
        assert(fresh && fresh->conversation_policy == 5 && fresh->payload.size() == 1);
        audio.PushTaskToEncodeQueue(kAudioTaskTypeEncodeToTestingQueue, {1, 2, 3, 4});
        audio.EncodeOnce(); assert(audio.audio_testing_queue_.size() == 1);
    } else if (test == "send_queue") {
        for (int i = 0; i < 8; ++i) audio.PushTaskToEncodeQueue(kAudioTaskTypeEncodeToSendQueue, {1, 2, 3, 4}, 1);
        assert(audio.audio_encode_queue_.size() == 2 && audio.debug_statistics_.encode_drop_count == 6);
        audio.EncodeOnce(); audio.SetConversationPolicy(2); audio.SetConversationPolicy(5);
        assert(!audio.PopPacketFromSendQueue());
        for (int i = 0; i < 50; ++i) {
            audio.PushTaskToEncodeQueue(kAudioTaskTypeEncodeToSendQueue, {1, 2, 3, 4}, 5); audio.EncodeOnce();
        }
        assert(audio.audio_send_queue_.size() == MAX_SEND_PACKETS_IN_QUEUE);
        audio.ApplyConversationPolicy(); assert(audio.audio_send_queue_.empty() && audio.audio_encode_queue_.empty());
    } else if (test == "lite") {
        LiteAudioEngine lite;
        std::vector<uint32_t> tokens; std::vector<std::vector<int16_t>> outputs;
        lite.output_callback_ = [&](std::vector<int16_t>&& data, uint32_t policy) { tokens.push_back(policy); outputs.push_back(std::move(data)); };
        lite.EnableVoiceProcessing(true, 1); lite.Feed({1, 2}, 1);
        lite.EnableVoiceProcessing(false, 2); lite.EnableVoiceProcessing(true, 5);
        lite.Feed({3, 4, 5, 6}, 1); assert(outputs.empty());
        lite.Feed({7, 8}, 5); assert(outputs.empty()); lite.Feed({9, 10}, 5);
        assert(tokens == std::vector<uint32_t>{5} && outputs[0] == (std::vector<int16_t>{7, 8, 9, 10}));
    } else if (test == "lazy_init") {
        audio.SetConversationPolicy(2); audio.ApplyConversationPolicy(); audio.SetConversationPolicy(5);
        assert(!audio.InitializeAudioEngine() && !audio.audio_engine_initialized_);
        audio.audio_engine_->ok = true;
        assert(audio.InitializeAudioEngine());
        AfeAudioEngine afe;
        afe.voice_policy_.store(audio.audio_engine_->voice_policy);
        afe.Feed({1, 2, 3, 4}, 5);
        assert(afe.iface.feeds == 1);
    } else if (test == "afe") {
        AfeAudioEngine afe; int outputs = 0; uint32_t last_policy = 0;
        int16_t pcm[] = {1, 2, 3, 4}; afe.iface.result.data = pcm; afe.iface.result.data_size = sizeof(pcm);
        afe.output_callback_ = [&](std::vector<int16_t>&&, uint32_t policy) { ++outputs; last_policy = policy; };
        afe.iface.on_fetch = [&]() { afe.EnableVoiceProcessing(false, 2); afe.EnableVoiceProcessing(true, 5); };
        afe.ProcessingTask(); assert(outputs == 0 && afe.iface.resets == 1);
        afe.Feed({1, 2, 3, 4}, 1); assert(afe.iface.feeds == 0);
        afe.Feed({1, 2, 3, 4}, 5); assert(afe.iface.feeds == 1);
        afe.ProcessingTask(); assert(outputs == 1 && last_policy == 5 && afe.iface.resets == 2);
        afe.HandleVoiceResult(&afe.iface.result, 1); assert(outputs == 1);
    } else { assert(false); }
}
'''


TRANSPORT_PLATFORM = r'''
#include <atomic>
#include <cassert>
#include <chrono>
#include <cstdint>
#include <cstring>
#include <deque>
#include <functional>
#include <memory>
#include <mutex>
#include <string>
#include <vector>
#define ESP_LOGE(...) ((void)0)
#define ESP_LOGI(...) ((void)0)
#define ESP_LOGW(...) ((void)0)
uint16_t htons(uint16_t x) { return static_cast<uint16_t>((x << 8) | (x >> 8)); }
uint16_t ntohs(uint16_t x) { return htons(x); }
uint32_t htonl(uint32_t x) { return ((x & 255U) << 24) | ((x & 65280U) << 8) | ((x >> 8) & 65280U) | (x >> 24); }
uint32_t ntohl(uint32_t x) { return htonl(x); }
@PACKET@
@BINARY@
class Protocol {
public:
    inline static std::atomic<uint32_t> next_channel_generation_{0};
    std::atomic<uint32_t> channel_generation_{0};
    std::shared_ptr<std::atomic<uint32_t>> channel_callback_generation_ = std::make_shared<std::atomic<uint32_t>>(0);
    std::atomic<bool> channel_closing_{false}, channel_opening_{false};
    std::atomic<bool> require_conversation_session_{false};
    const std::atomic<uint32_t>* conversation_policy_source_ = nullptr;
    std::function<void(std::unique_ptr<AudioStreamPacket>)> on_incoming_audio_;
    std::function<void(uint32_t)> on_audio_channel_closed_;
    int server_sample_rate_ = 16000, server_frame_duration_ = 60;
    std::string session_id_ = "current";
    std::chrono::steady_clock::time_point last_incoming_time_;
    @PROTOCOL_METHODS@
};
struct cJSON { const char* valuestring = nullptr; std::string type, session; bool has_session = false; };
cJSON* cJSON_Parse(const char* data) {
    auto* root = new cJSON; std::string text(data); auto split = text.find('|');
    root->type = text.substr(0, split); root->has_session = split != std::string::npos;
    if (root->has_session) root->session = text.substr(split + 1);
    return root;
}
cJSON* cJSON_ParseWithLength(const char* data, size_t len) { return cJSON_Parse(std::string(data, len).c_str()); }
cJSON* cJSON_GetObjectItem(const cJSON* root, const char* key) {
    static cJSON type_item, session_item;
    if (!std::strcmp(key, "type")) { type_item.valuestring = root->type.c_str(); return &type_item; }
    session_item.valuestring = !std::strcmp(key, "session_id") && root->has_session ? root->session.c_str() : nullptr;
    return &session_item;
}
bool cJSON_IsString(const cJSON* item) { return item && item->valuestring; }
void cJSON_Delete(cJSON* root) { delete root; }
struct Application {
    std::deque<std::function<void()>> tasks;
    static Application& GetInstance() { static Application app; return app; }
    void Schedule(std::function<void()>&& task) { tasks.push_back(std::move(task)); }
    void Flush() { auto pending = std::move(tasks); tasks.clear(); for (auto& task : pending) task(); }
};
struct Udp { int sends = 0; int Send(const std::string&) { ++sends; return 1; } };
struct Mqtt { std::function<void(const std::string&, const std::string&)> message; void OnMessage(decltype(message) cb) { message = std::move(cb); } };
class MqttProtocol : public Protocol {
public:
    std::mutex channel_mutex_;
    std::shared_ptr<Udp> udp_ = std::make_shared<Udp>();
    std::unique_ptr<Mqtt> mqtt_ = std::make_unique<Mqtt>();
    std::string aes_nonce_ = std::string(16, '\0'), retired_session_id_ = "old";
    uint32_t local_sequence_ = 0;
    std::atomic<bool> awaiting_hello_{false}, remote_goodbye_{false};
    std::shared_ptr<std::atomic<bool>> alive_ = std::make_shared<std::atomic<bool>>(true);
    std::function<void(const cJSON*, uint32_t)> on_incoming_json_;
    std::function<void()> on_crypt;
    int closes = 0, hellos = 0;
    MqttProtocol() { BeginChannel(); @MQTT_CALLBACK@); }
    bool SendAudio(std::unique_ptr<AudioStreamPacket>);
    bool CryptAesCtr(const uint8_t* input, size_t size, const uint8_t*, uint8_t* out) { if (on_crypt) { auto hook = std::move(on_crypt); on_crypt = {}; hook(); } std::memcpy(out, input, size); return true; }
    void CloseAudioChannel(bool) { ++closes; }
    void ParseServerHello(const cJSON* root) { ++hellos; if (root->has_session) session_id_ = root->session; }
    void Receive(const char* data) { mqtt_->message("topic", data); }
};
bool socket_on_worker = false, check_socket_destruction = false;
int socket_destructions = 0, pongs = 0;
std::function<void()> on_pong;
struct WebSocket {
    int sends = 0;
    ~WebSocket() { if (check_socket_destruction) assert(socket_on_worker); ++socket_destructions; }
    std::function<void()> on_connected;
    std::function<void(const char*, size_t, bool)> data;
    std::function<void()> disconnected;
    std::function<void(const char*, size_t)> ping;
    void OnPing(decltype(ping) cb) { ping = std::move(cb); }
    void SendPong(const char*, size_t) { ++pongs; if (on_pong) on_pong(); }
    bool IsConnected() { if (on_connected) { auto hook = std::move(on_connected); on_connected = {}; hook(); } return true; }
    bool Send(const void*, size_t, bool) { ++sends; return true; }
    void OnData(decltype(data) cb) { data = std::move(cb); }
    void OnDisconnected(decltype(disconnected) cb) { disconnected = std::move(cb); }
};
class WebsocketProtocol : public Protocol {
public:
    std::shared_ptr<WebSocket> websocket_ = std::make_shared<WebSocket>();
    std::function<void(const cJSON*, uint32_t)> on_incoming_json_;
    int version_ = 1, hellos = 0;
    bool SendAudio(std::unique_ptr<AudioStreamPacket>);
    void ParseServerHello(const cJSON*) { ++hellos; }
    void CloseAudioChannel(bool send_goodbye = true);
    void Register(uint32_t generation) { @WS_CALLBACK@); @WS_CLOSED@); @WS_PING@ }
};
std::unique_ptr<AudioStreamPacket> Packet(uint32_t policy) { auto p = std::make_unique<AudioStreamPacket>(); p->conversation_policy = policy; p->payload = {1, 2, 3}; return p; }
'''


TRANSPORT_CHECKS = r'''
int main(int argc, char** argv) {
    assert(argc == 2); const std::string test = argv[1];
    std::atomic<uint32_t> policy{1};
    if (test == "send") {
        for (int version : {1, 2, 3}) {
            WebsocketProtocol ws; ws.version_ = version; ws.SetConversationPolicySource(&policy); policy.store(1);
            assert(ws.SendAudio(Packet(1)) && ws.websocket_->sends == 1);
            ws.websocket_->on_connected = [&]() { policy.store(2); policy.store(5); };
            assert(!ws.SendAudio(Packet(1)) && ws.websocket_->sends == 1);
            assert(ws.SendAudio(Packet(5)) && ws.websocket_->sends == 2);
            ws.SetChannelClosing(true); assert(!ws.SendAudio(Packet(5)));
        }
        MqttProtocol mqtt; mqtt.SetConversationPolicySource(&policy); policy.store(1);
        assert(mqtt.SendAudio(Packet(1)) && mqtt.udp_->sends == 1);
        mqtt.on_crypt = [&]() { policy.store(2); policy.store(5); };
        assert(!mqtt.SendAudio(Packet(1)) && mqtt.udp_->sends == 1);
        assert(mqtt.SendAudio(Packet(5)) && mqtt.udp_->sends == 2);
        mqtt.on_crypt = [&]() { mqtt.BeginChannel(); };
        assert(!mqtt.SendAudio(Packet(5)) && mqtt.udp_->sends == 2);
        mqtt.SetChannelClosing(true); assert(!mqtt.SendAudio(Packet(5)));
    } else if (test == "mqtt_sessions") {
        MqttProtocol mqtt; mqtt.SetConversationPolicySource(&policy);
        int received = 0; uint32_t generation = 0;
        mqtt.on_incoming_json_ = [&](const cJSON*, uint32_t token) { ++received; generation = token; };
        mqtt.Receive("tts"); mqtt.Receive("tts|old"); assert(received == 2);
        policy.store(5);
        for (const auto* type : {"tts", "stt", "llm"}) {
            mqtt.Receive(type); mqtt.Receive((std::string(type) + "|old").c_str());
        }
        assert(received == 2);
        mqtt.Receive("tts|current"); mqtt.Receive("stt|current"); mqtt.Receive("llm|current");
        assert(received == 5 && generation == mqtt.channel_generation_.load());
        mqtt.Receive("system"); mqtt.Receive("mcp"); mqtt.Receive("alert"); assert(received == 8);
        mqtt.awaiting_hello_.store(true); mqtt.Receive("tts|current"); assert(received == 8);
        mqtt.Receive("hello|old"); assert(mqtt.hellos == 0);
        mqtt.Receive("hello|fresh"); assert(mqtt.hellos == 1 && mqtt.session_id_ == "fresh");
        mqtt.awaiting_hello_.store(false); mqtt.Receive("tts|fresh"); assert(received == 9);
        policy.store(1); mqtt.Receive("goodbye|fresh");
        mqtt.BeginChannel(); Application::GetInstance().Flush(); assert(mqtt.closes == 0);
    } else if (test == "pong_close") {
        auto& app = Application::GetInstance();
        WebsocketProtocol ws;
        const auto old = ws.BeginChannel(); ws.Register(old);
        ws.websocket_->ping("a", 1); app.Flush(); assert(pongs == 1);
        auto stale_ping = ws.websocket_->ping;
        ws.websocket_->ping("b", 1);
        const auto fresh = ws.BeginChannel(); ws.Register(fresh);
        app.Flush(); assert(pongs == 1);
        stale_ping("c", 1); app.Flush(); assert(pongs == 1);
        ws.websocket_->ping("d", 1);
        ws.SetChannelClosing(true);
        check_socket_destruction = true;
        // If Pong reaches weak.lock(), simulate worker close while that main
        // callback owns the socket: the final destructor would run on main.
        on_pong = [&]() { socket_on_worker = true; ws.CloseAudioChannel(); socket_on_worker = false; };
        app.Flush();
        assert(pongs == 1 && socket_destructions == 0 && ws.websocket_.use_count() == 1);
        socket_on_worker = true; ws.CloseAudioChannel(); socket_on_worker = false;
        assert(socket_destructions == 1);
        // The token, unlike Protocol, is safe to retain across owner destruction.
        std::function<void(const char*, size_t)> retired_ping;
        {
            auto retired = std::make_unique<WebsocketProtocol>();
            retired->Register(retired->BeginChannel()); retired_ping = retired->websocket_->ping;
            retired->SetChannelClosing(true);
            socket_on_worker = true; retired->CloseAudioChannel(); retired.reset(); socket_on_worker = false;
        }
        retired_ping("e", 1); app.Flush(); assert(pongs == 1 && socket_destructions == 2);
        check_socket_destruction = false;
    } else if (test == "websocket_generation") {
        WebsocketProtocol ws;
        auto old = ws.BeginChannel(); ws.Register(old);
        auto old_data = ws.websocket_->data; auto old_close = ws.websocket_->disconnected;
        int audio = 0, json = 0; uint32_t last_audio = 0, last_json = 0, last_close = 0;
        ws.on_incoming_audio_ = [&](std::unique_ptr<AudioStreamPacket> p) { ++audio; last_audio = p->channel_generation; };
        ws.on_incoming_json_ = [&](const cJSON*, uint32_t generation) { ++json; last_json = generation; };
        ws.on_audio_channel_closed_ = [&](uint32_t generation) { last_close = generation; };
        char pcm[] = {1, 2, 3}; old_data(pcm, sizeof(pcm), true); assert(audio == 1 && last_audio == old);
        auto fresh = ws.BeginChannel(); ws.Register(fresh);
        old_data(pcm, sizeof(pcm), true); old_data("tts", 3, false); assert(audio == 1 && json == 0);
        old_close(); assert(last_close == old && last_close != fresh);
        ws.websocket_->data(pcm, sizeof(pcm), true); ws.websocket_->data("tts", 3, false);
        assert(audio == 2 && json == 1 && last_audio == fresh && last_json == fresh);
        ws.SetChannelClosing(true); ws.websocket_->data(pcm, sizeof(pcm), true); assert(audio == 2);
    } else { assert(false); }
}
'''
