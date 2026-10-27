#include "afe_audio_engine.h"

#include <cassert>
#include <cstring>
#include <string_view>
#include <vector>

#include <esp_heap_caps.h>
#include <esp_log.h>
#include <esp_timer.h>
#include <esp_vadn_models.h>

#include "audio_service.h"
#include "wake_words/custom_wake_word.h"

#define TAG "AfeAudioEngine"

#if CONFIG_USE_AUDIO_PROCESSOR
static constexpr bool kUseAfeForVoiceProcessing = true;
#else
static constexpr bool kUseAfeForVoiceProcessing = false;
#endif

AfeAudioEngine::AfeAudioEngine() { event_group_ = xEventGroupCreate(); }

AfeAudioEngine::~AfeAudioEngine() {
    custom_wake_word_.reset();
    if (afe_data_ != nullptr) {
        afe_iface_->destroy(afe_data_);
    }
    if (processing_task_stack_ != nullptr) {
        heap_caps_free(processing_task_stack_);
    }
    if (processing_task_buffer_ != nullptr) {
        heap_caps_free(processing_task_buffer_);
    }
    if (wake_word_encode_task_stack_ != nullptr) {
        heap_caps_free(wake_word_encode_task_stack_);
    }
    if (wake_word_encode_task_buffer_ != nullptr) {
        heap_caps_free(wake_word_encode_task_buffer_);
    }
    if (owns_models_ && models_ != nullptr) {
        esp_srmodel_deinit(models_);
    }
    if (event_group_ != nullptr) {
        vEventGroupDelete(event_group_);
    }
}

