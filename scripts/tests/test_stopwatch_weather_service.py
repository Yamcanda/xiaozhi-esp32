"""Native tests compile the complete production weather service and codecs.

No source extraction, network calls, downloaded libraries or SDK/build writes.
Platform seams replace only board I/O; cJSON is the repository's actual source.
"""
import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
BOARD = ROOT / "main/boards/m5stack/stopwatch"
CJSON = ROOT / "managed_components/espressif__cjson/cJSON"
MINGW = pathlib.Path("D:/sdk/mingw64/bin/g++.exe")
COMPILER = str(MINGW) if MINGW.is_file() else shutil.which("g++")
ENV = os.environ.copy()
if COMPILER:
    ENV["PATH"] = str(pathlib.Path(COMPILER).parent) + os.pathsep + ENV.get("PATH", "")

STUBS = {
    "sdkconfig.h": "#pragma once\n#ifndef CONFIG_STOPWATCH_LOCAL_APPS\n#define CONFIG_STOPWATCH_LOCAL_APPS 1\n#endif\n",
    "esp_timer.h": "#pragma once\n#include <cstdint>\nnamespace clock_test { inline int64_t microseconds=10000000; }\ninline int64_t esp_timer_get_time(){return clock_test::microseconds;}\n",
    "esp_random.h": "#pragma once\n#include <cstdint>\ninline uint32_t esp_random(){return 12345;}\n",
    "wifi_manager.h": "#pragma once\nclass WifiManager{public: static WifiManager& GetInstance(){static WifiManager w;return w;} bool IsConnected(){return false;}};\n",
    "freertos/FreeRTOS.h": "#pragma once\n#include <cstdint>\nusing TaskHandle_t=void*;\n#define pdPASS 1\n#define pdTRUE 1\n#define pdMS_TO_TICKS(x) (x)\n",
    "freertos/task.h": "#pragma once\n#include \"FreeRTOS.h\"\ninline int xTaskCreate(void(*)(void*),const char*,int,void*,int,TaskHandle_t*){return 0;}\ninline void xTaskNotifyGive(TaskHandle_t){}\ninline void ulTaskNotifyTake(int,uint32_t){}\ninline void vTaskDelay(uint32_t){}\ninline void vTaskDelete(void*){}\n",
    "nvs.h": r'''
#pragma once
#include <cstring>
#include <map>
#include <string>
using nvs_handle_t = unsigned;
constexpr int ESP_OK=0, NVS_READONLY=0, NVS_READWRITE=1;
namespace nvs_test { inline std::map<std::string,std::string> blobs; inline bool fail_set=false, fail_commit=false; inline int writes=0; }
inline int nvs_open(const char* ns,int,nvs_handle_t* h){if(std::string(ns)!="sw_weather")return -1;*h=1;return 0;}
inline void nvs_close(nvs_handle_t){}
inline int nvs_get_blob(nvs_handle_t,const char* key,void* dst,size_t* len){auto i=nvs_test::blobs.find(key);if(i==nvs_test::blobs.end())return -1;if(!dst){*len=i->second.size();return 0;}if(*len<i->second.size())return -1;std::memcpy(dst,i->second.data(),i->second.size());*len=i->second.size();return 0;}
inline int nvs_set_blob(nvs_handle_t,const char* key,const void* p,size_t len){++nvs_test::writes;if(nvs_test::fail_set)return -1;nvs_test::blobs[key]=std::string(static_cast<const char*>(p),len);return 0;}
inline int nvs_commit(nvs_handle_t){return nvs_test::fail_commit?-1:0;}
''',
    "board.h": r'''
#pragma once
#include <algorithm>
#include <cstdlib>
#include <cstring>
#include <functional>
#include <memory>
#include <optional>
#include <string>
#include "esp_timer.h"
namespace http_test {
inline std::string body="{}", type="application/json", encoding, retry;
inline size_t declared=0, chunk=173, offset=0;
inline int status=200, opens=0, reads=0, closes=0, timeout=0;
inline bool open_ok=true, read_error=false, no_status=false;
inline int64_t read_advance_us=0;
inline std::function<void()> after_read;
inline void Reset(){body="{}";type="application/json";encoding.clear();retry.clear();declared=0;chunk=173;offset=0;status=200;opens=reads=closes=timeout=0;open_ok=true;read_error=no_status=false;read_advance_us=0;after_read={};}
}
class Http {public:
void SetTimeout(int ms){http_test::timeout=ms;}void SetKeepAlive(bool enabled){if(enabled)std::abort();}
void SetHeader(const std::string& key,const std::string& value){if(key=="Accept-Encoding"&&value!="identity")std::abort();}
bool Open(const std::string& method,const std::string& url){++http_test::opens;http_test::offset=0;return method=="GET"&&url.rfind("https://",0)==0&&http_test::open_ok;}
std::optional<int> GetStatusCode(){return http_test::no_status?std::optional<int>{}:http_test::status;}
std::string GetResponseHeader(const std::string& key)const{if(key=="content-type")return http_test::type;if(key=="content-encoding")return http_test::encoding;if(key=="retry-after")return http_test::retry;return {};}
size_t GetBodyLength(){return http_test::declared;}
std::optional<int> Read(char* buffer,size_t size){++http_test::reads;clock_test::microseconds+=http_test::read_advance_us;if(http_test::after_read)http_test::after_read();if(http_test::read_error)return {};auto count=std::min(size,std::min(http_test::chunk,http_test::body.size()-http_test::offset));std::memcpy(buffer,http_test::body.data()+http_test::offset,count);http_test::offset+=count;return static_cast<int>(count);}
void Close(){++http_test::closes;}
};
class NetworkInterface {public:std::unique_ptr<Http> CreateHttp(int){return std::make_unique<Http>();}};
class Board {public:static Board& GetInstance(){static Board b;return b;}NetworkInterface* GetNetwork(){return &net;}private:NetworkInterface net;};
''',
}

