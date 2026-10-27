"""Compile the complete board-local alarm service and MCP adapter with native I/O.

Real C++23 implementations, real MCP schema/envelopes, and managed cJSON are used.
Only NVS/FreeRTOS are stubbed; no firmware build, generated files, or hardware I/O.
"""

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
BOARD = ROOT / "main/boards/m5stack/stopwatch"
CJSON = ROOT / "managed_components/espressif__cjson/cJSON"
MINGW = Path("D:/sdk/mingw64/bin/g++.exe")
COMPILER = str(MINGW) if MINGW.is_file() else shutil.which("g++")
ENV = os.environ.copy()
if COMPILER:
    ENV["PATH"] = str(Path(COMPILER).parent) + os.pathsep + ENV.get("PATH", "")

STUBS = {
    "sdkconfig.h": "#pragma once\n#ifndef CONFIG_STOPWATCH_LOCAL_APPS\n#define CONFIG_STOPWATCH_LOCAL_APPS 1\n#endif\n",
    "esp_system.h": '#pragma once\n#include <cstdlib>\n[[noreturn]] inline void esp_system_abort(const char*) { std::abort(); }\n',
    "mbedtls/base64.h": '#pragma once\n#include <cstddef>\n#include <cstdlib>\ninline int mbedtls_base64_encode(unsigned char*, size_t, size_t*, const unsigned char*, size_t) { std::abort(); }\n',
    "freertos/FreeRTOS.h": r'''
#pragma once
#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cstdint>
#include <memory>
#include <mutex>
#include <thread>
#include <vector>
namespace task_test {
struct Task { std::mutex mutex; std::condition_variable cv; bool notified=false; std::thread thread; };
inline std::vector<std::unique_ptr<Task>> tasks;
inline thread_local Task* current = nullptr;
inline bool fail_start = false;
inline void JoinAll() { for (auto& task : tasks) if (task->thread.joinable()) task->thread.join(); tasks.clear(); }
}
using TaskHandle_t = task_test::Task*;
#define pdPASS 1
#define pdTRUE 1
#define pdMS_TO_TICKS(x) (x)
''',
    "freertos/task.h": r'''
#pragma once
#include "FreeRTOS.h"
inline int xTaskCreate(void(*entry)(void*),const char*,int,void* arg,int,TaskHandle_t* handle) {
    if (task_test::fail_start) return 0;
    auto task=std::make_unique<task_test::Task>(); *handle=task.get(); auto* raw=task.get();
    task->thread=std::thread([=]{task_test::current=raw;entry(arg);});
    task_test::tasks.push_back(std::move(task)); return pdPASS;
}
inline void xTaskNotifyGive(TaskHandle_t task) { std::lock_guard<std::mutex> lock(task->mutex); task->notified=true; task->cv.notify_all(); }
inline void ulTaskNotifyTake(int,uint32_t ms) {
    auto* task=task_test::current; std::unique_lock<std::mutex> lock(task->mutex);
    task->cv.wait_for(lock,std::chrono::milliseconds(ms),[&]{return task->notified;});task->notified=false;
}
inline void vTaskDelay(uint32_t ms){std::this_thread::sleep_for(std::chrono::milliseconds(ms));}
inline void vTaskDelete(void*){}
''',
    "nvs.h": r'''
#pragma once
#include <atomic>
#include <cassert>
#include <cstring>
#include <map>
#include <mutex>
#include <string>
#include <thread>
using nvs_handle_t = unsigned;
constexpr int ESP_OK=0, ESP_ERR_NVS_NOT_FOUND=1, NVS_READONLY=0, NVS_READWRITE=1;
namespace nvs_test {
inline std::mutex mutex;
inline std::map<std::string,std::string> committed, pending;
inline std::atomic<bool> fail_open=false, fail_set=false, fail_commit=false, fail_read=false;
inline std::atomic<int> opens=0, closes=0, writes=0, commits=0;
inline const auto main_thread=std::this_thread::get_id();
inline void WorkerOnly(){assert(std::this_thread::get_id()!=main_thread);}
inline void Reset(){std::lock_guard<std::mutex> lock(mutex); committed.clear();pending.clear();fail_open=fail_set=fail_commit=fail_read=false;opens=closes=writes=commits=0;}
}
inline int nvs_open(const char* ns,int,nvs_handle_t* handle) {
    nvs_test::WorkerOnly();++nvs_test::opens;assert(std::string(ns)=="sw_alarm");
    if(nvs_test::fail_open)return -1;*handle=1;return ESP_OK;
}
inline void nvs_close(nvs_handle_t){nvs_test::WorkerOnly();++nvs_test::closes;std::lock_guard<std::mutex> lock(nvs_test::mutex);nvs_test::pending.clear();}
inline int nvs_get_blob(nvs_handle_t,const char* key,void* destination,size_t* length){
    nvs_test::WorkerOnly();std::lock_guard<std::mutex> lock(nvs_test::mutex);
    if(nvs_test::fail_read)return -1;auto found=nvs_test::committed.find(key);if(found==nvs_test::committed.end())return ESP_ERR_NVS_NOT_FOUND;
    if(!destination){*length=found->second.size();return ESP_OK;}
    if(*length<found->second.size())return -1;std::memcpy(destination,found->second.data(),found->second.size());*length=found->second.size();return ESP_OK;
}
inline int nvs_set_blob(nvs_handle_t,const char* key,const void* data,size_t length){
    nvs_test::WorkerOnly();++nvs_test::writes;if(nvs_test::fail_set)return -1;
    std::lock_guard<std::mutex> lock(nvs_test::mutex);nvs_test::pending[key]=std::string(static_cast<const char*>(data),length);return ESP_OK;
}
inline int nvs_commit(nvs_handle_t){
    nvs_test::WorkerOnly();++nvs_test::commits;if(nvs_test::fail_commit)return -1;
    std::lock_guard<std::mutex> lock(nvs_test::mutex);for(auto& item:nvs_test::pending)nvs_test::committed[item.first]=item.second;return ESP_OK;
}
''',
}

