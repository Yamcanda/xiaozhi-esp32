"""Compile the complete board-local player with deterministic worker/I/O seams.

The production packet type and Ogg demuxer are used unchanged. No source
extraction, SDK build, downloads, generated output, or hardware are needed.
"""
import math
import os
import pathlib
import shutil
import struct
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
BOARD = ROOT / "main/boards/m5stack/stopwatch"
MINGW = pathlib.Path("D:/sdk/mingw64/bin/g++.exe")
COMPILER = str(MINGW) if MINGW.is_file() else shutil.which("g++") or shutil.which("c++")
FFMPEG_PATH = pathlib.Path("D:/bin/ffmpeg/bin/ffmpeg.exe")
FFMPEG = str(FFMPEG_PATH) if FFMPEG_PATH.is_file() else shutil.which("ffmpeg")
ENV = os.environ.copy()
if COMPILER:
    ENV["PATH"] = str(pathlib.Path(COMPILER).parent) + os.pathsep + ENV.get("PATH", "")

PLATFORM = r'''
#pragma once
#include <algorithm>
#include <atomic>
#include <cassert>
#include <chrono>
#include <condition_variable>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <functional>
#include <mutex>
#include <thread>
#include <vector>
#undef assert
#define assert(c) do { if (!(c)) { std::fprintf(stderr,"assertion failed: %s at %s:%d\n",#c,__FILE__,__LINE__); std::abort(); } } while (0)
namespace player_test {
inline std::mutex mutex;
inline std::condition_variable cv;
inline std::thread worker;
inline std::thread::id worker_id;
inline uint64_t now = 1000;
inline unsigned parks = 0, permits = 0, creates = 0, main_delays = 0;
inline bool waiting = false, allow_start = false, teardown = false, exited = false;
inline bool fail_create = false;
struct MotorEvent { uint64_t at; bool on; };
inline std::vector<MotorEvent> motor;
inline std::function<void(bool)> motor_hook;
inline void CheckWorker() { assert(std::this_thread::get_id() == worker_id); }
inline uint64_t Now() { std::lock_guard<std::mutex> lock(mutex); return now; }
inline void SetMotor(bool on) {
    CheckWorker();
    motor.push_back({Now(), on});
    if (motor_hook) motor_hook(on);
}
inline void Parked() {
    std::unique_lock<std::mutex> lock(mutex);
    allow_start = true; cv.notify_all();
    assert(cv.wait_for(lock, std::chrono::seconds(3), [] { return waiting || exited; }));
    assert(waiting && !exited);
}
inline void Step(uint64_t milliseconds = 20) {
    std::unique_lock<std::mutex> lock(mutex);
    assert(waiting && !teardown && !exited);
    const auto before = parks;
    now += milliseconds; ++permits; cv.notify_all();
    assert(cv.wait_for(lock, std::chrono::seconds(3), [before] { return parks > before || exited; }));
    assert(waiting && !exited && parks == before + 1);
}
inline void Join() {
    if (worker.joinable()) worker.join();
    assert(exited);
}
}
'''