bool AfeAudioEngine::Initialize(AudioCodec* codec, int frame_duration_ms,
                                srmodel_list_t* models_list) {
    if (afe_data_ != nullptr || (codec_ != nullptr && !kUseAfeForVoiceProcessing &&
                                 wake_detector_ == WakeDetector::kNone)) {
        return true;
    }
    if (codec == nullptr) {
        ESP_LOGE(TAG, "Codec is null");
        return false;
    }

    codec_ = codec;
    frame_samples_ = frame_duration_ms * 16000 / 1000;
    output_buffer_.reserve(frame_samples_);

    if (models_list == nullptr) {
        models_ = esp_srmodel_init("model");
        owns_models_ = models_ != nullptr;
    } else {
        models_ = models_list;
    }

    char* wakenet_model_name = nullptr;
    char* multinet_model_name = nullptr;
    if (models_ != nullptr && models_->num > 0) {
        wakenet_model_name = esp_srmodel_filter(models_, ESP_WN_PREFIX, nullptr);
        multinet_model_name = esp_srmodel_filter(models_, ESP_MN_PREFIX, nullptr);
        for (int i = 0; i < models_->num; ++i) {
            ESP_LOGI(TAG, "Model %d: %s", i, models_->model_name[i]);
        }
    }

    if (multinet_model_name != nullptr) {
        wake_detector_ = WakeDetector::kMultiNet;
        custom_wake_word_ = std::make_unique<CustomWakeWord>();
        custom_wake_word_->OnWakeWordDetected([this](const std::string& wake_word) {
            const auto policy = processing_wake_word_policy_;
            if (!ClaimWakeWordDetection(policy)) {
                // An older Feed may have stopped MultiNet after a newer enable.
                // Feed and this callback share ProcessingTask, so rearm it here.
                if (wake_word_policy_.load() & 1U) {
                    custom_wake_word_->Start();
                }
                return;
            }
            last_detected_wake_word_ = wake_word;
            UpdateActiveState();
            if (wake_word_detected_callback_) {
                wake_word_detected_callback_(wake_word, policy);
            }
        });
        if (!custom_wake_word_->Initialize(codec_, models_)) {
            ESP_LOGE(TAG, "Failed to initialize MultiNet wake-word detector");
            custom_wake_word_.reset();
            wake_detector_ = WakeDetector::kNone;
            return false;
        }
    } else if (wakenet_model_name != nullptr) {
        wake_detector_ = WakeDetector::kWakeNet;
#if CONFIG_SEND_WAKE_WORD_DATA
        if (!wake_word_audio_cache_.Initialize(16000 * 2)) {
            ESP_LOGW(TAG, "Wake-word audio upload disabled: PSRAM cache allocation failed");
        }
#endif
    }

    const bool needs_afe = kUseAfeForVoiceProcessing || wake_detector_ != WakeDetector::kNone;
    if (!needs_afe) {
        ESP_LOGI(TAG, "Initialized as raw engine because AFE features are disabled");
        return true;
    }

    int ref_num = codec_->input_reference() ? 1 : 0;
    std::string input_format;
    for (int i = 0; i < codec_->input_channels() - ref_num; ++i) {
        input_format.push_back('M');
    }
    for (int i = 0; i < ref_num; ++i) {
        input_format.push_back('R');
    }

    char* vad_model_name =
        models_ == nullptr ? nullptr : esp_srmodel_filter(models_, ESP_VADN_PREFIX, nullptr);
    afe_config_t* afe_config =
        afe_config_init(input_format.c_str(), models_, AFE_TYPE_FD, AFE_MODE_LOW_COST);
    if (afe_config == nullptr) {
        ESP_LOGE(TAG, "Failed to create AFE configuration");
        return false;
    }

    afe_config->aec_init = codec_->input_reference();
    afe_config->aec_mode = AEC_MODE_FD_LOW_COST;
    afe_config->aec_nlp_level = AEC_NLP_LEVEL_VERYAGGR;
    afe_config->ns_init = false;
    afe_config->vad_init = kUseAfeForVoiceProcessing;
    afe_config->vad_mode = VAD_MODE_0;
    afe_config->vad_min_noise_ms = 100;
    if (vad_model_name != nullptr) {
        afe_config->vad_model_name = vad_model_name;
    }
    afe_config->wakenet_init = wake_detector_ == WakeDetector::kWakeNet;
    afe_config->wakenet_model_name = nullptr;
    afe_config->wakenet_model_name_2 = nullptr;
    if (wake_detector_ == WakeDetector::kWakeNet && models_ != nullptr) {
        std::vector<char*> wakenet_models;
        for (int i = 0; i < models_->num; ++i) {
            char* name = models_->model_name[i];
            if (name != nullptr && strncmp(name, ESP_WN_PREFIX, strlen(ESP_WN_PREFIX)) == 0) {
                wakenet_models.push_back(name);
            }
        }
        if (!wakenet_models.empty()) {
            afe_config->wakenet_model_name = wakenet_models[0];
        }
        if (wakenet_models.size() > 1) {
            afe_config->wakenet_model_name_2 = wakenet_models[1];
        }
        // Only the two models selected above are active in the AFE.
        wake_words_.clear();
        for (auto name : {afe_config->wakenet_model_name, afe_config->wakenet_model_name_2}) {
            if (name == nullptr) {
                continue;
            }
            auto words = esp_srmodel_get_wake_words(models_, name);
            if (words == nullptr) {
                continue;
            }
            std::string_view remaining(words);
            while (!remaining.empty()) {
                const auto end = remaining.find(';');
                const auto word = remaining.substr(0, end);
                if (!word.empty()) {
                    wake_words_.emplace_back(word);
                }
                if (end == std::string_view::npos) {
                    break;
                }
                remaining.remove_prefix(end + 1);
            }
        }
    }
    afe_config->agc_init = false;
    afe_config->memory_alloc_mode = AFE_MEMORY_ALLOC_MORE_PSRAM;

    ESP_LOGI(TAG, "Before AFE create: free=%u min=%u largest=%u",
             heap_caps_get_free_size(MALLOC_CAP_INTERNAL),
             heap_caps_get_minimum_free_size(MALLOC_CAP_INTERNAL),
             heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL));
    afe_iface_ = esp_afe_handle_from_config(afe_config);
    if (afe_iface_ != nullptr) {
        afe_data_ = afe_iface_->create_from_config(afe_config);
    }
    afe_config_free(afe_config);

    if (afe_iface_ == nullptr || afe_data_ == nullptr) {
        ESP_LOGE(TAG, "Failed to create FD AFE instance");
        afe_iface_ = nullptr;
        afe_data_ = nullptr;
        return false;
    }

    if (wake_detector_ == WakeDetector::kWakeNet) {
        afe_iface_->disable_wakenet(afe_data_);
    }
    if (codec_->input_reference()) {
        afe_iface_->disable_aec(afe_data_);
    }
    afe_iface_->print_pipeline(afe_data_);

    if (processing_task_stack_ == nullptr) {
        processing_task_stack_ = static_cast<StackType_t*>(
            heap_caps_malloc(kProcessingTaskStackSize, MALLOC_CAP_SPIRAM));
    }
    if (processing_task_buffer_ == nullptr) {
        processing_task_buffer_ =
            static_cast<StaticTask_t*>(heap_caps_malloc(sizeof(StaticTask_t), MALLOC_CAP_INTERNAL));
    }
    if (processing_task_stack_ == nullptr || processing_task_buffer_ == nullptr) {
        ESP_LOGE(TAG, "Failed to allocate AFE processing task memory");
        afe_iface_->destroy(afe_data_);
        afe_data_ = nullptr;
        afe_iface_ = nullptr;
        return false;
    }

    processing_task_ = xTaskCreateStatic(
        [](void* arg) {
            auto* engine = static_cast<AfeAudioEngine*>(arg);
            engine->ProcessingTask();
            vTaskDelete(nullptr);
        },
        "audio_afe", kProcessingTaskStackSize, this, 3, processing_task_stack_,
        processing_task_buffer_);
    if (processing_task_ == nullptr) {
        ESP_LOGE(TAG, "Failed to create AFE processing task, internal free=%u largest=%u",
                 heap_caps_get_free_size(MALLOC_CAP_INTERNAL),
                 heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL));
        afe_iface_->destroy(afe_data_);
        afe_data_ = nullptr;
        afe_iface_ = nullptr;
        return false;
    }

    const char* detector = wake_detector_ == WakeDetector::kWakeNet
                               ? "WakeNet"
                               : (wake_detector_ == WakeDetector::kMultiNet ? "MultiNet" : "none");
    ESP_LOGI(TAG, "Initialized FD AFE, detector: %s, NS: off, feed: %d, fetch: %d", detector,
             afe_iface_->get_feed_chunksize(afe_data_), afe_iface_->get_fetch_chunksize(afe_data_));
    ESP_LOGI(TAG, "After AFE create: free=%u min=%u largest=%u",
             heap_caps_get_free_size(MALLOC_CAP_INTERNAL),
             heap_caps_get_minimum_free_size(MALLOC_CAP_INTERNAL),
             heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL));
    return true;
}

