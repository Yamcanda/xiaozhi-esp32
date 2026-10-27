#include "stopwatch_alarm_player.h"

#if CONFIG_STOPWATCH_LOCAL_APPS

#include <esp_timer.h>

#include <array>
#include <memory>
#include <utility>

#include "audio_service.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

namespace {
constexpr uint32_t kTickMs = 20;
constexpr uint32_t kMotorMs = 70;
constexpr uint32_t kShortGapMs = 180;
constexpr uint32_t kLongGapMs = 720;
constexpr int kSampleRate = 24000;
constexpr int kFrameMs = 60;

// Two mono CELT-only Opus packets: 70 ms of 1760 Hz, then silence, 120 ms
// decoded total (including 2.5 ms encoder delay). No decoder reset or Ogg
// parsing is needed at runtime. Generated with FFmpeg/libopus:
// sine=frequency=1760:sample_rate=24000:duration=0.070
// -af afade=t=in:st=0:d=0.002,afade=t=out:st=0.068:d=0.002,volume=2,
//     apad=whole_dur=0.1175
// -ac 1 -ar 24000 -c:a libopus -application lowdelay -b:a 12000
// -vbr off -frame_duration 60 -f opus
// The two audio packets (not OpusHead/OpusTags/Ogg pages) are embedded below.
constexpr std::array<std::array<uint8_t, 90>, 2> kPulsePackets = {{
    {0xbb, 0x43, 0x03, 0x7f, 0x6b, 0x56, 0x32, 0x44, 0xd5, 0x47, 0xc4, 0xb4, 0xd0, 0xa6, 0x9a,
     0xdf, 0x9c, 0x10, 0xee, 0x1c, 0xe6, 0x35, 0x63, 0x99, 0x2a, 0x4e, 0x49, 0x6c, 0x2e, 0x53,
     0xf6, 0x9d, 0x55, 0x4c, 0x6d, 0xe6, 0xc5, 0x99, 0x74, 0xd7, 0x78, 0x04, 0x5d, 0xe7, 0xf2,
     0x42, 0x0f, 0xf8, 0xb0, 0x38, 0xa1, 0x34, 0xc1, 0x1f, 0x2f, 0x2e, 0x88, 0x03, 0xd7, 0x9d,
     0x3a, 0x6e, 0xb3, 0x25, 0x2f, 0x3b, 0x7e, 0x49, 0x14, 0x68, 0xcf, 0xde, 0x27, 0x1e, 0x38,
     0xf1, 0x47, 0x45, 0x5b, 0xb1, 0xea, 0x7c, 0x57, 0xf7, 0x0b, 0xb5, 0xd7, 0x00, 0x00, 0x00},
    {0xbb, 0x43, 0x03, 0x70, 0xa5, 0xfb, 0xfe, 0xd9, 0xeb, 0x9b, 0xc8, 0x2f, 0x5a, 0x34, 0x7c,
     0x9b, 0xdc, 0x68, 0xce, 0xf0, 0x50, 0x93, 0xdc, 0xf5, 0x32, 0x4a, 0xb2, 0x2d, 0x0b, 0x1c,
     0x59, 0x60, 0x68, 0x51, 0x13, 0x5b, 0x8f, 0x01, 0xd6, 0xdc, 0x61, 0x5a, 0xfd, 0x52, 0x6f,
     0x6d, 0x42, 0x63, 0x01, 0x97, 0x57, 0xa1, 0x54, 0x91, 0xde, 0xf7, 0x48, 0x00, 0x00, 0x5e,
     0xe4, 0xe5, 0xfa, 0xa6, 0x65, 0xb0, 0xf2, 0x92, 0xf7, 0xf9, 0x5b, 0xb3, 0x75, 0xcf, 0xc4,
     0xd7, 0xc7, 0x2e, 0xaf, 0x42, 0xa9, 0x23, 0xbd, 0xee, 0x90, 0x00, 0x05, 0x00, 0x00, 0x00},
}};
static_assert(kPulsePackets.size() * kFrameMs <= 120);

TickType_t TickDelay() {
    const TickType_t ticks = pdMS_TO_TICKS(kTickMs);
    return ticks > 0 ? ticks : 1;
}

uint64_t NowMs() { return static_cast<uint64_t>(esp_timer_get_time()) / 1000; }
}  // namespace

StopwatchAlarmPlayer::StopwatchAlarmPlayer(AudioService& audio, std::function<void(bool)> motor)
    : audio_(audio), motor_(std::move(motor)) {}

StopwatchAlarmPlayer::~StopwatchAlarmPlayer() {
    if (!started_.load(std::memory_order_acquire)) {
        return;
    }
    SetRinging(false);
    shutdown_.store(true, std::memory_order_release);
    // Board owns us for its lifetime. Teardown, unlike request/ack, joins;
    // never forcibly delete a worker while it owns an audio mutex or I2C call.
    while (!finished_.load(std::memory_order_acquire)) {
        vTaskDelay(TickDelay());
    }
}