STUBS = {
    "sdkconfig.h": "#pragma once\n#ifndef CONFIG_STOPWATCH_LOCAL_APPS\n#define CONFIG_STOPWATCH_LOCAL_APPS 1\n#endif\n",
    "cJSON.h": "#pragma once\nstruct cJSON;\n",
    "esp_log.h": "#pragma once\n#define ESP_LOGE(...) ((void)0)\n#define ESP_LOGW(...) ((void)0)\n#define ESP_LOGI(...) ((void)0)\n#define ESP_LOGD(...) ((void)0)\n",
    "test_platform.h": PLATFORM,
    "esp_timer.h": '#pragma once\n#include "test_platform.h"\ninline int64_t esp_timer_get_time(){return static_cast<int64_t>(player_test::Now()*1000);}\n',
    "freertos/FreeRTOS.h": r'''
#pragma once
#include <cstdint>
using TickType_t = uint32_t;
using TaskHandle_t = void*;
constexpr int pdPASS = 1;
#define pdMS_TO_TICKS(x) (x)
''',
    "freertos/task.h": r'''
#pragma once
#include "FreeRTOS.h"
#include "test_platform.h"
inline int xTaskCreate(void(*entry)(void*),const char*,uint32_t stack,void* arg,int priority,TaskHandle_t*) {
    using namespace player_test;
    ++creates;
    assert(stack <= 4096 && priority <= 2 && !worker.joinable());
    if (fail_create) return 0;
    worker = std::thread([=] {
        {
            std::unique_lock<std::mutex> lock(mutex);
            worker_id = std::this_thread::get_id();
            cv.wait(lock, [] { return allow_start || teardown; });
        }
        entry(arg);
    });
    return pdPASS;
}
inline void vTaskDelay(TickType_t ticks) {
    using namespace player_test;
    assert(ticks > 0 && ticks <= 20);
    std::unique_lock<std::mutex> lock(mutex);
    if (std::this_thread::get_id() == worker_id) {
        ++parks; waiting = true; cv.notify_all();
        cv.wait(lock, [] { return permits || teardown; });
        waiting = false;
        if (permits) --permits;
        else now += ticks;
    } else {
        ++main_delays; teardown = true; cv.notify_all();
        lock.unlock();
        std::this_thread::sleep_for(std::chrono::milliseconds(1));
    }
}
inline void vTaskDelete(void* task) {
    using namespace player_test;
    CheckWorker(); assert(!task);
    std::lock_guard<std::mutex> lock(mutex);
    exited = true; waiting = false; cv.notify_all();
}
''',
    # Only the two permitted audio APIs exist in the seam. Calling PlaySound,
    # ResetDecoder, or a PCM/codec API fails compilation, not just a string test.
    "audio_service.h": r'''
#pragma once
#include "test_platform.h"
#include "protocol.h"
class AudioService {
public:
    struct Submission { uint64_t at; AudioStreamPacket packet; bool accepted; };
    std::vector<Submission> submissions;
    uint64_t busy_until = 0;
    bool stalled = false;
    int drops = 0, idle_checks = 0;
    std::function<void()> after_push;
    bool IsPlaybackIdle() {
        player_test::CheckWorker(); ++idle_checks;
        return !stalled && player_test::Now() >= busy_until;
    }
    bool PushPacketToDecodeQueue(std::unique_ptr<AudioStreamPacket> packet, bool wait) {
        player_test::CheckWorker(); assert(!wait && packet);
        assert(packet->sample_rate == 24000 && packet->frame_duration == 60);
        assert(packet->timestamp == 0 && packet->playback_id == 0);
        assert(packet->channel_generation == 0 && packet->conversation_policy == 1);
        assert(packet->payload.size() == 90);
        const auto at = player_test::Now();
        const bool accepted = drops == 0;
        if (drops) --drops;
        if (accepted) busy_until = std::max(busy_until, at) + packet->frame_duration;
        submissions.push_back({at, std::move(*packet), accepted});
        if (after_push) after_push();
        return accepted;
    }
};
''',
}