void AfeAudioEngine::Feed(std::vector<int16_t>&& data, uint32_t voice_policy) {
    EventBits_t bits = xEventGroupGetBits(event_group_);
    if ((bits & kVoiceProcessingEnabled) && !kUseAfeForVoiceProcessing) {
        OutputRawAudio(data, voice_policy);
    }
    if (afe_data_ == nullptr || (bits & kAfeActive) == 0) {
        return;
    }

    std::lock_guard<std::mutex> lock(input_buffer_mutex_);
    if ((xEventGroupGetBits(event_group_) & kAfeActive) == 0 ||
        (voice_policy & ~1U) != (voice_policy_.load() & ~1U)) {
        return;
    }
    input_buffer_.insert(input_buffer_.end(), data.begin(), data.end());
    size_t chunk_size = afe_iface_->get_feed_chunksize(afe_data_) * codec_->input_channels();
    while (input_buffer_.size() >= chunk_size) {
        afe_iface_->feed(afe_data_, input_buffer_.data());
        input_buffer_.erase(input_buffer_.begin(), input_buffer_.begin() + chunk_size);
    }
}

void AfeAudioEngine::EnableWakeWordDetection(bool enable, uint32_t policy) {
    wake_word_policy_.store(enable ? (policy | 1U) : (policy & ~1U));
    if (!HasWakeWord()) {
        return;
    }

    // WakeNet controls are deferred to ProcessingTask; event bits mirror the atomic state.
    if (wake_detector_ == WakeDetector::kMultiNet) {
        if (enable) {
            custom_wake_word_->Start();
        } else {
            custom_wake_word_->Stop();
        }
    }
    UpdateActiveState();
}