HARNESS = r'''
#include "stopwatch_weather_service.h"
#include "stopwatch_weather_codec.h"
#include "nvs.h"
#include "stopwatch_weather_service.cc"
#include <cJSON.h>
#include <algorithm>
#include <atomic>
#include <cassert>
#include <chrono>
#include <cmath>
#include <condition_variable>
#include <functional>
#include <iostream>
#include <map>
#include <mutex>
#include <thread>
#include <vector>
using namespace stopwatch_weather;
#undef assert
#define assert(condition) do { if (!(condition)) { std::fprintf(stderr,"assertion failed: %s at %s:%d\\n",#condition,__FILE__,__LINE__); std::_Exit(1); } } while(0)
namespace c = stopwatch_weather::codec;
constexpr int64_t T = 1770001200; // Deliberately not midnight.
std::string Ip(const char* city="Shenzhen", double lat=22.5431,double lon=114.0579){
    return std::string("{\"success\":true,\"city\":\"")+city+"\",\"country\":\"China\",\"country_code\":\"CN\",\"region\":\"Guangdong\",\"latitude\":"+std::to_string(lat)+",\"longitude\":"+std::to_string(lon)+"}";
}
std::string Search(const char* name="Shenzhen",int id=1795565){
    return std::string("{\"results\":[{\"id\":")+std::to_string(id)+",\"name\":\""+name+"\",\"latitude\":22.5431,\"longitude\":114.0579,\"admin1\":\"Guangdong\",\"country\":\"China\",\"country_code\":\"CN\"}]}";
}
std::string Weather(int64_t at=T){
    std::string s=R"({"latitude":22.54,"longitude":114.06,"utc_offset_seconds":28800,"current_units":{"time":"unixtime","temperature_2m":"°C","apparent_temperature":"°C","relative_humidity_2m":"%","wind_speed_10m":"km/h","weather_code":"wmo code"},"current":{"time":TIME,"temperature_2m":-8.5,"apparent_temperature":null,"relative_humidity_2m":null,"wind_speed_10m":8,"weather_code":97,"is_day":0},"hourly_units":{"time":"unixtime","temperature_2m":"°C","precipitation_probability":"%","weather_code":"wmo code"},"hourly":{"time":HOURS,"temperature_2m":[1,2,3,4,5,6,7,8],"precipitation_probability":[null,0,10,20,30,40,50,60],"weather_code":[0,1,2,3,45,61,71,95]},"daily_units":{"time":"unixtime","temperature_2m_min":"°C","temperature_2m_max":"°C","precipitation_probability_max":"%","weather_code":"wmo code"},"daily":{"time":DAYS,"temperature_2m_min":[-10,-11,-12],"temperature_2m_max":[5,6,7],"precipitation_probability_max":[null,0,50],"weather_code":[3,71,95]}})";
    auto replace=[&](const std::string& x,const std::string& y){auto p=s.find(x);assert(p!=std::string::npos);s.replace(p,x.size(),y);};
    replace("TIME",std::to_string(at));
    std::string a="[";
    for(int i=0;i<8;++i){if(i)a+=",";a+=std::to_string(at+(i-2)*3600);}
    replace("HOURS",a+"]");
    auto midnight=((at+28800)/86400)*86400-28800;
    replace("DAYS","["+std::to_string(midnight)+","+std::to_string(midnight+86400)+","+std::to_string(midnight+2*86400)+"]");
    return s;
}
void Replace(std::string& s,const std::string& old,const std::string& value){auto p=s.find(old);assert(p!=std::string::npos);s.replace(p,old.size(),value);}
bool Until(const std::function<bool()>& pred){for(int i=0;i<3000;++i){if(pred())return true;std::this_thread::sleep_for(std::chrono::milliseconds(1));}return false;}
class Fake final: public Platform {
public:
    std::atomic<uint64_t> now{10000};
    std::atomic<bool> connected{true}, fail_start{false}, fail_write{false};
    std::atomic<int> writes{0}, starts{0}, calls{0}, active{0}, max_active{0}, waits{0};
    std::mutex m;std::condition_variable cv;std::thread worker;
    std::map<std::string,std::string> blobs;
    std::vector<std::string> urls;
    std::function<HttpReply(const std::string&,const std::function<bool()>&)> responder;
    uint64_t NowMs()const override{return now.load();}
    uint32_t Random32()const override{static std::atomic<uint32_t> r{123};return ++r;}
    bool Connected()const override{return connected.load();}
    HttpReply Get(const std::string& url,size_t limit,const std::function<bool()>& cancel)override{
        assert(std::this_thread::get_id()==worker.get_id());assert(limit==4096||limit==16384);
        int n=++active;assert(n==1);max_active.store(std::max(max_active.load(),n));++calls;
        {std::lock_guard<std::mutex> l(m);urls.push_back(url);}
        auto r=responder?responder(url,cancel):HttpReply{true,200,url.find("ipwho")!=std::string::npos?Ip():(url.find("geocoding")!=std::string::npos?Search():Weather()),"",""};
        --active;return r;
    }
    bool ReadBlob(const char* key,std::string& bytes,size_t limit)override{std::lock_guard<std::mutex> l(m);auto i=blobs.find(key);if(i==blobs.end()||i->second.size()>limit)return false;bytes=i->second;return true;}
    bool WriteBlob(const char* key,const std::string& bytes)override{assert(bytes.size()<=2048);++writes;if(fail_write)return false;std::lock_guard<std::mutex> l(m);blobs[key]=bytes;return true;}
    bool StartWorker(void(*entry)(void*),void* arg)override{++starts;if(fail_start)return false;assert(!worker.joinable());worker=std::thread([=]{entry(arg);});return true;}
    void WakeWorker()override{cv.notify_all();}
    void WaitWorker(uint32_t)override{++waits;std::unique_lock<std::mutex> l(m);cv.wait_for(l,std::chrono::milliseconds(2));}
    void JoinWorker()override{if(worker.joinable())worker.join();}
    void Advance(uint64_t ms){now+=ms;WakeWorker();}
    ~Fake(){assert(!worker.joinable());}
};

void Parsers(){
    Location loc;std::string error;
    assert(c::ParseIp(Ip(),loc,error));assert(loc.display_name=="深圳");assert(loc.id.rfind("ip:",0)==0);
    assert(!c::ParseIp("{\"success\":false}",loc,error));
    auto bad=Ip();Replace(bad,"22.543100","91");assert(!c::ParseIp(bad,loc,error));
    assert(!c::ParseIp(Ip()+" trailing",loc,error));
    bad=Ip();bad.insert(1,"\"success\":true,");assert(!c::ParseIp(bad,loc,error));
    bad=Ip();Replace(bad,"Shenzhen",std::string("\xc0\xaf",2));assert(!c::ParseIp(bad,loc,error));
    bad=Ip();Replace(bad,"Shenzhen","a\\u0000b");assert(!c::ParseIp(bad,loc,error));
    bad=Ip();bad.push_back('\0');assert(!c::ParseIp(bad,loc,error));
    std::array<Candidate,5> candidates;size_t count=0;
    assert(c::ParseSearch(Search(),"shenzhen",candidates,count,error)&&count==1);
    assert(candidates[0].location.id=="geonames:1795565");
    assert(c::ParseSearch("{}","none",candidates,count,error)&&count==0);
    bad=Search();Replace(bad,"Shenzhen","城市");assert(c::ParseSearch(bad,"chengshi",candidates,count,error));
    assert(candidates[0].location.display_name.find("chengshi CN geonames:")==0);
    bad=Search();Replace(bad,"Shenzhen",std::string(513,'x'));assert(!c::ParseSearch(bad,"x",candidates,count,error));
    bad="{\"a\":"+std::string(9,'[')+"0"+std::string(9,']')+"}";assert(!c::ParseSearch(bad,"x",candidates,count,error));
    bad="{\"a\":[";for(int i=0;i<1100;++i){if(i)bad+=",";bad+="0";}bad+="]}";assert(!c::ParseSearch(bad,"x",candidates,count,error));
    auto city=BuiltinLocation(3);Forecast f;
    assert(c::ParseForecast(Weather(),city,f,error));
    assert(f.temperature==-8.5f&&std::isnan(f.feels_like)&&std::isnan(f.humidity));
    assert(f.hours[0].time==T+3600&&f.hours[0].temperature==4&&f.hours[3].time==T+4*3600);
    assert(std::isnan(f.days[0].precipitation)&&f.days[1].precipitation==0);
    assert(f.code==97&&WeatherCondition(97)==Condition::Thunderstorm);
    for(auto pair:std::vector<std::pair<std::string,std::string>>{{"\"temperature_2m\":-8.5","\"temperature_2m\":null"},{"\"temperature_2m\":-8.5","\"temperature_2m\":1e999"},{"\"temperature_2m\":-8.5","\"temperature_2m\":71"},{"\"wind_speed_10m\":\"km/h\"","\"wind_speed_10m\":\"mph\""},{"\"is_day\":0","\"is_day\":2"},{"[1,2,3,4,5,6,7,8]","[1,2]"}}){bad=Weather();Replace(bad,pair.first,pair.second);assert(!c::ParseForecast(bad,city,f,error));}
    bad=Weather();Replace(bad,"\"latitude\":22.54","\"latitude\":null");assert(!c::ParseForecast(bad,city,f,error));
    bad=Weather();Replace(bad,"\"longitude\":114.06","\"longitude\":181");assert(!c::ParseForecast(bad,city,f,error));
    bad=Weather();Replace(bad,"\"longitude\":114.06","\"longitude\":113.99");assert(c::ParseForecast(bad,city,f,error));
    bad=Weather();Replace(bad,std::to_string(T+3600),std::to_string(T));assert(!c::ParseForecast(bad,city,f,error));
    assert(WeatherCondition(0)==Condition::Clear&&WeatherCondition(61)==Condition::Rain&&WeatherCondition(85)==Condition::Snow&&WeatherCondition(95)==Condition::Thunderstorm&&WeatherCondition(123)==Condition::Unknown);
    assert(std::string(ConditionText(0,false))=="晴夜");
    assert(FormatUtcOffset(19800)=="UTC+05:30"&&FormatUtcOffset(20700)=="UTC+05:45"&&FormatUtcOffset(-12600)=="UTC-03:30");
    assert(FormatLocalTime(1704067200,20700,"%Y-%m-%d %H:%M")=="2024-01-01 05:45");
    assert(FormatLocalTime(1704067200,-12600,"%Y-%m-%d %H:%M")=="2023-12-31 20:30");
    assert(c::ValidQuery("Xi'an, CN","cn")&&!c::ValidQuery("","CN")&&!c::ValidQuery("x","CHN")&&!c::ValidQuery(std::string(97,'a'),""));
    assert(c::EncodeQuery("a b&中")=="a%20b%26%E4%B8%AD");
    assert(c::SearchUrl(" Paris, France ","").find("name=Paris&count=5")!=std::string::npos);
    assert(c::SearchUrl("Paris, FR","").find("countryCode=FR")!=std::string::npos);
    assert(!c::ValidQuery("Paris,,France","")&&!c::ValidQuery("Paris,","")&&!c::ValidQuery("Paris,one,two,three",""));
    auto paris=Search("Paris",2988507);Replace(paris,"Guangdong","Ile-de-France");Replace(paris,"China","France");Replace(paris,"CN","FR");
    assert(c::ParseSearch(paris,"Paris, France",candidates,count,error)&&count==1);
    assert(c::ParseSearch(paris,"Paris, Ile-de-France, FR",candidates,count,error)&&count==1);
    assert(c::ParseSearch(paris,"Paris, Texas",candidates,count,error)&&count==0);
    assert(c::ParseSearch(paris,"Paris, France",candidates,count,error,"US")&&count==0);
    assert(c::ForecastUrl(city).find("forecast_days=3&forecast_hours=8")!=std::string::npos);
    assert(c::RetryAfterMs("120",0)==120000&&c::RetryAfterMs("garbage",0)==0&&c::RetryAfterMs("9999999999999",0)==0);
    assert(c::RetryAfterMs("Mon, 01 Jan 2024 00:02:00 GMT",1704067200)==120000);
    std::cout<<"parsers/time/units/UTF8/bounds OK\n";
}
void Blobs(){
    c::Config config;config.automatic=false;config.revision=5;config.location=BuiltinLocation(3);config.recent_count=3;for(int i=0;i<3;++i)config.recent[i]=BuiltinLocation(i);
    std::string bytes;assert(c::EncodeConfig(config,bytes));assert(bytes.size()<1536);
    c::Config decoded;assert(c::DecodeConfig(bytes,decoded)&&decoded.revision==5&&SameLocation(decoded.location,config.location));
    auto corrupt=bytes;corrupt[10]^=1;assert(!c::DecodeConfig(corrupt,decoded));corrupt=bytes;corrupt[5]=8;assert(!c::DecodeConfig(corrupt,decoded));assert(!c::DecodeConfig(bytes+"x",decoded));assert(!c::DecodeConfig(std::string(2049,'x'),decoded));
    c::Cache cache;cache.automatic=false;cache.config_revision=5;std::string error;assert(c::ParseForecast(Weather(),config.location,cache.forecast,error));
    assert(c::EncodeCache(cache,bytes)&&bytes.size()<1536);c::Cache out;assert(c::DecodeCache(bytes,out)&&out.forecast.code==97&&std::isnan(out.forecast.humidity));
    corrupt=bytes;corrupt.back()^=1;assert(!c::DecodeCache(corrupt,out));
    std::cout<<"compact versioned checked blobs OK (cache "<<bytes.size()<<" bytes)\n";
}
void DefaultsCommits(){
    Fake io;io.fail_start=true;
    {
        StopwatchWeatherService svc(io);assert(io.starts==0&&io.calls==0);auto s=svc.GetSnapshot();assert(s.automatic&&!s.has_data&&s.config_revision==0);
        auto r=svc.SearchCity("Shenzhen");assert(!r.ok&&r.error=="worker_unavailable");
        auto loc=BuiltinLocation(3);io.fail_write=true;r=svc.SetLocation(false,loc,0);assert(!r.ok&&r.error=="storage_failed"&&svc.GetSnapshot().automatic);
        io.fail_write=false;r=svc.SetLocation(false,loc,0);assert(r.ok&&!r.pending&&r.error=="worker_unavailable"&&r.config_revision==1);
        int writes=io.writes;r=svc.SetLocation(false,loc,1);assert(r.ok&&io.writes==writes);
        assert(!svc.SetLocation(false,BuiltinLocation(1),0).ok);auto arbitrary=loc;arbitrary.latitude+=1;assert(!svc.SetLocation(false,arbitrary,1).ok);
        assert(!svc.SetLocationToken("manual","made-up").ok);assert(svc.GetSnapshot().location.display_name=="深圳");
    }
    {
        StopwatchWeatherService svc(io);auto s=svc.GetSnapshot();assert(!s.automatic&&s.config_revision==1&&s.recent_count==1);
        auto r=svc.SetLocationToken("auto");assert(r.ok&&svc.GetSnapshot().automatic&&r.config_revision==2);
    }
    io.blobs["config_v1"]="corrupt";{StopwatchWeatherService svc(io);assert(svc.GetSnapshot().automatic&&svc.GetSnapshot().config_revision==0);}
    // Production checked NVS (not a Settings wrapper): failure must not publish.
    nvs_test::blobs.clear();nvs_test::fail_commit=true;{StopwatchWeatherService svc;assert(!svc.SetLocation(false,BuiltinLocation(3),0).ok&&svc.GetSnapshot().automatic);}nvs_test::fail_commit=false;nvs_test::blobs.clear();
    std::cout<<"default/lazy startup/checked commits/restart/conflict OK\n";
}
void WorkerCache(){
    Fake io;
    {
        StopwatchWeatherService svc(io);svc.SetVisible(true);assert(Until([&]{return io.waits>0;}));io.Advance(2001);
        assert(Until([&]{return svc.GetSnapshot().has_data;}));auto s=svc.GetSnapshot();assert(s.automatic&&s.age_known&&!s.stale&&!s.cached&&s.location.display_name=="深圳");assert(io.calls==2&&io.max_active==1);
        assert(Until([&]{return io.writes==1;}));int calls=io.calls;for(int i=0;i<5;++i){svc.SetVisible(false);svc.SetVisible(true);svc.RequestRefresh();}std::this_thread::sleep_for(std::chrono::milliseconds(20));assert(io.calls==calls);svc.SetVisible(false);io.Advance(16*60000);std::this_thread::sleep_for(std::chrono::milliseconds(20));assert(io.calls==calls&&svc.GetSnapshot().stale);
        svc.SetVisible(true);assert(Until([&]{return io.calls==calls+1;}));assert(Until([&]{return svc.GetSnapshot().state==State::Ready;}));assert(io.writes==1); // one-hour write gate
        svc.SetVisible(false);io.Advance(25*60*60000ULL);assert(!svc.GetSnapshot().has_data);
    }
    {
        StopwatchWeatherService svc(io);auto s=svc.GetSnapshot();assert(s.has_data&&s.cached&&s.stale&&!s.age_known&&s.location.display_name=="深圳");assert(io.starts==1); // ctor never starts another task
    }
    std::cout<<"single worker/visible TTL/history/24h/write throttling OK\n";
}
void EffectiveCurrentCommits(){
    Fake io;
    io.responder=[](const std::string& url,const std::function<bool()>&){
        return HttpReply{true,200,url.find("ipwho")!=std::string::npos?Ip("Dongguan",23.0207,113.7518):Weather(),"",""};
    };
    StopwatchWeatherService svc(io);
    svc.SetVisible(true);assert(Until([&]{return io.waits>0;}));io.Advance(2001);
    assert(Until([&]{return svc.GetSnapshot().has_data;}));
    svc.SetVisible(false);io.connected=false;
    int waits=io.waits;assert(Until([&]{return io.waits>waits;})); // cache write has released storage lock
    auto current=svc.GetSnapshot();
    assert(current.automatic&&current.config_revision==0&&current.recent_count==0);
    assert(current.location.city=="Dongguan"&&current.location.id.rfind("ip:",0)==0&&io.calls==2);
    for(int i=0;i<kCityCount;++i)assert(!SameLocation(current.location,BuiltinLocation(i)));
    const int writes=io.writes;
    auto reject=[&](const Location& location){
        assert(ValidLocation(location));
        auto r=svc.SetLocation(false,location,current.config_revision);
        assert(!r.ok&&r.error=="invalid_location"&&r.config_revision==current.config_revision);
        auto after=svc.GetSnapshot();
        assert(after.automatic&&after.recent_count==0&&ExactLocation(after.location,current.location));
        assert(io.writes==writes);
    };
    std::array<Location,8> tampered;tampered.fill(current.location);
    tampered[0].city+=" changed";
    tampered[1].display_name+=" changed";
    tampered[2].region+=" changed";
    tampered[3].country+=" changed";
    tampered[4].country_code="US";
    tampered[5].latitude+=1;
    tampered[6].longitude+=1;
    tampered[7].id+="-changed";
    for(const auto& location:tampered)reject(location);
    auto arbitrary=current.location;arbitrary.id="ip:1.0000,2.0000";arbitrary.latitude=1;arbitrary.longitude=2;
    reject(arbitrary);
    auto token=svc.SetLocationToken("manual",current.location.id);
    assert(!token.ok&&token.error=="invalid_selection");
    token=svc.SetLocationToken("manual");assert(!token.ok&&token.error=="invalid_selection"&&io.writes==writes);

    io.fail_write=true;
    auto failed=svc.SetLocation(false,current.location,current.config_revision);
    assert(!failed.ok&&failed.error=="storage_failed"&&failed.config_revision==current.config_revision);
    auto unchanged=svc.GetSnapshot();
    assert(unchanged.automatic&&unchanged.config_revision==current.config_revision&&unchanged.recent_count==0);
    assert(ExactLocation(unchanged.location,current.location)&&unchanged.has_data);
    assert(ExactLocation(unchanged.forecast.location,current.forecast.location)&&unchanged.forecast.temperature==current.forecast.temperature);
    std::string bytes;assert(!io.ReadBlob("config_v1",bytes,c::kBlobLimit)&&io.writes==writes+1);
    io.fail_write=false;
    auto committed=svc.SetLocation(false,current.location,current.config_revision);
    assert(committed.ok&&committed.config_revision==1&&io.writes==writes+2);
    auto manual=svc.GetSnapshot();
    assert(!manual.automatic&&manual.config_revision==1&&ExactLocation(manual.location,current.location));
    assert(manual.recent_count==1&&ExactLocation(manual.recent[0],current.location));
    c::Config persisted;assert(io.ReadBlob("config_v1",bytes,c::kBlobLimit)&&c::DecodeConfig(bytes,persisted));
    assert(!persisted.automatic&&persisted.revision==1&&ExactLocation(persisted.location,current.location));
    assert(persisted.recent_count==1&&ExactLocation(persisted.recent[0],current.location));
    auto stale=svc.SetLocation(false,current.location,current.config_revision);
    assert(!stale.ok&&stale.error=="conflict"&&stale.config_revision==1&&io.writes==writes+2);
    assert(svc.SetLocation(false,current.location,1).ok&&io.writes==writes+2); // unchanged selection is a no-op
    assert(svc.SetLocation(false,BuiltinLocation(0),1).ok);
    assert(!SameLocation(svc.GetSnapshot().location,current.location));
    // No longer effective-current, but the independently authorized recent entry remains selectable.
    auto recent=svc.SetLocation(false,current.location,2);assert(recent.ok&&recent.config_revision==3);
    std::cout<<"exact effective-auto selection/tampering/token isolation/checked commit/revision/recent OK\n";
}
void EffectiveCurrentChanges(){
    Fake io;std::atomic<bool> moved=false;
    io.responder=[&](const std::string& url,const std::function<bool()>&){
        auto ip=moved.load()?Ip("Foshan",23.0215,113.1214):Ip("Dongguan",23.0207,113.7518);
        return HttpReply{true,200,url.find("ipwho")!=std::string::npos?ip:Weather(),"",""};
    };
    StopwatchWeatherService svc(io);
    svc.SetVisible(true);assert(Until([&]{return io.waits>0;}));io.Advance(2001);
    assert(Until([&]{return svc.GetSnapshot().has_data;}));
    auto old=svc.GetSnapshot();
    int waits=io.waits;assert(Until([&]{return io.waits>waits;}));
    moved=true;io.Advance(6*60*60000ULL+1);
    assert(Until([&]{auto s=svc.GetSnapshot();return s.location.city=="Foshan"&&s.has_data&&!s.pending&&s.state==State::Ready;}));
    svc.SetVisible(false);io.connected=false;
    waits=io.waits;assert(Until([&]{return io.waits>waits;}));
    auto current=svc.GetSnapshot();
    assert(current.automatic&&current.config_revision==old.config_revision&&current.recent_count==0&&io.calls==4);
    assert(!SameLocation(current.location,old.location));
    int writes=io.writes;
    auto obsolete=svc.SetLocation(false,old.location,current.config_revision);
    assert(!obsolete.ok&&obsolete.error=="invalid_location"&&io.writes==writes);
    assert(ExactLocation(svc.GetSnapshot().location,current.location));
    auto changed=svc.SetLocation(false,BuiltinLocation(0),current.config_revision);
    assert(changed.ok&&changed.config_revision==1);
    writes=io.writes;
    for(const auto& location:{old.location,current.location}){
        auto rejected=svc.SetLocation(false,location,changed.config_revision);
        assert(!rejected.ok&&rejected.error=="invalid_location"&&rejected.config_revision==1&&io.writes==writes);
    }
    assert(ExactLocation(svc.GetSnapshot().location,BuiltinLocation(0))&&svc.GetSnapshot().recent_count==1);
    std::cout<<"effective IP change/config change revoke uncommitted old locations OK\n";
}
void SearchTokens(){
    Fake io;
    {
        StopwatchWeatherService svc(io);auto r=svc.SearchCity("shenzhen","cn");assert(r.ok&&r.pending&&r.request_id);assert(Until([&]{return svc.GetSearch(r.request_id).state==SearchState::Ready;}));
        auto found=svc.GetSearch(r.request_id);assert(found.count==1&&found.config_revision==0&&!found.candidates[0].token.empty());
        auto again=svc.SearchCity("shenzhen","CN");assert(again.ok&&again.request_id==r.request_id&&io.calls==1);
        auto commit=svc.SetLocationToken("manual",found.candidates[0].token);assert(commit.ok&&commit.config_revision==1&&!svc.GetSnapshot().automatic);
        assert(!svc.SetLocationToken("manual",found.candidates[0].token).ok);assert(svc.GetSearch(r.request_id).state==SearchState::Superseded);
        auto next=svc.SearchCity("beijing");assert(next.ok);assert(Until([&]{return svc.GetSearch(next.request_id).state==SearchState::Ready;}));auto old=svc.GetSearch(next.request_id);io.Advance(10*60000+1);assert(svc.SearchVersion(next.request_id)!=old.version);assert(svc.GetSearch(next.request_id).state==SearchState::Expired);assert(!svc.SetLocationToken("manual",old.candidates[0].token).ok);
        auto cancel=svc.SearchCity("cancel");assert(cancel.ok);svc.CancelSearch(cancel.request_id);assert(svc.GetSearch(cancel.request_id).state==SearchState::Superseded);
    }
    std::cout<<"search cache/token/revision/cancel/expiry OK\n";
}
void QueuesAndBlocked(){
    Fake io;std::atomic<bool> entered=false,release=false;
    io.responder=[&](const std::string& url,const std::function<bool()>& cancelled){entered=true;while(!release)std::this_thread::sleep_for(std::chrono::milliseconds(1));return HttpReply{true,200,url.find("ipwho")!=std::string::npos?Ip():Search(),"",""};};
    {
        StopwatchWeatherService svc(io);auto a=svc.SearchCity("a");assert(a.ok&&Until([&]{return entered.load();}));auto b=svc.SearchCity("b");assert(b.ok);assert(!svc.SearchCity("c").ok);
        auto start=std::chrono::steady_clock::now();auto r=svc.SetLocation(false,BuiltinLocation(3),0);assert(r.ok);assert(std::chrono::steady_clock::now()-start<std::chrono::milliseconds(100));
        assert(svc.GetSearch(a.request_id).state==SearchState::Superseded);release=true;
        assert(Until([&]{return io.active==0;}));assert(!svc.GetSnapshot().has_data);
    }
    // Active old-auto IP response must never replace a newer manual location.
    Fake old;entered=false;release=false;old.responder=[&](const std::string&,const std::function<bool()>&){entered=true;while(!release)std::this_thread::sleep_for(std::chrono::milliseconds(1));return HttpReply{true,200,Ip(),"",""};};
    {StopwatchWeatherService svc(old);svc.SetVisible(true);assert(Until([&]{return old.waits>0;}));old.Advance(2001);assert(Until([&]{return entered.load();}));assert(svc.SetLocation(false,BuiltinLocation(0),0).ok);svc.SetVisible(false);release=true;assert(Until([&]{return old.active==0;}));assert(svc.GetSnapshot().location.display_name=="北京"&&!svc.GetSnapshot().has_data);}
    std::cout<<"bounded queue/nonblocking setter/old-result rejection OK\n";
}
void RateLimits(){
    Fake io;io.responder=[](const std::string&,const std::function<bool()>&){return HttpReply{false,429,"","120","http_429"};};
    {StopwatchWeatherService svc(io);svc.SetVisible(true);assert(Until([&]{return io.waits>0;}));io.Advance(2001);assert(Until([&]{return io.calls==1&&svc.GetSnapshot().state==State::Error;}));int calls=io.calls;for(int i=0;i<10;++i){svc.SetVisible(false);svc.SetVisible(true);svc.RequestRefresh();}io.Advance(119000);std::this_thread::sleep_for(std::chrono::milliseconds(20));assert(io.calls==calls);io.Advance(2000);assert(Until([&]{return io.calls==2;}));svc.SetVisible(false);}
    Fake search;search.responder=[](const std::string&,const std::function<bool()>&){return HttpReply{false,429,"","invalid","http_429"};};
    {StopwatchWeatherService svc(search);auto r=svc.SearchCity("one");assert(r.ok&&Until([&]{return svc.GetSearch(r.request_id).state==SearchState::Error;}));auto next=svc.SearchCity("two");assert(!next.ok&&next.error=="rate_limited");search.Advance(3600001);assert(svc.SearchCity("two").ok);}
    Fake offline;offline.connected=false;{StopwatchWeatherService svc(offline);svc.SetVisible(true);assert(Until([&]{return svc.GetSnapshot().state==State::Offline;}));assert(offline.calls==0);offline.connected=true;offline.WakeWorker();std::this_thread::sleep_for(std::chrono::milliseconds(10));offline.Advance(2001);assert(Until([&]{return svc.GetSnapshot().has_data;}));}
    std::cout<<"429/backoff/reopen/offline/reconnect OK\n";
}
void Pending429AndCommitFailure(){
    Fake io;std::atomic<bool> entered=false,release=false;
    io.responder=[&](const std::string&,const std::function<bool()>&){entered=true;while(!release)std::this_thread::sleep_for(std::chrono::milliseconds(1));return HttpReply{false,429,"","120","http_429"};};
    {StopwatchWeatherService svc(io);auto a=svc.SearchCity("first");assert(a.ok&&Until([&]{return entered.load();}));auto b=svc.SearchCity("queued");assert(b.ok);release=true;assert(Until([&]{return svc.GetSearch(b.request_id).state==SearchState::Error;}));assert(svc.GetSearch(b.request_id).error=="rate_limited"&&io.calls==1);}
    Fake fail;entered=false;release=false;
    fail.responder=[&](const std::string& url,const std::function<bool()>& cancel){if(!entered.exchange(true)){while(!release)std::this_thread::sleep_for(std::chrono::milliseconds(1));}return HttpReply{true,200,url.find("ipwho")!=std::string::npos?Ip():Weather(),"",""};};
    {StopwatchWeatherService svc(fail);svc.SetVisible(true);assert(Until([&]{return fail.waits>0;}));fail.Advance(2001);assert(Until([&]{return entered.load();}));fail.fail_write=true;auto r=svc.SetLocation(false,BuiltinLocation(0),0);assert(!r.ok&&svc.GetSnapshot().automatic&&!svc.GetSnapshot().pending);fail.fail_write=false;release=true;fail.Advance(61000);assert(Until([&]{return svc.GetSnapshot().has_data;}));assert(svc.GetSnapshot().location.display_name=="深圳"&&svc.GetSnapshot().state==State::Ready);}
    std::cout<<"queued 429 rejection/failed-commit old-demand recovery OK\n";
}
void PersistenceLimits(){
    Fake io;std::atomic<int> temperature{10};
    io.responder=[&](const std::string& url,const std::function<bool()>&){auto body=url.find("ipwho")!=std::string::npos?Ip():Weather();if(url.find("forecast?")!=std::string::npos)Replace(body,"\"temperature_2m\":-8.5","\"temperature_2m\":"+std::to_string(temperature.load()));return HttpReply{true,200,body,"",""};};
    {StopwatchWeatherService svc(io);svc.SetVisible(true);assert(Until([&]{return io.waits>0;}));io.Advance(2001);assert(Until([&]{return svc.GetSnapshot().has_data&&io.writes==1;}));
        ++temperature;io.Advance(16*60000);assert(Until([&]{return svc.GetSnapshot().forecast.temperature==11;}));assert(io.writes==1);
        ++temperature;io.Advance(45*60000);assert(Until([&]{return svc.GetSnapshot().forecast.temperature==12&&io.writes==2;}));
        auto first=svc.SetLocation(false,BuiltinLocation(0),0);assert(first.ok);assert(Until([&]{return svc.GetSnapshot().has_data;}));assert(io.writes==3); // only config; global cache gate
        ++temperature;io.Advance(16*60000);assert(Until([&]{return svc.GetSnapshot().forecast.temperature==13&&io.writes==4;}));
        auto second=svc.SetLocation(false,BuiltinLocation(1),1);assert(second.ok);assert(Until([&]{return svc.GetSnapshot().has_data;}));assert(io.writes==5);
        ++temperature;io.Advance(16*60000);assert(Until([&]{return svc.GetSnapshot().forecast.temperature==14&&io.writes==6;}));
        // Returning to Beijing still honors its per-city 60-minute gate.
        assert(svc.SetLocation(false,BuiltinLocation(0),2).ok);assert(Until([&]{return svc.GetSnapshot().has_data;}));assert(io.writes==7);
        svc.SetVisible(false);
    }
    Fake failure;failure.fail_write=true;{StopwatchWeatherService svc(failure);svc.SetVisible(true);assert(Until([&]{return failure.waits>0;}));failure.Advance(2001);assert(Until([&]{return svc.GetSnapshot().has_data&&failure.writes==1;}));assert(svc.GetSnapshot().state==State::Ready&&failure.blobs.empty());}
    std::cout<<"changed-cache same-city/global/return-city gates and RAM fallback OK\n";
}
void DeviceHttpAdapter(){
    DevicePlatform io;
    auto get=[&](size_t limit=4096){return io.Get("https://ipwho.is/",limit,[]{return false;});};
    http_test::Reset();http_test::body=Ip();http_test::type="Application/JSON; charset=utf-8";http_test::encoding="IDENTITY";http_test::chunk=17;auto r=get();assert(r.ok&&r.body==Ip()&&http_test::reads>2&&http_test::closes==1&&http_test::timeout==5000);
    http_test::Reset();http_test::type="application/jsonp";r=get();assert(!r.ok&&r.error=="not_json"&&http_test::reads==0&&http_test::closes==1);
    http_test::Reset();http_test::type="text/html";r=get();assert(!r.ok&&r.error=="not_json");
    http_test::Reset();http_test::encoding="gzip";r=get();assert(!r.ok&&r.error=="unsupported_encoding"&&http_test::reads==0);
    http_test::Reset();http_test::declared=4097;r=get();assert(!r.ok&&r.error=="response_too_large"&&http_test::reads==0);
    http_test::Reset();http_test::body=std::string(4097,'x');r=get();assert(!r.ok&&r.error=="response_too_large"&&r.body.size()<=4096);
    http_test::Reset();http_test::body="{}";http_test::declared=10;r=get();assert(!r.ok&&r.error=="truncated_response");
    http_test::Reset();http_test::read_error=true;r=get();assert(!r.ok&&r.error=="read_failed"&&http_test::closes==1);
    http_test::Reset();http_test::open_ok=false;r=get();assert(!r.ok&&r.error=="connect_failed"&&http_test::closes==1);
    http_test::Reset();http_test::no_status=true;r=get();assert(!r.ok&&r.error=="missing_status"&&http_test::closes==1);
    http_test::Reset();http_test::status=429;http_test::retry="120";r=get();assert(!r.ok&&r.status==429&&r.retry_after=="120"&&http_test::reads==0);
    http_test::Reset();http_test::status=302;r=get();assert(!r.ok&&r.status==302&&http_test::opens==1&&http_test::reads==0);
    http_test::Reset();http_test::read_advance_us=15000001;r=get();assert(!r.ok&&r.error=="read_timeout"&&http_test::closes==1);
    http_test::Reset();r=io.Get("https://evil.example/",4096,[]{return false;});assert(!r.ok&&http_test::opens==0);
    http_test::Reset();r=io.Get("https://ipwho.is/",4096,[]{return true;});assert(!r.ok&&r.error=="cancelled"&&http_test::opens==0);
    http_test::Reset();bool cancelled=false;http_test::after_read=[&]{cancelled=true;};r=io.Get("https://ipwho.is/",4096,[&]{return cancelled;});assert(!r.ok&&r.error=="cancelled"&&http_test::closes==1);http_test::Reset();
    std::cout<<"real DevicePlatform streaming HTTP adapter/status/type/encoding/length/read/cancel/budget OK\n";
}
int main(){std::cout.setf(std::ios::unitbuf);Parsers();Blobs();DefaultsCommits();WorkerCache();EffectiveCurrentCommits();EffectiveCurrentChanges();SearchTokens();QueuesAndBlocked();RateLimits();Pending429AndCommitFailure();PersistenceLimits();DeviceHttpAdapter();std::cout<<"all production weather service tests passed\n";}
'''


