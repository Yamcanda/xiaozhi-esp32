"""Exercise production wake-policy methods with deterministic host-side race interleavings."""

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
    """Use the production field types/defaults rather than recreating policy state."""
    lines = header.splitlines()
    return "\n".join(next(line for line in lines
                          if re.search(r"\b" + re.escape(name) + r"\b\s*(?:\{|=|;)", line)
                          and ";" in line) for name in names)


class WakeWordPolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        compiler = shlex.split(os.environ.get("CXX", "c++"))
        if not compiler or shutil.which(compiler[0]) is None:
            raise unittest.SkipTest("requires a host C++23 compiler")
        cls.tmp = tempfile.TemporaryDirectory(prefix="wake-word-policy-test-")
        cls.addClassCleanup(cls.tmp.cleanup)
        app = (ROOT / "main/application.cc").read_text(encoding="utf-8")
        app_header = (ROOT / "main/application.h").read_text(encoding="utf-8")
        audio = (ROOT / "main/audio/audio_service.cc").read_text(encoding="utf-8")
        audio_header = (ROOT / "main/audio/audio_service.h").read_text(encoding="utf-8")
        source = PLATFORM.replace("AUDIO_FIELDS", fields(audio_header, (
            "wake_word_detection_requested_", "wake_word_detection_inhibited_", "wake_word_policy_",
            "wake_word_running_policy_")))
        source = source.replace("std::atomic<uint32_t> wake_word_running_policy_",
                                "HookAtomic<uint32_t> wake_word_running_policy_")
        source = source.replace("APPLICATION_FIELDS", fields(app_header, (
            "wake_word_policy_", "wake_word_event_policy_", "wake_word_invocation_policy_",
            "wake_word_activation_handler_", "pending_wake_word_", "pending_wake_policy_",
            "conversation_policy_", "conversation_ready_policy_", "applied_conversation_policy_",
            "active_channel_generation_",
            "draining_channel_generation_", "reply_ended_", "protocol_closing_",
            "open_conversation_policy_", "reset_protocol_pending_", "reply_drain_deadline_",
            "protocol_initialization_task_", "protocol_initialization_pending_",
            "has_server_time_", "startup_completed_",
            "pending_startup_wake_word_hint_", "last_error_message_", "protocol_opening_",
            "protocol_open_close_pending_", "text_interaction_id_", "text_interaction_policy_",
            "text_reply_generation_", "text_interaction_muted_", "text_interaction_needs_close_",
            "deferred_mcp_messages_",
            "deferred_mcp_bytes_", "listening_start_guard_")))
        source = source.replace("@CLOSE_JOB@", definition(app_header, "struct ConversationCloseJob") + ";")
        source = source.replace("AUDIO_CONVERSATION_FIELDS", fields(audio_header, (
            "conversation_policy_", "reply_channel_generation_")))
        source = source.replace("AUDIO_CONVERSATION_POLICY", definition(
            audio_header, "void SetConversationPolicy("))
        source = source.replace("AUDIO_CALLBACK", definition(
            audio, "audio_engine_->OnWakeWordDetected("))
        source = source.replace("APPLICATION_CALLBACK", definition(
            app, "callbacks.on_wake_word_detected ="))
        for signature in (
            "void AudioService::EnableWakeWordDetection(",
            "void AudioService::SetWakeWordDetectionInhibited(",
            "void AudioService::ApplyWakeWordDetection(",
            "void AudioService::UpdateWakeWordRunningState(",
            "void AudioService::SetCallbacks(",
            "void AudioService::EncodeWakeWord(",
            "const std::string& AudioService::GetLastWakeWord(",
            "std::unique_ptr<AudioStreamPacket> AudioService::PopWakeWordPacket(",
        ):
            source += definition(audio, signature) + "\n"
        for signature in (
            "bool Application::SetDeviceState(",
            "void Application::Schedule(",
            "void Application::SetWakeWordDetectionEnabled(",
            "void Application::SetWakeWordActivationHandler(",
            "bool Application::IsWakeWordDetectionCurrent(",
            "bool Application::IsWakeWordInvocationCurrent(",
            "void Application::ResumeWakeWordActivation(",
            "bool Application::IsConversationCurrent(",
            "bool Application::IsConversationAvailable(",
            "void Application::SetConversationEnabled(",
            "void Application::ApplyConversationPolicy(",
            "void Application::FinishConversationDrain(",
            "void Application::CloseAudioChannel(",
            "void Application::CloseConversationChannel(",
            "void Application::ReleaseConversationClose(",
            "void Application::CompleteConversationClose(",
            "void Application::ResumeProtocolInitialization(",
            "void Application::HandleActivationDoneEvent(",
            "void Application::ResetProtocol(",
            "void Application::CancelWakeWordInvoke(",
            "void Application::HandleWakeWordDetectedEvent(",
            "void Application::BeginWakeWordInvoke(",
            "void Application::ContinueWakeWordInvoke(",
            "void Application::WakeWordInvoke(",
            "void Application::AbortSpeaking(",
            "void Application::SetListeningMode(",
        ):
            source += definition(app, signature) + "\n"
        # Preserve Run's detector-before-schedule priority and real batch swap.
        run = definition(app, "void Application::Run()")
        source += "void Application::FlushEvents() {\nconst auto bits = events; events = 0;\n"
        for match in re.finditer(
                r"if \(bits & (?:MAIN_EVENT_WAKE_WORD_DETECTED|MAIN_EVENT_SCHEDULE)\)", run):
            source += definition(run, match.group()) + "\n"
        source += "}\n"
        afe = (ROOT / "main/audio/engines/afe_audio_engine.cc").read_text(encoding="utf-8")
        afe_header = (ROOT / "main/audio/engines/afe_audio_engine.h").read_text(encoding="utf-8")
        lite = (ROOT / "main/audio/engines/lite_audio_engine.cc").read_text(encoding="utf-8")
        lite_header = (ROOT / "main/audio/engines/lite_audio_engine.h").read_text(encoding="utf-8")
        engines = ENGINE_PLATFORM.replace("AFE_FIELDS", fields(afe_header, (
            "wake_word_policy_", "processing_wake_word_policy_", "control_generation_")))
        engines = engines.replace("std::atomic<uint32_t> wake_word_policy_",
                                  "HookAtomic<uint32_t> wake_word_policy_")
        engines = engines.replace("LITE_FIELDS", fields(lite_header, ("wake_word_policy_",)))
        engines = engines.replace("MULTINET_CALLBACK", definition(
            afe, "custom_wake_word_->OnWakeWordDetected("))
        source += engines
        for signature in (
            "void AfeAudioEngine::EnableWakeWordDetection(",
            "void AfeAudioEngine::OnWakeWordDetected(",
            "void AfeAudioEngine::UpdateActiveState(",
            "bool AfeAudioEngine::ClaimWakeWordDetection(",
            "void AfeAudioEngine::HandleWakeWordResult(",
            "void AfeAudioEngine::ProcessingTask(",
        ):
            method = definition(afe, signature).replace("AfeAudioEngine::", "TestAfe::")
            if "::ProcessingTask(" in method:
                # Only bound the task loop: keep actual pre-fetch token capture, fetch,
                # generation checks, WakeNet/MultiNet dispatch and voice dispatch unchanged.
                method = method.replace("while (true)",
                                        "for (int host_step = 0; host_step < 1; ++host_step)", 1)
            source += method + "\n"
        for signature in (
            "void LiteAudioEngine::Feed(",
            "void LiteAudioEngine::EnableWakeWordDetection(",
            "void LiteAudioEngine::OnWakeWordDetected(",
            "bool LiteAudioEngine::CreateWakeWordLocked(",
        ):
            source += definition(lite, signature).replace("LiteAudioEngine::", "TestLite::") + "\n"
        source += CHECKS
        source_path = Path(cls.tmp.name) / "wake_word_policy.cc"
        source_path.write_text(source, encoding="utf-8")
        cls.binaries = []
        # Both engine branches and both wake-upload branches must compile and behave identically.
        for afe in (0, 1):
            for upload in (0, 1):
                executable = Path(cls.tmp.name) / f"wake-policy-{afe}-{upload}.exe"
                compiled = subprocess.run(
                    compiler + ["-std=c++23", "-Wall", "-Wextra", "-Werror",
                                "-Wno-unused-parameter", "-fno-exceptions", "-fno-rtti",
                                f"-DCONFIG_IDF_TARGET_ESP32S3={afe}",
                                f"-DCONFIG_SEND_WAKE_WORD_DATA={upload}",
                                str(source_path), "-o", str(executable)],
                    capture_output=True, text=True, timeout=60,
                )
                if compiled.returncode:
                    raise AssertionError(compiled.stdout + compiled.stderr)
                cls.binaries.append((afe, upload, str(executable)))

    def run_case(self, name):
        for afe, upload, binary in self.binaries:
            with self.subTest(afe=afe, upload=upload):
                result = subprocess.run([binary, name], capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_default_policy_preserves_wake_and_public_invocation(self):
        self.run_case("default")

    def test_inhibit_replays_latest_runtime_request_not_running_snapshot(self):
        self.run_case("intent")

    def test_inhibit_covers_initialization_failure_and_lite_restore(self):
        self.run_case("engine_failure")

    def test_disable_is_immediate_but_engine_changes_are_scheduled(self):
        self.run_case("immediate")

    def test_event_already_copied_by_run_is_rejected_after_disable_enable(self):
        self.run_case("event_snapshot")

    def test_callback_cannot_adopt_reenabled_policy_before_audio_rearm(self):
        self.run_case("callback_before_rearm")

    def test_callback_in_flight_when_policy_changes_stays_stale(self):
        self.run_case("callback_in_flight")

    def test_only_latest_scheduled_policy_applies_even_when_tasks_reorder(self):
        self.run_case("schedule_reorder")

    def test_stale_begin_does_not_encode_or_connect(self):
        self.run_case("stale_begin")

    def test_generation_is_rechecked_after_encoding_wake_audio(self):
        self.run_case("encode_race")

    def test_generation_is_rechecked_after_connecting_transition(self):
        self.run_case("transition_race")

    def test_queued_continuation_is_cancelled_without_opening_channel(self):
        self.run_case("queued_continuation")

    def test_policy_change_while_channel_open_blocks_never_starts_listening(self):
        self.run_case("open_race")

    def test_disable_enable_while_channel_open_blocks_rejects_old_generation(self):
        self.run_case("open_aba")

    def test_upload_wait_and_send_recheck_generation(self):
        self.run_case("upload_race")

    def test_public_scheduled_invocations_cannot_abort_close_or_take_over_when_stale(self):
        self.run_case("public_stale")

    def test_public_invocations_require_original_device_state(self):
        self.run_case("public_state")

    def test_policy_does_not_stop_existing_conversation_or_change_other_audio(self):
        self.run_case("active_conversation")

    def test_cancelled_wake_does_not_cancel_new_manual_connection(self):
        self.run_case("manual_connection")

    def test_afe_result_delivered_after_reenable_retains_original_policy_token(self):
        self.run_case("afe_delayed_delivery")

    def test_afe_fetch_crossing_reenable_is_rejected_even_with_voice_active(self):
        self.run_case("afe_fetch_aba")

    def test_multinet_callback_delivered_after_reenable_retains_fetch_policy(self):
        self.run_case("multinet_delayed_delivery")

    def test_lite_synchronous_dispatch_retains_policy_and_stale_delivery_is_rejected(self):
        self.run_case("lite_delayed_delivery")

    def test_wakenet_pre_mutation_race_cannot_disarm_reenabled_detection(self):
        self.run_case("afe_claim_aba")

    def test_multinet_pre_mutation_race_cannot_disarm_reenabled_detection(self):
        self.run_case("multinet_claim_aba")

    def test_service_pre_mutation_race_cannot_clear_reenabled_running_state(self):
        self.run_case("service_claim_aba")

    def test_afe_obsolete_state_publication_replays_latest_armed_state(self):
        self.run_case("afe_publish_aba")

    def test_service_obsolete_state_publication_replays_latest_running_state(self):
        self.run_case("service_publish_aba")

    def test_real_detector_opts_into_foreground_with_a_fresh_wake_invocation(self):
        self.run_case("foreground")

    def test_detector_handoff_waits_for_worker_close_and_main_completion(self):
        self.run_case("foreground_close")

    def test_close_completion_queued_before_same_turn_detector_waits_for_enabled_policy_apply(self):
        self.run_case("foreground_close_same_turn")

    def test_detector_in_protocol_null_retirement_gap_still_foregrounds_locally(self):
        self.run_case("foreground_null_gap")

    def test_close_completion_before_policy_apply_defers_initialization_notification(self):
        self.run_case("foreground_init_same_turn")

    def test_policy_close_waits_for_initialization_worker_completion(self):
        self.run_case("initialization_close")

    def test_reset_waits_for_initialization_worker_completion(self):
        self.run_case("initialization_reset")

    def test_handoff_survives_protocol_reset_until_replacement_is_initialized(self):
        self.run_case("foreground_reset_pending")

    def test_published_replacement_cannot_open_before_actual_activation_completion(self):
        self.run_case("foreground_early_protocol")

    def test_manual_invocation_cannot_use_foreground_activation_hook(self):
        self.run_case("foreground_manual")

    def test_stale_detector_cannot_foreground_after_conversation_exit_reentry(self):
        self.run_case("foreground_detector_aba")

    def test_pending_handoff_is_rejected_after_conversation_exit_reentry(self):
        self.run_case("foreground_pending_aba")

    def test_declined_or_non_enabling_activation_rearms_detection_without_takeover(self):
        self.run_case("foreground_declined")

    def test_foreground_hook_does_not_override_disabled_wake_policy(self):
        self.run_case("foreground_wake_disabled")

    def test_unregistered_board_keeps_detection_inhibited_outside_foreground(self):
        self.run_case("foreground_unregistered")

    def test_channel_failure_and_missing_protocol_remain_safe(self):
        self.run_case("failure")


PLATFORM = r'''
#include <atomic>
#include <cassert>
#include <cstdint>
#include <deque>
#include <functional>
#include <memory>
#include <mutex>
#include <new>
#include <string>
#include <utility>
#include <vector>
// A test-only atomic wrapper pauses exactly BEFORE a production CAS changes state.
// The method under test is not rewritten; the wrapper still performs a real atomic CAS.
template <typename T> class HookAtomic {
    std::atomic<T> value_;
public:
    std::function<void()> before_claim;
    HookAtomic(T value) : value_(value) {}
    T load() const { return value_.load(); }
    void store(T value) { value_.store(value); }
    operator T() const { return load(); }
    bool compare_exchange_strong(T& expected, T desired) {
        auto hook = std::move(before_claim);
        before_claim = {};
        if (hook) hook();
        return value_.compare_exchange_strong(expected, desired);
    }
};
#define ESP_LOGD(...) ((void)0)
#define ESP_LOGE(...) ((void)0)
#define ESP_LOGI(...) ((void)0)
#define TAG "WakePolicyTest"
constexpr uint32_t MAIN_EVENT_SCHEDULE = 1 << 0;
constexpr uint32_t MAIN_EVENT_WAKE_WORD_DETECTED = 1 << 2;
constexpr uint32_t AS_EVENT_WAKE_WORD_RUNNING = 1 << 1;
constexpr uint32_t AS_EVENT_AUDIO_PROCESSOR_RUNNING = 1 << 2;
constexpr int pdPASS = 1, pdTRUE = 1, portMAX_DELAY = -1;
using TaskHandle_t = void*;
using EventGroupHandle_t = uint32_t*;
int64_t esp_timer_get_time() { return 0; }
bool on_worker = false, activation_notified = false;
int close_tasks_created = 0, close_tasks_deleted = 0;
std::deque<std::function<void()>> workers;
std::function<void()> acknowledge_worker, on_activation_notify;
int xTaskCreate(void (*entry)(void*), const char*, int, void* arg, int, void*) {
    ++close_tasks_created;
    workers.push_back([entry, arg]() { entry(arg); });
    return pdPASS;
}
TaskHandle_t xTaskGetCurrentTaskHandle() { assert(on_worker); return reinterpret_cast<void*>(1); }
void xTaskNotifyGive(TaskHandle_t task) {
    assert(!on_worker);
    if (task == reinterpret_cast<void*>(2)) {
        activation_notified = true;
        if (on_activation_notify) on_activation_notify();
    }
}
void ulTaskNotifyTake(int, int) {
    assert(on_worker); on_worker = false; acknowledge_worker(); on_worker = true;
}
void vTaskDelete(void*) { assert(on_worker); ++close_tasks_deleted; }
void RunWorker() {
    assert(!workers.empty()); auto worker = std::move(workers.front()); workers.pop_front();
    on_worker = true; worker(); on_worker = false;
}
std::function<void(uint32_t)> on_set_bits, before_clear_bits;
uint32_t xEventGroupGetBits(EventGroupHandle_t events) { return *events; }
uint32_t xEventGroupClearBits(EventGroupHandle_t events, uint32_t bits) {
    if (before_clear_bits) before_clear_bits(bits);
    auto old = *events;
    *events &= ~bits;
    return old;
}
uint32_t xEventGroupSetBits(EventGroupHandle_t events, uint32_t bits) {
    *events |= bits;
    if (on_set_bits) on_set_bits(bits);
    return *events;
}
int resampler_resets = 0;
void esp_ae_rate_cvt_reset(void*) { ++resampler_resets; }
struct AudioStreamPacket { uint32_t conversation_policy = 1; std::vector<uint8_t> payload; };
struct AudioServiceCallbacks {
    std::function<void(const std::string&, uint32_t)> on_wake_word_detected;
};
class FakeEngine {
public:
    bool enabled = false, has_wake = true, restore_ok = true;
    int enables = 0, disables = 0, restores = 0, encodes = 0;
    int packets = 0;
    std::string last_word = "Hello";
    std::function<void(const std::string&, uint32_t)> detected;
    uint32_t policy = 1;
    std::function<void()> on_encode, on_pop;
    void OnWakeWordDetected(std::function<void(const std::string&, uint32_t)> callback) {
        detected = std::move(callback);
    }
    bool RestoreWakeWordResources() { ++restores; return restore_ok; }
    bool HasWakeWord() const { return has_wake; }
    void EnableWakeWordDetection(bool enable, uint32_t token = 1) {
        policy = token;
        enabled = enable;
        if (enable) ++enables; else ++disables;
    }
    void EncodeWakeWordData() { ++encodes; if (on_encode) on_encode(); }
    const std::string& GetLastDetectedWakeWord() const { return last_word; }
    bool GetWakeWordOpus(std::vector<uint8_t>& payload) {
        if (on_pop) on_pop();
        if (packets == 0) return false;
        --packets;
        payload = {1, 2, 3};
        return true;
    }
    void Detect() { enabled = false; if (detected) detected(last_word, policy); }
};
using LiteAudioEngine = FakeEngine;
class AudioService {
public:
    AUDIO_FIELDS
    AUDIO_CONVERSATION_FIELDS
    uint32_t events = 0;
    EventGroupHandle_t event_group_ = &events;
    std::unique_ptr<FakeEngine> audio_engine_ = std::make_unique<FakeEngine>();
    bool audio_engine_initialized_ = false, init_ok = true;
    int initializations = 0, decoder_resets = 0, sounds = 0;
    int voice_changes = 0, playback_changes = 0;
    std::mutex input_resampler_mutex_;
    void* input_resampler_ = reinterpret_cast<void*>(1);
    AudioServiceCallbacks callbacks_;
    AudioService() { AUDIO_CALLBACK); }
    bool InitializeAudioEngine() {
        ++initializations;
        if (!init_ok) return false;
        audio_engine_initialized_ = true;
        return true;
    }
    void EnableWakeWordDetection(bool requested);
    void SetWakeWordDetectionInhibited(bool inhibited, uint32_t policy = 1);
    void ApplyWakeWordDetection();
    void UpdateWakeWordRunningState();
    void SetCallbacks(AudioServiceCallbacks& callbacks);
    void EncodeWakeWord();
    const std::string& GetLastWakeWord() const;
    std::unique_ptr<AudioStreamPacket> PopWakeWordPacket();
    std::unique_ptr<AudioStreamPacket> PopPacketFromSendQueue() { return nullptr; }
    bool IsWakeWordRunning() const { return events & AS_EVENT_WAKE_WORD_RUNNING; }
    AUDIO_CONVERSATION_POLICY
    void SetReplyChannel(uint32_t generation) { reply_channel_generation_.store(generation); }
    // Audio queue fencing and reply draining are exercised by test_conversation_policy.py.
    void ApplyConversationPolicy() { ++voice_changes; }
    bool IsPlaybackIdle() const { return true; }
    void ResetDecoder() { ++decoder_resets; }
    void PlaySound(const std::string&) { ++sounds; }
};
enum DeviceState {
    kDeviceStateIdle, kDeviceStateConnecting, kDeviceStateListening,
    kDeviceStateSpeaking, kDeviceStateNotifying, kDeviceStateActivating,
    kDeviceStateWifiConfiguring
};
enum ListeningMode { kListeningModeAutoStop };
enum AbortReason { kAbortReasonNone, kAbortReasonWakeWordDetected };
namespace Lang::Sounds { const std::string OGG_POPUP = "popup", OGG_SUCCESS = "success"; }
namespace Lang::Strings { constexpr const char* VERSION = "version "; }
namespace SystemInfo { void PrintHeapStats() {} }
enum class PowerSaveLevel { PERFORMANCE, LOW_POWER };
struct Display {
    void ShowNotification(const char*) {}
    void SetChatMessage(const char*, const char*) {}
};
struct Ota {
    bool HasServerTime() const { return false; }
    std::string GetCurrentVersion() const { return "test"; }
};
class Board {
public:
    int power_changes = 0;
    Display display;
    Display* GetDisplay() { return &display; }
    static Board& GetInstance() { static Board board; return board; }
    void SetPowerSaveLevel(PowerSaveLevel) { ++power_changes; }
};
class Protocol {
public:
    bool opened = false, open_ok = true, closing = false;
    uint32_t generation = 0;
    int opens = 0, closes = 0, wake_messages = 0, audio_packets = 0;
    int starts = 0, aborts = 0;
    std::string last_wake_word;
    std::function<void()> on_open, on_send, on_wake_message;
    uint32_t channel_generation() const { return generation; }
    void SetChannelClosing(bool value) { closing = value; }
    bool UsesConversationPolicy() const { return false; }
    bool IsAudioChannelOpened() const { assert(!closing); return opened; }
    bool OpenAudioChannel() {
        assert(!closing && !on_worker);
        ++opens;
        ++generation;
        if (on_open) on_open();
        opened = open_ok;
        return open_ok;
    }
    void CloseAudioChannel() { assert(on_worker || !closing); ++closes; opened = false; }
    void SendAudio(std::unique_ptr<AudioStreamPacket>) {
        assert(!closing && !on_worker);
        ++audio_packets;
        if (on_send) on_send();
    }
    void SendWakeWordDetected(const std::string& word) {
        assert(!closing && !on_worker);
        last_wake_word = word;
        ++wake_messages;
        if (on_wake_message) on_wake_message();
    }
    void SendStartListening(ListeningMode) { assert(!closing && !on_worker); ++starts; }
    void SendAbortSpeaking(AbortReason) { assert(!closing && !on_worker); ++aborts; }
};
class DeviceStateMachine {
public:
    DeviceState state = kDeviceStateIdle;
    bool allow_transition = true;
    std::function<void(DeviceState)> on_transition;
    DeviceState GetState() const { return state; }
    bool TransitionTo(DeviceState next) {
        if (!allow_transition) return false;
        state = next;
        if (on_transition) on_transition(next);
        return true;
    }
};
class Application {
public:
    APPLICATION_FIELDS
    @CLOSE_JOB@
    bool pending_listening_start_ = false;
    // Guarded starts run with real helpers in test_text_interaction.py.
    void ResumeListeningAudio() { assert(!listening_start_guard_); }
    void ResumeProtocolInitialization();
    // Real text/worker interleavings are covered separately; no text is active here.
    void FinishTextInteraction(bool) { assert(text_interaction_id_ == 0); }
    void ResumeTextInteraction() { assert(text_interaction_id_ == 0); }
    void FlushDeferredMcpMessages() { assert(deferred_mcp_messages_.empty()); }
    void PublishAudioChannelOpened(uint32_t generation) {
        active_channel_generation_.store(generation); audio_service_.SetReplyChannel(generation);
    }
    void HandleActivationDoneEvent();
    void ResetProtocol();
    std::unique_ptr<Ota> ota_;
    AudioService audio_service_;
    AudioServiceCallbacks callbacks;
    std::mutex mutex_;
    std::deque<std::function<void()>> main_tasks_;
    std::shared_ptr<Protocol> protocol_ = std::make_shared<Protocol>();
    uint32_t events = 0;
    EventGroupHandle_t event_group_ = &events;
    DeviceStateMachine state_machine_;
    ListeningMode listening_mode_ = kListeningModeAutoStop;
    bool aborted_ = false, play_popup_on_listening_ = false;
    int notifications_stopped = 0;
    Application() {
        APPLICATION_CALLBACK;
        audio_service_.SetCallbacks(callbacks);
        acknowledge_worker = [this]() { Flush(); };
    }
    DeviceState GetDeviceState() const { return state_machine_.GetState(); }
    bool SetDeviceState(DeviceState state);
    void Schedule(std::function<void()>&& callback);
    void SetWakeWordDetectionEnabled(bool enabled);
    void SetWakeWordActivationHandler(std::function<bool()> handler);
    bool IsWakeWordDetectionCurrent(uint32_t policy) const;
    bool IsWakeWordInvocationCurrent(uint32_t policy) const;
    void ResumeWakeWordActivation();
    bool IsConversationAvailable() const;
    bool IsConversationCurrent(uint32_t policy) const;
    void SetConversationEnabled(bool enabled);
    void ApplyConversationPolicy(uint32_t policy);
    void FinishConversationDrain();
    void CloseAudioChannel();
    void CloseConversationChannel();
    void ReleaseConversationClose(ConversationCloseJob* job);
    void CompleteConversationClose();
    void CancelWakeWordInvoke(uint32_t policy);
    void HandleWakeWordDetectedEvent();
    void BeginWakeWordInvoke(const std::string& word, uint32_t policy);
    void ContinueWakeWordInvoke(const std::string& word, uint32_t policy);
    void WakeWordInvoke(const std::string& word);
    void AbortSpeaking(AbortReason reason);
    void SetListeningMode(ListeningMode mode);
    ListeningMode GetDefaultListeningMode() const { return kListeningModeAutoStop; }
    void StopNotification() { ++notifications_stopped; SetDeviceState(kDeviceStateIdle); }
    void FlushEvents();
    void Flush() {
        std::deque<std::function<void()>> tasks;
        {
            std::lock_guard<std::mutex> lock(mutex_);
            tasks = std::move(main_tasks_);
            main_tasks_.clear();
        }
        for (auto& task : tasks) task();
    }
    void Drain() { for (int i = 0; i < 10 && !main_tasks_.empty(); ++i) Flush(); }
    void Enable() { audio_service_.EnableWakeWordDetection(true); }
    void Detect() { audio_service_.audio_engine_->Detect(); }
    void ExpectNoTakeover() const {
        assert(protocol_->starts == 0 && protocol_->wake_messages == 0);
        assert(protocol_->closes == 0 && protocol_->aborts == 0);
        assert(!aborted_ && !play_popup_on_listening_);
        assert(GetDeviceState() != kDeviceStateListening);
    }
};
'''


ENGINE_PLATFORM = r'''
// Speech inference is stubbed, but all policy capture and detection dispatch methods
// below are extracted unchanged from production (only the task's infinite loop is bounded).
#define ESP_LOGE(...) ((void)0)
#define ESP_LOGW(...) ((void)0)
constexpr bool kUseAfeForVoiceProcessing = true;
constexpr int ESP_FAIL = -1, WAKENET_DETECTED = 1;
constexpr int pdFALSE = 0;
using EventBits_t = uint32_t;
uint32_t xEventGroupWaitBits(uint32_t* group, uint32_t, int, int, int) { return *group; }
struct afe_fetch_result_t {
    int ret_value = 0, wakeup_state = WAKENET_DETECTED, wakenet_model_index = 1;
    int16_t* data = nullptr;
    int data_size = 0;
};
struct FakeAfeInterface {
    afe_fetch_result_t result;
    std::function<void()> on_fetch;
    afe_fetch_result_t* fetch_with_delay(void*, int) {
        if (on_fetch) on_fetch();
        return &result;
    }
};
struct FakeCustomWakeWord {
    bool running = false;
    std::function<void(const std::string&)> callback;
    void OnWakeWordDetected(std::function<void(const std::string&)> cb) { callback = std::move(cb); }
    void Start() { running = true; }
    void Stop() { running = false; }
    void FeedMono(const int16_t*, size_t) {
        if (running) { running = false; callback("MultiNet"); }
    }
};
struct FakeWakeCache { void Store(const int16_t*, size_t) {} };
class TestAfe {
public:
    enum class WakeDetector { kNone, kWakeNet, kMultiNet };
    static constexpr EventBits_t kWakeWordEnabled = 1 << 0;
    static constexpr EventBits_t kVoiceProcessingEnabled = 1 << 1;
    static constexpr EventBits_t kAfeActive = 1 << 2;
    std::atomic<uint32_t> voice_policy_{1};
    uint32_t processing_voice_policy_ = 0;
    AFE_FIELDS
    WakeDetector wake_detector_ = WakeDetector::kWakeNet;
    std::unique_ptr<FakeCustomWakeWord> custom_wake_word_ = std::make_unique<FakeCustomWakeWord>();
    std::function<void(const std::string&, uint32_t)> wake_word_detected_callback_;
    uint32_t events = kWakeWordEnabled | kVoiceProcessingEnabled | kAfeActive;
    uint32_t* event_group_ = &events;
    std::string last_detected_wake_word_;
    std::vector<std::string> wake_words_ = {"WakeNet"};
    std::vector<int16_t> input_buffer_;
    std::mutex input_buffer_mutex_;
    void* afe_data_ = reinterpret_cast<void*>(1);
    FakeAfeInterface iface;
    FakeAfeInterface* afe_iface_ = &iface;
    std::atomic<bool> reset_pending_{false}, output_reset_pending_{false}, afe_control_dirty_{false};
    FakeWakeCache wake_word_audio_cache_;
    int voice_frames = 0;
    TestAfe() { MULTINET_CALLBACK); }
    bool HasWakeWord() const { return true; }
    void EnableWakeWordDetection(bool enable, uint32_t policy = 1);
    void OnWakeWordDetected(std::function<void(const std::string&, uint32_t)> callback);
    void UpdateActiveState();
    bool ClaimWakeWordDetection(uint32_t policy);
    void HandleWakeWordResult(const afe_fetch_result_t* result, uint32_t policy);
    void ProcessingTask();
    void ApplyPendingReset() { reset_pending_ = false; }
    void ApplyAfeControls() {}
    void HandleVoiceResult(const afe_fetch_result_t*, uint32_t) { ++voice_frames; }
};
struct EspWakeWord {
    std::function<void(const std::string&)> callback;
    void OnWakeWordDetected(std::function<void(const std::string&)> cb) { callback = std::move(cb); }
    bool Initialize(void*, void*) { return true; }
    void Start() {}
    void Stop() {}
    void Feed(const std::vector<int16_t>&) { callback("Lite"); }
};
class TestLite {
public:
    LITE_FIELDS
    bool should_have_wake_word_ = true;
    std::atomic<bool> wake_word_enabled_{false}, voice_processing_enabled_{false};
    std::unique_ptr<EspWakeWord> wake_word_;
    std::function<void(const std::string&, uint32_t)> wake_word_detected_callback_;
    std::mutex wake_word_mutex_;
    void* codec_ = nullptr;
    void* models_list_ = nullptr;
    void OnWakeWordDetected(std::function<void(const std::string&, uint32_t)> callback);
    bool CreateWakeWordLocked();
    void Feed(std::vector<int16_t>&& data, uint32_t voice_policy = 1);
    void EnableWakeWordDetection(bool enable, uint32_t policy = 1);
    void OutputRawAudio(const std::vector<int16_t>&, uint32_t) {}
};
'''


CHECKS = r'''
int main(int argc, char** argv) {
    assert(argc == 2);
    std::string test = argv[1];
    Application app;
    auto& audio = app.audio_service_;
    auto& engine = *audio.audio_engine_;
    auto& protocol = *app.protocol_;
    if (test == "default") {
        assert(app.wake_word_policy_ == 1 && !audio.IsWakeWordRunning());
        app.Enable();
        assert(audio.IsWakeWordRunning() && engine.enabled);
        const int enables = engine.enables;
        app.SetWakeWordDetectionEnabled(true);
        assert(app.main_tasks_.empty() && engine.enables == enables);
        app.Detect();
        assert(!audio.IsWakeWordRunning());
        app.HandleWakeWordDetectedEvent();
        assert(app.GetDeviceState() == kDeviceStateConnecting);
        app.Drain();
        assert(app.GetDeviceState() == kDeviceStateListening && protocol.opens == 1);
        assert(protocol.wake_messages == CONFIG_SEND_WAKE_WORD_DATA);
        Application manual;
        manual.protocol_->opened = true;
        manual.WakeWordInvoke("Button");
        manual.Drain();
        assert(manual.GetDeviceState() == kDeviceStateListening);
    } else if (test == "intent") {
        audio.SetWakeWordDetectionInhibited(true);
        for (int i = 0; i < 20; ++i) audio.EnableWakeWordDetection(true);
        assert(!audio.IsWakeWordRunning() && audio.initializations == 0);
        audio.SetWakeWordDetectionInhibited(false);
        assert(audio.IsWakeWordRunning());
        app.Detect();  // Runtime stopped itself, but requested intent is still true.
        assert(!audio.IsWakeWordRunning() && audio.wake_word_detection_requested_);
        audio.SetWakeWordDetectionInhibited(true);
        audio.SetWakeWordDetectionInhibited(false);
        assert(audio.IsWakeWordRunning());
        audio.SetWakeWordDetectionInhibited(true);
        audio.EnableWakeWordDetection(false);
        audio.SetWakeWordDetectionInhibited(false);
        assert(!audio.IsWakeWordRunning());
        audio.SetWakeWordDetectionInhibited(true);
        audio.EnableWakeWordDetection(true);
        audio.EnableWakeWordDetection(false);
        audio.EnableWakeWordDetection(true);
        audio.SetWakeWordDetectionInhibited(false);
        assert(audio.IsWakeWordRunning());
    } else if (test == "engine_failure") {
        audio.init_ok = false;
        app.Enable();
        assert(!audio.IsWakeWordRunning() && audio.wake_word_detection_requested_);
        audio.SetWakeWordDetectionInhibited(true);
        audio.init_ok = true;
        audio.SetWakeWordDetectionInhibited(false);
        assert(audio.IsWakeWordRunning());
        audio.EnableWakeWordDetection(false);
        engine.has_wake = false;
        audio.EnableWakeWordDetection(true);
        assert(!audio.IsWakeWordRunning());
        engine.has_wake = true;
        engine.restore_ok = false;
        audio.EnableWakeWordDetection(true);
        assert(audio.IsWakeWordRunning() == bool(CONFIG_IDF_TARGET_ESP32S3));
        engine.restore_ok = true;
        audio.EnableWakeWordDetection(true);
        assert(audio.IsWakeWordRunning());
    } else if (test == "immediate") {
        app.Enable();
        const int disables = engine.disables;
        app.SetWakeWordDetectionEnabled(false);
        assert(!app.IsWakeWordInvocationCurrent(1));
        assert(engine.enabled && engine.disables == disables);
        app.callbacks.on_wake_word_detected("Late", 1);
        assert(!(app.events & MAIN_EVENT_WAKE_WORD_DETECTED));
        app.WakeWordInvoke("Button");
        app.Flush();
        assert(!audio.IsWakeWordRunning() && !engine.enabled);
        assert(protocol.opens == 0 && engine.encodes == 0);
        app.ExpectNoTakeover();
    } else if (test == "event_snapshot") {
        app.Enable();
        app.Detect();
        auto snapshot = app.events;
        app.events = 0;  // Run already consumed/cleared the group, not its local snapshot.
        app.SetWakeWordDetectionEnabled(false);
        app.SetWakeWordDetectionEnabled(true);
        assert(snapshot & MAIN_EVENT_WAKE_WORD_DETECTED);
        app.HandleWakeWordDetectedEvent();  // Runs before queued policy callbacks in Run.
        assert(protocol.opens == 0 && engine.encodes == 0);
        app.Drain();
        app.ExpectNoTakeover();
        app.Detect();
        app.HandleWakeWordDetectedEvent();
        app.Drain();
        assert(app.GetDeviceState() == kDeviceStateListening);
    } else if (test == "callback_before_rearm") {
        app.Enable();
        app.SetWakeWordDetectionEnabled(false);
        app.SetWakeWordDetectionEnabled(true);
        app.Detect();  // Still the old engine run; do not stamp it with the new request.
        assert(!(app.events & MAIN_EVENT_WAKE_WORD_DETECTED));
        app.Flush();
        assert(audio.IsWakeWordRunning() && engine.enabled);
        app.ExpectNoTakeover();
        app.Detect();
        app.HandleWakeWordDetectedEvent();
        app.Drain();
        assert(app.GetDeviceState() == kDeviceStateListening);
    } else if (test == "callback_in_flight") {
        app.Enable();
        on_set_bits = [&](uint32_t bits) {
            if (bits != MAIN_EVENT_WAKE_WORD_DETECTED) return;
            app.SetWakeWordDetectionEnabled(false);
            app.SetWakeWordDetectionEnabled(true);
        };
        app.Detect();
        on_set_bits = {};
        app.HandleWakeWordDetectedEvent();
        app.Drain();
        assert(engine.encodes == 0 && protocol.opens == 0);
        app.ExpectNoTakeover();
    } else if (test == "schedule_reorder") {
        app.Enable();
        app.SetWakeWordDetectionEnabled(false);
        app.SetWakeWordDetectionEnabled(true);
        assert(app.main_tasks_.size() == 2);
        std::swap(app.main_tasks_[0], app.main_tasks_[1]);
        app.Flush();
        assert(audio.IsWakeWordRunning() && !audio.wake_word_detection_inhibited_);
        app.SetWakeWordDetectionEnabled(false);
        app.Flush();
        audio.EnableWakeWordDetection(true);  // Idle/recovery callback while inhibited.
        assert(!audio.IsWakeWordRunning());
        audio.EnableWakeWordDetection(false);  // Listening/WiFi policy changed meanwhile.
        app.SetWakeWordDetectionEnabled(true);
        app.Flush();
        assert(!audio.IsWakeWordRunning());
        audio.EnableWakeWordDetection(true);
        assert(audio.IsWakeWordRunning());
    } else if (test == "stale_begin") {
        auto old = app.wake_word_policy_.load();
        app.SetWakeWordDetectionEnabled(false);
        app.SetWakeWordDetectionEnabled(true);
        app.BeginWakeWordInvoke("Old", old);
        assert(engine.encodes == 0 && protocol.opens == 0);
        app.ExpectNoTakeover();
    } else if (test == "encode_race") {
        engine.on_encode = [&]() { app.SetWakeWordDetectionEnabled(false); };
        app.BeginWakeWordInvoke("Hello", app.wake_word_policy_);
        assert(engine.encodes == 1 && app.GetDeviceState() == kDeviceStateIdle);
        app.Drain();
        app.ExpectNoTakeover();
    } else if (test == "transition_race") {
        app.state_machine_.on_transition = [&](DeviceState state) {
            if (state == kDeviceStateConnecting) app.SetWakeWordDetectionEnabled(false);
        };
        app.BeginWakeWordInvoke("Hello", app.wake_word_policy_);
        assert(protocol.opens == 0 && app.GetDeviceState() == kDeviceStateIdle);
        app.Drain();
        app.ExpectNoTakeover();
    } else if (test == "queued_continuation") {
        app.BeginWakeWordInvoke("Hello", app.wake_word_policy_);
        assert(app.GetDeviceState() == kDeviceStateConnecting);
        app.SetWakeWordDetectionEnabled(false);
        app.SetWakeWordDetectionEnabled(true);
        app.Drain();  // Continuation is ahead of both policy-application callbacks.
        assert(protocol.opens == 0 && app.GetDeviceState() == kDeviceStateIdle);
        app.ExpectNoTakeover();
    } else if (test == "open_race" || test == "open_aba") {
        protocol.on_open = [&]() {
            app.SetWakeWordDetectionEnabled(false);
            if (test == "open_aba") app.SetWakeWordDetectionEnabled(true);
        };
        app.BeginWakeWordInvoke("Hello", app.wake_word_policy_);
        app.Drain();
        assert(protocol.opens == 1 && protocol.opened);
        assert(app.GetDeviceState() == kDeviceStateIdle);
        app.ExpectNoTakeover();  // An open transport is not an active voice conversation.
    } else if (test == "upload_race") {
#if CONFIG_SEND_WAKE_WORD_DATA
        for (int stage = 0; stage < 3; ++stage) {
            Application upload;
            auto& sender = *upload.protocol_;
            auto& detector = *upload.audio_service_.audio_engine_;
            sender.opened = true;
            detector.packets = 1;
            auto disable = [&]() { upload.SetWakeWordDetectionEnabled(false); };
            if (stage == 0) detector.on_pop = disable;
            if (stage == 1) sender.on_send = disable;
            if (stage == 2) sender.on_wake_message = disable;
            upload.BeginWakeWordInvoke("Upload", upload.wake_word_policy_);
            upload.Drain();
            assert(upload.GetDeviceState() == kDeviceStateIdle);
            assert(sender.audio_packets == (stage == 0 ? 0 : 1));
            assert(sender.wake_messages == (stage == 2 ? 1 : 0));
            assert(sender.starts == 0 && !upload.play_popup_on_listening_);
        }
#endif
    } else if (test == "public_stale") {
        for (auto state : {kDeviceStateIdle, kDeviceStateNotifying,
                           kDeviceStateSpeaking, kDeviceStateListening}) {
            Application queued;
            queued.state_machine_.state = state;
            queued.WakeWordInvoke("Button");
            queued.SetWakeWordDetectionEnabled(false);
            queued.SetWakeWordDetectionEnabled(true);
            queued.Drain();
            assert(queued.GetDeviceState() == state);
            assert(queued.protocol_->opens == 0 && queued.protocol_->closes == 0);
            assert(queued.protocol_->aborts == 0 && queued.notifications_stopped == 0);
            assert(queued.audio_service_.audio_engine_->encodes == 0);
        }
    } else if (test == "public_state") {
        app.WakeWordInvoke("Button");
        app.SetDeviceState(kDeviceStateSpeaking);
        app.Drain();
        assert(protocol.aborts == 0 && engine.encodes == 0);
        Application speaking;
        speaking.state_machine_.state = kDeviceStateSpeaking;
        speaking.WakeWordInvoke("Button");
        speaking.Drain();
        assert(speaking.protocol_->aborts == 1);
        Application listening;
        listening.state_machine_.state = kDeviceStateListening;
        listening.WakeWordInvoke("Button");
        listening.Drain();
        assert(listening.protocol_->closes == 1);
    } else if (test == "active_conversation") {
        for (auto state : {kDeviceStateListening, kDeviceStateSpeaking, kDeviceStateNotifying}) {
            Application active;
            active.Enable();
            active.state_machine_.state = state;
            active.protocol_->opened = true;
            active.SetWakeWordDetectionEnabled(false);
            active.Drain();
            assert(active.GetDeviceState() == state && active.protocol_->opened);
            assert(active.protocol_->closes == 0 && active.protocol_->aborts == 0);
            assert(active.notifications_stopped == 0);
            assert(active.audio_service_.decoder_resets == 0 && active.audio_service_.sounds == 0);
            assert(active.audio_service_.voice_changes == 0 && active.audio_service_.playback_changes == 0);
        }
    } else if (test == "manual_connection") {
        auto old = app.wake_word_policy_.load();
        app.BeginWakeWordInvoke("Hello", old);
        app.SetDeviceState(kDeviceStateIdle);  // Wake no longer owns this connection.
        app.SetDeviceState(kDeviceStateConnecting);  // A fresh manual request.
        app.SetWakeWordDetectionEnabled(false);
        app.Drain();
        assert(app.GetDeviceState() == kDeviceStateConnecting && protocol.opens == 0);
        app.ContinueWakeWordInvoke("Hello", old);
        assert(app.GetDeviceState() == kDeviceStateConnecting);
    } else if (test == "afe_delayed_delivery" || test == "multinet_delayed_delivery" ||
               test == "afe_fetch_aba") {
        app.Enable();
        TestAfe afe;
        if (test == "multinet_delayed_delivery") afe.wake_detector_ = TestAfe::WakeDetector::kMultiNet;
        afe.EnableWakeWordDetection(true, app.wake_word_policy_);
        std::vector<uint32_t> delivered;
        auto reenable = [&]() {
            // Keep voice processing active: the old AFE control_generation_ fence alone
            // cannot distinguish this disable/enable cycle.
            const auto control_generation = afe.control_generation_.load();
            app.SetWakeWordDetectionEnabled(false);
            app.Drain();
            afe.EnableWakeWordDetection(false, app.wake_word_policy_);
            app.SetWakeWordDetectionEnabled(true);
            app.Drain();
            afe.EnableWakeWordDetection(true, app.wake_word_policy_);
            assert(afe.control_generation_ == control_generation);
            assert(audio.IsWakeWordRunning() && engine.enabled);
        };
        bool delay_old_callback = true;
        afe.OnWakeWordDetected([&](const std::string& word, uint32_t token) {
            // Production dispatch has already captured the original token. Pause delivery
            // until policy and both engines have fully re-enabled, then enter production
            // AudioService/Application callbacks with the original result.
            if (delay_old_callback && test != "afe_fetch_aba") {
                delay_old_callback = false;
                reenable();
            }
            delivered.push_back(token);
            engine.detected(word, token);
        });
        if (test == "afe_fetch_aba") {
            afe.iface.on_fetch = reenable;
        }
        afe.ProcessingTask();
        assert(audio.IsWakeWordRunning());  // Stale callback must not clear service running bits.
        assert(!(app.events & MAIN_EVENT_WAKE_WORD_DETECTED));
        app.HandleWakeWordDetectedEvent();
        app.ExpectNoTakeover();
        assert(protocol.opens == 0 && engine.encodes == 0);
        if (test == "afe_fetch_aba") assert(delivered.empty());
        else assert(delivered.size() == 1 && delivered[0] == 1);
        // Even if delivery bypasses AudioService, Application itself must reject the old token.
        app.callbacks.on_wake_word_detected("Old", 1);
        assert(!(app.events & MAIN_EVENT_WAKE_WORD_DETECTED));
        afe.iface.on_fetch = {};
        afe.EnableWakeWordDetection(true, app.wake_word_policy_);
        afe.ProcessingTask();
        assert(delivered.back() == app.wake_word_policy_);
        assert(app.events & MAIN_EVENT_WAKE_WORD_DETECTED);
        assert(!audio.IsWakeWordRunning());
        app.HandleWakeWordDetectedEvent();
        app.Drain();
        assert(app.GetDeviceState() == kDeviceStateListening && protocol.opens == 1);
    } else if (test == "lite_delayed_delivery") {
        app.Enable();
        TestLite lite;
        assert(lite.CreateWakeWordLocked());
        lite.EnableWakeWordDetection(true, app.wake_word_policy_);
        std::string pending_word;
        uint32_t pending_token = 0;
        lite.OnWakeWordDetected([&](const std::string& word, uint32_t token) {
            // Capture actual Lite Feed/CreateWakeWordLocked callback output without
            // extending its mutex lifetime; defer only the downstream delivery.
            pending_word = word;
            pending_token = token;
        });
        lite.Feed(std::vector<int16_t>{1, 2, 3});
        assert(pending_token == 1 && !lite.wake_word_enabled_);
        app.SetWakeWordDetectionEnabled(false);
        app.Drain();
        lite.EnableWakeWordDetection(false, app.wake_word_policy_);
        app.SetWakeWordDetectionEnabled(true);
        app.Drain();
        lite.EnableWakeWordDetection(true, app.wake_word_policy_);
        engine.detected(pending_word, pending_token);
        assert(audio.IsWakeWordRunning() && !(app.events & MAIN_EVENT_WAKE_WORD_DETECTED));
        app.ExpectNoTakeover();
        lite.Feed(std::vector<int16_t>{4, 5, 6});
        assert(pending_token == app.wake_word_policy_);
        engine.detected(pending_word, pending_token);
        app.HandleWakeWordDetectedEvent();
        app.Drain();
        assert(app.GetDeviceState() == kDeviceStateListening && protocol.opens == 1);
    } else if (test == "afe_claim_aba" || test == "multinet_claim_aba" ||
               test == "service_claim_aba" || test == "afe_publish_aba" ||
               test == "service_publish_aba") {
        app.Enable();
        TestAfe afe;
        // Exercise UpdateActiveState's inactive path as well as wake-bit publication.
        afe.events &= ~TestAfe::kVoiceProcessingEnabled;
        if (test == "multinet_claim_aba") afe.wake_detector_ = TestAfe::WakeDetector::kMultiNet;
        afe.EnableWakeWordDetection(true, app.wake_word_policy_);
        afe.OnWakeWordDetected([&](const std::string& word, uint32_t token) {
            engine.detected(word, token);
        });
        bool reenabled = false;
        auto reenable = [&]() {
            reenabled = true;
            app.SetWakeWordDetectionEnabled(false);
            app.Drain();
            afe.EnableWakeWordDetection(false, app.wake_word_policy_);
            app.SetWakeWordDetectionEnabled(true);
            app.Drain();
            afe.EnableWakeWordDetection(true, app.wake_word_policy_);
            assert(audio.IsWakeWordRunning());
            assert((afe.events & (TestAfe::kWakeWordEnabled | TestAfe::kAfeActive)) ==
                   (TestAfe::kWakeWordEnabled | TestAfe::kAfeActive));
        };
        if (test == "afe_claim_aba" || test == "multinet_claim_aba") {
            afe.wake_word_policy_.before_claim = reenable;
        } else if (test == "service_claim_aba") {
            audio.wake_word_running_policy_.before_claim = reenable;
        } else {
            bool paused = false;
            before_clear_bits = [&](uint32_t bits) {
                const auto target = test == "afe_publish_aba"
                    ? TestAfe::kWakeWordEnabled : AS_EVENT_WAKE_WORD_RUNNING;
                if (paused || bits != target) return;
                paused = true;
                reenable();
            };
        }
        if (test == "service_claim_aba" || test == "service_publish_aba") {
            engine.detected("Old", 1);
        } else {
            afe.ProcessingTask();
        }
        before_clear_bits = {};
        assert(reenabled);
        // Resume the old production state mutation only AFTER complete disable+reenable.
        // Its CAS must fail or its event mirror must be reconciled to the latest state.
        assert(audio.IsWakeWordRunning());
        assert((afe.wake_word_policy_.load() & 1U) != 0);
        assert((afe.events & (TestAfe::kWakeWordEnabled | TestAfe::kAfeActive)) ==
               (TestAfe::kWakeWordEnabled | TestAfe::kAfeActive));
        if (test == "multinet_claim_aba") assert(afe.custom_wake_word_->running);
        assert(!(app.events & MAIN_EVENT_WAKE_WORD_DETECTED));
        app.HandleWakeWordDetectedEvent();
        app.ExpectNoTakeover();
        afe.ProcessingTask();
        assert(app.events & MAIN_EVENT_WAKE_WORD_DETECTED);
        assert(!audio.IsWakeWordRunning());
        app.HandleWakeWordDetectedEvent();
        app.Drain();
        assert(app.GetDeviceState() == kDeviceStateListening && protocol.opens == 1);
    } else if (test == "foreground" || test == "foreground_close") {
        const bool closing = test == "foreground_close";
        if (closing) {
            protocol.opened = true; protocol.generation = 7;
            app.active_channel_generation_.store(7);
        }
        int activations = 0;
        app.SetWakeWordActivationHandler([&]() {
            assert(!app.IsConversationAvailable() && !on_worker);
            ++activations;
            app.SetConversationEnabled(true);
            return true;
        });
        app.Enable(); app.SetConversationEnabled(false); app.Drain();
        const auto detected_policy = app.wake_word_policy_.load();
        assert(audio.IsWakeWordRunning() && engine.enabled);
        assert(app.IsWakeWordDetectionCurrent(detected_policy));
        assert(!app.IsWakeWordInvocationCurrent(detected_policy));
        assert(close_tasks_created == (closing ? 1 : 0));
        engine.last_word = "Foreground";
        app.Detect();  // Exercise the real AudioService and Application callbacks.
        assert(app.events & MAIN_EVENT_WAKE_WORD_DETECTED);
        assert(!audio.IsWakeWordRunning() && activations == 0);
        app.HandleWakeWordDetectedEvent();
        assert(activations == 1 && !app.IsConversationAvailable());
        assert(app.pending_wake_word_ == "Foreground");
        assert(app.pending_wake_policy_ == app.wake_word_policy_);
        assert(app.pending_wake_policy_ != detected_policy);
        assert(engine.encodes == 0 && protocol.opens == 0);
        if (closing) {
            app.Drain();
            assert(app.protocol_closing_ && protocol.closing);
            assert(engine.encodes == 0 && protocol.opens == 0 && protocol.closes == 0);
            assert(app.GetDeviceState() == kDeviceStateIdle && app.pending_wake_policy_ != 0);
            RunWorker();
            // Even a finished worker cannot begin until main acknowledges completion.
            assert(protocol.closes == 1 && close_tasks_deleted == 1);
            assert(app.protocol_closing_ && engine.encodes == 0 && protocol.opens == 0);
            app.Flush();
            assert(!app.protocol_closing_ && !protocol.closing && app.IsConversationAvailable());
            assert(app.GetDeviceState() == kDeviceStateConnecting && engine.encodes == 1);
            assert(protocol.opens == 0 && app.wake_word_invocation_policy_ == app.wake_word_policy_);
        }
        app.Drain();
        assert(app.GetDeviceState() == kDeviceStateListening && app.IsConversationAvailable());
        assert(app.pending_wake_policy_ == 0 && app.pending_wake_word_.empty());
        assert(app.wake_word_invocation_policy_ == 0 && engine.encodes == 1);
        assert(protocol.opens == 1 && protocol.closes == (closing ? 1 : 0));
        assert(app.active_channel_generation_ == protocol.channel_generation());
        assert(protocol.wake_messages == CONFIG_SEND_WAKE_WORD_DATA);
#if CONFIG_SEND_WAKE_WORD_DATA
        assert(protocol.last_wake_word == "Foreground");
#endif
        assert(protocol.aborts == 0 && audio.decoder_resets == 0);
    } else if (test == "foreground_close_same_turn") {
        protocol.opened = true; protocol.generation = 7;
        app.active_channel_generation_.store(7);
        int activations = 0;
        app.SetWakeWordActivationHandler([&]() {
            ++activations; app.SetConversationEnabled(true); return true;
        });
        app.Enable(); app.SetConversationEnabled(false); app.Drain();
        RunWorker();  // Leaves CompleteConversationClose queued before the new wake.
        assert(app.protocol_closing_ && app.main_tasks_.size() == 1);
        assert(protocol.closes == 1 && audio.IsWakeWordRunning());
        app.Detect();
        assert((app.events & (MAIN_EVENT_WAKE_WORD_DETECTED | MAIN_EVENT_SCHEDULE)) ==
               (MAIN_EVENT_WAKE_WORD_DETECTED | MAIN_EVENT_SCHEDULE));
        app.FlushEvents();  // Detector, then [Complete, Apply(enabled), Resume].
        for (int i = 0; i < 10 && !app.main_tasks_.empty(); ++i) app.FlushEvents();
        // A second retirement may be necessary, but the accepted wake must survive it.
        while (!workers.empty()) {
            RunWorker();
            for (int i = 0; i < 10 && !app.main_tasks_.empty(); ++i) app.FlushEvents();
        }
        assert(activations == 1 && app.IsConversationAvailable());
        assert(app.GetDeviceState() == kDeviceStateListening && protocol.opens == 1);
        assert(engine.encodes == 1 && protocol.wake_messages == CONFIG_SEND_WAKE_WORD_DATA);
        assert(app.pending_wake_policy_ == 0 && app.wake_word_invocation_policy_ == 0);
    } else if (test == "foreground_init_same_turn") {
        protocol.opened = true; protocol.generation = 7;
        app.active_channel_generation_.store(7);
        int activations = 0;
        app.SetWakeWordActivationHandler([&]() {
            ++activations; app.SetConversationEnabled(true); return true;
        });
        app.Enable(); app.SetConversationEnabled(false); app.Drain();
        app.ResetProtocol(); app.Drain();
        app.protocol_initialization_task_ = reinterpret_cast<void*>(2);
        RunWorker();  // Queues Complete before this turn's detector enables voice.
        assert(!app.protocol_ && !activation_notified);
        on_activation_notify = [&]() {
            assert(app.applied_conversation_policy_ == app.conversation_policy_.load());
            assert(app.conversation_ready_policy_ == app.conversation_policy_.load());
            assert(app.protocol_initialization_pending_ && !app.protocol_closing_);
            app.protocol_ = std::make_shared<Protocol>();
        };
        app.Detect(); app.FlushEvents(); app.Drain();
        assert(activations == 1 && activation_notified && workers.empty());
        assert(app.protocol_initialization_pending_ && !app.protocol_->closing);
        assert(app.pending_wake_policy_ != 0 && engine.encodes == 0);
        app.ota_ = std::make_unique<Ota>();
        app.HandleActivationDoneEvent(); app.Drain();
        assert(app.GetDeviceState() == kDeviceStateListening && app.protocol_->opens == 1);
        assert(engine.encodes == 1 && app.pending_wake_policy_ == 0);
    } else if (test == "initialization_close" || test == "initialization_reset") {
        const bool reset = test == "initialization_reset";
        protocol.opened = true; protocol.generation = 7;
        app.protocol_initialization_pending_ = true;
        const auto* initializing = app.protocol_.get();
        app.SetConversationEnabled(false); app.Drain();
        app.SetConversationEnabled(true); app.Drain();
        if (reset) { app.ResetProtocol(); app.Drain(); }
        assert(app.protocol_.get() == initializing && !protocol.closing);
        assert(workers.empty() && protocol.closes == 0 && !app.protocol_closing_);
        assert(app.conversation_ready_policy_ == 0 && app.protocol_initialization_pending_);
        app.ota_ = std::make_unique<Ota>();
        app.HandleActivationDoneEvent(); app.Drain();
        assert(!app.protocol_initialization_pending_ && app.protocol_closing_);
        assert(workers.size() == 1 && protocol.closing && protocol.closes == 0);
        RunWorker(); app.Drain();
        assert(!app.protocol_closing_ && app.IsConversationAvailable());
        assert(!app.reset_protocol_pending_ && bool(app.protocol_) == !reset);
    } else if (test == "foreground_null_gap" || test == "foreground_reset_pending" ||
               test == "foreground_early_protocol") {
        const bool null_gap = test == "foreground_null_gap";
        const bool early_protocol = test == "foreground_early_protocol";
        protocol.opened = true; protocol.generation = 7;
        app.active_channel_generation_.store(7);
        int activations = 0;
        app.SetWakeWordActivationHandler([&]() {
            assert(!on_worker && !app.IsConversationAvailable());
            ++activations; app.SetConversationEnabled(true); return true;
        });
        app.Enable(); app.SetConversationEnabled(false); app.Drain();
        assert(app.protocol_closing_ && audio.IsWakeWordRunning());
        app.ResetProtocol(); app.Drain();
        app.protocol_initialization_task_ = reinterpret_cast<void*>(2);
        assert(app.reset_protocol_pending_ && close_tasks_created == 1);
        if (early_protocol) {
            on_activation_notify = [&]() {
                // Pause the awakened worker after factory assignment, before Start returns.
                assert(app.protocol_initialization_pending_ && !app.protocol_);
                on_worker = true;
                app.protocol_ = std::make_shared<Protocol>();
                on_worker = false;
            };
        }
        if (!null_gap) {
            app.Detect(); app.HandleWakeWordDetectedEvent(); app.Drain();
            assert(activations == 1 && app.pending_wake_policy_ != 0 && engine.encodes == 0);
        }
        acknowledge_worker = [&]() {
            app.Flush();  // Execute production ReleaseConversationClose on main.
            assert(!app.protocol_ && app.protocol_closing_ && !activation_notified);
            if (null_gap) {
                // A real detector can finish while the old protocol's worker still owns teardown.
                assert(audio.IsWakeWordRunning() && engine.enabled);
                app.Detect();
                assert(app.events & MAIN_EVENT_WAKE_WORD_DETECTED);
                app.HandleWakeWordDetectedEvent();
                assert(activations == 1 && app.pending_wake_policy_ != 0);
                app.Drain();
            }
            assert(engine.encodes == 0 && !app.IsConversationAvailable());
        };
        RunWorker();
        assert(!app.protocol_ && app.protocol_closing_ && !activation_notified);
        app.Drain();
        // Notifying initialization is not completion, even if its worker publishes protocol_.
        assert(activation_notified && !app.protocol_closing_ && app.protocol_initialization_pending_);
        assert(bool(app.protocol_) == early_protocol);
        assert(app.pending_wake_policy_ != 0 && app.pending_wake_word_ == "Hello");
        assert(engine.encodes == 0 && app.GetDeviceState() == kDeviceStateIdle);
        if (early_protocol) {
            // A second actual detector must not bypass the pending initialization.
            assert(audio.IsWakeWordRunning());
            app.Detect(); app.HandleWakeWordDetectedEvent(); app.Drain();
            assert(app.protocol_->opens == 0 && engine.encodes == 0);
            assert(app.pending_wake_policy_ != 0 && activations == 1);
        }
        if (!early_protocol) app.protocol_ = std::make_shared<Protocol>();
        app.ResumeWakeWordActivation(); app.Drain();
        assert(app.protocol_->opens == 0 && engine.encodes == 0);
        assert(app.pending_wake_policy_ != 0 && app.GetDeviceState() == kDeviceStateIdle);
        // Only the extracted main-task completion handler may release the handoff.
        app.ota_ = std::make_unique<Ota>();
        app.HandleActivationDoneEvent(); app.Drain();
        assert(!app.protocol_initialization_pending_);
        assert(app.GetDeviceState() == kDeviceStateListening && app.IsConversationAvailable());
        assert(app.pending_wake_policy_ == 0 && engine.encodes == 1 && activations == 1);
        assert(app.protocol_->opens == 1 && app.protocol_->wake_messages == CONFIG_SEND_WAKE_WORD_DATA);
    } else if (test == "foreground_manual") {
        int activations = 0;
        app.SetWakeWordActivationHandler([&]() { ++activations; app.SetConversationEnabled(true); return true; });
        app.Enable(); app.SetConversationEnabled(false); app.Drain();
        const auto policy = app.conversation_policy_.load();
        for (auto state : {kDeviceStateIdle, kDeviceStateNotifying,
                           kDeviceStateSpeaking, kDeviceStateListening}) {
            app.state_machine_.state = state;
            app.WakeWordInvoke("Button");
            assert(app.main_tasks_.empty());
            app.Drain();
            assert(app.GetDeviceState() == state && app.conversation_policy_ == policy);
        }
        assert(activations == 0 && app.pending_wake_policy_ == 0 && app.pending_wake_word_.empty());
        assert(protocol.opens == 0 && protocol.closes == 0 && protocol.aborts == 0);
        assert(engine.encodes == 0 && app.notifications_stopped == 0 && audio.IsWakeWordRunning());
    } else if (test == "foreground_detector_aba") {
        int activations = 0;
        app.SetWakeWordActivationHandler([&]() { ++activations; app.SetConversationEnabled(true); return true; });
        app.Enable();
        const auto foreground_old = app.wake_word_policy_.load();
        app.Detect(); app.events = 0;
        app.SetConversationEnabled(false); app.SetConversationEnabled(true); app.Drain();
        app.HandleWakeWordDetectedEvent();  // Old foreground event in Run's bit snapshot.
        engine.detected("Old foreground", foreground_old);
        app.callbacks.on_wake_word_detected("Old foreground", foreground_old);
        app.HandleWakeWordDetectedEvent(); app.Drain();
        assert(app.IsConversationAvailable() && audio.IsWakeWordRunning());
        assert(activations == 0 && protocol.opens == 0 && engine.encodes == 0);
        assert(!(app.events & MAIN_EVENT_WAKE_WORD_DETECTED));
        app.SetConversationEnabled(false); app.Drain();
        const auto old = app.wake_word_policy_.load();
        app.Detect();
        assert(app.events & MAIN_EVENT_WAKE_WORD_DETECTED);
        app.events = 0;  // Run has copied the old detection bit into its local snapshot.
        app.SetConversationEnabled(true); app.SetConversationEnabled(false); app.Drain();
        assert(audio.IsWakeWordRunning() && app.wake_word_policy_ != old);
        app.HandleWakeWordDetectedEvent();
        engine.detected("Old", old);  // Original detector result delivered after rearming.
        app.callbacks.on_wake_word_detected("Old", old);  // Application also fences it.
        app.HandleWakeWordDetectedEvent(); app.Drain();
        assert(!(app.events & MAIN_EVENT_WAKE_WORD_DETECTED) && audio.IsWakeWordRunning());
        assert(activations == 0 && app.pending_wake_policy_ == 0 && engine.encodes == 0);
        assert(protocol.opens == 0 && !app.IsConversationAvailable());
        app.Detect(); app.HandleWakeWordDetectedEvent(); app.Drain();
        assert(activations == 1 && protocol.opens == 1 && app.GetDeviceState() == kDeviceStateListening);
    } else if (test == "foreground_pending_aba") {
        protocol.opened = true; protocol.generation = 7;
        app.active_channel_generation_.store(7);
        int activations = 0;
        app.SetWakeWordActivationHandler([&]() { ++activations; app.SetConversationEnabled(true); return true; });
        app.Enable(); app.SetConversationEnabled(false); app.Drain();
        app.Detect(); app.HandleWakeWordDetectedEvent(); app.Drain();
        const auto old_pending = app.pending_wake_policy_;
        assert(old_pending != 0 && activations == 1 && close_tasks_created == 1);
        assert(!app.IsConversationAvailable() && engine.encodes == 0);
        app.SetConversationEnabled(false); app.SetConversationEnabled(true); app.Drain();
        assert(app.wake_word_policy_ != old_pending && close_tasks_created == 1);
        RunWorker();
        assert(app.pending_wake_policy_ == old_pending && engine.encodes == 0);
        app.Drain();
        assert(app.IsConversationAvailable() && app.GetDeviceState() == kDeviceStateIdle);
        assert(app.pending_wake_policy_ == 0 && app.pending_wake_word_.empty());
        assert(engine.encodes == 0 && protocol.opens == 0 && protocol.closes == 1);
        assert(protocol.wake_messages == 0 && activations == 1);
    } else if (test == "foreground_declined") {
        int activations = 0;
        bool accepted = false;
        app.SetWakeWordActivationHandler([&]() { ++activations; return accepted; });
        app.Enable(); app.SetConversationEnabled(false); app.Drain();
        for (bool response : {false, true}) {
            // Returning true alone is not permission: the board must also enable foreground.
            accepted = response;
            app.Detect(); app.HandleWakeWordDetectedEvent(); app.Drain();
            assert(!app.IsConversationAvailable() && audio.IsWakeWordRunning());
            assert(app.pending_wake_policy_ == 0 && engine.encodes == 0 && protocol.opens == 0);
        }
        assert(activations == 2);
    } else if (test == "foreground_wake_disabled") {
        int activations = 0;
        app.SetWakeWordActivationHandler([&]() { ++activations; app.SetConversationEnabled(true); return true; });
        app.Enable(); app.SetWakeWordDetectionEnabled(false); app.SetConversationEnabled(false); app.Drain();
        assert(!audio.IsWakeWordRunning() && !engine.enabled && audio.wake_word_detection_inhibited_);
        engine.detected("Disabled", app.wake_word_policy_);
        app.callbacks.on_wake_word_detected("Disabled", app.wake_word_policy_);
        app.HandleWakeWordDetectedEvent(); app.Drain();
        assert(activations == 0 && engine.encodes == 0 && protocol.opens == 0);
        app.SetWakeWordDetectionEnabled(true); app.Drain();
        assert(audio.IsWakeWordRunning() && !audio.wake_word_detection_inhibited_);
        app.Detect(); app.HandleWakeWordDetectedEvent(); app.Drain();
        assert(activations == 1 && app.GetDeviceState() == kDeviceStateListening);
    } else if (test == "foreground_unregistered") {
        protocol.opened = true; protocol.generation = 7;
        app.active_channel_generation_.store(7);
        app.Enable(); app.SetConversationEnabled(false); app.Drain();
        assert(app.protocol_closing_ && audio.wake_word_detection_inhibited_ && !audio.IsWakeWordRunning());
        RunWorker(); app.Drain();
        assert(!app.protocol_closing_ && audio.wake_word_detection_inhibited_ && !engine.enabled);
        app.SetWakeWordDetectionEnabled(false); app.SetWakeWordDetectionEnabled(true); app.Drain();
        assert(audio.wake_word_detection_inhibited_ && !audio.IsWakeWordRunning());
        const auto policy = app.wake_word_policy_.load();
        assert(!app.IsWakeWordDetectionCurrent(policy) && !app.IsWakeWordInvocationCurrent(policy));
        app.callbacks.on_wake_word_detected("Unregistered", policy);
        app.WakeWordInvoke("Button"); app.HandleWakeWordDetectedEvent(); app.Drain();
        assert(!(app.events & MAIN_EVENT_WAKE_WORD_DETECTED));
        assert(app.pending_wake_policy_ == 0 && engine.encodes == 0 && protocol.opens == 0);
        app.SetConversationEnabled(true); app.Drain(); RunWorker(); app.Drain();
        assert(app.IsConversationAvailable() && audio.IsWakeWordRunning());
        assert(protocol.opens == 0 && protocol.aborts == 0);
    } else if (test == "failure") {
        protocol.open_ok = false;
        app.BeginWakeWordInvoke("Hello", app.wake_word_policy_);
        app.Drain();
        assert(app.GetDeviceState() == kDeviceStateIdle && protocol.opens == 1);
        app.ExpectNoTakeover();
        Application no_protocol;
        no_protocol.protocol_.reset();
        no_protocol.WakeWordInvoke("Button");
        no_protocol.Detect();
        no_protocol.HandleWakeWordDetectedEvent();
        no_protocol.BeginWakeWordInvoke("Missing", no_protocol.wake_word_policy_);
        no_protocol.Drain();
        assert(no_protocol.audio_service_.audio_engine_->encodes == 0);
        Application denied;
        denied.state_machine_.allow_transition = false;
        denied.BeginWakeWordInvoke("Denied", denied.wake_word_policy_);
        assert(denied.GetDeviceState() == kDeviceStateIdle);
        assert(denied.audio_service_.IsWakeWordRunning());
    } else {
        assert(false);
    }
}
'''