HARNESS = r'''
#include "stopwatch_alarm_player.h"
#include "audio_service.h"
#include "ogg_demuxer.h"
#include <cstring>
#include <iostream>
#include <string>
using namespace player_test;

void Initial(AudioService& audio, StopwatchAlarmPlayer& player) {
    assert(player.IsStopped());
    assert(player.Start()); assert(player.Start());
    assert(creates == 1 && motor.empty() && audio.submissions.empty());
    assert(!player.IsStopped()); // Worker has not run its initial off/ack yet.
    Parked();
    assert(player.IsStopped() && motor.size() == 1 && !motor.back().on);
    assert(audio.submissions.empty() && main_delays == 0);
}
void TickTo(uint64_t at) {
    while (Now() < at) Step(std::min<uint64_t>(20, at - Now()));
}
std::vector<uint64_t> OnTimes() {
    std::vector<uint64_t> times;
    for (const auto& event : motor) if (event.on) times.push_back(event.at);
    return times;
}
void CheckSilent(AudioService& audio, StopwatchAlarmPlayer& player) {
    assert(player.IsStopped());
    const auto writes = motor.size(), packets = audio.submissions.size();
    const auto checks = audio.idle_checks;
    for (int i = 0; i < 300; ++i) Step();
    player.SetRinging(false); player.SetRinging(false);
    assert(player.IsStopped() && motor.size() == writes);
    assert(audio.submissions.size() == packets && audio.idle_checks == checks);
}
void Timing() {
    AudioService audio;
    {
        StopwatchAlarmPlayer player(audio, SetMotor);
        Initial(audio, player);
        const auto writes = motor.size();
        player.SetRinging(true); player.SetRinging(true);
        assert(!player.IsStopped() && motor.size() == writes && audio.submissions.empty());
        Step();
        const auto first = Now();
        assert(OnTimes().size() == 1 && audio.submissions.size() == 2);
        const std::vector<uint64_t> offsets{0,260,520,780,1580,1840,2100,2360};
        TickTo(first + offsets.back() + 100);
        const auto ons = OnTimes();
        assert(ons.size() == offsets.size() && audio.submissions.size() == 2 * ons.size());
        for (size_t i = 0; i < ons.size(); ++i) {
            assert(ons[i] == first + offsets[i]);
            assert(audio.submissions[2*i].at == ons[i]);
            assert(audio.submissions[2*i+1].at == ons[i]);
            const uint64_t expected_gap = (i % 4 == 3) ? 720 : 180;
            if (i+1 < ons.size()) {
                const auto gap = ons[i+1] - ons[i] - 70;
                assert(gap >= expected_gap && gap <= expected_gap + 20);
            }
            bool off = false;
            for (const auto& e : motor) if (!e.on && e.at >= ons[i]+70 && e.at <= ons[i]+90) off = true;
            assert(off);
        }
        player.SetRinging(false); assert(!player.IsStopped());
        Step(); CheckSilent(audio, player);
    }
    Join();
}
void StopActive(bool after_first_packet) {
    AudioService audio;
    {
        StopwatchAlarmPlayer player(audio, SetMotor);
        Initial(audio, player);
        if (after_first_packet) audio.after_push = [&] {
            player.SetRinging(false);
            assert(!player.IsStopped());
        };
        player.SetRinging(true); Step();
        assert(motor.back().on && !player.IsStopped());
        assert(audio.submissions.size() == (after_first_packet ? 1 : 2));
        player.SetRinging(false); player.SetRinging(false);
        assert(!player.IsStopped() && motor.back().on);
        Step(); assert(!motor.back().on);
        CheckSilent(audio, player);
        // Re-arm is explicit and restarts at the first pulse.
        audio.after_push = {};
        player.SetRinging(true); Step();
        assert(!player.IsStopped() && motor.back().on);
        const auto count = audio.submissions.size();
        assert(count == (after_first_packet ? 3 : 4));
        player.SetRinging(false); Step(); CheckSilent(audio, player);
    }
    Join();
}
void Generations() {
    AudioService audio;
    {
        StopwatchAlarmPlayer player(audio, SetMotor);
        Initial(audio, player);
        for (int i = 0; i < 10; ++i) {
            player.SetRinging(true); player.SetRinging(false);
            assert(!player.IsStopped()); // An old stop ack cannot pass an ABA.
            Step(); assert(player.IsStopped());
            assert(audio.submissions.empty() && motor.size() == 1);
        }
        player.SetRinging(true); Step();
        const auto count = audio.submissions.size();
        player.SetRinging(false); player.SetRinging(true); player.SetRinging(false);
        assert(!player.IsStopped()); Step(); CheckSilent(audio, player);
        assert(audio.submissions.size() == count);
    }
    Join();
}
void DropBusyLate() {
    AudioService audio;
    {
        StopwatchAlarmPlayer player(audio, SetMotor);
        Initial(audio, player);
        audio.drops = 1;
        player.SetRinging(true); Step();
        const auto first = Now();
        assert(audio.submissions.size() == 1 && !audio.submissions.back().accepted);
        TickTo(first + 260);
        assert(audio.submissions.size() == 3); // No retry within the dropped pulse.
        audio.stalled = true;
        TickTo(first + 2000);
        assert(audio.submissions.size() == 3 && OnTimes().size() > 4);
        audio.stalled = false;
        const auto count = audio.submissions.size();
        const auto pulses = OnTimes().size();
        Step(10000); // A delayed worker does not catch up with a burst.
        assert(audio.submissions.size() == count + 2 && OnTimes().size() == pulses + 1);
        player.SetRinging(false); Step(); CheckSilent(audio, player);
    }
    Join();
}
void StartupFailure() {
    AudioService audio;
    {
        StopwatchAlarmPlayer player(audio, SetMotor);
        player.SetRinging(true); assert(!player.IsStopped());
        fail_create = true;
        assert(!player.Start() && creates == 1 && motor.empty() && audio.submissions.empty());
        player.SetRinging(false); assert(player.IsStopped());
        fail_create = false;
        assert(player.Start() && creates == 2 && !player.IsStopped());
        Parked(); CheckSilent(audio, player);
    }
    Join();
}
void OptionalMotor() {
    AudioService audio;
    {
        StopwatchAlarmPlayer player(audio, {});
        assert(player.Start());
        player.SetRinging(true); Parked();
        assert(audio.submissions.size() == 2 && motor.empty());
        player.SetRinging(false); Step(); CheckSilent(audio, player);
    }
    Join();
}
void Teardown() {
    AudioService audio;
    {
        StopwatchAlarmPlayer player(audio, SetMotor);
        Initial(audio, player);
        player.SetRinging(true); Step();
        assert(motor.back().on);
    }
    Join(); assert(!motor.back().on && audio.submissions.size() == 2);
}
void MotorCancellation() {
    AudioService audio;
    {
        StopwatchAlarmPlayer player(audio, SetMotor);
        Initial(audio, player);
        motor_hook = [&](bool on) {
            if (on) { player.SetRinging(false); assert(!player.IsStopped()); }
        };
        player.SetRinging(true); Step();
        assert(audio.submissions.empty() && motor.back().on && !player.IsStopped());
        Step(); CheckSilent(audio, player);
        motor_hook = {};
    }
    Join();
}
void PacketFixture(bool dump) {
    AudioService audio;
    {
        StopwatchAlarmPlayer player(audio, SetMotor);
        Initial(audio, player);
        player.SetRinging(true); Step();
        player.SetRinging(false); Step();
        assert(player.IsStopped() && audio.submissions.size() == 2);
        // Exercise the real demuxer API/header against a minimal Ogg wrapper.
        std::vector<uint8_t> ogg;
        const auto page = [&](const std::vector<uint8_t>& data) {
            const auto at = ogg.size();
            ogg.resize(at + 28, 0);
            std::memcpy(ogg.data()+at,"OggS",4);
            ogg[at+26] = 1; ogg[at+27] = static_cast<uint8_t>(data.size());
            ogg.insert(ogg.end(),data.begin(),data.end());
        };
        page({'O','p','u','s','H','e','a','d',1,1,0,0,0xc0,0x5d,0,0,0,0,0});
        page({'O','p','u','s','T','a','g','s',0,0,0,0,0,0,0,0});
        for (const auto& s : audio.submissions) page(s.packet.payload);
        unsigned packets = 0;
        OggDemuxer demuxer;
        demuxer.OnPacket([&](const uint8_t* data,int rate,int duration,size_t size) {
            assert(rate == 24000 && duration == 60 && size == 90);
            assert(packets < 2);
            assert(std::memcmp(data,audio.submissions[packets++].packet.payload.data(),size) == 0);
        });
        assert(demuxer.Process(ogg.data(),ogg.size()) == ogg.size());
        assert(demuxer.Finish() && !demuxer.HasError() && packets == 2);
        if (dump) {
            for (const auto& s : audio.submissions) {
                const auto& p = s.packet.payload;
                std::cout.put(static_cast<char>(p.size())); std::cout.put(0);
                std::cout.write(reinterpret_cast<const char*>(p.data()),p.size());
            }
        }
    }
    Join();
}
int main(int argc,char** argv) {
#ifdef _WIN32
    if (argc == 2 && std::string(argv[1]) == "dump") {
        _setmode(_fileno(stdout), _O_BINARY);
    }
#endif
    assert(argc == 2);
    const std::string scenario = argv[1];
    if (scenario == "timing") Timing();
    else if (scenario == "stop_active") StopActive(false);
    else if (scenario == "stop_inflight") StopActive(true);
    else if (scenario == "generations") Generations();
    else if (scenario == "drops_busy_late") DropBusyLate();
    else if (scenario == "startup_failure") StartupFailure();
    else if (scenario == "optional_motor") OptionalMotor();
    else if (scenario == "teardown") Teardown();
    else if (scenario == "motor_cancel") MotorCancellation();
    else if (scenario == "packets" || scenario == "dump") PacketFixture(scenario == "dump");
    else assert(false);
}
'''