void AfeAudioEngine::EnableVoiceProcessing(bool enable, uint32_t policy) {
    voice_policy_.store(enable ? (policy | 1U) : (policy & ~1U));
    if (enable) {
        xEventGroupSetBits(event_group_, kVoiceProcessingEnabled);
    } else {
        xEventGroupClearBits(event_group_, kVoiceProcessingEnabled);
        is_speaking_ = false;
    }
    UpdateActiveState();
}

void AfeAudioEngine::EnableDeviceAec(bool enable) {
    device_aec_enabled_.store(enable);
    if (enable && (codec_ == nullptr || !codec_->input_reference())) {
        ESP_LOGW(TAG, "Device AEC requires a playback reference channel");
    }
    UpdateAecState();
}

bool AfeAudioEngine::HasWakeWord() const { return wake_detector_ != WakeDetector::kNone; }

std::vector<std::string> AfeAudioEngine::GetWakeWords() const {
    return custom_wake_word_ ? custom_wake_word_->GetWakeWords() : wake_words_;
}

bool AfeAudioEngine::IsWakeWordDetectionEnabled() const {
    return (wake_word_policy_.load() & 1U) != 0;
}

bool AfeAudioEngine::IsVoiceProcessingEnabled() const {
    return event_group_ != nullptr &&
           (xEventGroupGetBits(event_group_) & kVoiceProcessingEnabled) != 0;
}

size_t AfeAudioEngine::GetFeedSize() const {
    return afe_data_ == nullptr ? frame_samples_ : afe_iface_->get_feed_chunksize(afe_data_);
}

void AfeAudioEngine::OnWakeWordDetected(
    std::function<void(const std::string& wake_word, uint32_t policy)> callback) {
    wake_word_detected_callback_ = std::move(callback);
}

void AfeAudioEngine::OnOutput(
    std::function<void(std::vector<int16_t>&& data, uint32_t policy)> callback) {
    output_callback_ = std::move(callback);
}

void AfeAudioEngine::OnVadStateChange(std::function<void(bool speaking)> callback) {
    vad_state_change_callback_ = std::move(callback);
}

void AfeAudioEngine::UpdateActiveState() {
    // Event bits wake tasks; wake_word_policy_ is authoritative. A competing enable
    // may run between our snapshot and publication, so reconcile again if it changed.
    // No lock or wait: inference and callbacks never participate in this transition.
    while (true) {
        const auto policy = wake_word_policy_.load();
        const bool voice_enabled =
            (xEventGroupGetBits(event_group_) & kVoiceProcessingEnabled) != 0;
        if (policy & 1U) {
            xEventGroupSetBits(event_group_, kWakeWordEnabled);
        } else {
            xEventGroupClearBits(event_group_, kWakeWordEnabled);
        }
        const bool afe_active = afe_data_ != nullptr &&
                                ((policy & 1U) || (kUseAfeForVoiceProcessing && voice_enabled));
        if (afe_active) {
            xEventGroupSetBits(event_group_, kAfeActive);
        } else {
            xEventGroupClearBits(event_group_, kAfeActive);
            control_generation_.fetch_add(1);
            // Input clearing/reset is owned by ProcessingTask and serialized with Feed.
            // Do not lock input_buffer_mutex_ or clear live input from this task.
            if (afe_data_ != nullptr) {
                reset_pending_ = true;
            }
        }
        if (!voice_enabled) {
            output_reset_pending_ = true;
        }
        afe_control_dirty_ = true;
        if (policy == wake_word_policy_.load() &&
            voice_enabled == ((xEventGroupGetBits(event_group_) & kVoiceProcessingEnabled) != 0)) {
            return;
        }
    }
}

void AfeAudioEngine::UpdateAecState() {
    if (afe_data_ == nullptr || codec_ == nullptr || !codec_->input_reference()) {
        return;
    }
    afe_control_dirty_ = true;
}