HARNESS = r'''
#include <algorithm>
#include <atomic>
#include <cassert>
#include <chrono>
#include <condition_variable>
#include <cstdlib>
#include <functional>
#include <iostream>
#include <map>
#include <mutex>
#include <string>
#include <thread>
#include <vector>
#include "mcp_server.h"
#include "stopwatch_alarm_service.h"
#include "stopwatch_alarm_tools.h"
// Include the entire production TU, not extracted functions. Also permits
// focused tests of the private bounded/versioned persistence codec.
#include "stopwatch_alarm_service.cc"

using namespace stopwatch_alarm;
using namespace std::chrono_literals;
#undef assert
#define assert(condition) do { if (!(condition)) { std::fprintf(stderr,"assertion failed: %s at %s:%d\n",#condition,__FILE__,__LINE__); std::_Exit(1); } } while(0)

bool Until(const std::function<bool()>& condition) {
    for (int i=0;i<4000;++i) { if(condition())return true;std::this_thread::sleep_for(1ms); }
    return false;
}

class Fake final : public Platform {
public:
    const std::thread::id main_thread=std::this_thread::get_id();
    std::mutex mutex;std::condition_variable cv;std::thread thread;
    std::atomic<int> reads{0},writes{0},starts{0},waits{0};
    std::atomic<bool> fail_start{false},fail_read{false},fail_write{false};
    std::atomic<bool> block_write{false},blocked_write{false},block_read{false},blocked_read{false};
    std::string bytes;
    bool notified=false;
    ReadStatus ReadBlob(std::string& out,size_t limit) override {
        assert(std::this_thread::get_id()!=main_thread);++reads;blocked_read=block_read.load();
        while(block_read)std::this_thread::sleep_for(1ms);blocked_read=false;
        if(fail_read)return ReadStatus::Error;
        std::lock_guard<std::mutex> lock(mutex);if(bytes.empty())return ReadStatus::Missing;
        if(bytes.size()>limit)return ReadStatus::Error;out=bytes;return ReadStatus::Ok;
    }
    bool WriteBlob(const std::string& data) override {
        assert(std::this_thread::get_id()!=main_thread);assert(!data.empty()&&data.size()<=kBlobLimit);
        ++writes;blocked_write=block_write.load();while(block_write)std::this_thread::sleep_for(1ms);blocked_write=false;
        if(fail_write)return false;std::lock_guard<std::mutex> lock(mutex);bytes=data;return true;
    }
    bool StartWorker(void(*entry)(void*),void* argument) override {
        ++starts;if(fail_start)return false;assert(!thread.joinable());thread=std::thread([=]{entry(argument);});return true;
    }
    void WakeWorker() override {std::lock_guard<std::mutex> lock(mutex);notified=true;cv.notify_all();}
    void WaitWorker(uint32_t milliseconds) override {
        ++waits;std::unique_lock<std::mutex> lock(mutex);cv.wait_for(lock,std::chrono::milliseconds(milliseconds),[&]{return notified;});notified=false;
    }
    void JoinWorker() override {if(thread.joinable())thread.join();}
    ~Fake(){assert(!thread.joinable());}
};

void Ready(StopwatchAlarmService& service) {
    assert(service.Initialize());assert(Until([&]{return service.GetSnapshot().initialized;}));
}
Result Done(StopwatchAlarmService& service,Result result) {
    assert(result.ok&&result.pending&&result.request_id);
    assert(Until([&]{return !service.GetRequest(result.request_id).pending;}));
    return service.GetRequest(result.request_id);
}
Result Added(StopwatchAlarmService& service,int hour,int minute,const std::string& reminder="",const std::string& date="") {
    auto result=Done(service,service.Add(hour,minute,reminder,date));assert(result.ok&&result.id&&!result.pending);return result;
}
time_t At(int year,int month,int day,int hour,int minute,int second=0) {
    std::tm local{};local.tm_year=year-1900;local.tm_mon=month-1;local.tm_mday=day;
    local.tm_hour=hour;local.tm_min=minute;local.tm_sec=second;local.tm_isdst=-1;
    auto value=std::mktime(&local);assert(value>=0);return value;
}
std::vector<Alarm> Ring(StopwatchAlarmService& service,time_t now,size_t count) {
    auto alarms=service.Poll(now);
    assert(Until([&]{auto more=service.Poll(now);alarms.insert(alarms.end(),more.begin(),more.end());return alarms.size()>=count;}));
    assert(alarms.size()==count);return alarms;
}

std::vector<McpTool*> registered;
McpServer::McpServer()=default;
McpServer::~McpServer()=default;
void McpServer::AddTool(const std::string& name,const std::string& description,const PropertyList& properties,ToolCallback callback) {
    auto tool=std::make_unique<McpTool>(name,description,properties,std::move(callback));registered.push_back(tool.get());tools_.push_back(std::move(tool));
}
McpTool& Tool(const std::string& suffix) {
    for(auto* tool:registered)if(tool->name()=="self.alarm."+suffix)return *tool;std::abort();
}
using Value=std::variant<bool,int,std::string>;
std::expected<std::string,std::string> Call(const std::string& suffix,std::initializer_list<std::pair<std::string,Value>> arguments={}) {
    auto& tool=Tool(suffix);auto properties=tool.properties();
    for(const auto& [key,value]:arguments) {
        bool found=false;
        for(auto& property:properties)if(property.name()==key){found=true;auto set=std::visit([&](const auto& item){return property.set_value(item);},value);if(!set)return std::unexpected(set.error());}
        assert(found);
    }
    return tool.Call(properties);
}
CJsonUniquePtr Json(const std::string& suffix,std::initializer_list<std::pair<std::string,Value>> arguments={}) {
    auto text=Call(suffix,arguments);assert(text);CJsonUniquePtr envelope(cJSON_Parse(text->c_str()));assert(envelope);
    assert(cJSON_IsFalse(cJSON_GetObjectItemCaseSensitive(envelope.get(),"isError")));
    auto* content=cJSON_GetArrayItem(cJSON_GetObjectItemCaseSensitive(envelope.get(),"content"),0);
    auto* payload=cJSON_GetObjectItemCaseSensitive(content,"text");assert(cJSON_IsString(payload));
    CJsonUniquePtr result(cJSON_Parse(payload->valuestring));assert(result);return result;
}
cJSON* Field(cJSON* parent,const char* key){auto* value=cJSON_GetObjectItemCaseSensitive(parent,key);assert(value);return value;}
std::string Text(cJSON* parent,const char* key){auto* value=Field(parent,key);assert(cJSON_IsString(value));return value->valuestring;}

void Defaults() {
    Fake io;StopwatchAlarmService service(io);auto snapshot=service.GetSnapshot();
    assert(io.reads==0&&io.writes==0&&io.starts==0&&!snapshot.initialized);
    io.block_read=true;auto started=std::chrono::steady_clock::now();assert(service.Initialize());
    assert(std::chrono::steady_clock::now()-started<100ms&&Until([&]{return io.blocked_read.load();}));
    auto rejected=service.Add(7,30);assert(!rejected.ok&&rejected.error=="not_ready");
    assert(service.GetSnapshot().pending);io.block_read=false;Ready(service);assert(io.reads==1&&io.starts==1);
    service.Initialize();service.Initialize();assert(io.starts==1&&io.writes==0);
    assert(service.Poll(0).empty()&&!service.GetSnapshot().time_valid);
    assert(service.Poll(At(2026,10,26,7,30)).empty()&&service.GetSnapshot().time_valid);
    auto version=service.Version();assert(service.Poll(-1).empty());assert(!service.GetSnapshot().time_valid&&service.Version()!=version);
    assert(service.GetSnapshot().alarms.empty()&&service.GetSnapshot().revision==0);
}

void Invalid() {
    Fake io;StopwatchAlarmService service(io);Ready(service);
    for(auto [hour,minute]:std::vector<std::pair<int,int>>{{-1,0},{24,0},{0,-1},{0,60},{INT32_MAX,0}}){auto r=service.Add(hour,minute);assert(!r.ok&&r.error=="invalid_time");}
    for(const std::string& text:std::vector<std::string>{std::string(513,'x'),std::string("a\0b",3),"x\n",std::string("\xc0\xaf",2),std::string("\xe2\x82",2),std::string("\xed\xa0\x80",3),std::string("\xf4\x90\x80\x80",4),std::string("\xc2\x85",2),std::string("\x7f",1)}) {
        auto r=service.Add(1,2,text);assert(!r.ok&&r.error=="invalid_reminder");
    }
    for(const std::string& date:std::vector<std::string>{"2026-2-01","2026-02-29","2100-02-29","2024-04-31","2019-12-31","10000-01-01","2026-13-01","2026-00-01","2026-01-00","2026-01-32","2026/01/01","2026-01-01x",std::string("2026-01-01\0",11)}) {
        auto r=service.Add(1,2,"",date);assert(!r.ok&&r.error=="invalid_date");
    }
    assert(!service.SetEnabled(0,true).ok&&!service.Remove(0).ok&&io.writes==0);
    Added(service,0,0,std::string(512,'x'),"2024-02-29");
    Added(service,23,59,"喝水提醒 😀","2400-02-29");
    assert(service.GetSnapshot().alarms[1].reminder=="喝水提醒 😀");
}

void CrudFull() {
    Fake io;StopwatchAlarmService service(io);Ready(service);
    std::vector<uint32_t> ids;
    for(size_t i=0;i<kMaxAlarms;++i)ids.push_back(Added(service,i%24,i%60).id);
    auto snapshot=service.GetSnapshot();assert(snapshot.alarms.size()==16&&snapshot.revision==16);
    assert(Done(service,service.Add(1,1)).error=="full"&&io.writes==16);
    assert(Done(service,service.SetEnabled(ids[4],false,snapshot.revision)).ok);
    auto writes=io.writes.load();assert(Done(service,service.SetEnabled(ids[4],false)).ok&&io.writes==writes);
    assert(!service.GetSnapshot().alarms[4].enabled);
    auto stale=Done(service,service.Remove(ids[5],snapshot.revision));assert(!stale.ok&&stale.error=="conflict");
    assert(Done(service,service.Remove(ids[5])).ok);
    auto next=Added(service,3,9);assert(next.id>ids.back());
    auto missing=Done(service,service.SetEnabled(ids[5],true));assert(!missing.ok&&missing.error=="not_found");
    missing=Done(service,service.Remove(UINT32_MAX));assert(!missing.ok&&missing.error=="not_found");
    snapshot=service.GetSnapshot();assert(snapshot.alarms.size()==16&&snapshot.last_result.request_id==missing.request_id);
    // Completed-request history is bounded, but current pending entries remain queryable.
    assert(Done(service,service.Remove(ids[0])).ok);
    auto oldest=Added(service,1,1);assert(Done(service,service.Remove(oldest.id)).ok);
    for(int i=0;i<static_cast<int>(kResultCapacity);++i){auto result=Added(service,1,2);assert(Done(service,service.Remove(result.id)).ok);}
    assert(service.GetRequest(oldest.request_id).error=="unknown_request");
}

void PersistenceFailure() {
    Fake io;
    {
        StopwatchAlarmService service(io);Ready(service);auto id=Added(service,8,15,"提醒","2026-10-27").id;
        const auto original=service.GetSnapshot();const auto bytes=io.bytes;io.fail_write=true;
        auto r=Done(service,service.Add(9,1));assert(!r.ok&&r.id==0&&r.error=="storage_failed");
        r=Done(service,service.SetEnabled(id,false));assert(!r.ok&&r.error=="storage_failed");
        r=Done(service,service.Remove(id));assert(!r.ok&&r.error=="storage_failed");
        auto after=service.GetSnapshot();assert(after.revision==original.revision&&after.alarms.size()==1&&after.alarms[0].enabled&&io.bytes==bytes);
        io.fail_write=false;auto next=Added(service,10,2);assert(next.id==id+1);
    }
    {StopwatchAlarmService reboot(io);Ready(reboot);auto s=reboot.GetSnapshot();assert(s.alarms.size()==2&&s.revision==2&&s.alarms[0].reminder=="提醒"&&s.alarms[0].date=="2026-10-27");assert(Added(reboot,1,2).id==3);}
}

void BlockedQueue() {
    Fake io;StopwatchAlarmService service(io);Ready(service);io.block_write=true;
    auto first=service.Add(3,4);assert(first.ok&&Until([&]{return io.blocked_write.load();}));
    assert(service.GetSnapshot().alarms.empty()&&service.GetRequest(first.request_id).pending);
    std::vector<Result> requests{first};
    auto start=std::chrono::steady_clock::now();
    for(int i=0;i<3;++i){requests.push_back(service.Add(i,5));assert(requests.back().ok);}
    auto rejected=service.Add(4,5);assert(!rejected.ok&&rejected.error=="busy");
    assert(service.GetSnapshot().pending_count==4);service.Poll(At(2026,10,26,1,2));
    for(int i=0;i<100;++i){service.GetSnapshot();service.Version();service.GetRequest(first.request_id);}
    assert(std::chrono::steady_clock::now()-start<100ms);
    io.block_write=false;
    for(auto request:requests){auto result=Done(service,request);assert(result.ok);}
    auto s=service.GetSnapshot();assert(s.alarms.size()==4&&s.revision==4);
    for(size_t i=0;i<s.alarms.size();++i)assert(s.alarms[i].id==i+1);
}

void DailyAndOnce() {
    Fake io;
    auto day=At(2026,10,26,7,30);auto tomorrow=At(2026,10,27,7,30);
    {
        StopwatchAlarmService service(io);Ready(service);
        auto daily=Added(service,7,30,"每日喝水").id;
        auto once=Added(service,7,30,"生日提醒","2026-10-26").id;
        auto future=Added(service,7,30,"未来","2026-10-27").id;
        auto disabled=Added(service,7,30).id;assert(Done(service,service.SetEnabled(disabled,false)).ok);
        assert(service.Poll(0).empty());auto due=Ring(service,day,2);
        assert(due[0].id==daily&&due[1].id==once&&due[1].reminder=="生日提醒");
        for(int i=0;i<10;++i)assert(service.Poll(day+i).empty());
        auto snapshot=service.GetSnapshot();assert(!snapshot.alarms[1].enabled&&snapshot.alarms[0].last_triggered_date=="2026-10-26");
        assert(Done(service,service.SetEnabled(once,true)).ok);assert(service.Poll(day).empty());
        assert(service.Poll(At(2026,10,25,7,30)).empty());
        due=Ring(service,tomorrow,2);assert(due[0].id==daily&&due[1].id==future);
        assert(service.Poll(day).empty());
    }
    {
        StopwatchAlarmService reboot(io);Ready(reboot);assert(reboot.Poll(tomorrow).empty());
        auto due=Ring(reboot,At(2026,10,28,7,30),1);assert(due[0].reminder=="每日喝水");
    }
}

void AllSameMinute() {
    Fake io;StopwatchAlarmService service(io);Ready(service);
    for(int i=0;i<16;++i)Added(service,23,59,std::to_string(i));
    auto writes=io.writes.load();auto due=Ring(service,At(2026,12,31,23,59,1),16);
    assert(io.writes==writes+1);for(size_t i=0;i<due.size();++i)assert(due[i].id==i+1&&due[i].reminder==std::to_string(i));
    assert(service.Poll(At(2026,12,31,23,59,59)).empty());
    auto next=Ring(service,At(2027,1,1,23,59),16);assert(next.size()==16);
}

void SyncAndFailures() {
    Fake io;StopwatchAlarmService service(io);Ready(service);Added(service,8,30,"sync");
    assert(service.Poll(0).empty());assert(service.Poll(At(2026,10,26,8,29)).empty());
    // A forward sync past the target minute never catches up.
    assert(service.Poll(At(2026,10,26,8,31)).empty());
    io.fail_write=true;auto when=At(2026,10,26,8,30);assert(service.Poll(when).empty());
    assert(Until([&]{return service.GetSnapshot().error=="storage_failed";}));
    assert(service.GetSnapshot().alarms[0].last_triggered_date.empty());
    assert(service.Poll(when).empty());assert(Until([&]{return io.writes>=3;}));
    io.fail_write=false;auto due=Ring(service,when,1);assert(due[0].reminder=="sync");
    // Even after losing synchronization, same-day and earlier-day alarms cannot repeat.
    assert(service.Poll(0).empty());assert(service.Poll(when).empty());
    assert(service.Poll(At(2026,10,25,8,30)).empty());
    assert(Ring(service,At(2026,10,27,8,30),1).size()==1);
}

void BlockedDueClockCorrection() {
    Fake io;StopwatchAlarmService service(io);Ready(service);Added(service,9,5,"do not replay");
    io.block_write=true;auto when=At(2026,10,26,9,5);assert(service.Poll(when).empty());
    assert(Until([&]{return io.blocked_write.load();}));
    assert(service.Poll(At(2026,10,26,9,6)).empty());io.block_write=false;
    assert(Until([&]{return service.GetSnapshot().alarms[0].last_triggered_date=="2026-10-26";}));
    assert(service.Poll(At(2026,10,26,9,6)).empty()&&service.Poll(when).empty());
    io.block_write=true;when=At(2026,10,27,9,5);service.Poll(when);assert(Until([&]{return io.blocked_write.load();}));
    service.Poll(0);io.block_write=false;assert(Until([&]{return !service.GetSnapshot().pending;}));assert(service.Poll(when).empty());
}

void PurgeAndAtMostOnce() {
    Fake io;
    const auto when=At(2026,10,26,6,0);
    {
        StopwatchAlarmService service(io);Ready(service);auto id=Added(service,6,0,"persist before delivery").id;
        service.Poll(when);assert(Until([&]{return service.GetSnapshot().alarms[0].last_triggered_date=="2026-10-26";}));
        // Disabling after the durable trigger but before handoff discards outbox.
        assert(Done(service,service.SetEnabled(id,false)).ok);assert(service.Poll(when).empty());
        assert(Done(service,service.SetEnabled(id,true)).ok);assert(service.Poll(when).empty());
        auto id2=Added(service,6,0,"delete pending handoff").id;
        service.Poll(when);assert(Until([&]{return service.GetSnapshot().alarms[1].last_triggered_date=="2026-10-26";}));
        assert(Done(service,service.Remove(id2)).ok);assert(service.Poll(when).empty());
        // Simulate a crash after commit but before handing the alarm to board.
        service.Poll(At(2026,10,27,6,0));assert(Until([&]{return service.GetSnapshot().alarms[0].last_triggered_date=="2026-10-27";}));
    }
    {StopwatchAlarmService reboot(io);Ready(reboot);assert(reboot.Poll(At(2026,10,27,6,0)).empty());}
}

void CodecAndCorruption() {
    Config config;config.revision=7;config.next_id=17;
    for(uint32_t id=1;id<=16;++id){Alarm a;a.id=id;a.hour=23;a.minute=59;a.reminder=std::string(512,'x');config.alarms.push_back(a);}
    std::string bytes;assert(Encode(config,bytes)&&bytes.size()<=kBlobLimit);Config decoded;
    assert(Decode(bytes,decoded)&&decoded.alarms.size()==16&&decoded.revision==7&&decoded.next_id==17);
    for(size_t at=0;at<bytes.size();at+=131){auto bad=bytes;bad[at]^=1;assert(!Decode(bad,decoded));}
    assert(!Decode(bytes+"x",decoded)&&!Decode(std::string(kBlobLimit+1,'x'),decoded));
    auto bad=bytes;bad[4]=2;assert(!Decode(bad,decoded));
    auto recalc=[](std::string& value){value.resize(value.size()-4);Put(value,Checksum(value,value.size()),4);};
    bad=bytes;bad[6]=17;recalc(bad);assert(!Decode(bad,decoded));
    bad=bytes;bad[16]=0;bad[17]=bad[18]=bad[19]=0;recalc(bad);assert(!Decode(bad,decoded));
    bad=bytes;bad[20]=24;recalc(bad);assert(!Decode(bad,decoded));
    bad=bytes;bad[22]=2;recalc(bad);assert(!Decode(bad,decoded));
    // Valid checksum cannot legitimize a duplicate ID or malformed UTF-8.
    bad=bytes;bad[16+18+512]=1;recalc(bad);assert(!Decode(bad,decoded));
    bad=bytes;bad[34]=static_cast<char>(0xc0);recalc(bad);assert(!Decode(bad,decoded));
    Fake io;io.bytes="not an alarm blob";
    {StopwatchAlarmService service(io);assert(service.Initialize());assert(Until([&]{return service.GetSnapshot().error=="storage_corrupt";}));assert(!service.GetSnapshot().initialized&&!service.Add(1,2).ok&&io.writes==0);}
    Fake failed;failed.fail_read=true;
    {StopwatchAlarmService service(failed);assert(service.Initialize());assert(Until([&]{return service.GetSnapshot().error=="storage_failed";}));assert(!service.GetSnapshot().initialized);failed.fail_read=false;Ready(service);}
    Fake worker;worker.fail_start=true;
    {StopwatchAlarmService service(worker);assert(!service.Initialize());auto r=service.Add(1,2);assert(!r.ok&&r.error=="worker_unavailable"&&worker.reads==0);worker.fail_start=false;Ready(service);}
}

void ExhaustionBounds() {
    Config config;config.revision=kAnyRevision-1;config.next_id=1;
    Fake revision;assert(Encode(config,revision.bytes));
    {StopwatchAlarmService service(revision);Ready(service);auto result=Done(service,service.Add(1,2));assert(!result.ok&&result.id==0&&result.error=="revision_exhausted"&&revision.writes==0);}
    config.revision=0;config.next_id=UINT32_MAX;
    Fake ids;assert(Encode(config,ids.bytes));
    {
        StopwatchAlarmService service(ids);Ready(service);auto last=Added(service,1,2);assert(last.id==UINT32_MAX);
        assert(Done(service,service.Remove(last.id)).ok);auto result=Done(service,service.Add(3,4));
        assert(!result.ok&&result.id==0&&result.error=="id_exhausted");
    }
    {StopwatchAlarmService reboot(ids);Ready(reboot);auto result=Done(reboot,reboot.Add(3,4));assert(!result.ok&&result.error=="id_exhausted");}
}

void DeviceNvs() {
    nvs_test::Reset();
    {
        StopwatchAlarmService service;Ready(service);auto id=Added(service,10,20,"real NVS adapter").id;
        auto snapshot=service.GetSnapshot();
        for(auto* failure:{&nvs_test::fail_open,&nvs_test::fail_set,&nvs_test::fail_commit}) {
            *failure=true;auto result=Done(service,service.Remove(id));assert(!result.ok&&result.error=="storage_failed");
            assert(service.GetSnapshot().revision==snapshot.revision&&service.GetSnapshot().alarms.size()==1);*failure=false;
        }
    }
    task_test::JoinAll();
    {StopwatchAlarmService reboot;Ready(reboot);assert(reboot.GetSnapshot().alarms.size()==1&&reboot.GetSnapshot().alarms[0].reminder=="real NVS adapter");}
    task_test::JoinAll();
    assert(nvs_test::committed.count("storage_v1")==1);
    nvs_test::committed["storage_v1"]=std::string(kBlobLimit+1,'x');
    {StopwatchAlarmService corrupt;assert(corrupt.Initialize());assert(Until([&]{return corrupt.GetSnapshot().error=="storage_failed";}));assert(!corrupt.GetSnapshot().initialized);}
    task_test::JoinAll();
}

void SetTimezone(const char* timezone) {
#ifdef _WIN32
    _putenv_s("TZ",timezone);_tzset();
#else
    setenv("TZ",timezone,1);tzset();
#endif
}

void TimezoneAndDst() {
    SetTimezone("CST-8");
    {
        Fake io;StopwatchAlarmService service(io);Ready(service);Added(service,8,15,"local not UTC");
        // 2026-10-26 00:15 UTC is 08:15 in this device's configured timezone.
        auto due=Ring(service,static_cast<time_t>(1792973700),1);assert(due[0].hour==8&&due[0].minute==15);
    }
    SetTimezone("EST5EDT,M3.2.0/2,M11.1.0/2");
    {
        Fake io;StopwatchAlarmService service(io);Ready(service);Added(service,1,30,"DST once");
        std::tm first{};first.tm_year=126;first.tm_mon=10;first.tm_mday=1;first.tm_hour=1;first.tm_min=30;first.tm_isdst=1;
        auto daylight=std::mktime(&first);first.tm_isdst=0;auto standard=std::mktime(&first);
        // POSIX timezone-rule support is platform-specific. If DST is active,
        // verify the repeated wall-clock minute cannot schedule twice.
        auto due=Ring(service,daylight,1);assert(due.size()==1);assert(service.Poll(standard).empty());
    }
    SetTimezone("UTC0");
}

void PurgeSiblingReminder() {
    Fake io;StopwatchAlarmService service(io);Ready(service);auto id=Added(service,5,30,"remove me").id;
    Added(service,5,30,"keep this reminder");auto when=At(2026,10,26,5,30);service.Poll(when);
    assert(Until([&]{auto s=service.GetSnapshot();return s.alarms[1].last_triggered_date=="2026-10-26";}));
    assert(Done(service,service.SetEnabled(id,false)).ok);auto due=service.Poll(when);
    assert(due.size()==1&&due[0].reminder=="keep this reminder");
}

void RevisionQueuedConflict() {
    Fake io;StopwatchAlarmService service(io);Ready(service);auto id=Added(service,5,30).id;
    const auto revision=service.GetSnapshot().revision;io.block_write=true;
    auto first=service.SetEnabled(id,false,revision);assert(first.ok&&Until([&]{return io.blocked_write.load();}));
    auto second=service.Remove(id,revision);assert(second.ok);io.block_write=false;
    assert(Done(service,first).ok);auto result=Done(service,second);assert(!result.ok&&result.error=="conflict");
    assert(service.GetSnapshot().alarms.size()==1&&!service.GetSnapshot().alarms[0].enabled);
}

void Concurrency() {
    Fake io;StopwatchAlarmService service(io);Ready(service);Added(service,13,15);
    std::atomic<bool> done=false;std::atomic<int> accepted=0;
    std::thread reader([&]{while(!done){auto s=service.GetSnapshot();assert(s.alarms.size()<=16);service.Version();service.Poll(At(2026,10,26,13,14));}});
    std::vector<std::thread> writers;
    for(int i=0;i<4;++i)writers.emplace_back([&]{for(int j=0;j<30;++j){auto r=service.SetEnabled(1,j%2);if(r.ok){++accepted;auto finished=Done(service,r);assert(finished.ok);}else assert(r.error=="busy");}});
    for(auto& thread:writers)thread.join();done=true;reader.join();assert(accepted>0);
    auto result=Done(service,service.SetEnabled(1,true));assert(result.ok);
}

void McpSchema() {
    Fake io;StopwatchAlarmService service(io);Ready(service);RegisterStopwatchAlarmTools(service);
    assert(registered.size()==4);
    for(auto* tool:registered){assert(!tool->user_only());CJsonUniquePtr schema(cJSON_Parse(tool->to_json().c_str()));assert(schema);assert(tool->description().find("LOCAL")!=std::string::npos||tool->description().find("local")!=std::string::npos);}
    auto& add=Tool("add");auto required=add.properties().GetRequired();assert(required.size()==2&&required[0]=="hour"&&required[1]=="minute");
    assert(add.properties()["hour"].min_value()==0&&add.properties()["hour"].max_value()==23);
    assert(add.properties()["minute"].max_value()==59&&add.properties()["reminder"].max_length()==512&&add.properties()["date"].max_length()==10);
    assert(add.properties()["date"].has_default_value()&&add.properties()["reminder"].has_default_value());
    assert(!Call("add",{{"hour",24},{"minute",1}}));assert(!Call("add",{{"hour",1},{"minute",-1}}));
    assert(!Call("add",{{"hour",1},{"minute",2},{"reminder",std::string(513,'x')}}));assert(io.writes==0);
    assert(Tool("set_enabled").properties()["enabled"].type()==kPropertyTypeBoolean);
    for(const std::string id:{"0","-1","+1","1x"," 1","1 ","4294967296"}) {
        assert(!Call("remove",{{"id",id}}));assert(!Call("set_enabled",{{"id",id},{"enabled",true}}));assert(!Call("get_status",{{"request_id",id}}));
    }
}

void McpAccepted() {
    Fake io;StopwatchAlarmService service(io);Ready(service);RegisterStopwatchAlarmTools(service);io.block_write=true;
    auto reply=Json("add",{{"hour",14},{"minute",25},{"reminder",std::string("吃药提醒")},{"date",std::string("2026-10-26")}});
    assert(Text(reply.get(),"status")=="accepted");const auto request=Text(reply.get(),"request_id");
    assert(Until([&]{return io.blocked_write.load();}));auto status=Json("get_status",{{"request_id",request}});
    assert(cJSON_GetArraySize(Field(status.get(),"alarms"))==0&&Text(Field(status.get(),"request"),"state")=="pending");
    assert(Text(status.get(),"time_basis")=="device_local"&&cJSON_IsFalse(Field(status.get(),"time_valid")));
    io.block_write=false;assert(Until([&]{return !service.GetRequest(static_cast<uint32_t>(std::stoul(request))).pending;}));
    status=Json("get_status",{{"request_id",request}});assert(Text(Field(status.get(),"request"),"state")=="committed");
    auto* alarm=cJSON_GetArrayItem(Field(status.get(),"alarms"),0);assert(Text(alarm,"reminder")=="吃药提醒"&&Text(alarm,"repeat")=="once"&&Text(alarm,"date")=="2026-10-26");
    reply=Json("set_enabled",{{"id",std::string("1")},{"enabled",false}});assert(Text(reply.get(),"status")=="accepted");
    auto result=service.GetSnapshot().last_result;assert(Until([&]{return !service.GetSnapshot().alarms[0].enabled;}));
    (void)result;io.fail_write=true;reply=Json("remove",{{"id",std::string("1")}});auto id=static_cast<uint32_t>(std::stoul(Text(reply.get(),"request_id")));
    assert(Until([&]{return !service.GetRequest(id).pending;}));status=Json("get_status",{{"request_id",std::to_string(id)}});
    assert(Text(Field(status.get(),"request"),"state")=="error"&&Text(Field(status.get(),"request"),"error")=="storage_failed");
    assert(cJSON_GetArraySize(Field(status.get(),"alarms"))==1);
    status=Json("get_status",{{"request_id",std::string("4294967295")}});assert(Text(Field(status.get(),"request"),"error")=="unknown_request");
    assert(!Call("add",{{"hour",1},{"minute",2},{"date",std::string("2026-02-29")}}));
    io.fail_write=false;
}

int main(int argc,char** argv) {
    assert(argc==2);
    SetTimezone("UTC0");
    const std::map<std::string,std::function<void()>> cases={
        {"defaults",Defaults},{"invalid",Invalid},{"crud_full",CrudFull},{"persistence_failure",PersistenceFailure},
        {"blocked_queue",BlockedQueue},{"daily_once",DailyAndOnce},{"same_minute",AllSameMinute},{"sync_failure",SyncAndFailures},
        {"blocked_due_clock",BlockedDueClockCorrection},{"purge_reboot",PurgeAndAtMostOnce},{"codec",CodecAndCorruption},
        {"device_nvs",DeviceNvs},{"exhaustion",ExhaustionBounds},{"timezone_dst",TimezoneAndDst},{"purge_sibling",PurgeSiblingReminder},
        {"queued_conflict",RevisionQueuedConflict},{"concurrency",Concurrency},{"mcp_schema",McpSchema},{"mcp_accepted",McpAccepted}};
    auto found=cases.find(argv[1]);assert(found!=cases.end());found->second();task_test::JoinAll();
    std::cout<<argv[1]<<" production alarm service/tools passed\n";
}
'''