# MinGW stdout is text mode unless explicitly changed for the packet dump.
HARNESS = '#ifdef _WIN32\n#include <fcntl.h>\n#include <io.h>\n#endif\n' + HARNESS


def ogg_page(packet, sequence, header_type=0, granule=0):
    """A checked Ogg container for independently decoding production payloads."""
    laces = bytes([255] * (len(packet) // 255) + [len(packet) % 255])
    header = b"OggS\x00" + bytes([header_type])
    header += struct.pack("<QII", granule, 1, sequence) + b"\x00" * 4
    page = bytearray(header + bytes([len(laces)]) + laces + packet)
    crc = 0
    for byte in page:
        crc ^= byte << 24
        for _ in range(8):
            crc = ((crc << 1) ^ (0x04C11DB7 if crc & 0x80000000 else 0)) & 0xFFFFFFFF
    struct.pack_into("<I", page, 22, crc)
    return bytes(page)


@unittest.skipUnless(COMPILER, "requires a native C++ compiler")
class StopwatchAlarmPlayerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="stopwatch-alarm-player-")
        cls.addClassCleanup(cls.tmp.cleanup)
        cls.directory = pathlib.Path(cls.tmp.name)
        for name, contents in STUBS.items():
            path = cls.directory / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(contents, encoding="utf-8")
        harness = cls.directory / "harness.cc"
        harness.write_text(HARNESS, encoding="utf-8")
        cls.binary = cls.directory / ("player.exe" if os.name == "nt" else "player")
        subprocess.run(
            [COMPILER, "-std=c++23", "-O1", "-Wall", "-Wextra", "-Werror",
             "-Wno-unused-variable", "-fno-exceptions", "-fno-rtti", "-pthread",
             "-I", str(cls.directory), "-I", str(BOARD),
             "-I", str(ROOT / "main/protocols"), "-I", str(ROOT / "main/audio/demuxer"),
             str(BOARD / "stopwatch_alarm_player.cc"),
             str(ROOT / "main/audio/demuxer/ogg_demuxer.cc"), str(harness),
             "-o", str(cls.binary)],
            check=True, env=ENV, timeout=60,
        )

    def scenario(self, name):
        return subprocess.run([str(self.binary), name], check=True, env=ENV,
                              capture_output=True, timeout=10)

    def test_four_pulses_motor_width_gaps_and_repeat(self):
        self.scenario("timing")

    def test_stop_is_worker_ack_and_idempotent_with_no_post_ack_io(self):
        self.scenario("stop_active")

    def test_stop_during_submission_skips_second_packet_and_waits_for_ack(self):
        self.scenario("stop_inflight")

    def test_stop_restart_stop_cannot_reuse_a_stale_ack(self):
        self.scenario("generations")

    def test_drops_busy_playback_and_late_ticks_never_build_backlog(self):
        self.scenario("drops_busy_late")

    def test_nonblocking_idempotent_start_and_failed_start_retry(self):
        self.scenario("startup_failure")

    def test_optional_motor(self):
        self.scenario("optional_motor")

    def test_lifetime_teardown_turns_motor_off_and_joins(self):
        self.scenario("teardown")

    def test_request_from_motor_callback_never_submits_audio(self):
        self.scenario("motor_cancel")

    def test_static_packets_use_real_demuxer_and_protocol_type(self):
        self.scenario("packets")

    def test_guard_off_needs_no_audio_or_freertos_headers(self):
        directory = self.directory / "disabled"
        directory.mkdir(exist_ok=True)
        (directory / "sdkconfig.h").write_text("#define CONFIG_STOPWATCH_LOCAL_APPS 0\n",
                                               encoding="utf-8")
        harness = directory / "disabled.cc"
        harness.write_text('#include "stopwatch_alarm_player.h"\nint main() {}\n',
                           encoding="utf-8")
        subprocess.run(
            [COMPILER, "-std=c++23", "-Wall", "-Wextra", "-Werror", "-I", str(directory),
             "-I", str(BOARD), str(BOARD / "stopwatch_alarm_player.cc"), str(harness),
             "-o", str(directory / "disabled.exe")],
            check=True, env=ENV, timeout=30,
        )

    @unittest.skipUnless(FFMPEG, "optional waveform check requires ffmpeg")
    def test_embedded_opus_decodes_to_short_1760hz_beeps_without_reset(self):
        dump = self.scenario("dump").stdout
        packets = []
        while dump:
            size = struct.unpack_from("<H", dump)[0]
            packets.append(dump[2:2 + size])
            dump = dump[2 + size:]
        self.assertEqual([len(p) for p in packets], [90, 90])
        head = b"OpusHead" + struct.pack("<BBHIhB", 1, 1, 0, 24000, 0, 0)
        tags = b"OpusTags" + struct.pack("<II", 0, 0)
        ogg = ogg_page(head, 0, 2) + ogg_page(tags, 1)
        # Decode four copies in one stream: no resets between pulses.
        for index, packet in enumerate(packets * 4):
            ogg += ogg_page(packet, index + 2, 4 if index == 7 else 0, (index + 1) * 2880)
        decoded = subprocess.run(
            [FFMPEG, "-hide_banner", "-loglevel", "error", "-f", "ogg", "-i", "pipe:0",
             "-ar", "24000", "-ac", "1", "-f", "s16le", "pipe:1"],
            input=ogg, capture_output=True, check=True, timeout=15,
        ).stdout
        samples = struct.unpack("<" + "h" * (len(decoded) // 2), decoded)
        self.assertEqual(len(samples), 4 * 2880)  # 4 * 120 ms at 24 kHz.
        for index in range(4):
            pulse = samples[index * 2880:(index + 1) * 2880]
            tone = pulse[240:1440]  # Ignore onset and encoded trailing silence.
            rms = math.sqrt(sum(x * x for x in tone) / len(tone))
            tail = pulse[2400:2880]
            tail_rms = math.sqrt(sum(x * x for x in tail) / len(tail))
            self.assertGreater(rms, 500)
            self.assertLess(tail_rms, rms * 0.1)
            crossings = sum(a <= 0 < b for a, b in zip(tone, tone[1:]))
            self.assertAlmostEqual(crossings * 24000 / len(tone), 1760, delta=40)
            # Audible region is approximately 70 ms, never a long alarm clip.
            audible_blocks = sum(
                math.sqrt(sum(x * x for x in pulse[start:start + 240]) / 240) > rms * 0.15
                for start in range(0, len(pulse), 240)
            )
            self.assertGreaterEqual(audible_blocks, 6)
            self.assertLessEqual(audible_blocks, 9)


if __name__ == "__main__":
    unittest.main()