void AfeAudioEngine::ApplyAfeControls() {
    EventBits_t bits = xEventGroupGetBits(event_group_);
    if (wake_detector_ == WakeDetector::kWakeNet) {
        if (bits & kWakeWordEnabled) {
            afe_iface_->enable_wakenet(afe_data_);
        } else {
            afe_iface_->disable_wakenet(afe_data_);
        }
    }
    if (codec_->input_reference()) {
        const bool enable_aec = (bits & kWakeWordEnabled) ||
                                (device_aec_enabled_.load() && (bits & kVoiceProcessingEnabled));
        if (enable_aec) {
            afe_iface_->enable_aec(afe_data_);
        } else {
            afe_iface_->disable_aec(afe_data_);
        }
    }
}

void AfeAudioEngine::ApplyPendingReset() {
    if (!reset_pending_.exchange(false)) {
        return;
    }
    // Discard audio recorded before (re)activation. Holding input_buffer_mutex_
    // serializes the reset against Feed(); fetch/reset both run in this task.
    std::lock_guard<std::mutex> lock(input_buffer_mutex_);
    input_buffer_.clear();
    afe_iface_->reset_buffer(afe_data_);
}

void AfeAudioEngine::ProcessingTask() {
    while (true) {
        xEventGroupWaitBits(event_group_, kAfeActive, pdFALSE, pdTRUE, portMAX_DELAY);
        ApplyPendingReset();
        if ((xEventGroupGetBits(event_group_) & kAfeActive) == 0) {
            continue;
        }
        if (afe_control_dirty_.exchange(false)) {
            // WakeNet/AEC toggles are not safe against a concurrent fetch,
            // so they are applied here, in the task that owns the fetch side.
            ApplyAfeControls();
        }
        const auto voice_policy = voice_policy_.load();
        if (processing_voice_policy_ != voice_policy) {
            // AFE history predates this PCM epoch, even if wake keeps AFE active.
            // Only the processing task resets fetch state; Feed is serialized by
            // the existing input mutex and rechecks the PCM token under it.
            reset_pending_ = true;
            ApplyPendingReset();
            processing_voice_policy_ = voice_policy;
        }
        const uint32_t generation = control_generation_.load();
        // Capture provenance before fetch: callbacks can outlive disable/re-enable,
        // even when voice processing keeps the AFE active throughout the policy change.
        const uint32_t wake_word_policy = wake_word_policy_.load();
        auto* result = afe_iface_->fetch_with_delay(afe_data_, portMAX_DELAY);
        if (generation != control_generation_.load() ||
            (xEventGroupGetBits(event_group_) & kAfeActive) == 0) {
            // A disable/re-enable may make an old blocked fetch return after the
            // AFE is active again. Reset immediately and never process that frame.
            ApplyPendingReset();
            continue;
        }
        if (result == nullptr || result->ret_value == ESP_FAIL) {
            if (result != nullptr) {
                ESP_LOGW(TAG, "AFE fetch failed: %d", result->ret_value);
            }
            continue;
        }

        EventBits_t bits = xEventGroupGetBits(event_group_);
        if (bits & kWakeWordEnabled) {
            HandleWakeWordResult(result, wake_word_policy);
        }
        if (kUseAfeForVoiceProcessing && (bits & kVoiceProcessingEnabled)) {
            HandleVoiceResult(result, voice_policy);
        }
    }
}

bool AfeAudioEngine::ClaimWakeWordDetection(uint32_t policy) {
    if ((policy & 1U) == 0) {
        return false;
    }
    // Serialize detection's auto-stop with re-enable without locking around inference.
    return wake_word_policy_.compare_exchange_strong(policy, policy & ~1U);
}

