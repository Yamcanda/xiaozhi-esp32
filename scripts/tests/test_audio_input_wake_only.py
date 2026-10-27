"""Run actual input-task/engine bodies with fake codec I/O and speech inference.

Set AUDIO_INPUT_WAKE_ONLY_LEGACY_GUARD=1 to restore the old capture-only guard
in the generated C++ source, without modifying production, and reproduce failures.
"""

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
                          and ";" in line and "(" not in line) for name in names)


def host_source(legacy_guard=False):
    audio = (ROOT / "main/audio/audio_service.cc").read_text(encoding="utf-8")
    header = (ROOT / "main/audio/audio_service.h").read_text(encoding="utf-8")
    afe = (ROOT / "main/audio/engines/afe_audio_engine.cc").read_text(encoding="utf-8")
    afe_header = (ROOT / "main/audio/engines/afe_audio_engine.h").read_text(encoding="utf-8")
    lite = (ROOT / "main/audio/engines/lite_audio_engine.cc").read_text(encoding="utf-8")
    lite_header = (ROOT / "main/audio/engines/lite_audio_engine.h").read_text(encoding="utf-8")
    queue = (ROOT / "main/audio/fixed_queue.h").read_text(encoding="utf-8")
    source = PLATFORM.replace("@QUEUE@", queue.replace("#include <esp_system.h>", ""))
    constants = ("OPUS_FRAME_DURATION_MS", "MAX_ENCODE_TASKS_IN_QUEUE", "MAX_SEND_PACKETS_IN_QUEUE",
                 "AS_EVENT_AUDIO_TESTING_RUNNING", "AS_EVENT_WAKE_WORD_RUNNING",
                 "AS_EVENT_AUDIO_PROCESSOR_RUNNING", "AS_EVENT_AUDIO_INPUT_STOP_REQUEST",
                 "AUDIO_POWER_CHECK_INTERVAL_MS")
    source = source.replace("@CONSTANTS@", "\n".join(
        next(line for line in header.splitlines() if line.startswith("#define " + name + " "))
        for name in constants))
    source = source.replace("@AFE_FIELDS@", fields(afe_header, (
        "voice_policy_", "processing_voice_policy_", "output_buffer_policy_", "wake_word_policy_",
        "processing_wake_word_policy_", "control_generation_", "reset_pending_",
        "output_reset_pending_", "afe_control_dirty_", "device_aec_enabled_", "is_speaking_")))
    source = source.replace("@LITE_FIELDS@", fields(lite_header, (
        "voice_policy_", "output_buffer_policy_", "wake_word_policy_", "wake_word_enabled_",
        "voice_processing_enabled_")))
    source = source.replace("@AUDIO_FIELDS@", fields(header, (
        "conversation_policy_", "wake_word_policy_", "wake_word_running_policy_",
        "wake_word_detection_requested_", "wake_word_detection_inhibited_",
        "voice_processing_requested_", "voice_detected_", "service_stopped_",
        "audio_input_need_warmup_", "audio_engine_initialized_", "last_input_time_",
        "last_encode_drop_log_time_")))
    source = source.replace("@TASK@", definition(header, "struct AudioTask") + ";")
    source = source.replace("@STATISTICS@", definition(header, "struct DebugStatistics") + ";")
    source = source.replace("@POLICY@", definition(header, "void SetConversationPolicy("))
    source = source.replace("@CURRENT@", definition(header, "bool IsConversationPolicyCurrent("))
    for name in ("OnOutput", "OnVadStateChange", "OnWakeWordDetected"):
        source = source.replace("@" + name.upper() + "@", definition(
            audio, "audio_engine_->" + name + "(") + ");")
    source += "\n#if CONFIG_IDF_TARGET_ESP32S3\n"
    for name in ("Feed", "EnableWakeWordDetection", "EnableVoiceProcessing", "UpdateActiveState",
                 "ApplyAfeControls", "ApplyPendingReset", "ProcessingTask", "HandleWakeWordResult",
                 "HandleVoiceResult", "OutputRawAudio", "OnOutput", "OnVadStateChange",
                 "OnWakeWordDetected"):
        method = definition(afe, "void AfeAudioEngine::" + name + "(")
        if name == "ProcessingTask":
            method = method.replace("while (true)",
                                    "for (int host_step = 0; host_step < 1; ++host_step)", 1)
        source += method + "\n"
    source += definition(afe, "bool AfeAudioEngine::ClaimWakeWordDetection(") + "\n#else\n"
    for name in ("Feed", "EnableWakeWordDetection", "EnableVoiceProcessing", "OutputRawAudio",
                 "OnOutput", "OnVadStateChange", "OnWakeWordDetected"):
        source += definition(lite, "void LiteAudioEngine::" + name + "(") + "\n"
    source += definition(lite, "bool LiteAudioEngine::CreateWakeWordLocked(") + "\n#endif\n"
    for name in ("ReadAudioData", "InitializeAudioEngine"):
        source += definition(audio, "bool AudioService::" + name + "(") + "\n"
    for name in ("AudioInputTask", "EnableWakeWordDetection", "SetWakeWordDetectionInhibited",
                 "ApplyWakeWordDetection", "UpdateWakeWordRunningState", "ApplyConversationPolicy",
                 "EnableVoiceProcessing", "PushTaskToEncodeQueue"):
        method = definition(audio, "void AudioService::" + name + "(")
        if name == "AudioInputTask":
            # Bound only the outer loop. Preserve the real wait/read/feed, stop,
            # warmup, policy snapshot and post-read epoch checks unchanged.
            method = method.replace("while (true)",
                                    "for (int host_step = 0; host_step < input_steps; ++host_step)", 1)
            if legacy_guard:
                method, count = re.subn(
                    r"if \(\(\(policy & 1U\) != 0 \|\| "
                    r"\(bits & AS_EVENT_WAKE_WORD_RUNNING\) != 0\) &&\s*"
                    r"ReadAudioData\(data, 16000, samples\)\) \{",
                    "if ((policy & 1U) == 0) {\n                continue;\n            }\n"
                    "            if (ReadAudioData(data, 16000, samples)) {",
                    method, count=1)
                if count != 1:
                    raise AssertionError("could not substitute the legacy guard in generated source")
        source += method + "\n"
    source += definition(audio, "std::unique_ptr<AudioStreamPacket> AudioService::PopPacketFromSendQueue(")
    return source + CHECKS


class AudioInputWakeOnlyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        compiler = shlex.split(os.environ.get("CXX", "c++"))
        if not compiler or shutil.which(compiler[0]) is None:
            raise unittest.SkipTest("requires a host C++23 compiler")
        cls.tmp = tempfile.TemporaryDirectory(prefix="audio-input-wake-only-")
        cls.addClassCleanup(cls.tmp.cleanup)
        source = Path(cls.tmp.name) / "audio_input.cc"
        source.write_text(host_source(os.environ.get("AUDIO_INPUT_WAKE_ONLY_LEGACY_GUARD") == "1"),
                          encoding="utf-8")
        cls.binaries = []
        # The StopWatch path uses processed AFE. Also protect raw AFE and Lite.
        for name, afe, processor in (("afe", 1, 1), ("afe_raw", 1, 0), ("lite", 0, 0)):
            binary = Path(cls.tmp.name) / (name + ".exe")
            result = subprocess.run(
                compiler + ["-std=c++23", "-Wall", "-Wextra", "-Werror",
                            "-Wno-unused-parameter", "-fno-exceptions", "-fno-rtti",
                            f"-DCONFIG_IDF_TARGET_ESP32S3={afe}",
                            f"-DHOST_AFE_PROCESSOR={processor}", "-DCONFIG_USE_AUDIO_DEBUGGER=1",
                            str(source), "-o", str(binary)],
                capture_output=True, text=True, timeout=60)
            if result.returncode:
                raise AssertionError(result.stdout + result.stderr)
            cls.binaries.append((name, str(binary)))

    def run_case(self, name, engines=None):
        for engine, binary in self.binaries:
            if engines is not None and engine not in engines:
                continue
            with self.subTest(engine=engine):
                result = subprocess.run([binary, name], capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_initial_disabled_conversation_reads_and_detects_without_voice_or_uplink(self):
        self.run_case("initial_home")

    def test_voice_exit_retains_wake_only_pcm_without_resuming_old_uplink(self):
        self.run_case("voice_exit")

    def test_disabled_without_wake_rejects_even_a_stale_processor_bit(self):
        self.run_case("no_wake")

    def test_default_enabled_wake_retains_stock_detection(self):
        self.run_case("default_wake")

    def test_default_enabled_voice_produces_current_epoch_uplink(self):
        self.run_case("default_voice")

    def test_disabled_to_enabled_during_read_rejects_original_pcm(self):
        self.run_case("read_enable")

    def test_disabled_enabled_disabled_aba_during_read_rejects_original_pcm(self):
        self.run_case("read_disabled_aba")

    def test_enabled_disabled_enabled_aba_during_read_rejects_original_pcm(self):
        self.run_case("read_enabled_aba")

    def test_inhibition_during_read_prevents_actual_inference_and_uplink(self):
        self.run_case("read_inhibit")

    def test_input_timeout_retries_without_terminating_task(self):
        self.run_case("read_retry")

    def test_service_stop_disables_codec_on_input_task_without_reading(self):
        self.run_case("service_stop")

    def test_power_stop_rechecks_stale_active_snapshot_before_reading(self):
        self.run_case("power_stop_idle")

    def test_power_stop_with_armed_wake_preserves_detector_only_capture(self):
        self.run_case("power_stop_wake")

    def test_warmup_delays_once_then_resumes_detector_only_capture(self):
        self.run_case("warmup")

    def test_even_or_stale_pcm_cannot_pass_engine_voice_output_gates(self):
        self.run_case("output_gates")

    def test_processed_afe_fetch_spanning_voice_exit_discards_old_result_then_detects_fresh_pcm(self):
        # Only processed AFE has an active fetch while voice is enabled without wake.
        self.run_case("afe_fetch_transition", ("afe",))


PLATFORM = r'''
#include <atomic>
#include <cassert>
#include <chrono>
#include <condition_variable>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <deque>
#include <functional>
#include <memory>
#include <mutex>
#include <string>
#include <utility>
#include <vector>
#define ESP_LOGD(...) ((void)0)
#define ESP_LOGW(...) ((void)0)
#define ESP_LOGE(...) ((void)0)
#define TAG "AudioInputTest"
#define pdMS_TO_TICKS(ms) (ms)
@CONSTANTS@
constexpr int pdFALSE = 0, pdTRUE = 1, portMAX_DELAY = -1;
constexpr int ESP_FAIL = -1, WAKENET_DETECTED = 1, VAD_SPEECH = 1, VAD_SILENCE = 0;
constexpr bool kUseAfeForVoiceProcessing = HOST_AFE_PROCESSOR;
using EventBits_t = uint32_t;
using EventGroupHandle_t = uint32_t*;
using esp_ae_sample_t = void*;
int input_steps = 1;
std::vector<int> delays;
std::function<void()> after_wait_snapshot;
uint32_t xEventGroupGetBits(uint32_t* group) { return *group; }
uint32_t xEventGroupSetBits(uint32_t* group, uint32_t bits) { return *group |= bits; }
uint32_t xEventGroupClearBits(uint32_t* group, uint32_t bits) { return *group &= ~bits; }
uint32_t xEventGroupWaitBits(uint32_t* group, uint32_t, int, int, int) {
    // Idle waits return an empty snapshot instead of blocking this bounded host loop.
    const auto snapshot = *group;
    auto hook = std::move(after_wait_snapshot); after_wait_snapshot = {};
    if (hook) hook();
    return snapshot;
}
void vTaskDelay(int ticks) { delays.push_back(ticks); }
int esp_timer_stop(void*) { return 0; }
int esp_timer_start_periodic(void*, int64_t) { return 0; }
int64_t esp_timer_get_time() { return 0; }
void esp_ae_rate_cvt_reset(void*) {}
void esp_ae_rate_cvt_get_max_out_sample_num(void*, uint32_t, uint32_t*) { std::abort(); }
void esp_ae_rate_cvt_process(void*, esp_ae_sample_t, uint32_t, esp_ae_sample_t, uint32_t*) { std::abort(); }
[[noreturn]] void esp_system_abort(const char*) { std::abort(); }
@QUEUE@
struct srmodel_list_t {};
struct AudioStreamPacket { uint32_t conversation_policy = 1; };
enum AudioTaskType { kAudioTaskTypeEncodeToSendQueue, kAudioTaskTypeEncodeToTestingQueue,
                     kAudioTaskTypeDecodeToPlaybackQueue };
@TASK@
@STATISTICS@
struct AudioCodec {
    bool input_on = false, fail_next_read = false;
    int reads = 0, enables = 0, disables = 0;
    std::function<void()> on_read;
    bool input_enabled() const { return input_on; }
    int input_sample_rate() const { return 16000; }
    int input_channels() const { return 1; }
    bool input_reference() const { return false; }
    void EnableInput(bool enabled) {
        input_on = enabled;
        if (enabled) ++enables; else ++disables;
    }
    bool InputData(std::vector<int16_t>& data) {
        ++reads;
        assert(input_on && data.size() == 160);
        for (size_t i = 0; i < data.size(); ++i) data[i] = static_cast<int16_t>(i + 1);
        auto hook = std::move(on_read); on_read = {};
        if (hook) hook();
        if (fail_next_read) { fail_next_read = false; return false; }
        return true;
    }
};
#if CONFIG_IDF_TARGET_ESP32S3
struct afe_fetch_result_t {
    int ret_value = 0, wakeup_state = WAKENET_DETECTED, wakenet_model_index = 1;
    int vad_state = VAD_SPEECH;
    int16_t* data = nullptr;
    int data_size = 0;
};
struct FakeAfeInterface {
    std::deque<std::vector<int16_t>> pending;
    std::vector<int16_t> fetched;
    afe_fetch_result_t result;
    bool wakenet_on = false;
    int feeds = 0, fetches = 0, resets = 0;
    std::function<void()> on_fetch;
    int get_feed_chunksize(void*) { return 160; }
    void feed(void*, const int16_t* data) {
        ++feeds; pending.emplace_back(data, data + 160);
    }
    void reset_buffer(void*) { ++resets; pending.clear(); }
    void enable_wakenet(void*) { wakenet_on = true; }
    void disable_wakenet(void*) { wakenet_on = false; }
    void enable_aec(void*) {}
    void disable_aec(void*) {}
    afe_fetch_result_t* fetch_with_delay(void*, int) {
        ++fetches;
        if (pending.empty()) return nullptr;  // Never synthesize a detection without fed PCM.
        fetched = std::move(pending.front()); pending.pop_front();
        result.data = fetched.data(); result.data_size = fetched.size() * sizeof(int16_t);
        result.wakeup_state = wakenet_on ? WAKENET_DETECTED : 0;
        auto hook = std::move(on_fetch); on_fetch = {};
        if (hook) hook();
        return &result;
    }
};
struct FakeCustomWakeWord {
    void Start() {}
    void Stop() {}
    void FeedMono(const int16_t*, size_t) { std::abort(); }
};
class AfeAudioEngine {
public:
    enum class WakeDetector { kNone, kWakeNet, kMultiNet };
    static constexpr EventBits_t kWakeWordEnabled = 1, kVoiceProcessingEnabled = 2, kAfeActive = 4;
    @AFE_FIELDS@
    uint32_t events = 0;
    EventGroupHandle_t event_group_ = &events;
    AudioCodec* codec_ = nullptr;
    int frame_samples_ = 0;
    void* afe_data_ = reinterpret_cast<void*>(1);
    FakeAfeInterface iface;
    FakeAfeInterface* afe_iface_ = &iface;
    WakeDetector wake_detector_ = WakeDetector::kWakeNet;
    std::unique_ptr<FakeCustomWakeWord> custom_wake_word_ = std::make_unique<FakeCustomWakeWord>();
    std::vector<std::string> wake_words_ = {"WakeNet"};
    std::string last_detected_wake_word_;
    std::vector<int16_t> input_buffer_, output_buffer_;
    std::mutex input_buffer_mutex_;
    std::function<void(const std::string&, uint32_t)> wake_word_detected_callback_;
    std::function<void(std::vector<int16_t>&&, uint32_t)> output_callback_;
    std::function<void(bool)> vad_state_change_callback_;
    // Only firmware/model allocation is stubbed; all control/feed/fetch/output bodies are real.
    bool Initialize(AudioCodec* codec, int frame_ms, srmodel_list_t*) {
        codec_ = codec; frame_samples_ = frame_ms * 16000 / 1000; return true;
    }
    bool HasWakeWord() const { return true; }
    void EnableDeviceAec(bool) {}
    void Feed(std::vector<int16_t>&&, uint32_t policy = 1);
    void EnableWakeWordDetection(bool, uint32_t policy = 1);
    void EnableVoiceProcessing(bool, uint32_t policy = 1);
    void UpdateActiveState();
    void ApplyAfeControls();
    void ApplyPendingReset();
    void ProcessingTask();
    bool ClaimWakeWordDetection(uint32_t);
    void HandleWakeWordResult(const afe_fetch_result_t*, uint32_t);
    void HandleVoiceResult(const afe_fetch_result_t*, uint32_t);
    void OutputRawAudio(const std::vector<int16_t>&, uint32_t);
    void OnOutput(std::function<void(std::vector<int16_t>&&, uint32_t)>);
    void OnVadStateChange(std::function<void(bool)>);
    void OnWakeWordDetected(std::function<void(const std::string&, uint32_t)>);
};
using SelectedEngine = AfeAudioEngine;
#else
struct EspWakeWord {
    bool running = false;
    int feeds = 0;
    std::function<void(const std::string&)> callback;
    bool Initialize(AudioCodec*, srmodel_list_t*) { return true; }
    void OnWakeWordDetected(std::function<void(const std::string&)> cb) { callback = std::move(cb); }
    void Start() { running = true; }
    void Stop() { running = false; }
    void Feed(const std::vector<int16_t>& data) {
        assert(running && data.size() == 160); ++feeds;
        running = false; callback("Lite");
    }
};
class LiteAudioEngine {
public:
    @LITE_FIELDS@
    AudioCodec* codec_ = nullptr;
    srmodel_list_t* models_list_ = nullptr;
    bool should_have_wake_word_ = true;
    std::unique_ptr<EspWakeWord> wake_word_;
    std::mutex wake_word_mutex_, output_mutex_;
    std::vector<int16_t> output_buffer_;
    int frame_samples_ = 0;
    std::function<void(const std::string&, uint32_t)> wake_word_detected_callback_;
    std::function<void(std::vector<int16_t>&&, uint32_t)> output_callback_;
    std::function<void(bool)> vad_state_change_callback_;
    bool Initialize(AudioCodec* codec, int frame_ms, srmodel_list_t*) {
        codec_ = codec; frame_samples_ = frame_ms * 16000 / 1000; return CreateWakeWordLocked();
    }
    bool HasWakeWord() const { return wake_word_ != nullptr; }
    bool RestoreWakeWordResources() { return CreateWakeWordLocked(); }
    void EnableDeviceAec(bool) {}
    bool CreateWakeWordLocked();
    void Feed(std::vector<int16_t>&&, uint32_t policy = 1);
    void EnableWakeWordDetection(bool, uint32_t policy = 1);
    void EnableVoiceProcessing(bool, uint32_t policy = 1);
    void OutputRawAudio(const std::vector<int16_t>&, uint32_t);
    void OnOutput(std::function<void(std::vector<int16_t>&&, uint32_t)>);
    void OnVadStateChange(std::function<void(bool)>);
    void OnWakeWordDetected(std::function<void(const std::string&, uint32_t)>);
};
using SelectedEngine = LiteAudioEngine;
#endif
class ObservedEngine : public SelectedEngine {
public:
    std::vector<uint32_t> fed_policies;
    int outputs = 0;
    void Feed(std::vector<int16_t>&& data, uint32_t policy) {
        fed_policies.push_back(policy); SelectedEngine::Feed(std::move(data), policy);
    }
    void OnOutput(std::function<void(std::vector<int16_t>&&, uint32_t)> cb) {
        SelectedEngine::OnOutput([this, cb](std::vector<int16_t>&& data, uint32_t policy) {
            ++outputs; cb(std::move(data), policy);
        });
    }
    void Process() {
#if CONFIG_IDF_TARGET_ESP32S3
        ProcessingTask();
#endif
    }
    bool VoiceEnabled() const {
#if CONFIG_IDF_TARGET_ESP32S3
        return (events & kVoiceProcessingEnabled) != 0;
#else
        return voice_processing_enabled_;
#endif
    }
    int InferenceFeeds() const {
#if CONFIG_IDF_TARGET_ESP32S3
        return iface.feeds;
#else
        return wake_word_ ? wake_word_->feeds : 0;
#endif
    }
};
struct AudioServiceCallbacks {
    std::function<void(const std::string&, uint32_t)> on_wake_word_detected;
    std::function<void(bool)> on_vad_change;
};
struct AudioDebugger {
    int feeds = 0;
    void Feed(const std::vector<int16_t>&) { ++feeds; }
};
class AudioService {
public:
    std::unique_ptr<AudioDebugger> audio_debugger_;
    @AUDIO_FIELDS@
    AudioCodec codec;
    AudioCodec* codec_ = &codec;
    uint32_t events = 0;
    EventGroupHandle_t event_group_ = &events;
    std::unique_ptr<ObservedEngine> audio_engine_ = std::make_unique<ObservedEngine>();
    AudioServiceCallbacks callbacks_;
    int detections = 0, vad_changes = 0, decoder_resets = 0;
    uint32_t detected_policy = 0;
    bool device_aec_enabled_ = false;
    srmodel_list_t* models_list_ = nullptr;
    std::mutex input_resampler_mutex_, audio_queue_mutex_;
    std::condition_variable audio_queue_cv_;
    void* input_resampler_ = nullptr;
    void* audio_power_timer_ = nullptr;
    FixedQueue<AudioTask, MAX_ENCODE_TASKS_IN_QUEUE> audio_encode_queue_;
    FixedQueue<std::unique_ptr<AudioStreamPacket>, MAX_SEND_PACKETS_IN_QUEUE> audio_send_queue_;
    FixedQueue<std::unique_ptr<AudioStreamPacket>, 100> audio_testing_queue_;
    FixedQueue<uint32_t, 3> timestamp_queue_;
    DebugStatistics debug_statistics_;
    AudioService() {
        service_stopped_.store(false);
        @ONOUTPUT@
        @ONVADSTATECHANGE@
        @ONWAKEWORDDETECTED@
        callbacks_.on_wake_word_detected = [this](const std::string&, uint32_t policy) {
            ++detections; detected_policy = policy;
        };
        callbacks_.on_vad_change = [this](bool) { ++vad_changes; };
    }
    @POLICY@
    @CURRENT@
    bool InitializeAudioEngine();
    bool ReadAudioData(std::vector<int16_t>&, int, int);
    void AudioInputTask();
    void EnableWakeWordDetection(bool);
    void SetWakeWordDetectionInhibited(bool, uint32_t policy = 1);
    void ApplyWakeWordDetection();
    void UpdateWakeWordRunningState();
    void ApplyConversationPolicy();
    void EnableVoiceProcessing(bool);
    void PushTaskToEncodeQueue(AudioTaskType, std::vector<int16_t>&&, uint32_t policy = 1);
    std::unique_ptr<AudioStreamPacket> PopPacketFromSendQueue();
    void EnableAudioTesting(bool) { std::abort(); }
    void ResetDecoder() { ++decoder_resets; }
    void WakeOnly(uint32_t conversation = 2, uint32_t wake = 7) {
        SetConversationPolicy(conversation);
        ApplyConversationPolicy();
        SetWakeWordDetectionInhibited(false, wake);
        EnableWakeWordDetection(true);
        audio_engine_->Process();  // Apply initial AFE resets before microphone PCM arrives.
    }
    void Input(int steps = 1) { input_steps = steps; AudioInputTask(); }
    void Capture() { Input(); audio_engine_->Process(); }
};
#define CHECK(expression) do { if (!(expression)) { \
    std::fprintf(stderr, "%s:%d: %s\n", __FILE__, __LINE__, #expression); return 1; \
} } while (false)
'''


CHECKS = r'''
int main(int argc, char** argv) {
    CHECK(argc == 2);
    const std::string test = argv[1];
    AudioService audio;
    auto& engine = *audio.audio_engine_;
    auto& codec = audio.codec;
    if (test == "initial_home" || test == "warmup" || test == "power_stop_wake") {
        audio.WakeOnly();
        CHECK(engine.voice_policy_.load() == 2 && !engine.VoiceEnabled());
        CHECK((audio.events & AS_EVENT_WAKE_WORD_RUNNING) != 0);
        if (test == "warmup") audio.audio_input_need_warmup_.store(true);
        if (test == "power_stop_wake") {
            codec.input_on = true;
            audio.events |= AS_EVENT_AUDIO_INPUT_STOP_REQUEST;
        }
        audio.Input(test == "warmup" ? 2 : 1); engine.Process();
        CHECK(codec.reads == 1 && engine.fed_policies == std::vector<uint32_t>{2});
        CHECK(engine.InferenceFeeds() == 1 && audio.detections == 1 && audio.detected_policy == 7);
        CHECK(engine.outputs == 0 && audio.vad_changes == 0);
        CHECK(audio.audio_encode_queue_.empty() && !audio.PopPacketFromSendQueue());
        CHECK((audio.events & (AS_EVENT_WAKE_WORD_RUNNING | AS_EVENT_AUDIO_PROCESSOR_RUNNING)) == 0);
        CHECK(codec.disables == 0 && audio.conversation_policy_.load() == 2);
        CHECK(!audio.audio_debugger_);  // Wake-only PCM must never reach the debug host.
        if (test == "warmup") CHECK(delays == std::vector<int>{120});
        if (test == "power_stop_wake") CHECK(!(audio.events & AS_EVENT_AUDIO_INPUT_STOP_REQUEST));
    } else if (test == "voice_exit" || test == "default_voice" || test == "output_gates") {
        if (test == "output_gates") { audio.SetConversationPolicy(5); audio.ApplyConversationPolicy(); }
        const auto policy = audio.conversation_policy_.load();
        audio.EnableVoiceProcessing(true);
        CHECK(engine.VoiceEnabled() && (audio.events & AS_EVENT_AUDIO_PROCESSOR_RUNNING));
        engine.Process();
        if (test == "output_gates") {
            const std::vector<int16_t> pcm(960, 1);
            engine.OutputRawAudio(pcm, 2); engine.OutputRawAudio(pcm, 1);
#if CONFIG_IDF_TARGET_ESP32S3
            engine.iface.result.data = const_cast<int16_t*>(pcm.data());
            engine.iface.result.data_size = pcm.size() * sizeof(int16_t);
            engine.HandleVoiceResult(&engine.iface.result, 2);
            engine.HandleVoiceResult(&engine.iface.result, 1);
#endif
            CHECK(engine.outputs == 0 && audio.audio_encode_queue_.empty());
        }
        audio.Input(); CHECK(codec.reads == 0 && delays == std::vector<int>{120});
        for (int i = 0; i < 6; ++i) audio.Capture();
        CHECK(codec.reads == 6 && engine.outputs == 1 && audio.detections == 0);
        CHECK(audio.audio_encode_queue_.size() == 1);
        CHECK(audio.audio_encode_queue_.front().conversation_policy == policy);
        CHECK(audio.audio_encode_queue_.front().pcm.size() == 960);
        CHECK(engine.fed_policies == std::vector<uint32_t>(6, policy));
        if (test == "voice_exit") {
            audio.WakeOnly();
            CHECK(!engine.VoiceEnabled() && audio.audio_encode_queue_.empty());
            const auto previous = codec.reads;
            audio.Capture();
            CHECK(codec.reads == previous + 1 && engine.fed_policies.back() == 2);
            CHECK(audio.detections == 1 && audio.detected_policy == 7);
            CHECK(engine.outputs == 1 && audio.audio_encode_queue_.empty());
            CHECK(!audio.PopPacketFromSendQueue() && !engine.VoiceEnabled());
        }
    } else if (test == "no_wake") {
        audio.SetConversationPolicy(2); audio.ApplyConversationPolicy();
        audio.Input(); CHECK(codec.reads == 0);
        delays.clear();
        // An obsolete processor event must not authorize detector-only microphone reads.
        audio.events = AS_EVENT_AUDIO_PROCESSOR_RUNNING;
        audio.Input(2);
        CHECK(codec.reads == 0 && engine.fed_policies.empty());
        CHECK(engine.outputs == 0 && audio.detections == 0 && audio.audio_encode_queue_.empty());
        CHECK(delays == (std::vector<int>{10, 10}));  // Rejected snapshots must not busy-spin.
    } else if (test == "default_wake" || test == "read_retry") {
        audio.EnableWakeWordDetection(true); engine.Process();
        if (test == "read_retry") codec.fail_next_read = true;
        audio.Input(test == "read_retry" ? 2 : 1); engine.Process();
        CHECK(codec.reads == (test == "read_retry" ? 2 : 1));
        CHECK(engine.fed_policies == std::vector<uint32_t>{1});
        CHECK(audio.detections == 1 && audio.detected_policy == 1 && engine.outputs == 0);
        CHECK(audio.audio_encode_queue_.empty() && !engine.VoiceEnabled());
        if (test == "read_retry") CHECK(delays == std::vector<int>{10});
    } else if (test == "read_enable" || test == "read_disabled_aba" || test == "read_enabled_aba") {
        const bool initially_enabled = test == "read_enabled_aba";
        audio.WakeOnly(initially_enabled ? 1 : 2);
        const uint32_t fresh = test == "read_disabled_aba" ? 6 : 5;
        codec.on_read = [&]() {
            if (initially_enabled) { audio.SetConversationPolicy(2); audio.ApplyConversationPolicy(); }
            audio.SetConversationPolicy(5); audio.ApplyConversationPolicy();
            if (fresh == 6) { audio.SetConversationPolicy(6); audio.ApplyConversationPolicy(); }
            audio.SetWakeWordDetectionInhibited(false, 9);
            audio.EnableWakeWordDetection(true);
        };
        audio.Input();
        CHECK(codec.reads == 1 && engine.fed_policies.empty());
        CHECK(!audio.audio_debugger_);  // Capture policy changed while the codec read blocked.
        CHECK(audio.detections == 0 && engine.InferenceFeeds() == 0 && engine.outputs == 0);
        engine.Process(); audio.Capture();
        CHECK(codec.reads == 2 && engine.fed_policies == std::vector<uint32_t>{fresh});
        CHECK(audio.detections == 1 && audio.detected_policy == 9 && engine.outputs == 0);
        CHECK(audio.audio_encode_queue_.empty() && !audio.PopPacketFromSendQueue());
    } else if (test == "read_inhibit") {
        audio.WakeOnly();
        codec.on_read = [&]() { audio.SetWakeWordDetectionInhibited(true, 8); };
        audio.Input(); engine.Process();
        CHECK(codec.reads == 1 && engine.InferenceFeeds() == 0 && audio.detections == 0);
        CHECK(engine.outputs == 0 && audio.audio_encode_queue_.empty());
        CHECK(!(audio.events & AS_EVENT_WAKE_WORD_RUNNING));
    } else if (test == "service_stop") {
        audio.WakeOnly(); codec.input_on = true;
        audio.service_stopped_.store(true);
        audio.Input(3);
        CHECK(codec.reads == 0 && codec.disables == 1 && !codec.input_on);
        CHECK(engine.fed_policies.empty() && delays.empty());
    } else if (test == "power_stop_idle") {
        audio.WakeOnly(); codec.input_on = true;
        audio.events |= AS_EVENT_AUDIO_INPUT_STOP_REQUEST;
        // waitBits returned wake-active, but inhibition removes it before the task rechecks.
        after_wait_snapshot = [&]() { audio.SetWakeWordDetectionInhibited(true, 8); };
        audio.Input();
        CHECK(codec.reads == 0 && codec.disables == 1 && !codec.input_on);
        CHECK(engine.fed_policies.empty() && !(audio.events & AS_EVENT_AUDIO_INPUT_STOP_REQUEST));
#if CONFIG_IDF_TARGET_ESP32S3
    } else if (test == "afe_fetch_transition") {
        audio.EnableVoiceProcessing(true);
        engine.Process();
        audio.Input();  // Voice warmup.
        audio.Input();  // Old conversation PCM remains in the AFE until fetch.
        const auto generation = engine.control_generation_.load();
        engine.iface.on_fetch = [&]() {
            // The old fetch is in flight while main disables voice and rearms wake.
            // Do not recurse into ProcessingTask: it owns fetch/reset state.
            audio.SetConversationPolicy(2); audio.ApplyConversationPolicy();
            audio.SetWakeWordDetectionInhibited(false, 7);
            audio.EnableWakeWordDetection(true);
        };
        engine.Process();
        CHECK(codec.reads == 1 && engine.control_generation_.load() > generation);
        CHECK(engine.outputs == 0 && audio.detections == 0);
        engine.Process();  // Adopt/reset the fresh even capture epoch, then await PCM.
        audio.Capture();
        CHECK(codec.reads == 2 && engine.fed_policies == (std::vector<uint32_t>{1, 2}));
        CHECK(audio.detections == 1 && audio.detected_policy == 7);
        CHECK(engine.outputs == 0 && audio.audio_encode_queue_.empty() && !engine.VoiceEnabled());
#endif
    } else { CHECK(false); }
    return 0;
}
'''
