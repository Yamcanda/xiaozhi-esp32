#include "protocol.h"
#include "assets.h"

#include <esp_log.h>

#define TAG "Protocol"

void Protocol::AddTextFontCapabilities(cJSON* root) {
    auto capability = Assets::GetInstance().text_font_capability();
    cJSON* features = cJSON_GetObjectItem(root, "features");
    if (cJSON_IsObject(features)) {
        cJSON_AddBoolToObject(features, "glyph_push", capability.glyph_push);
    }

    if (!capability.glyph_push) {
        return;
    }
    cJSON* font = cJSON_CreateObject();
    cJSON_AddStringToObject(font, "bundle", capability.bundle.c_str());
    cJSON_AddStringToObject(font, "charset", capability.charset.c_str());
    cJSON_AddNumberToObject(font, "size", capability.size);
    cJSON_AddNumberToObject(font, "bpp", capability.bpp);
    cJSON_AddItemToObject(root, "text_font", font);
}

void Protocol::OnIncomingJson(
    std::function<void(const cJSON* root, uint32_t generation)> callback) {
    on_incoming_json_ = callback;
}

void Protocol::OnIncomingAudio(
    std::function<void(std::unique_ptr<AudioStreamPacket> packet)> callback) {
    on_incoming_audio_ = callback;
}

void Protocol::OnAudioChannelOpened(std::function<void(uint32_t generation)> callback) {
    on_audio_channel_opened_ = callback;
}

void Protocol::OnAudioChannelClosed(std::function<void(uint32_t generation)> callback) {
    on_audio_channel_closed_ = callback;
}

void Protocol::OnNetworkError(std::function<void(const std::string& message)> callback) {
    on_network_error_ = callback;
}

void Protocol::OnConnected(std::function<void()> callback) { on_connected_ = callback; }

void Protocol::OnDisconnected(std::function<void()> callback) { on_disconnected_ = callback; }

void Protocol::SetError(const std::string& message) {
    error_occurred_ = true;
    if (on_network_error_ != nullptr) {
        on_network_error_(message);
    }
}

void Protocol::SetError(const std::string& message, const std::string& detail) {
    if (detail.empty()) {
        SetError(message);
        return;
    }
    SetError(message + "\n" + detail);
}

void Protocol::SendAbortSpeaking(AbortReason reason) {
    std::string message = "{\"session_id\":\"" + session_id_ + "\",\"type\":\"abort\"";
    if (reason == kAbortReasonWakeWordDetected) {
        message += ",\"reason\":\"wake_word_detected\"";
    }
    message += "}";
    SendText(message);
}

bool Protocol::IsTextRequestValid(const std::string& text) {
    if (text.empty() || text.size() > kMaxTextInteractionBytes ||
        text.find('\0') != std::string::npos) {
        return false;
    }
    return text.find_first_not_of(" \t\r\n\f\v") != std::string::npos;
}

bool Protocol::SendTextRequest(const std::string& text) {
    const auto policy = conversation_policy_source_ ? conversation_policy_source_->load() : 1U;
    if (!IsTextRequestValid(text) || IsChannelClosing() || IsChannelOpening() || !(policy & 1U) ||
        !IsAudioChannelOpened()) {
        return false;
    }
    // This bounded allocation is on the interaction path, never per audio frame.
    auto* root = cJSON_CreateObject();
    if (root == nullptr) {
        return false;
    }
    const bool complete = cJSON_AddStringToObject(root, "session_id", session_id_.c_str()) &&
                          cJSON_AddStringToObject(root, "type", "listen") &&
                          cJSON_AddStringToObject(root, "state", "detect") &&
                          cJSON_AddStringToObject(root, "text", text.c_str());
    auto* json = complete ? cJSON_PrintUnformatted(root) : nullptr;
    cJSON_Delete(root);
    if (json == nullptr) {
        return false;
    }
    const std::string message(json);
    cJSON_free(json);
    // Encoding may have overlapped a complete foreground disable/enable cycle.
    return !IsChannelClosing() &&
           (!conversation_policy_source_ || conversation_policy_source_->load() == policy) &&
           SendText(message);
}

void Protocol::SendWakeWordDetected(const std::string& wake_word) { SendTextRequest(wake_word); }

void Protocol::SendStartListening(ListeningMode mode) {
    if (IsChannelClosing() || (conversation_policy_source_ &&
                              !(conversation_policy_source_->load() & 1U))) {
        return;
    }
    std::string message = "{\"session_id\":\"" + session_id_ + "\"";
    message += ",\"type\":\"listen\",\"state\":\"start\"";
    if (mode == kListeningModeRealtime) {
        message += ",\"mode\":\"realtime\"";
    } else if (mode == kListeningModeAutoStop) {
        message += ",\"mode\":\"auto\"";
    } else {
        message += ",\"mode\":\"manual\"";
    }
    message += "}";
    SendText(message);
}

void Protocol::SendStopListening() {
    std::string message =
        "{\"session_id\":\"" + session_id_ + "\",\"type\":\"listen\",\"state\":\"stop\"}";
    SendText(message);
}

void Protocol::SendMcpMessage(const std::string& payload) {
    std::string message =
        "{\"session_id\":\"" + session_id_ + "\",\"type\":\"mcp\",\"payload\":" + payload + "}";
    if (SendText(message)) {
        ESP_LOGI(TAG, "MCP transport send accepted");
    } else {
        ESP_LOGW(TAG, "MCP transport send failed");
    }
}

bool Protocol::IsTimeout() const {
    const int kTimeoutSeconds = 120;
    auto now = std::chrono::steady_clock::now();
    auto duration = std::chrono::duration_cast<std::chrono::seconds>(now - last_incoming_time_);
    bool timeout = duration.count() > kTimeoutSeconds;
    if (timeout) {
        ESP_LOGE(TAG, "Channel timeout %ld seconds", (long)duration.count());
    }
    return timeout;
}