void AfeAudioEngine::HandleWakeWordResult(const afe_fetch_result_t* result, uint32_t policy) {
    if (policy != wake_word_policy_.load()) {
        return;
    }
    if (wake_detector_ == WakeDetector::kMultiNet) {
        // MultiNet invokes its callback synchronously inside FeedMono, on this task.
        processing_wake_word_policy_ = policy;
        custom_wake_word_->FeedMono(result->data, result->data_size / sizeof(int16_t));
        return;
    }

#if CONFIG_SEND_WAKE_WORD_DATA
    wake_word_audio_cache_.Store(result->data, result->data_size / sizeof(int16_t));
#endif
    if (result->wakeup_state != WAKENET_DETECTED) {
        return;
    }

    int model_index = result->wakenet_model_index - 1;
    if (model_index < 0 || model_index >= static_cast<int>(wake_words_.size())) {
        ESP_LOGE(TAG, "Invalid WakeNet model index: %d", result->wakenet_model_index);
        return;
    }

    if (!ClaimWakeWordDetection(policy)) {
        return;
    }
    last_detected_wake_word_ = wake_words_[model_index];
    // UpdateActiveState marks the AFE controls dirty; the next loop iteration
    // of ProcessingTask disables WakeNet via ApplyAfeControls.
    UpdateActiveState();
    if (wake_word_detected_callback_) {
        wake_word_detected_callback_(last_detected_wake_word_, policy);
    }
}

void AfeAudioEngine::HandleVoiceResult(const afe_fetch_result_t* result, uint32_t policy) {
    if ((policy & 1U) == 0 || policy != voice_policy_.load()) {
        return;
    }
    if (output_reset_pending_.exchange(false) || output_buffer_policy_ != policy) {
        output_buffer_.clear();
        output_buffer_policy_ = policy;
    }
    if (vad_state_change_callback_) {
        if (result->vad_state == VAD_SPEECH && !is_speaking_) {
            is_speaking_ = true;
            vad_state_change_callback_(true);
        } else if (result->vad_state == VAD_SILENCE && is_speaking_) {
            is_speaking_ = false;
            vad_state_change_callback_(false);
        }
    }
    if (!output_callback_) {
        return;
    }

    size_t samples = result->data_size / sizeof(int16_t);
    output_buffer_.insert(output_buffer_.end(), result->data, result->data + samples);
    while (output_buffer_.size() >= static_cast<size_t>(frame_samples_)) {
        if (output_buffer_.size() == static_cast<size_t>(frame_samples_)) {
            output_callback_(std::move(output_buffer_), policy);
            output_buffer_.clear();
            output_buffer_.reserve(frame_samples_);
        } else {
            output_callback_(std::vector<int16_t>(output_buffer_.begin(),
                                                  output_buffer_.begin() + frame_samples_), policy);
            output_buffer_.erase(output_buffer_.begin(), output_buffer_.begin() + frame_samples_);
        }
    }
}

void AfeAudioEngine::OutputRawAudio(const std::vector<int16_t>& data, uint32_t policy) {
    if (!output_callback_ || codec_ == nullptr || (policy & 1U) == 0 ||
        policy != voice_policy_.load()) {
        return;
    }
    if (output_reset_pending_.exchange(false) || output_buffer_policy_ != policy) {
        output_buffer_.clear();
        output_buffer_policy_ = policy;
    }
    const size_t channels = codec_->input_channels();
    if (channels <= 1) {
        output_buffer_.insert(output_buffer_.end(), data.begin(), data.end());
    } else {
        for (size_t i = 0; i < data.size(); i += channels) {
            output_buffer_.push_back(data[i]);
        }
    }
    while (output_buffer_.size() >= static_cast<size_t>(frame_samples_)) {
        output_callback_(
            std::vector<int16_t>(output_buffer_.begin(), output_buffer_.begin() + frame_samples_),
            policy);
        output_buffer_.erase(output_buffer_.begin(), output_buffer_.begin() + frame_samples_);
    }
}