@unittest.skipUnless(COMPILER and (CJSON / "cJSON.c").is_file(),
                     "requires native C++23 and managed cJSON")
class StopwatchAlarmServiceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory(prefix="stopwatch-alarm-service-")
        cls.addClassCleanup(cls.directory.cleanup)
        build = Path(cls.directory.name)
        for name, content in STUBS.items():
            path = build / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        source = build / "harness.cc"
        source.write_text(HARNESS, encoding="utf-8")
        cls.binary = build / "alarm-service.exe"
        result = subprocess.run(
            [COMPILER, "-std=c++23", "-fno-exceptions", "-fno-rtti", "-O1", "-g",
             "-Wall", "-Wextra", "-Werror", "-Wno-misleading-indentation", "-pthread",
             f"-I{build}", f"-I{BOARD}", f"-I{ROOT / 'main'}", f"-I{CJSON}",
             str(source), str(BOARD / "stopwatch_alarm_tools.cc"),
             "-x", "c++", str(CJSON / "cJSON.c"), "-o", str(cls.binary)],
            capture_output=True, text=True, encoding="utf-8", errors="replace", env=ENV,
            timeout=120)
        if result.returncode:
            raise AssertionError(result.stdout + result.stderr)
        # Also compile the enabled service as its own translation unit, as IDF
        # does. The executable includes it whole only to exercise codec internals.
        result = subprocess.run(
            [COMPILER, "-std=c++23", "-fno-exceptions", "-fno-rtti", "-Wall", "-Wextra",
             "-Werror", "-Wno-misleading-indentation", "-pthread", f"-I{build}", f"-I{BOARD}",
             "-c", str(BOARD / "stopwatch_alarm_service.cc"),
             "-o", str(build / "enabled-service.o")],
            capture_output=True, text=True, env=ENV, timeout=60)
        if result.returncode:
            raise AssertionError(result.stdout + result.stderr)
        for name in ("stopwatch_alarm_service.cc", "stopwatch_alarm_tools.cc"):
            result = subprocess.run(
                [COMPILER, "-std=c++23", "-fno-exceptions", "-fno-rtti", "-Wall", "-Wextra",
                 "-Werror", "-DCONFIG_STOPWATCH_LOCAL_APPS=0", f"-I{build}",
                 "-c", str(BOARD / name), "-o", str(build / (name + ".o"))],
                capture_output=True, text=True, env=ENV, timeout=60)
            if result.returncode:
                raise AssertionError(result.stdout + result.stderr)
        # Enabled public headers remain usable without IDF/LVGL/MCP includes.
        headers = build / "headers.cc"
        headers.write_text('#include "stopwatch_alarm_service.h"\n'
                           '#include "stopwatch_alarm_tools.h"\n', encoding="utf-8")
        result = subprocess.run(
            [COMPILER, "-std=c++23", "-Wall", "-Wextra", "-Werror", f"-I{build}",
             f"-I{BOARD}", "-c", str(headers), "-o", str(build / "headers.o")],
            capture_output=True, text=True, env=ENV, timeout=60)
        if result.returncode:
            raise AssertionError(result.stdout + result.stderr)

    def run_case(self, name):
        result = subprocess.run([str(self.binary), name], capture_output=True, text=True,
                                encoding="utf-8", errors="replace", env=ENV, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("production alarm service/tools passed", result.stdout)

    def test_nonblocking_worker_initialization_and_clock_validity(self):
        self.run_case("defaults")

    def test_strict_time_utf8_and_gregorian_date_validation(self):
        self.run_case("invalid")

    def test_capacity_stable_ids_revisions_and_result_retention(self):
        self.run_case("crud_full")

    def test_storage_failure_preserves_ram_and_reboot(self):
        self.run_case("persistence_failure")

    def test_bounded_fifo_and_nonblocking_readers_while_nvs_blocks(self):
        self.run_case("blocked_queue")

    def test_daily_once_date_and_persistent_backwards_deduplication(self):
        self.run_case("daily_once")

    def test_all_sixteen_due_in_same_minute_share_one_commit(self):
        self.run_case("same_minute")

    def test_sync_no_catchup_and_failed_trigger_retry(self):
        self.run_case("sync_failure")

    def test_clock_correction_during_blocked_due_commit(self):
        self.run_case("blocked_due_clock")

    def test_disable_delete_purge_outbox_and_commit_before_handoff(self):
        self.run_case("purge_reboot")

    def test_bounded_versioned_crc_codec_and_load_errors(self):
        self.run_case("codec")

    def test_revision_and_stable_id_exhaustion_never_wrap(self):
        self.run_case("exhaustion")

    def test_real_nvs_adapter_checked_commit_on_worker_only(self):
        self.run_case("device_nvs")

    def test_device_local_timezone_and_repeated_dst_minute(self):
        self.run_case("timezone_dst")

    def test_disabling_one_outbox_alarm_preserves_sibling_reminder(self):
        self.run_case("purge_sibling")

    def test_queued_revision_is_rechecked_after_previous_commit(self):
        self.run_case("queued_conflict")

    def test_concurrent_crud_snapshot_and_poll(self):
        self.run_case("concurrency")

    def test_ordinary_mcp_schema_and_strict_parameters(self):
        self.run_case("mcp_schema")

    def test_mcp_accepted_pending_committed_and_failure_are_distinct(self):
        self.run_case("mcp_accepted")


if __name__ == "__main__":
    unittest.main()