@unittest.skipUnless(COMPILER and (CJSON / "cJSON.c").is_file(), "native g++ and repository cJSON required")
class WeatherServiceNativeTests(unittest.TestCase):
    def test_production_service_and_codecs(self):
        with tempfile.TemporaryDirectory(prefix="stopwatch-weather-service-") as directory:
            work = pathlib.Path(directory)
            for name, content in STUBS.items():
                path = work / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")
            source = work / "harness.cc"
            source.write_text(HARNESS, encoding="utf-8")
            exe = work / "weather-service.exe"
            command = [COMPILER, "-std=c++17", "-O1", "-g", "-Wall", "-Wextra", "-pthread",
                       "-I", str(work), "-I", str(BOARD), "-I", str(CJSON),
                       str(source),
                       str(BOARD / "stopwatch_weather_codec.cc"),
                       str(BOARD / "stopwatch_weather_data.cc"), str(CJSON / "cJSON.c"),
                       "-o", str(exe)]
            built = subprocess.run(command, capture_output=True, text=True, env=ENV)
            self.assertEqual(built.returncode, 0, built.stdout + built.stderr)
            try:
                run = subprocess.run([str(exe)], capture_output=True, text=True, env=ENV, timeout=45)
            except subprocess.TimeoutExpired as error:
                self.fail(f"native harness timed out; stdout={error.stdout!r}; stderr={error.stderr!r}")
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
            self.assertIn("all production weather service tests passed", run.stdout)
            print(run.stdout, end="")
            # The two whole translation units must be dependency-free and empty
            # when stock firmware disables local apps.
            for name in ("stopwatch_weather_service.cc", "stopwatch_weather_codec.cc", "stopwatch_weather_data.cc"):
                off = subprocess.run([COMPILER, "-std=c++17", "-DCONFIG_STOPWATCH_LOCAL_APPS=0",
                                      "-I", str(work), "-c", str(BOARD / name),
                                      "-o", str(work / (name + ".o"))],
                                     capture_output=True, text=True, env=ENV)
                self.assertEqual(off.returncode, 0, off.stdout + off.stderr)


if __name__ == "__main__":
    unittest.main()