void AfeAudioEngine::EncodeWakeWordData() {
    if (wake_detector_ == WakeDetector::kMultiNet) {
        custom_wake_word_->EncodeWakeWordData();
        return;
    }
    if (wake_detector_ != WakeDetector::kWakeNet) {
        return;
    }

    const size_t stack_size = 4096 * 6;
    wake_word_opus_.clear();
    if (wake_word_encode_task_stack_ == nullptr) {
        wake_word_encode_task_stack_ =
            static_cast<StackType_t*>(heap_caps_malloc(stack_size, MALLOC_CAP_SPIRAM));
        assert(wake_word_encode_task_stack_ != nullptr);
    }
    if (wake_word_encode_task_buffer_ == nullptr) {
        wake_word_encode_task_buffer_ =
            static_cast<StaticTask_t*>(heap_caps_malloc(sizeof(StaticTask_t), MALLOC_CAP_INTERNAL));
        assert(wake_word_encode_task_buffer_ != nullptr);
    }

    wake_word_encode_task_ = xTaskCreateStatic(
        [](void* arg) {
            auto* engine = static_cast<AfeAudioEngine*>(arg);
            auto start_time = esp_timer_get_time();
            esp_opus_enc_config_t opus_enc_cfg = AS_OPUS_ENC_CONFIG();
            void* encoder_handle = nullptr;
            auto ret =
                esp_opus_enc_open(&opus_enc_cfg, sizeof(esp_opus_enc_config_t), &encoder_handle);
            if (encoder_handle == nullptr) {
                ESP_LOGE(TAG, "Failed to create wake-word encoder: %d", ret);
                engine->wake_word_audio_cache_.Clear();
                {
                    std::lock_guard<std::mutex> lock(engine->wake_word_mutex_);
                    engine->wake_word_opus_.emplace_back();
                    engine->wake_word_cv_.notify_all();
                }
                vTaskDelete(nullptr);
                return;
            }

            int frame_size = 0;
            int outbuf_size = 0;
            esp_opus_enc_get_frame_size(encoder_handle, &frame_size, &outbuf_size);
            frame_size /= sizeof(int16_t);
            int packets = 0;
            std::vector<int16_t> input(frame_size);
            esp_audio_enc_in_frame_t in = {};
            esp_audio_enc_out_frame_t out = {};

            const size_t cached_samples = engine->wake_word_audio_cache_.Size();
            for (size_t offset = 0; offset + static_cast<size_t>(frame_size) <= cached_samples;
                 offset += frame_size) {
                if (engine->wake_word_audio_cache_.Read(offset, input.data(), frame_size) !=
                    static_cast<size_t>(frame_size)) {
                    break;
                }
                std::vector<uint8_t> opus_buf(outbuf_size);
                in.buffer = reinterpret_cast<uint8_t*>(input.data());
                in.len = frame_size * sizeof(int16_t);
                out.buffer = opus_buf.data();
                out.len = outbuf_size;
                out.encoded_bytes = 0;
                ret = esp_opus_enc_process(encoder_handle, &in, &out);
                if (ret == ESP_AUDIO_ERR_OK) {
                    std::lock_guard<std::mutex> lock(engine->wake_word_mutex_);
                    engine->wake_word_opus_.emplace_back(opus_buf.data(),
                                                         opus_buf.data() + out.encoded_bytes);
                    engine->wake_word_cv_.notify_all();
                    ++packets;
                } else {
                    ESP_LOGE(TAG, "Failed to encode wake-word audio: %d", ret);
                }
            }

            engine->wake_word_audio_cache_.Clear();
            esp_opus_enc_close(encoder_handle);
            ESP_LOGI(TAG, "Encoded wake word into %d packets in %ld ms", packets,
                     static_cast<long>((esp_timer_get_time() - start_time) / 1000));
            {
                std::lock_guard<std::mutex> lock(engine->wake_word_mutex_);
                engine->wake_word_opus_.emplace_back();
                engine->wake_word_cv_.notify_all();
            }
            vTaskDelete(nullptr);
        },
        "encode_wake_word", stack_size, this, 2, wake_word_encode_task_stack_,
        wake_word_encode_task_buffer_);
}

bool AfeAudioEngine::GetWakeWordOpus(std::vector<uint8_t>& opus) {
    if (wake_detector_ == WakeDetector::kMultiNet) {
        return custom_wake_word_->GetWakeWordOpus(opus);
    }
    if (wake_detector_ != WakeDetector::kWakeNet) {
        return false;
    }
    std::unique_lock<std::mutex> lock(wake_word_mutex_);
    wake_word_cv_.wait(lock, [this]() { return !wake_word_opus_.empty(); });
    opus.swap(wake_word_opus_.front());
    wake_word_opus_.pop_front();
    return !opus.empty();
}
