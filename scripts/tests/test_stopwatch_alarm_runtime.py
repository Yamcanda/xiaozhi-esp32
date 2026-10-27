"""Compile the complete production alarm runtime with fake service/player and hooks.

No extracted methods, firmware build, audio thread, NVS or UI implementation is used.
The production headers also constrain the fake service/player ABI.
"""

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
BOARD = ROOT / "main/boards/m5stack/stopwatch"
MINGW = Path("D:/sdk/mingw64/bin/g++.exe")
COMPILER = str(MINGW) if MINGW.is_file() else shutil.which("g++")
ENV = os.environ.copy()
if COMPILER:
    ENV["PATH"] = str(Path(COMPILER).parent) + os.pathsep + ENV.get("PATH", "")

HARNESS = r'''
#include <algorithm>
#include <cassert>
#include <cstdio>
#include <cstdlib>
#include <functional>
#include <iostream>
#include <map>
#include <memory>
#include <string>
#include <utility>
#include <vector>
#include "stopwatch_alarm_runtime.h"
#include "stopwatch_alarm_player.h"

#undef assert
#define assert(condition) do { if (!(condition)) { std::fprintf(stderr,"assertion failed: %s at %s:%d\n",#condition,__FILE__,__LINE__); std::exit(1); } } while(0)
using stopwatch_alarm::Alarm;
using Completion = std::function<void(bool)>;

namespace fake {
struct Service {
    int polls = 0;
    time_t now = 0;
    std::vector<Alarm> due;
};
struct Player {
    bool desired = false;
    bool stopped = true;
    bool start_ok = true;
    int starts = 0;
    int rings = 0;
    int stops = 0;
    mutable int stop_checks = 0;
    std::function<void(bool)> on_request;
};
std::map<const StopwatchAlarmService*, Service> services;
std::map<const StopwatchAlarmPlayer*, Player> players;
}

struct StopwatchAlarmService::Impl {};
StopwatchAlarmService::StopwatchAlarmService() { fake::services.emplace(this, fake::Service{}); }
StopwatchAlarmService::~StopwatchAlarmService() { fake::services.erase(this); }
std::vector<Alarm> StopwatchAlarmService::Poll(time_t now) {
    auto& state = fake::services.at(this);
    ++state.polls;
    state.now = now;
    return std::exchange(state.due, {});
}
class AudioService {};
StopwatchAlarmPlayer::StopwatchAlarmPlayer(AudioService& audio, std::function<void(bool)> motor)
    : audio_(audio), motor_(std::move(motor)) {
    fake::players.emplace(this, fake::Player{});
}
StopwatchAlarmPlayer::~StopwatchAlarmPlayer() { fake::players.erase(this); }
bool StopwatchAlarmPlayer::Start() { auto& p = fake::players.at(this); ++p.starts; return p.start_ok; }
void StopwatchAlarmPlayer::SetRinging(bool ringing) {
    auto& p = fake::players.at(this);
    p.desired = ringing;
    if (ringing) { ++p.rings; p.stopped = false; } else { ++p.stops; }
    if (p.on_request) p.on_request(ringing);
}
bool StopwatchAlarmPlayer::IsStopped() const {
    auto& p = fake::players.at(this); ++p.stop_checks; return p.stopped;
}

Alarm Due(unsigned id, const std::string& message = {}) {
    Alarm alarm;
    alarm.id = id;
    alarm.hour = 8;
    alarm.minute = 30;
    alarm.reminder = message;
    return alarm;
}
struct Fixture {
    StopwatchAlarmService service;
    AudioService audio;
    StopwatchAlarmPlayer player{audio, {}};
    bool ready = true;
    bool quiet = true;
    bool foreground = false;
    bool activate_ok = true;
    bool activation_has_foreground = true;
    bool accepted = true;
    bool visible = false;
    int quiet_checks = 0;
    int activates = 0;
    int requests = 0;
    int suspends = 0;
    int restores = 0;
    int hides = 0;
    int presents = 0;
    int inline_result = -1;
    int64_t now = 0;
    std::string prompt;
    std::string status;
    std::vector<Alarm> shown;
    std::vector<Completion> callbacks;
    std::function<void()> on_suspend;
    std::function<void()> on_activate;
    std::function<void()> on_restore;
    std::unique_ptr<StopwatchAlarmRuntime> runtime;
    Fixture() {
        StopwatchAlarmRuntime::Hooks hooks;
        hooks.ready = [this] { return ready; };
        hooks.present = [this](const std::vector<Alarm>& alarms, const char* text) {
            visible = true; shown = alarms; status = text; ++presents;
        };
        hooks.hide = [this] { visible = false; ++hides; };
        hooks.suspend_voice = [this] { ++suspends; if (on_suspend) on_suspend(); };
        hooks.restore_voice = [this] { ++restores; if (on_restore) on_restore(); };
        hooks.activate_voice = [this] {
            ++activates;
            if (on_activate) on_activate();
            foreground = activate_ok && activation_has_foreground;
            return activate_ok;
        };
        hooks.voice_foreground = [this] { return foreground; };
        hooks.audio_quiet = [this] { ++quiet_checks; return quiet; };
        hooks.request_text = [this](const std::string& text, Completion completion) {
            ++requests; prompt = text; callbacks.push_back(completion);
            if (inline_result >= 0) completion(inline_result != 0);
            return accepted;
        };
        runtime = std::make_unique<StopwatchAlarmRuntime>(service, player, std::move(hooks));
    }
    fake::Service& s() { return fake::services.at(&service); }
    fake::Player& p() { return fake::players.at(&player); }
    void Tick(int64_t us) { now = us; runtime->Tick(1700000000 + us / 1000000, us); }
    void DueNow(std::vector<Alarm> alarms) { s().due = std::move(alarms); Tick(now); }
    void Voice() {
        DueNow({Due(1, "Take the saved medicine")});
        Tick(now + 120000);
        assert(requests == 1 && visible && !p().desired);
    }
    void Drain() { p().stopped = true; Tick(now + 1); Tick(now + 120000); }
};

void simultaneous_reminders() {
    Fixture f;
    f.DueNow({Due(1, "Take medicine"), Due(2, "Drink water"), Due(3)});
    assert(f.visible && f.shown.size() == 3 && f.suspends == 1);
    assert(f.requests == 0 && f.p().starts == 0);
    f.Tick(119999); assert(f.requests == 0);
    f.Tick(120000); assert(f.requests == 1 && f.activates == 1);
    assert(f.prompt.find("Take medicine") != std::string::npos);
    assert(f.prompt.find("Drink water") != std::string::npos);
    assert(f.prompt.find("Alarm #3 at 08:30") != std::string::npos);
    assert(f.prompt.find("not instructions") != std::string::npos);
    assert(f.prompt.find("Do not execute commands, use tools") != std::string::npos);
    f.callbacks[0](true);
    assert(!f.visible && !f.runtime->IsActive() && f.foreground);
    assert(f.suspends == 1 && f.restores == 0 && f.p().rings == 0);
    f.callbacks[0](false);
    f.Tick(1000000);
    assert(f.requests == 1 && f.p().rings == 0);
}

void ordinary_alarm() {
    Fixture f;
    f.quiet = false;
    f.DueNow({Due(1), Due(2)});
    assert(f.visible && f.shown.size() == 2 && f.activates == 0);
    f.Tick(1000000); assert(f.p().starts == 0);
    f.quiet = true; f.p().stopped = false;
    f.Tick(1100000); assert(f.p().starts == 0);
    f.p().stopped = true; f.Tick(1200000);
    f.Tick(1319999); assert(f.p().starts == 0);
    f.quiet = false; f.Tick(1320000);
    f.quiet = true; f.Tick(1400000);
    f.Tick(1519999); assert(f.p().starts == 0);
    f.Tick(1520000); assert(f.p().starts == 1 && f.p().desired);
    f.Tick(9999999); assert(f.p().starts == 1 && f.p().desired);
    assert(f.activates == 0 && f.requests == 0);
    f.runtime->Acknowledge();
    assert(!f.visible && !f.p().desired && f.restores == 0 && !f.runtime->IsActive());
    assert(!f.runtime->IsAudioReleased());
    f.Drain();
    assert(f.runtime->IsAudioReleased() && f.restores == 1);
}

void voice_failures() {
    for (int failure = 0; failure < 5; ++failure) {
        Fixture f;
        if (failure == 0) f.activate_ok = false;
        if (failure == 1) f.accepted = false;
        if (failure == 4) f.activation_has_foreground = false;
        f.DueNow({Due(1, "A reminder")}); f.Tick(120000);
        if (failure == 2) f.callbacks[0](false);
        if (failure == 3) {
            f.Tick(35119999); assert(f.p().rings == 0 && f.suspends == 1);
            f.on_suspend = [&] { f.callbacks[0](true); };
            f.Tick(35120000);
        }
        assert(f.visible && f.p().rings == 0 && f.suspends == 2);
        f.Tick(f.now + 1); f.Tick(f.now + 120000);
        assert(f.p().desired && f.p().rings == 1 && f.activates == 1);
        f.Tick(f.now + 40000000);
        assert(f.activates == 1 && f.requests == ((failure == 0 || failure == 4) ? 0 : 1));
        if (!f.callbacks.empty()) f.callbacks[0](true);
        assert(f.visible && f.p().desired);
    }
}

void pending_never_beeps() {
    Fixture f; f.Voice();
    for (int second = 1; second <= 34; ++second) f.Tick(second * 1000000LL);
    assert(f.p().starts == 0 && f.p().rings == 0 && f.requests == 1);
    f.s().due = {Due(2), Due(3, "Later due")}; f.Tick(35000000);
    assert(f.shown.size() == 1 && f.requests == 1 && f.p().rings == 0);
    f.callbacks[0](true);
    assert(!f.visible && f.runtime->IsActive());
    f.quiet = false; f.Tick(35100000);
    assert(f.visible && f.shown.size() == 2 && f.requests == 1);
    f.quiet = true; f.Tick(35200000); f.Tick(35320000);
    assert(f.requests == 2 && f.p().rings == 0);
}

void acknowledgement_and_stale() {
    Fixture f; f.Voice();
    f.s().due = {Due(2, "Second reminder")}; f.Tick(1000000);
    const auto stale = f.callbacks[0];
    f.on_suspend = [&] { stale(true); stale(false); };
    f.runtime->Acknowledge();
    assert(!f.visible && f.restores == 0 && f.runtime->IsActive());
    stale(true); assert(!f.visible && f.p().rings == 0);
    f.Tick(1100000); f.Tick(1220000);
    assert(f.requests == 2 && f.shown.size() == 1 && f.shown[0].id == 2);
    stale(true); stale(false);
    assert(f.visible && f.runtime->IsActive());
    f.callbacks[1](true);
    assert(!f.runtime->IsActive() && !f.visible);
    f.runtime.reset();
    stale(true); stale(false); f.callbacks[1](false);
}

void user_cancel_and_leave_voice() {
    for (bool explicit_cancel : {false, true}) {
        Fixture f; f.Voice();
        f.s().due = {Due(2, "Later")}; f.Tick(1000000);
        if (explicit_cancel) f.runtime->CancelForUser();
        else { f.foreground = false; f.Tick(1100000); }
        assert(!f.runtime->IsActive() && !f.visible && !f.p().desired && f.restores == 0);
        f.callbacks[0](false); f.callbacks[0](true);
        f.Tick(50000000);
        assert(f.activates == 1 && f.requests == 1 && f.p().rings == 0);
    }
    Fixture f;
    f.on_activate = [&] { f.runtime->CancelForUser(); };
    f.DueNow({Due(1, "Cancel during activation")}); f.Tick(120000);
    assert(f.requests == 0 && !f.runtime->IsActive() && !f.visible);
}

void boot_and_system_defer() {
    Fixture f; f.ready = false;
    f.DueNow({Due(1, "First")});
    f.Tick(100000); f.Tick(999999);
    assert(f.s().polls == 1 && f.runtime->IsActive());
    assert(!f.visible && f.suspends == 0 && f.activates == 0 && f.quiet_checks == 0);
    assert(f.p().starts == 0 && f.p().stops == 0 && f.p().stop_checks == 0);
    f.s().due = {Due(2, "Second")}; f.Tick(1000000);
    assert(f.s().polls == 2 && !f.visible);
    f.ready = true; f.Tick(1100000);
    assert(f.visible && f.shown.size() == 2 && f.requests == 0);
    f.Tick(1220000); assert(f.requests == 1);
    const auto stale = f.callbacks[0];
    f.on_suspend = [&] { stale(true); };
    f.ready = false; f.Tick(1300000);
    assert(f.runtime->IsActive() && !f.visible && f.restores == 0 && !f.p().desired);
    stale(true); stale(false);
    f.ready = true; f.Tick(1400000); f.Tick(1520000);
    assert(f.visible && f.p().desired && f.requests == 1 && f.activates == 1);
    f.ready = false; f.Tick(1600000);
    assert(!f.p().desired && !f.visible);
    f.runtime->Acknowledge();
    assert(!f.runtime->IsActive() && f.restores == 0 && !f.runtime->IsAudioReleased());
    f.ready = true; f.p().stopped = true; f.Tick(2000000); f.Tick(2120000);
    assert(!f.visible && f.requests == 1 && f.restores == 0 && f.runtime->IsAudioReleased());
}

void defer_before_voice_and_callback_ready() {
    Fixture f;
    f.DueNow({Due(1, "Unattempted")});
    f.ready = false; f.Tick(100000);
    f.ready = true; f.Tick(200000); f.Tick(320000);
    assert(f.requests == 1 && f.activates == 1);
    // A ready transition can occur between the previous Tick and completion.
    f.ready = false; f.callbacks[0](true);
    assert(!f.visible && f.runtime->IsActive());
    f.ready = true; f.Tick(400000); f.Tick(520000);
    assert(f.p().desired && f.requests == 1);
}

void bounded_queue_and_prompt() {
    Fixture f;
    std::vector<Alarm> alarms;
    for (unsigned id = 1; id <= 24; ++id) alarms.push_back(Due(id, std::string(512, 'x')));
    alarms.push_back(Due(1, "duplicate"));
    f.DueNow(std::move(alarms));
    assert(f.shown.size() == 16);
    f.Tick(120000);
    assert(f.requests == 1 && f.prompt.size() <= 12 * 1024);
    assert(f.prompt.find("Alarm #16 ") != std::string::npos);
    assert(f.prompt.find("Alarm #17 ") == std::string::npos);
    assert(f.prompt.find("duplicate") == std::string::npos);
    for (unsigned id = 1; id <= 48; ++id) f.s().due.push_back(Due(id, "Later"));
    f.Tick(1000000);
    assert(f.shown.size() == 16);
    f.runtime->Acknowledge();
    f.Tick(1100000);
    assert(f.shown.size() == 16 && f.shown.front().id == 17 && f.shown.back().id == 32);
    f.runtime->CancelForUser();
    assert(!f.runtime->IsActive());

    Fixture deferred;
    deferred.ready = false;
    for (unsigned id = 1; id <= 48; ++id) deferred.s().due.push_back(Due(id));
    deferred.Tick(0); deferred.runtime->Acknowledge();
    deferred.ready = true; deferred.Tick(1000000);
    assert(!deferred.visible && !deferred.runtime->IsActive());
}

void inline_callbacks_and_player_failure() {
    for (int result : {0, 1}) {
        Fixture f; f.inline_result = result; f.accepted = false;
        f.DueNow({Due(1, "Inline result")}); f.Tick(120000);
        assert(f.requests == 1);
        if (result) assert(!f.runtime->IsActive() && !f.visible && f.suspends == 1);
        else { f.Drain(); assert(f.p().desired && f.suspends == 2); }
    }
    Fixture f;
    f.p().start_ok = false; f.DueNow({Due(1)}); f.Tick(120000);
    assert(f.visible && f.p().starts == 1 && !f.p().desired);
    f.Tick(1000000); f.Tick(2000000);
    assert(f.p().starts == 1 && f.requests == 0);
}

void poll_clock_and_defer_capacity() {
    Fixture f;
    f.Tick(0); f.Tick(100000); f.Tick(999999); assert(f.s().polls == 1);
    f.Tick(1000000); f.Tick(1900000); assert(f.s().polls == 2);
    f.Tick(0); assert(f.s().polls == 3);
    f.Tick(100000); assert(f.s().polls == 3);
    f.DueNow({});
    f.Tick(1000000);
    f.s().due = {Due(1, "Interrupted")}; f.Tick(2000000); f.Tick(2120000);
    assert(f.requests == 1);
    for (unsigned id = 2; id <= 17; ++id) f.s().due.push_back(Due(id));
    f.Tick(3000000);
    f.ready = false; f.Tick(3100000);
    f.ready = true; f.Tick(3200000);
    assert(f.shown.size() == 16 && f.shown.front().id == 1 && f.shown.back().id == 16);
    f.Tick(3320000); assert(f.p().desired && f.requests == 1);
}

void audio_release_idle_and_no_start() {
    Fixture f;
    const StopwatchAlarmRuntime& query = *f.runtime;
    assert(query.IsAudioReleased() && !query.IsActive());
    f.ready = false; f.Tick(0); f.Tick(1000000);
    assert(query.IsAudioReleased() && f.p().starts == 0 && f.p().stops == 0);
    assert(f.p().stop_checks == 0 && f.quiet_checks == 0);
    for (int i = 0; i < 100; ++i) assert(query.IsAudioReleased());
    f.runtime->CancelForUser(); f.runtime->Acknowledge();
    f.ready = true; f.Tick(2000000);
    assert(f.p().stop_checks == 0 && f.quiet_checks == 0 && f.restores == 0);

    Fixture failed;
    failed.p().start_ok = false;
    failed.DueNow({Due(1)}); failed.Tick(120000);
    assert(failed.runtime->IsAudioReleased() && failed.p().rings == 0);
    const int checks = failed.p().stop_checks;
    const int quiet_checks = failed.quiet_checks;
    failed.runtime->Acknowledge();
    assert(failed.restores == 0 && failed.runtime->IsAudioReleased());
    failed.Tick(120001);
    assert(failed.restores == 1 && failed.p().stop_checks == checks);
    assert(failed.quiet_checks == quiet_checks);

    Fixture voice;
    voice.Voice(); assert(voice.runtime->IsAudioReleased());
    voice.runtime->Acknowledge();
    assert(voice.restores == 0 && voice.runtime->IsAudioReleased());
    voice.Tick(120001);
    assert(voice.restores == 1 && voice.runtime->IsAudioReleased());
}

void audio_release_ack_inflight_and_quiet() {
    Fixture f;
    f.p().on_request = [&](bool ringing) {
        if (ringing) assert(!f.runtime->IsAudioReleased());
    };
    f.on_restore = [&] {
        assert(f.runtime->IsAudioReleased() && !f.runtime->IsActive() && !f.p().desired);
    };
    f.DueNow({Due(1)}); f.Tick(120000);
    assert(f.p().desired && !f.runtime->IsAudioReleased());
    f.runtime->Acknowledge();
    assert(!f.runtime->IsActive() && !f.visible && !f.runtime->IsAudioReleased());
    assert(f.restores == 0 && !f.p().desired);
    // In-flight worker submission has not acknowledged stop yet. Even an idle
    // Tick and an apparently quiet decoder cannot release audio prematurely.
    f.Tick(1000000); assert(!f.runtime->IsAudioReleased() && f.restores == 0);
    f.p().stopped = true; f.quiet = false; f.Tick(1100000);
    f.quiet = true; f.Tick(1200000); f.Tick(1319999);
    assert(!f.runtime->IsAudioReleased() && f.restores == 0);
    f.p().stopped = false; f.Tick(1320000);
    f.p().stopped = true; f.Tick(1400000);
    f.quiet = false; f.Tick(1519999);
    f.quiet = true; f.Tick(1600000); f.Tick(1719999);
    assert(!f.runtime->IsAudioReleased() && f.restores == 0);
    f.Tick(1720000);
    assert(f.runtime->IsAudioReleased() && f.restores == 1);
    const int checks = f.p().stop_checks;
    f.Tick(2000000);
    assert(f.restores == 1 && f.p().starts == 1 && f.p().stop_checks == checks);
}

void audio_release_cancel_restore_and_wake() {
    for (bool acknowledge_first : {false, true}) {
        Fixture f;
        f.DueNow({Due(1)}); f.Tick(120000);
        if (acknowledge_first) f.runtime->Acknowledge();
        f.runtime->CancelForUser();
        assert(!f.runtime->IsActive() && !f.runtime->IsAudioReleased() && f.restores == 0);
        // The core can queue a real wake against this query, without asking the
        // board/runtime to synthesize another wake when the gate later opens.
        int delivered_wake = 0;
        bool wake_pending = true;
        const auto consume_wake = [&] {
            if (wake_pending && f.runtime->IsAudioReleased()) {
                wake_pending = false; ++delivered_wake;
            }
        };
        consume_wake(); assert(delivered_wake == 0);
        f.Tick(200000); consume_wake(); assert(delivered_wake == 0);
        f.p().stopped = true; f.Tick(300000);
        f.runtime->CancelForUser(); // No active batch, cancel delayed ack policy.
        f.Tick(419999); consume_wake(); assert(delivered_wake == 0);
        f.Tick(420000); consume_wake();
        assert(delivered_wake == 1 && f.runtime->IsAudioReleased() && f.restores == 0);
        f.Tick(1000000); consume_wake();
        assert(delivered_wake == 1 && f.activates == 0 && f.requests == 0 && f.restores == 0);
    }
}

void audio_release_new_batch_revokes_restore() {
    Fixture f;
    f.DueNow({Due(1)}); f.Tick(120000);
    f.s().due = {Due(2, "New reminder")}; f.Tick(1000000);
    f.runtime->Acknowledge();
    assert(f.runtime->IsActive() && !f.runtime->IsAudioReleased() && f.restores == 0);
    f.Tick(1100000);
    assert(f.shown.size() == 1 && f.shown[0].id == 2 && f.requests == 0);
    f.p().stopped = true; f.Tick(1200000); f.Tick(1319999);
    assert(f.requests == 0 && !f.runtime->IsAudioReleased());
    f.on_activate = [&] { assert(f.runtime->IsAudioReleased()); };
    f.Tick(1320000);
    assert(f.runtime->IsAudioReleased() && f.requests == 1 && f.restores == 0);
    f.callbacks[0](true); f.Tick(1400000);
    assert(!f.runtime->IsActive() && f.restores == 0);

    // A new local batch on the exact release Tick must revoke restoration and
    // close the query again before its new ring request becomes visible.
    Fixture local;
    local.DueNow({Due(1)}); local.Tick(120000); local.runtime->Acknowledge();
    local.p().stopped = true; local.Tick(880000);
    local.s().due = {Due(2)}; local.Tick(1000000);
    assert(local.visible && local.shown[0].id == 2 && local.restores == 0);
    local.Tick(1120000);
    assert(local.p().desired && !local.runtime->IsAudioReleased() && local.restores == 0);
}

void audio_release_system_revokes_restore() {
    Fixture f;
    f.DueNow({Due(1)}); f.Tick(120000); f.runtime->Acknowledge();
    f.p().stopped = true; f.Tick(200000);
    f.ready = false;
    const int checks = f.p().stop_checks;
    const int quiet_checks = f.quiet_checks;
    f.Tick(320000); f.Tick(2000000);
    assert(!f.runtime->IsAudioReleased() && f.restores == 0);
    assert(f.p().stop_checks == checks && f.quiet_checks == quiet_checks);
    f.ready = true; f.Tick(2100000); f.Tick(2219999);
    assert(!f.runtime->IsAudioReleased());
    f.Tick(2220000);
    assert(f.runtime->IsAudioReleased() && f.restores == 0);

    Fixture deferred;
    deferred.DueNow({Due(1)}); deferred.Tick(120000);
    deferred.ready = false; deferred.Tick(200000);
    assert(!deferred.visible && !deferred.p().desired && !deferred.runtime->IsAudioReleased());
    deferred.runtime->Acknowledge(); // Not a restoration request on a system page.
    deferred.p().stopped = true; deferred.Tick(400000);
    assert(!deferred.runtime->IsActive() && !deferred.runtime->IsAudioReleased());
    deferred.ready = true; deferred.Tick(500000); deferred.Tick(620000);
    assert(deferred.runtime->IsAudioReleased() && deferred.restores == 0);
}

void audio_release_clock_rollback() {
    Fixture f;
    f.DueNow({Due(1)}); f.Tick(120000); f.runtime->Acknowledge();
    f.p().stopped = true; f.Tick(1000000); f.Tick(1100000);
    assert(!f.runtime->IsAudioReleased());
    f.Tick(1000); f.Tick(120999);
    assert(!f.runtime->IsAudioReleased() && f.restores == 0);
    f.Tick(121000);
    assert(f.runtime->IsAudioReleased() && f.restores == 1);
}

int main(int argc, char** argv) {
    assert(argc == 2);
    const std::map<std::string, void(*)()> cases = {
        {"simultaneous_reminders", simultaneous_reminders},
        {"ordinary_alarm", ordinary_alarm},
        {"voice_failures", voice_failures},
        {"pending_never_beeps", pending_never_beeps},
        {"acknowledgement_and_stale", acknowledgement_and_stale},
        {"user_cancel_and_leave_voice", user_cancel_and_leave_voice},
        {"boot_and_system_defer", boot_and_system_defer},
        {"defer_before_voice_and_callback_ready", defer_before_voice_and_callback_ready},
        {"bounded_queue_and_prompt", bounded_queue_and_prompt},
        {"inline_callbacks_and_player_failure", inline_callbacks_and_player_failure},
        {"poll_clock_and_defer_capacity", poll_clock_and_defer_capacity},
        {"audio_release_idle_and_no_start", audio_release_idle_and_no_start},
        {"audio_release_ack_inflight_and_quiet", audio_release_ack_inflight_and_quiet},
        {"audio_release_cancel_restore_and_wake", audio_release_cancel_restore_and_wake},
        {"audio_release_new_batch_revokes_restore", audio_release_new_batch_revokes_restore},
        {"audio_release_system_revokes_restore", audio_release_system_revokes_restore},
        {"audio_release_clock_rollback", audio_release_clock_rollback},
    };
    cases.at(argv[1])();
    std::cout << argv[1] << " passed\n";
}
'''


