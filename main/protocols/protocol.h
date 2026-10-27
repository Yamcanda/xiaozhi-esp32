#ifndef PROTOCOL_H
#define PROTOCOL_H

#include <atomic>
#include <cJSON.h>
#include <chrono>
#include <functional>
#include <memory>
#include <string>
#include <vector>

struct AudioStreamPacket {
    int sample_rate = 0;
    int frame_duration = 0;
    uint32_t timestamp = 0;
    uint32_t playback_id = 0;
    uint32_t media_position_ms = 0;
    uint32_t conversation_policy = 1;  // Local provenance only, never serialized.
    uint32_t channel_generation = 0;
    std::vector<uint8_t> payload;
};

struct BinaryProtocol2 {
    uint16_t version;
    uint16_t type;          // Message type (0: OPUS, 1: JSON)
    uint32_t reserved;      // Reserved for future use
    uint32_t timestamp;     // Timestamp in milliseconds (used for server-side AEC)
    uint32_t payload_size;  // Payload size in bytes
    uint8_t payload[];      // Payload data
} __attribute__((packed));

struct BinaryProtocol3 {
    uint8_t type;
    uint8_t reserved;
    uint16_t payload_size;
    uint8_t payload[];
} __attribute__((packed));

enum AbortReason { kAbortReasonNone, kAbortReasonWakeWordDetected };

enum ListeningMode {
    kListeningModeAutoStop,
    kListeningModeManualStop,
    kListeningModeRealtime  // 需要 AEC 支持
};

class Protocol {
public:
    virtual ~Protocol() = default;

    inline int server_sample_rate() const { return server_sample_rate_; }
    inline int server_frame_duration() const { return server_frame_duration_; }
    inline const std::string& session_id() const { return session_id_; }

    uint32_t channel_generation() const { return channel_generation_.load(); }
    void SetConversationPolicySource(const std::atomic<uint32_t>* source) {
        conversation_policy_source_ = source;
    }
    void SetChannelClosing(bool closing) {
        channel_callback_generation_->store(closing ? 0 : channel_generation_.load());
        channel_closing_.store(closing);
    }
    bool IsChannelClosing() const { return channel_closing_.load(); }
    // Explicit lifecycle gate for scheduled control reconnect work. Handshake
    // traffic on the opening worker/receive task must remain available.
    void SetChannelOpening(bool opening) { channel_opening_.store(opening); }
    bool IsChannelOpening() const { return channel_opening_.load(); }
    void RequireConversationSession() { require_conversation_session_.store(true); }
    bool UsesConversationPolicy() const {
        return require_conversation_session_.load() ||
               (conversation_policy_source_ != nullptr && conversation_policy_source_->load() != 1);
    }
    void OnIncomingAudio(std::function<void(std::unique_ptr<AudioStreamPacket> packet)> callback);
    void OnIncomingJson(std::function<void(const cJSON* root, uint32_t generation)> callback);
    void OnAudioChannelOpened(std::function<void(uint32_t generation)> callback);
    void OnAudioChannelClosed(std::function<void(uint32_t generation)> callback);
    void OnNetworkError(std::function<void(const std::string& message)> callback);
    void OnConnected(std::function<void()> callback);
    void OnDisconnected(std::function<void()> callback);

    virtual bool Start() = 0;
    virtual bool OpenAudioChannel() = 0;
    virtual void CloseAudioChannel(bool send_goodbye = true) = 0;
    virtual bool IsAudioChannelOpened() const = 0;
    virtual bool SendAudio(std::unique_ptr<AudioStreamPacket> packet) = 0;
    // Maximum UTF-8 payload bytes before JSON escaping; shared by all transports.
    static constexpr size_t kMaxTextInteractionBytes = 12 * 1024;
    static bool IsTextRequestValid(const std::string& text);
    // Uses the existing listen/detect wire contract. True means transport send
    // accepted, not that the server will run an LLM or produce speech.
    virtual bool SendTextRequest(const std::string& text);
    virtual void SendWakeWordDetected(const std::string& wake_word);
    virtual void SendStartListening(ListeningMode mode);
    virtual void SendStopListening();
    virtual void SendAbortSpeaking(AbortReason reason);
    virtual void SendMcpMessage(const std::string& message);

protected:
    std::function<void(const cJSON* root, uint32_t generation)> on_incoming_json_;
    std::function<void(std::unique_ptr<AudioStreamPacket> packet)> on_incoming_audio_;
    std::function<void(uint32_t generation)> on_audio_channel_opened_;
    std::function<void(uint32_t generation)> on_audio_channel_closed_;
    inline static std::atomic<uint32_t> next_channel_generation_{0};
    std::atomic<uint32_t> channel_generation_{0};
    // Queued transport work may outlive Protocol, but must not acquire socket
    // ownership after main begins closing or after a different channel opens.
    std::shared_ptr<std::atomic<uint32_t>> channel_callback_generation_ =
        std::make_shared<std::atomic<uint32_t>>(0);
    uint32_t BeginChannel() {
        const auto generation = next_channel_generation_.fetch_add(1) + 1;
        channel_generation_.store(generation);
        channel_callback_generation_->store(generation);
        return generation;
    }
    std::atomic<bool> channel_closing_{false};
    std::atomic<bool> channel_opening_{false};
    std::atomic<bool> require_conversation_session_{false};
    const std::atomic<uint32_t>* conversation_policy_source_ = nullptr;
    bool IsAudioPacketCurrent(const AudioStreamPacket& packet) const {
        return !channel_closing_.load() && (conversation_policy_source_ == nullptr ||
               ((packet.conversation_policy & 1U) != 0 &&
                packet.conversation_policy == conversation_policy_source_->load()));
    }
    std::function<void(const std::string& message)> on_network_error_;
    std::function<void()> on_connected_;
    std::function<void()> on_disconnected_;

    int server_sample_rate_ = 24000;
    int server_frame_duration_ = 60;
    bool error_occurred_ = false;
    std::string session_id_;
    std::chrono::time_point<std::chrono::steady_clock> last_incoming_time_;

    virtual bool SendText(const std::string& text) = 0;
    virtual void SetError(const std::string& message);
    void SetError(const std::string& message, const std::string& detail);
    virtual bool IsTimeout() const;
    static void AddTextFontCapabilities(cJSON* root);
};

#endif  // PROTOCOL_H
