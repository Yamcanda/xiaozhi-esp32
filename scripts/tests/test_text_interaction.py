"""Run production text/epoch/lifecycle methods and real cJSON on a host compiler.

No firmware build, network, wake engine or audio codec is needed. The application
harness uses the real text path, serialized close and manual/wake continuations;
only RTOS, network and hardware boundaries are deterministic fakes.
"""

import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import unittest

if __package__:
    from .test_conversation_policy import definition, fields
else:
    from test_conversation_policy import definition, fields


ROOT = Path(__file__).resolve().parents[2]


class TextInteractionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        compiler = shlex.split(os.environ.get("CXX", "c++"))
        if not compiler or shutil.which(compiler[0]) is None:
            raise unittest.SkipTest("requires a host C++23 compiler")
        cls.tmp = tempfile.TemporaryDirectory(prefix="text-interaction-test-")
        cls.addClassCleanup(cls.tmp.cleanup)
        build = Path(cls.tmp.name)
        cjson = ROOT / "managed_components/espressif__cjson/cJSON"
        # Prefer the real checked-out dependency when present; ordinary clean
        # host checkouts can use their system libcjson-dev instead.
        if (cjson / "cJSON.c").exists():
            json_flags = ["-I" + str(cjson), "-x", "c++", str(cjson / "cJSON.c")]
        else:
            json_flags = shlex.split(os.environ.get("TEXT_CJSON_CFLAGS", "-I/usr/include/cjson"))
            json_flags += shlex.split(os.environ.get("TEXT_CJSON_LIBS", "-lcjson"))
        (build / "esp_log.h").write_text("\n".join(
            f"#define ESP_LOG{level}(...) ((void)0)" for level in "EWID"), encoding="utf-8")
        (build / "assets.h").write_text(r'''
            #pragma once
            #include <string>
            struct Assets {
                struct Capability { bool glyph_push = false; std::string bundle, charset; int size = 20, bpp = 4; };
                static Assets& GetInstance() { static Assets assets; return assets; }
                Capability text_font_capability() { return {}; }
            };
        ''', encoding="utf-8")
        app = (ROOT / "main/application.cc").read_text(encoding="utf-8")
        header = (ROOT / "main/application.h").read_text(encoding="utf-8")
        source = PLATFORM.replace("@FIELDS@", fields(header, (
            "conversation_policy_", "conversation_ready_policy_", "applied_conversation_policy_",
            "active_channel_generation_", "protocol_instance_", "draining_channel_generation_",
            "reply_ended_", "protocol_closing_", "protocol_opening_", "protocol_open_close_pending_",
            "next_text_interaction_id_", "text_interaction_id_", "text_interaction_policy_",
            "text_interaction_generation_", "text_reply_generation_", "text_interaction_deadline_",
            "text_interaction_needs_close_", "text_interaction_sent_", "text_interaction_muted_",
            "pending_text_interaction_", "text_interaction_completion_", "kMaxDeferredMcpBytes",
            "deferred_mcp_messages_", "deferred_mcp_bytes_", "toggle_chat_policy_",
            "start_listening_policy_", "stop_listening_policy_", "open_conversation_policy_",
            "reset_protocol_pending_", "protocol_initialization_task_", "protocol_initialization_pending_",
            "reply_drain_deadline_", "wake_word_policy_", "wake_word_event_policy_",
            "wake_word_invocation_policy_", "wake_word_activation_handler_", "pending_wake_word_",
            "pending_wake_policy_", "listening_start_guard_")))
        for name in ("ConversationCloseJob", "TextInteractionOpenJob"):
            source = source.replace("@JOBS@", definition(header, "struct " + name) + ";\n@JOBS@")
        source = source.replace("@JOBS@", "")
        for marker, signature in (
            ("@AUDIO_CALLBACK@", "protocol_->OnIncomingAudio("),
            ("@OPENED_CALLBACK@", "protocol_->OnAudioChannelOpened("),
            ("@CLOSED_CALLBACK@", "protocol_->OnAudioChannelClosed("),
            ("@ERROR_CALLBACK@", "protocol_->OnNetworkError("),
            ("@SEND_AUDIO@", "if (bits & MAIN_EVENT_SEND_AUDIO)"),
            ("@PLAYBACK@", "if (bits & MAIN_EVENT_PLAYBACK_DRAINED)"),
            ("@CLOCK@", "if (bits & MAIN_EVENT_CLOCK_TICK)"),
            ("@TTS@", 'else if (strcmp(type->valuestring, "tts") == 0)'),
        ):
            method = definition(app, signature)
            if marker == "@TTS@":
                method = method.replace("else if", "if", 1)
            source = source.replace(marker, method)
        for signature in (
            "bool Application::SetDeviceState(", "void Application::Schedule(",
            "bool Application::RequestTextInteraction(", "void Application::ResumeTextInteraction(",
            "void Application::CompleteTextInteractionOpen(", "void Application::FinishTextInteraction(",
            "void Application::PublishAudioChannelOpened(", "void Application::FlushDeferredMcpMessages(",
            "void Application::SendMcpMessage(", "void Application::SetConversationEnabled(",
            "void Application::ApplyConversationPolicy(", "void Application::FinishConversationDrain(",
            "void Application::CloseAudioChannel(", "void Application::CloseConversationChannel(",
            "void Application::ReleaseConversationClose(", "void Application::CompleteConversationClose(",
            "void Application::ResumeProtocolInitialization(", "void Application::ResetProtocol(",
            "bool Application::IsConversationCurrent(", "bool Application::IsConversationAvailable(",
            "bool Application::IsChannelCurrent(", "bool Application::CanReceiveReply(",
            "void Application::HandleToggleChatEvent(", "void Application::HandleStartListeningEvent(",
            "void Application::HandleStopListeningEvent(", "void Application::ContinueOpenAudioChannel(",
            "void Application::HandleWakeWordDetectedEvent(", "void Application::ResumeWakeWordActivation(",
            "void Application::BeginWakeWordInvoke(", "void Application::ContinueWakeWordInvoke(",
            "void Application::CancelWakeWordInvoke(", "bool Application::IsWakeWordDetectionCurrent(",
            "bool Application::IsWakeWordInvocationCurrent(", "void Application::StartListeningAudio(",
            "void Application::SetListeningStartGuard(", "void Application::ResumeListeningAudio(",
            "void Application::HandleStateChangedEvent(", "void Application::SetListeningMode(",
            "void Application::AbortSpeaking(", "ListeningMode Application::GetDefaultListeningMode(",
        ):
            source += definition(app, signature) + "\n"
        # Compile the real replacement guard, not a no-op init gate.
        init = definition(app, "bool Application::InitializeProtocol(")
        source += init[:init.index("    auto& board")] + "    ++initializations; return true;\n}\n"
        source += CHECKS
        path = build / "text_interaction.cc"
        path.write_text(source, encoding="utf-8")
        cls.binaries = []
        for upload in (0, 1):
            executable = build / f"text-interaction-{upload}.exe"
            result = subprocess.run(compiler + [
                "-std=c++23", "-Wall", "-Wextra", "-Werror", "-Wno-unused-parameter",
                "-fno-exceptions", "-fno-rtti", f"-DCONFIG_SEND_WAKE_WORD_DATA={upload}",
                "-I" + str(build), "-I" + str(ROOT / "main/protocols"),
                str(path), str(ROOT / "main/protocols/protocol.cc"),
                *json_flags, "-o", str(executable),
            ], capture_output=True, text=True, timeout=60)
            if result.returncode:
                raise AssertionError(result.stdout + result.stderr)
            cls.binaries.append(str(executable))
        mqtt = (ROOT / "main/protocols/mqtt_protocol.cc").read_text(encoding="utf-8")
        ws = (ROOT / "main/protocols/websocket_protocol.cc").read_text(encoding="utf-8")
        transport = TRANSPORT_PLATFORM.replace("@MQTT_CONSTRUCTOR@", definition(mqtt, "MqttProtocol::MqttProtocol("))
        transport = transport.replace("@MQTT_MESSAGE@", definition(mqtt, "mqtt_->OnMessage("))
        for source_file, signatures in ((mqtt, (
            "bool MqttProtocol::SendText(", "bool MqttProtocol::IsAudioChannelOpened(")),
            (ws, ("bool WebsocketProtocol::SendText(", "bool WebsocketProtocol::IsAudioChannelOpened("))):
            for signature in signatures:
                transport += definition(source_file, signature) + "\n"
        transport += TRANSPORT_CHECKS
        path = build / "text_transports.cc"
        path.write_text(transport, encoding="utf-8")
        cls.transport_binary = str(build / "text-transports.exe")
        result = subprocess.run(compiler + [
            "-std=c++23", "-Wall", "-Wextra", "-Werror", "-Wno-unused-parameter",
            "-fno-exceptions", "-fno-rtti", "-I" + str(build),
            "-I" + str(ROOT / "main/protocols"), str(path),
            str(ROOT / "main/protocols/protocol.cc"), *json_flags,
            "-o", cls.transport_binary,
        ], capture_output=True, text=True, timeout=60)
        if result.returncode:
            raise AssertionError(result.stdout + result.stderr)

    def run_transport(self, case):
        result = subprocess.run([self.transport_binary, case], capture_output=True,
                                text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def run_case(self, case):
        for binary in self.binaries:
            with self.subTest(binary=Path(binary).name):
                result = subprocess.run([binary, case], capture_output=True, text=True,
                                        encoding="utf-8", timeout=10)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                if case == "json":
                    records = [json.loads(line) for line in result.stdout.splitlines()]
                    self.assertEqual(len(records), 2)
                    for record in records:
                        self.assertEqual(record, {
                            "session_id": 'session "\\\n中文', "type": "listen",
                            "state": "detect", "text": '提醒 "quoted" \\path\n第二行\t内容',
                        })

    def test_listening_guard_waits_for_worker_and_tail_then_clock_retries_all_modes(self):
        self.run_case("listening_guard")

    def test_listening_guard_pending_is_cancelled_by_state_and_policy(self):
        self.run_case("listening_guard_cancel")

    def test_default_or_removed_listening_guard_preserves_mode_playback_behavior(self):
        self.run_case("listening_guard_default")

    def test_listening_guard_preserves_real_detector_handoff_and_epoch(self):
        self.run_case("listening_guard_wake")

    def test_both_real_transport_send_methods_preserve_boolean_contract(self):
        self.run_transport("send")

    def test_real_mqtt_reconnect_closure_defers_and_rearms_while_opening(self):
        self.run_transport("reconnect")

    def test_real_mqtt_session_filter_and_queued_goodbye_cannot_race_new_text_open(self):
        self.run_transport("mqtt_session")

    def test_protocol_cjson_allocation_failures_release_every_partial_object(self):
        self.run_transport("allocation")

    def test_json_escaping_and_same_websocket_mqtt_send_contract(self):
        self.run_case("json")

    def test_protocol_rejects_empty_oversize_nul_disabled_closing_and_send_failure(self):
        self.run_case("validation")

    def test_acceptance_is_not_send_or_tts_start_and_first_turn_stays_muted(self):
        self.run_case("accepted")

    def test_aec_modes_resume_only_after_first_tts_stop_and_auto_playback_drain(self):
        self.run_case("aec")

    def test_waits_for_enabled_policy_apply_and_serialized_close_before_worker(self):
        self.run_case("ready")

    def test_waits_for_protocol_initialization_without_schedule_retry_loop(self):
        self.run_case("init")

    def test_partial_open_failure_closes_on_worker_and_completes_once(self):
        self.run_case("open_failure")

    def test_send_failure_retires_channel_without_microphone_or_success(self):
        self.run_case("send_failure")

    def test_home_cancel_during_open_defers_close_and_never_revives(self):
        self.run_case("home")

    def test_foreground_aba_during_open_cannot_send_or_overwrite_fresh_session(self):
        self.run_case("aba")

    def test_reset_during_open_keeps_owner_until_worker_and_destroys_off_main(self):
        self.run_case("reset")

    def test_manual_wake_audio_mcp_and_init_cannot_race_open_worker(self):
        self.run_case("serialization")

    def test_late_onopened_and_tts_from_old_generation_are_ignored(self):
        self.run_case("stale")

    def test_thirty_second_total_deadline_covers_ready_open_and_missing_tts(self):
        self.run_case("timeout")

    def test_single_slot_busy_or_disabled_rejects_without_completing_rejected_work(self):
        self.run_case("busy")

    def test_open_worker_creation_failure_falls_back_without_main_network_open(self):
        self.run_case("task_failure")

    def test_network_error_is_main_marshaled_and_stop_without_start_is_failure(self):
        self.run_case("reply_failure")


PLATFORM = r'''
#include <atomic>
#include <cassert>
#include <chrono>
#include <cstdint>
#include <cstring>
#include <deque>
#include <functional>
#include <iostream>
#include <memory>
#include <mutex>
#include <new>
#include <string>
#include <utility>
#include <vector>
#include "protocol.h"
#include "esp_log.h"
constexpr uint32_t MAIN_EVENT_SCHEDULE = 1, MAIN_EVENT_SEND_AUDIO = 2,
    MAIN_EVENT_PLAYBACK_DRAINED = 8192, MAIN_EVENT_ERROR = 16, MAIN_EVENT_CLOCK_TICK = 32;
constexpr int pdPASS = 1, pdTRUE = 1, portMAX_DELAY = -1;
using TaskHandle_t = void*;
using EventBits_t = uint32_t;
int64_t now_us = 0;
int64_t esp_timer_get_time() { return now_us; }
uint32_t xEventGroupSetBits(uint32_t* group, uint32_t bits) { return *group |= bits; }
bool on_worker = false, fail_task = false;
int opens = 0, closes = 0, destructions = 0;
std::deque<std::function<void()>> workers;
std::function<void()> acknowledge;
int xTaskCreate(void (*entry)(void*), const char*, int, void* arg, int, void*) {
    if (fail_task) return 0;
    workers.push_back([=]() { entry(arg); }); return pdPASS;
}
void xTaskNotifyGive(TaskHandle_t) { assert(!on_worker); }
TaskHandle_t xTaskGetCurrentTaskHandle() { assert(on_worker); return reinterpret_cast<void*>(1); }
void ulTaskNotifyTake(int, int) { assert(on_worker); on_worker = false; acknowledge(); on_worker = true; }
void vTaskDelete(void*) { assert(on_worker); }
void RunWorker() {
    assert(!workers.empty()); auto worker = std::move(workers.front()); workers.pop_front();
    on_worker = true; worker(); on_worker = false;
}
enum DeviceState { kDeviceStateUnknown, kDeviceStateIdle, kDeviceStateConnecting,
    kDeviceStateListening, kDeviceStateSpeaking, kDeviceStateNotifying, kDeviceStateActivating,
    kDeviceStateWifiConfiguring, kDeviceStateAudioTesting, kDeviceStateUpgrading };
enum AecMode { kAecOff, kAecOnDeviceSide, kAecOnServerSide };
enum class PowerSaveLevel { PERFORMANCE, LOW_POWER };
namespace Lang::Sounds { const std::string OGG_POPUP = "popup"; }
namespace Lang::Strings { constexpr const char* STANDBY = "idle", *CONNECTING = "connecting",
    *LISTENING = "listening", *SPEAKING = "speaking"; }
struct TextGlyph {};
struct TextGlyphPayload { static bool Parse(const cJSON*, std::vector<TextGlyph>&, uint8_t&) { return false; } };
struct SystemInfo { static void PrintHeapStats() {} };
struct Display {
    void UpdateStatusBar() {}
    void SetChatMessage(const char*, const char*) {} void SetStatus(const char*) {}
    void SetEmotion(const char*) {} void ClearChatMessages() {}
    void AddTextGlyphs(const std::vector<TextGlyph>&, uint8_t) {}
};
struct Codec { int output_sample_rate() { return 24000; } };
struct Led { void OnStateChanged() {} };
struct Board {
    Display display; Codec codec; Led led;
    static Board& GetInstance() { static Board board; return board; }
    Display* GetDisplay() { return &display; } Codec* GetAudioCodec() { return &codec; }
    Led* GetLed() { return &led; } void SetPowerSaveLevel(PowerSaveLevel) { assert(!on_worker); }
};
struct AudioService {
    bool voice = false, wake = true, playback_idle = true;
    uint32_t policy = 1, reply = 0;
    int starts = 0, resets = 0, encodes = 0, sounds = 0;
    std::deque<std::unique_ptr<AudioStreamPacket>> send;
    void SetConversationPolicy(uint32_t p) { policy = p; voice = false; wake = false; }
    void ApplyConversationPolicy() { voice = false; send.clear(); }
    void SetReplyChannel(uint32_t generation) { reply = generation; }
    void SetWakeWordDetectionInhibited(bool, uint32_t) {}
    void EnableWakeWordDetection(bool value) { assert(!on_worker); wake = value; }
    void EnableVoiceProcessing(bool value) { assert(!on_worker); voice = value; starts += value; }
    bool IsAudioProcessorRunning() const { return voice; }
    bool IsPlaybackIdle() const { return playback_idle; }
    bool IsAfeWakeWord() const { return false; }
    void EnableAudioTesting(bool) {}
    void ResetDecoder() { assert(!on_worker); ++resets; playback_idle = true; }
    void EncodeWakeWord() { assert(!on_worker); ++encodes; }
    const std::string& GetLastWakeWord() const { static std::string word = "wake"; return word; }
    void PlaySound(const std::string&) { ++sounds; }
    std::unique_ptr<AudioStreamPacket> PopWakeWordPacket() { return nullptr; }
    std::unique_ptr<AudioStreamPacket> PopPacketFromSendQueue() {
        if (send.empty()) return nullptr;
        auto packet = std::move(send.front()); send.pop_front(); return packet;
    }
    bool PushPacketToDecodeQueue(std::unique_ptr<AudioStreamPacket> packet) {
        if (packet->channel_generation != reply) return false;
        playback_idle = false; return true;
    }
};
struct HostProtocol : Protocol {
    bool opened = false, open_ok = true, send_ok = true, opening = false;
    std::string wire, transport = "websocket";
    int sends = 0, audio_sends = 0;
    std::function<void()> open_hook, send_hook;
    ~HostProtocol() { assert(on_worker); ++destructions; }
    bool Start() override { return true; }
    bool OpenAudioChannel() override {
        assert(on_worker && !opening && !IsChannelClosing()); opening = true; ++opens;
        const auto generation = BeginChannel();
        if (open_hook) open_hook();
        opened = open_ok; opening = false;
        session_id_ = "fresh-" + std::to_string(generation);
        if (opened && on_audio_channel_opened_) on_audio_channel_opened_(generation);
        return opened;
    }
    bool IsAudioChannelOpened() const override { assert(!opening); return opened && !IsChannelClosing(); }
    void CloseAudioChannel(bool = true) override {
        assert(on_worker && !opening); ++closes; opened = false;
        if (on_audio_channel_closed_) on_audio_channel_closed_(channel_generation());
    }
    bool SendAudio(std::unique_ptr<AudioStreamPacket>) override {
        assert(!opening && !on_worker && !IsChannelClosing()); ++audio_sends; return true;
    }
    bool SendText(const std::string& text) override {
        assert(!opening && !on_worker && !IsChannelClosing());
        wire = text; ++sends; if (send_hook) send_hook(); return send_ok;
    }
    void Fresh() { opened = true; BeginChannel(); }
    void Session(std::string session) { session_id_ = std::move(session); }
    void Opened(uint32_t generation) { on_audio_channel_opened_(generation); }
    void Closed(uint32_t generation) { on_audio_channel_closed_(generation); }
    void Error() { on_network_error_("network failure"); }
};
struct StateMachine {
    DeviceState state = kDeviceStateIdle;
    bool TransitionTo(DeviceState next) {
        if (state == kDeviceStateConnecting && next == kDeviceStateSpeaking) return false;
        state = next; return true;
    }
    DeviceState GetState() const { return state; }
};
class Application {
public:
    @FIELDS@
    @JOBS@
    std::shared_ptr<Protocol> protocol_;
    AudioService audio_service_;
    StateMachine state_machine_;
    std::mutex mutex_;
    std::deque<std::function<void()>> main_tasks_;
    uint32_t events = 0;
    uint32_t* event_group_ = &events;
    ListeningMode listening_mode_ = kListeningModeAutoStop;
    AecMode aec_mode_ = kAecOff;
    bool aborted_ = false, pending_listening_start_ = false, play_popup_on_listening_ = false;
    int initializations = 0, clock_ticks_ = 0;
    std::string last_error_message_;
    std::function<void(const std::string&)> mcp_broadcast_callback_;
    struct Notify { void OnPlaybackDrained() {} } notify_player_;
    Application() { Install(); acknowledge = [this]() { Drain(); }; }
    ~Application() {
        while (!workers.empty()) { RunWorker(); Drain(); }
        on_worker = true; protocol_.reset(); on_worker = false;
    }
    HostProtocol& Net() { return *static_cast<HostProtocol*>(protocol_.get()); }
    void Install() {
        assert(!protocol_); protocol_ = std::make_shared<HostProtocol>();
        protocol_->SetConversationPolicySource(&conversation_policy_);
        const auto protocol_instance = protocol_instance_.fetch_add(1) + 1;
        @AUDIO_CALLBACK@);
        @OPENED_CALLBACK@);
        @CLOSED_CALLBACK@);
        @ERROR_CALLBACK@);
    }
    void Flush() {
        assert(!on_worker); auto tasks = std::move(main_tasks_); main_tasks_.clear();
        for (auto& task : tasks) task();
    }
    void Drain() { for (int i = 0; i < 30 && !main_tasks_.empty(); ++i) Flush(); }
    void Worker() { RunWorker(); Drain(); }
    void MainWhileOpening(std::function<void()> operation) {
        assert(on_worker); on_worker = false; operation(); Drain(); on_worker = true;
    }
    DeviceState GetDeviceState() const { return state_machine_.state; }
    bool SetDeviceState(DeviceState);
    void Schedule(std::function<void()>&&);
    bool RequestTextInteraction(const std::string&, std::function<void(bool)> = {});
    void ResumeTextInteraction();
    void CompleteTextInteractionOpen(uint32_t, uint32_t, uint32_t, Protocol*, bool);
    void FinishTextInteraction(bool);
    void PublishAudioChannelOpened(uint32_t);
    void FlushDeferredMcpMessages();
    void SendMcpMessage(const std::string&);
    void SetConversationEnabled(bool);
    void ApplyConversationPolicy(uint32_t);
    void FinishConversationDrain();
    void CloseAudioChannel();
    void CloseConversationChannel();
    void ReleaseConversationClose(ConversationCloseJob*);
    void CompleteConversationClose();
    void ResumeProtocolInitialization();
    bool InitializeProtocol();
    void ResetProtocol();
    bool IsConversationCurrent(uint32_t) const;
    bool IsConversationAvailable() const;
    bool IsChannelCurrent(uint32_t) const;
    bool CanReceiveReply(uint32_t) const;
    void HandleToggleChatEvent();
    void HandleStartListeningEvent();
    void HandleStopListeningEvent();
    void ContinueOpenAudioChannel(ListeningMode, uint32_t);
    void HandleWakeWordDetectedEvent();
    void ResumeWakeWordActivation();
    void BeginWakeWordInvoke(const std::string&, uint32_t);
    void ContinueWakeWordInvoke(const std::string&, uint32_t);
    void CancelWakeWordInvoke(uint32_t);
    bool IsWakeWordDetectionCurrent(uint32_t) const;
    bool IsWakeWordInvocationCurrent(uint32_t) const;
    void HandleStateChangedEvent();
    void StartListeningAudio();
    void SetListeningStartGuard(std::function<bool()>);
    void ResumeListeningAudio();
    void ConfigureWakeWordForListening() {}
    void SetListeningMode(ListeningMode);
    ListeningMode GetDefaultListeningMode() const;
    void AbortSpeaking(AbortReason);
    void StopNotification() { SetDeviceState(kDeviceStateIdle); }
    void Tts(const char* state, uint32_t generation) {
        auto* root = cJSON_CreateObject(); cJSON_AddStringToObject(root, "type", "tts");
        cJSON_AddStringToObject(root, "state", state);
        const auto conversation = conversation_policy_.load();
        auto* type = cJSON_GetObjectItem(root, "type");
        auto* display = Board::GetInstance().GetDisplay();
        @TTS@
        cJSON_Delete(root);
    }
    void RunSend() { const auto bits = MAIN_EVENT_SEND_AUDIO; @SEND_AUDIO@ }
    void RunPlayback() { const auto bits = MAIN_EVENT_PLAYBACK_DRAINED; @PLAYBACK@ }
    void RunClock() { const auto bits = MAIN_EVENT_CLOCK_TICK; @CLOCK@ }
    void StartText() {
        assert(RequestTextInteraction("reminder"));
        while (!text_interaction_sent_ && !workers.empty()) Worker();
        assert(text_interaction_sent_ && !protocol_opening_);
    }
};
'''


TRANSPORT_PLATFORM = r'''
#include <atomic>
#include <cassert>
#include <chrono>
#include <cstdlib>
#include <cstring>
#include <deque>
#include <functional>
#include <memory>
#include <mutex>
#include <string>
#include <vector>
#include "protocol.h"
#include "esp_log.h"
constexpr int kDeviceStateIdle = 0, kDeviceStateConnecting = 1;
constexpr int MQTT_RECONNECT_INTERVAL_MS = 1000;
int alive_allocations = 0, allocation_countdown = -1;
void* CountedMalloc(size_t size) {
    if (allocation_countdown == 0) return nullptr;
    if (allocation_countdown > 0) --allocation_countdown;
    void* ptr = std::malloc(size); if (ptr) ++alive_allocations; return ptr;
}
void CountedFree(void* ptr) { if (ptr) --alive_allocations; std::free(ptr); }
struct Timer { std::function<void()> fire; int starts = 0; };
struct esp_timer_create_args_t { void (*callback)(void*); void* arg; };
using esp_timer_handle_t = Timer*;
int esp_timer_create(const esp_timer_create_args_t* args, esp_timer_handle_t* timer) {
    auto callback = args->callback; auto arg = args->arg;
    *timer = new Timer{[=]() { callback(arg); }}; return 0;
}
void esp_timer_start_once(Timer* timer, int64_t) { ++timer->starts; }
int xEventGroupCreate() { return 1; }
struct Application {
    int state = kDeviceStateIdle;
    std::deque<std::function<void()>> tasks;
    static Application& GetInstance() { static Application app; return app; }
    int GetDeviceState() const { return state; }
    void Schedule(std::function<void()>&& callback) { tasks.push_back(std::move(callback)); }
    void Drain() { auto pending = std::move(tasks); tasks.clear(); for (auto& task : pending) task(); }
};
namespace Lang::Strings { constexpr const char* SERVER_ERROR = "error"; }
struct Mqtt {
    bool connected = true, accepted = true;
    int sends = 0; std::string wire;
    std::function<void(const std::string&, const std::string&)> message;
    bool Publish(const std::string&, const std::string& text) { ++sends; wire = text; return accepted; }
    void OnMessage(decltype(message) callback) { message = std::move(callback); }
};
struct Socket {
    bool connected = true, accepted = true;
    int sends = 0; std::string wire;
    bool IsConnected() const { return connected; }
    bool Send(const std::string& text) { ++sends; wire = text; return accepted; }
};
struct Udp {};
struct MqttProtocol : Protocol {
    int event_group_handle_ = 0, reconnects = 0, closes = 0;
    Timer* reconnect_timer_ = nullptr;
    std::shared_ptr<std::atomic<bool>> alive_ = std::make_shared<std::atomic<bool>>(true);
    std::unique_ptr<Mqtt> mqtt_ = std::make_unique<Mqtt>();
    std::shared_ptr<Udp> udp_ = std::make_shared<Udp>();
    std::string publish_topic_ = "topic", retired_session_id_;
    mutable std::mutex channel_mutex_;
    std::atomic<bool> awaiting_hello_{false}, remote_goodbye_{false};
    MqttProtocol();
    ~MqttProtocol() { alive_->store(false); delete reconnect_timer_; }
    bool Start() override { return true; }
    bool OpenAudioChannel() override { return true; }
    void CloseAudioChannel(bool = true) override { ++closes; }
    bool SendAudio(std::unique_ptr<AudioStreamPacket>) override { return true; }
    bool IsAudioChannelOpened() const override;
    bool SendText(const std::string&) override;
    bool IsTimeout() const override { return false; }
    void StartMqttClient(bool) { ++reconnects; }
    void ParseServerHello(const cJSON*) {}
    void Setup() {
        BeginChannel(); session_id_ = "new-session";
        @MQTT_MESSAGE@);
    }
    void Receive(const char* message) { mqtt_->message("topic", message); }
};
struct WebsocketProtocol : Protocol {
    std::shared_ptr<Socket> websocket_ = std::make_shared<Socket>();
    bool Start() override { return true; }
    bool OpenAudioChannel() override { return true; }
    void CloseAudioChannel(bool = true) override {}
    bool SendAudio(std::unique_ptr<AudioStreamPacket>) override { return true; }
    bool IsAudioChannelOpened() const override;
    bool SendText(const std::string&) override;
    bool IsTimeout() const override { return false; }
};
@MQTT_CONSTRUCTOR@
'''


TRANSPORT_CHECKS = r'''
int main(int argc, char** argv) {
    assert(argc == 2); const std::string test = argv[1];
    auto& app = Application::GetInstance();
    if (test == "send") {
        std::atomic<uint32_t> policy{5}; WebsocketProtocol ws; MqttProtocol mqtt;
        for (Protocol* protocol : {static_cast<Protocol*>(&ws), static_cast<Protocol*>(&mqtt)}) {
            protocol->SetConversationPolicySource(&policy);
            assert(protocol->SendTextRequest("text \"quoted\"\n中文"));
            policy.store(6); assert(!protocol->SendTextRequest("text"));
            policy.store(9); protocol->SetChannelClosing(true); assert(!protocol->SendTextRequest("text"));
            protocol->SetChannelClosing(false); protocol->SetChannelOpening(true);
            assert(!protocol->SendTextRequest("text")); protocol->SetChannelOpening(false);
        }
        assert(ws.websocket_->sends == 1 && mqtt.mqtt_->sends == 1);
        ws.websocket_->accepted = false; mqtt.mqtt_->accepted = false;
        assert(!ws.SendTextRequest("text") && !mqtt.SendTextRequest("text"));
        ws.websocket_->connected = false; mqtt.udp_.reset();
        assert(!ws.SendTextRequest("text") && !mqtt.SendTextRequest("text"));
    } else if (test == "reconnect") {
        MqttProtocol mqtt;
        mqtt.reconnect_timer_->fire(); mqtt.SetChannelOpening(true); app.Drain();
        assert(mqtt.reconnects == 0 && mqtt.reconnect_timer_->starts == 1);
        mqtt.reconnect_timer_->fire(); mqtt.SetChannelOpening(false); app.state = kDeviceStateConnecting;
        app.Drain(); assert(mqtt.reconnects == 0 && mqtt.reconnect_timer_->starts == 2);
        mqtt.reconnect_timer_->fire(); app.state = kDeviceStateIdle; app.Drain();
        assert(mqtt.reconnects == 1);
        mqtt.SetChannelClosing(true); mqtt.reconnect_timer_->fire(); app.Drain();
        assert(mqtt.reconnects == 1 && mqtt.reconnect_timer_->starts == 3);
        mqtt.SetChannelClosing(false); mqtt.reconnect_timer_->fire(); app.Drain();
        assert(mqtt.reconnects == 2);
    } else if (test == "mqtt_session") {
        MqttProtocol mqtt; mqtt.Setup();
        std::atomic<uint32_t> policy{1}; mqtt.SetConversationPolicySource(&policy);
        int json = 0;
        mqtt.OnIncomingJson([&](const cJSON*, uint32_t) { ++json; });
        mqtt.Receive("{\"type\":\"tts\"}"); assert(json == 1);
        mqtt.RequireConversationSession();
        mqtt.Receive("{\"type\":\"tts\"}");
        mqtt.Receive("{\"type\":\"tts\",\"session_id\":\"old-session\"}"); assert(json == 1);
        mqtt.Receive("{\"type\":\"tts\",\"session_id\":\"new-session\"}"); assert(json == 2);
        // Legacy goodbye scheduled before this protocol opted into text ownership.
        MqttProtocol legacy; legacy.Setup();
        legacy.Receive("{\"type\":\"goodbye\",\"session_id\":\"new-session\"}");
        legacy.SetChannelOpening(true); app.Drain(); assert(legacy.closes == 0);
    } else if (test == "allocation") {
        WebsocketProtocol ws;
        cJSON_Hooks hooks{CountedMalloc, CountedFree}; cJSON_InitHooks(&hooks);
        for (int allocation = 0; allocation < 24; ++allocation) {
            allocation_countdown = allocation;
            (void)ws.SendTextRequest("text \"quote\"\n中文");
            assert(alive_allocations == 0);
        }
        allocation_countdown = -1; assert(ws.SendTextRequest("text")); assert(alive_allocations == 0);
        cJSON_InitHooks(nullptr);
    } else assert(false);
}
'''


CHECKS = r'''
int main(int argc, char** argv) {
    assert(argc == 2); const std::string test = argv[1];
    if (test == "json" || test == "validation") {
        for (const auto* transport : {"websocket", "mqtt"}) {
            auto protocol = std::make_unique<HostProtocol>(); auto& p = *protocol;
            p.transport = transport; p.Fresh(); std::atomic<uint32_t> policy{5};
            p.SetConversationPolicySource(&policy);
            if (test == "json") {
                p.Session("session \"\\\n中文");
                assert(p.SendTextRequest("提醒 \"quoted\" \\path\n第二行\t内容"));
                std::cout << p.wire << '\n';
            } else {
                for (const std::string& text : std::vector<std::string>{"", " \t\r\n", std::string("x\0y", 3),
                        std::string(Protocol::kMaxTextInteractionBytes + 1, 'x')}) {
                    assert(!p.SendTextRequest(text));
                }
                assert(p.sends == 0);
                assert(p.SendTextRequest(std::string(12 * 1024, 'x')) && p.sends == 1);
                policy.store(6); assert(!p.SendTextRequest("text"));
                policy.store(9); p.SetChannelClosing(true); assert(!p.SendTextRequest("text"));
                p.SetChannelClosing(false); p.opened = false; assert(!p.SendTextRequest("text"));
                p.opened = true; p.send_ok = false; assert(!p.SendTextRequest("text"));
                assert(p.sends == 2); p.send_ok = true; p.SendWakeWordDetected("wake"); assert(p.sends == 3);
            }
            on_worker = true; protocol.reset(); on_worker = false;
        }
        return 0;
    }
    if (test == "listening_guard" || test == "listening_guard_default" ||
        test == "listening_guard_cancel") {
        for (const auto mode : {kListeningModeManualStop, kListeningModeRealtime, kListeningModeAutoStop}) {
            if (test == "listening_guard_cancel") {
                for (int cancellation = 0; cancellation != 3; ++cancellation) {
                    Application app; app.Net().Fresh();
                    app.PublishAudioChannelOpened(app.Net().channel_generation());
                    auto& audio = app.audio_service_;
                    app.listening_mode_ = mode; app.state_machine_.state = kDeviceStateListening;
                    int guard_calls = 0;
                    app.SetListeningStartGuard([&]() { assert(!on_worker); ++guard_calls; return false; });
                    app.HandleStateChangedEvent();
                    assert(app.pending_listening_start_ && !audio.voice && app.Net().sends == 0);
                    if (cancellation == 0) app.SetDeviceState(kDeviceStateIdle);
                    else {
                        app.SetConversationEnabled(false);
                        if (cancellation == 2) app.SetConversationEnabled(true);
                    }
                    app.RunClock();
                    assert(!app.pending_listening_start_ && !audio.voice && guard_calls == 1);
                    app.Drain(); while (!workers.empty()) app.Worker();
                    app.RunClock(); app.RunPlayback();
                    assert(app.Net().sends == 0 && audio.starts == 0 && guard_calls == 1);
                }
                continue;
            }
            Application app; app.Net().Fresh();
            app.PublishAudioChannelOpened(app.Net().channel_generation());
            auto& audio = app.audio_service_;
            app.listening_mode_ = mode; app.state_machine_.state = kDeviceStateListening;
            if (test == "listening_guard_default") {
                audio.playback_idle = false;
                app.HandleStateChangedEvent();
                if (mode == kListeningModeAutoStop) {
                    assert(app.pending_listening_start_ && !audio.voice && app.Net().sends == 0);
                    app.RunClock(); assert(!audio.voice);
                    audio.playback_idle = true; app.RunClock();
                }
                assert(audio.voice && audio.starts == 1 && app.Net().sends == 1);
                app.RunClock(); app.RunPlayback();
                assert(audio.starts == 1 && app.Net().sends == 1 && !app.pending_listening_start_);
                audio.voice = false; audio.playback_idle = true;
                app.SetListeningStartGuard([]() { return false; });
                app.HandleStateChangedEvent(); assert(app.pending_listening_start_ && !audio.voice);
                app.SetListeningStartGuard({}); audio.playback_idle = false; app.RunClock();
                if (mode == kListeningModeAutoStop) {
                    assert(!audio.voice); audio.playback_idle = true; app.RunPlayback();
                }
                assert(audio.voice && audio.starts == 2 && app.Net().sends == 2);
                continue;
            }
            bool worker_done = false; int guard_calls = 0; int64_t release_at = 0;
            app.SetListeningStartGuard([&]() {
                assert(!on_worker); ++guard_calls;
                return worker_done && now_us >= release_at;
            });
            app.play_popup_on_listening_ = true;
            app.HandleStateChangedEvent();
            assert(app.pending_listening_start_ && !audio.voice && app.Net().sends == 0);
            assert(guard_calls == 1 && audio.sounds == 0 && workers.empty() && app.main_tasks_.empty());
            std::function<void()> local_completion = [&]() {
                assert(on_worker); worker_done = true; release_at = now_us + 120000;
            };
            xTaskCreate([](void* arg) { (*static_cast<std::function<void()>*>(arg))(); },
                        "local_playback", 0, &local_completion, 0, nullptr);
            app.RunClock(); assert(!worker_done && !audio.voice && guard_calls == 2);
            RunWorker();
            // Global playback can remain busy in manual/realtime. The guard, not
            // an unconditional decoder drain, owns their local release boundary.
            audio.playback_idle = mode == kListeningModeAutoStop;
            app.RunPlayback(); assert(worker_done && !audio.voice && guard_calls == 3);
            now_us = release_at - 1; app.RunClock();
            assert(!audio.voice && app.pending_listening_start_ && app.Net().sends == 0 && guard_calls == 4);
            now_us = release_at;
            if (mode == kListeningModeAutoStop) {
                audio.playback_idle = false; app.RunClock();
                assert(!audio.voice && guard_calls == 4);
                audio.playback_idle = true; app.RunClock();
            } else app.RunClock();
            assert(audio.voice && audio.starts == 1 && app.Net().sends == 1 && guard_calls == 5);
            assert(!app.pending_listening_start_ && audio.sounds == 1);
            assert(app.Net().wire.find("\"state\":\"start\"") != std::string::npos);
            app.RunClock(); app.RunPlayback();
            assert(audio.starts == 1 && app.Net().sends == 1 && guard_calls == 5);
        }
        return 0;
    }
    Application app; auto& audio = app.audio_service_;
    int callbacks = 0; bool result = false;
    auto completion = [&](bool started) { assert(!on_worker); ++callbacks; result = started; };
    if (test == "listening_guard_wake") {
        app.Net().Fresh(); app.PublishAudioChannelOpened(app.Net().channel_generation());
        app.aec_mode_ = kAecOnDeviceSide;
        int activations = 0; bool released = false;
        app.wake_word_activation_handler_ = [&]() { ++activations; return true; };
        app.SetListeningStartGuard([&]() { assert(!on_worker); return released; });
        const auto policy = app.wake_word_policy_.load();
        app.wake_word_event_policy_.store(policy); app.HandleWakeWordDetectedEvent();
        assert(activations == 1 && audio.encodes == 1 && app.wake_word_policy_ == policy);
        assert(app.GetDeviceState() == kDeviceStateListening && workers.empty());
        const auto detect_sends = app.Net().sends;
        app.HandleStateChangedEvent(); app.RunClock();
        assert(app.pending_listening_start_ && !audio.voice && app.Net().sends == detect_sends);
        // A second genuine detector while already Listening must not send the
        // legacy branch's listen/start around the still-false guard.
        app.wake_word_event_policy_.store(policy); app.HandleWakeWordDetectedEvent();
        assert(app.pending_listening_start_ && !audio.voice && app.Net().sends == detect_sends + 1);
        assert(app.Net().wire.find("\"type\":\"abort\"") != std::string::npos);
        assert(activations == 2 && audio.encodes == 1 && app.wake_word_policy_ == policy);
        released = true; audio.playback_idle = false; app.RunClock();
        assert(audio.voice && app.Net().sends == detect_sends + 2 && !app.pending_listening_start_);
        assert(activations == 2 && audio.encodes == 1 && app.wake_word_policy_ == policy);
    } else if (test == "accepted" || test == "aec") {
        for (auto aec : {kAecOff, kAecOnDeviceSide, kAecOnServerSide}) {
            app.aec_mode_ = aec;
            const auto before_sends = app.Net().sends;
            assert(app.RequestTextInteraction("reminder", completion));
            assert(callbacks == 0 && app.Net().sends == before_sends && opens == 0);
            while (!app.text_interaction_sent_) app.Worker();
            assert(!audio.voice && audio.encodes == 0 && app.Net().audio_sends == 0);
            assert(callbacks == 0 && app.GetDeviceState() == kDeviceStateIdle);
            app.HandleStateChangedEvent(); assert(!audio.voice && !audio.wake);
            const auto generation = app.active_channel_generation_.load();
            app.Tts("start", generation); app.Drain();
            assert(callbacks == 1 && result && app.GetDeviceState() == kDeviceStateSpeaking);
            app.HandleStateChangedEvent(); assert(!audio.voice);
            audio.playback_idle = false;
            app.Tts("stop", generation); app.Drain(); app.HandleStateChangedEvent();
            if (aec == kAecOff) {
                assert(!audio.voice && app.pending_listening_start_);
                audio.playback_idle = true; app.RunPlayback();
            }
            assert(audio.voice && app.GetDeviceState() == kDeviceStateListening);
            app.Tts("start", generation); app.Drain(); assert(callbacks == 1);
            app.SetConversationEnabled(false); app.SetConversationEnabled(true); app.Drain();
            while (!workers.empty()) app.Worker();
            callbacks = 0; opens = 0;
            if (test == "accepted") break;
        }
    } else if (test == "ready") {
        app.Net().Fresh(); app.SetConversationEnabled(false); app.SetConversationEnabled(true);
        assert(app.RequestTextInteraction("reminder", completion));
        assert(workers.empty() && opens == 0 && callbacks == 0);
        app.Drain(); assert(workers.size() == 1 && app.protocol_closing_);
        app.Worker(); assert(closes == 1 && workers.size() == 1 && app.protocol_opening_);
        app.Worker(); assert(opens == 1 && app.Net().sends == 1 && callbacks == 0);
    } else if (test == "init") {
        app.protocol_initialization_pending_ = true;
        app.state_machine_.state = kDeviceStateActivating;
        assert(app.RequestTextInteraction("reminder", completion));
        for (int i = 0; i < 10; ++i) app.ResumeTextInteraction();
        assert(workers.empty() && app.main_tasks_.empty() && opens == 0);
        app.state_machine_.state = kDeviceStateIdle;
        app.protocol_initialization_pending_ = false; app.ResumeTextInteraction();
        while (!app.text_interaction_sent_) app.Worker();
        assert(opens == 1 && callbacks == 0);
    } else if (test == "open_failure" || test == "send_failure" || test == "task_failure") {
        app.Net().open_ok = test != "open_failure";
        app.Net().send_ok = test != "send_failure";
        fail_task = test == "task_failure";
        assert(app.RequestTextInteraction("reminder", completion));
        if (!fail_task) app.Worker();
        assert(callbacks == 1 && !result && !audio.voice && app.text_interaction_id_ == 0);
        fail_task = false;
        while (!workers.empty()) app.Worker();
        assert(opens == (test == "task_failure" ? 0 : 1));
        if (test != "task_failure") assert(closes >= 1);
    } else if (test == "home" || test == "aba" || test == "reset") {
        app.Net().open_hook = [&]() { app.MainWhileOpening([&]() {
            if (test == "reset") app.ResetProtocol();
            else { app.SetConversationEnabled(false); if (test == "aba") app.SetConversationEnabled(true); }
            app.Drain();
            assert(closes == 0 && workers.empty() && app.protocol_opening_);
            assert(app.active_channel_generation_ == 0 && callbacks == 1 && !result);
            assert(!app.InitializeProtocol());
        }); };
        assert(app.RequestTextInteraction("reminder", completion)); app.Worker();
        assert(callbacks == 1 && app.text_interaction_id_ == 0 && !audio.voice);
        assert(workers.size() == 1 && app.protocol_closing_ && app.protocol_);
        assert(app.Net().sends == 0); app.Worker();
        assert(closes == 1 && callbacks == 1);
        if (test == "reset") assert(!app.protocol_ && destructions == 1);
    } else if (test == "serialization") {
        int activations = 0; app.wake_word_activation_handler_ = [&]() { ++activations; return true; };
        app.Net().open_hook = [&]() { app.MainWhileOpening([&]() {
            app.SendMcpMessage("{}"); app.Drain();
            app.toggle_chat_policy_.store(1); app.HandleToggleChatEvent();
            app.start_listening_policy_.store(1); app.HandleStartListeningEvent();
            app.wake_word_event_policy_.store(1); app.HandleWakeWordDetectedEvent();
            app.BeginWakeWordInvoke("wake", 1); app.ContinueWakeWordInvoke("wake", 1);
            app.ContinueOpenAudioChannel(kListeningModeManualStop, 1);
            app.StartListeningAudio(); app.AbortSpeaking(kAbortReasonNone);
            audio.send.push_back(std::make_unique<AudioStreamPacket>()); app.RunSend();
            assert(!app.InitializeProtocol() && app.initializations == 0);
            assert(app.Net().sends == 0 && app.Net().audio_sends == 0 && !audio.voice);
            assert(app.deferred_mcp_messages_.size() == 1 && opens == 1 && activations == 0);
        }); };
        assert(app.RequestTextInteraction("reminder", completion)); app.Worker();
        assert(app.Net().sends == 2 && app.deferred_mcp_messages_.empty());
        assert(audio.encodes == 0 && !audio.voice && callbacks == 0);
    } else if (test == "stale") {
        app.Net().Fresh(); const auto old = app.Net().channel_generation();
        app.PublishAudioChannelOpened(old); app.Net().Opened(old);
        assert(app.RequestTextInteraction("reminder", completion));
        while (!app.text_interaction_sent_) app.Worker();
        const auto fresh = app.active_channel_generation_.load(); assert(fresh != old);
        app.Net().Opened(old); app.Tts("start", old); app.Drain();
        assert(app.active_channel_generation_ == fresh && callbacks == 0);
        app.Tts("start", fresh); app.Drain(); assert(callbacks == 1 && result);
    } else if (test == "timeout") {
        for (int phase = 0; phase != 3; ++phase) {
            app.protocol_initialization_pending_ = phase == 0;
            assert(app.RequestTextInteraction("reminder", completion));
            if (phase == 2) app.Worker();
            now_us += 30000000; app.ResumeTextInteraction();
            assert(callbacks == phase + 1 && !result && app.text_interaction_id_ == 0 && !audio.voice);
            app.protocol_initialization_pending_ = false;
            while (!workers.empty()) app.Worker();
            if (!app.IsConversationAvailable()) app.CloseConversationChannel();
            while (!workers.empty()) app.Worker();
        }
    } else if (test == "busy") {
        for (const auto& text : {std::string(), std::string(" \n"), std::string(12289, 'x')})
            assert(!app.RequestTextInteraction(text, completion));
        app.state_machine_.state = kDeviceStateListening;
        assert(!app.RequestTextInteraction("reminder", completion));
        app.state_machine_.state = kDeviceStateSpeaking;
        assert(!app.RequestTextInteraction("reminder", completion));
        app.state_machine_.state = kDeviceStateIdle;
        app.SetConversationEnabled(false); app.Drain();
        assert(!app.RequestTextInteraction("reminder", completion));
        app.SetConversationEnabled(true); app.Drain();
        assert(app.RequestTextInteraction("reminder", completion));
        assert(!app.RequestTextInteraction("second", completion));
        assert(callbacks == 0);
    } else if (test == "reply_failure") {
        for (int error = 0; error != 2; ++error) {
            assert(app.RequestTextInteraction("reminder", completion));
            while (!app.text_interaction_sent_) app.Worker();
            if (error) {
                app.Net().Error(); assert(callbacks == 1);
            } else app.Tts("stop", app.active_channel_generation_);
            app.Drain(); assert(callbacks == error + 1 && !result && !audio.voice);
            while (!workers.empty()) app.Worker();
        }
    } else assert(false);
    while (!workers.empty()) app.Worker();
}
'''