bool StopwatchAlarmPlayer::Start() {
    if (started_.load(std::memory_order_acquire)) {
        return true;
    }
    stopped_request_.store(1, std::memory_order_release);
    if (xTaskCreate(WorkerEntry, "sw_alarm", 4096, this, 2, nullptr) != pdPASS) {
        return false;
    }
    started_.store(true, std::memory_order_release);
    return true;
}

void StopwatchAlarmPlayer::SetRinging(bool ringing) {
    auto request = request_.load(std::memory_order_acquire);
    while (((request & 1U) != 0) != ringing) {
        const uint32_t next = ((request + 2U) & ~1U) | (ringing ? 1U : 0U);
        if (request_.compare_exchange_weak(request, next, std::memory_order_acq_rel,
                                           std::memory_order_acquire)) {
            return;
        }
    }
}

bool StopwatchAlarmPlayer::IsStopped() const {
    const auto request = request_.load(std::memory_order_acquire);
    return (request & 1U) == 0 && (!started_.load(std::memory_order_acquire) ||
                                   stopped_request_.load(std::memory_order_acquire) == request);
}

bool StopwatchAlarmPlayer::IsCurrent(uint32_t request) const {
    return !shutdown_.load(std::memory_order_acquire) &&
           request_.load(std::memory_order_acquire) == request;
}

void StopwatchAlarmPlayer::SubmitPulse(uint32_t request) {
    // Do not accumulate multiple pulses behind stalled playback or TTS. At
    // most one 120 ms alarm batch can be outstanding, with no retry on drops.
    if (!IsCurrent(request) || !audio_.IsPlaybackIdle()) {
        return;
    }
    for (const auto& payload : kPulsePackets) {
        if (!IsCurrent(request)) {
            break;
        }
        // The service takes ownership. Only two small, fixed-size payload
        // allocations per pulse; never encode/allocate PCM in this worker.
        auto packet = std::make_unique<AudioStreamPacket>();
        packet->sample_rate = kSampleRate;
        packet->frame_duration = kFrameMs;
        packet->payload.assign(payload.begin(), payload.end());
        if (!IsCurrent(request)) {
            break;
        }
        if (!audio_.PushPacketToDecodeQueue(std::move(packet), false)) {
            break;
        }
    }
}

void StopwatchAlarmPlayer::WorkerEntry(void* argument) {
    auto* player = static_cast<StopwatchAlarmPlayer*>(argument);
    player->WorkerLoop();
    // No access to player after publishing this: the owner may now destroy it.
    player->finished_.store(true, std::memory_order_release);
    vTaskDelete(nullptr);
}

void StopwatchAlarmPlayer::WorkerLoop() {
    bool active = false;
    bool motor_on = false;
    uint32_t active_request = 0;
    unsigned pulse = 0;
    uint64_t next_pulse = 0;
    uint64_t motor_off_at = 0;
    if (motor_) {
        motor_(false);
    }
    while (!shutdown_.load(std::memory_order_acquire)) {
        const auto request = request_.load(std::memory_order_acquire);
        const auto now = NowMs();
        if ((request & 1U) == 0) {
            if (motor_on && motor_) {
                motor_(false);
            }
            motor_on = false;
            active = false;
            // Release only after the last submission and motor-off callback
            // have returned. Idle ticks perform no further motor/audio I/O.
            stopped_request_.store(request, std::memory_order_release);
        } else {
            if (!active || active_request != request) {
                if (motor_on && motor_) {
                    motor_(false);
                }
                motor_on = false;
                active = true;
                active_request = request;
                pulse = 0;
                next_pulse = now;
            }
            if (motor_on && now >= motor_off_at) {
                if (motor_) {
                    motor_(false);
                }
                motor_on = false;
            }
            if (now >= next_pulse && IsCurrent(request)) {
                if (motor_) {
                    motor_(true);
                }
                motor_on = true;
                motor_off_at = now + kMotorMs;
                next_pulse = motor_off_at + (pulse < 3 ? kShortGapMs : kLongGapMs);
                pulse = (pulse + 1) % 4;
                SubmitPulse(request);
            }
        }
        // Finite polling also services requests when notifications are lost.
        // No catch-up bursts after a delayed tick and no work on audio tasks.
        vTaskDelay(TickDelay());
    }
    if (motor_on && motor_) {
        motor_(false);
    }
    stopped_request_.store(request_.load(std::memory_order_acquire), std::memory_order_release);
}

#endif  // CONFIG_STOPWATCH_LOCAL_APPS