@unittest.skipUnless(COMPILER, "native g++ unavailable")
class StopwatchAlarmRuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="stopwatch_alarm_runtime_")
        cls.addClassCleanup(cls.tmp.cleanup)
        directory = Path(cls.tmp.name)
        (directory / "sdkconfig.h").write_text(
            "#pragma once\n#ifndef CONFIG_STOPWATCH_LOCAL_APPS\n"
            "#define CONFIG_STOPWATCH_LOCAL_APPS 1\n#endif\n",
            encoding="utf-8",
        )
        source = directory / "runtime_test.cc"
        source.write_text(HARNESS, encoding="utf-8")
        cls.binary = directory / ("runtime_test.exe" if os.name == "nt" else "runtime_test")
        command = [
            COMPILER, "-std=c++17", "-Wall", "-Wextra", "-Werror", "-O0", "-g",
            "-I", str(directory), "-I", str(BOARD),
            str(source), str(BOARD / "stopwatch_alarm_runtime.cc"), "-o", str(cls.binary),
        ]
        result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", env=ENV)
        if result.returncode:
            raise AssertionError(result.stdout + result.stderr)
        # Feature-off compilation must not require any board/core/audio headers.
        disabled = subprocess.run(
            [COMPILER, "-std=c++17", "-Wall", "-Wextra", "-Werror",
             "-DCONFIG_STOPWATCH_LOCAL_APPS=0", "-I", str(directory), "-I", str(BOARD),
             "-c", str(BOARD / "stopwatch_alarm_runtime.cc"),
             "-o", str(directory / "disabled.o")],
            capture_output=True, text=True, encoding="utf-8", env=ENV,
        )
        if disabled.returncode:
            raise AssertionError(disabled.stdout + disabled.stderr)

    def run_case(self, name):
        result = subprocess.run(
            [str(self.binary), name], capture_output=True, text=True,
            encoding="utf-8", env=ENV, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_simultaneous_reminders(self):
        self.run_case("simultaneous_reminders")

    def test_ordinary_alarm(self):
        self.run_case("ordinary_alarm")

    def test_voice_failures(self):
        self.run_case("voice_failures")

    def test_pending_never_beeps(self):
        self.run_case("pending_never_beeps")

    def test_acknowledgement_and_stale(self):
        self.run_case("acknowledgement_and_stale")

    def test_user_cancel_and_leave_voice(self):
        self.run_case("user_cancel_and_leave_voice")

    def test_boot_and_system_defer(self):
        self.run_case("boot_and_system_defer")

    def test_defer_before_voice_and_callback_ready(self):
        self.run_case("defer_before_voice_and_callback_ready")

    def test_bounded_queue_and_prompt(self):
        self.run_case("bounded_queue_and_prompt")

    def test_inline_callbacks_and_player_failure(self):
        self.run_case("inline_callbacks_and_player_failure")

    def test_poll_clock_and_defer_capacity(self):
        self.run_case("poll_clock_and_defer_capacity")

    def test_audio_release_idle_and_no_start(self):
        self.run_case("audio_release_idle_and_no_start")

    def test_audio_release_ack_inflight_and_quiet(self):
        self.run_case("audio_release_ack_inflight_and_quiet")

    def test_audio_release_cancel_restore_and_wake(self):
        self.run_case("audio_release_cancel_restore_and_wake")

    def test_audio_release_new_batch_revokes_restore(self):
        self.run_case("audio_release_new_batch_revokes_restore")

    def test_audio_release_system_revokes_restore(self):
        self.run_case("audio_release_system_revokes_restore")

    def test_audio_release_clock_rollback(self):
        self.run_case("audio_release_clock_rollback")


if __name__ == "__main__":
    unittest.main()
