"""Compile-extract real StopWatch alarm board/display adapter methods.

The runtime, service, audio, LVGL and timer are deliberately minimal fakes: their
own implementations have independent tests. These checks exercise the actual
board callbacks, shared-object injection, display lock ordering and main-task
handoff, including the stock CONFIG_STOPWATCH_LOCAL_APPS=0 button path.
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


def method(source, signature):
    """Extract one complete method while ignoring braces in strings/comments."""
    start = source.index(signature)
    brace = source.index("{", start)
    depth, end, state = 1, brace + 1, "code"
    while depth:
        character = source[end]
        following = source[end:end + 2]
        if state == "line":
            if character == "\n":
                state = "code"
        elif state == "block":
            if following == "*/":
                state = "code"
                end += 1
        elif state in ('"', "'"):
            if character == "\\":
                end += 1
            elif character == state:
                state = "code"
        elif following == "//":
            state = "line"
            end += 1
        elif following == "/*":
            state = "block"
            end += 1
        elif character in ('"', "'"):
            state = character
        else:
            depth += (character == "{") - (character == "}")
        end += 1
    return source[start:end] + "\n"


PREAMBLE = r'''
#include "device_state.h"
#include <algorithm>
#include <atomic>
#include <cassert>
#include <cstdarg>
#include <cstdint>
#include <cstdio>
#include <ctime>
#include <deque>
#include <functional>
#include <memory>
#include <mutex>
#include <string>
#include <utility>
#include <vector>

int display_depth=0, errors=0;
bool on_main=false, on_timer=false, lock_allowed=true;
std::vector<std::string> trace;
std::function<void()> after_display_unlock;
#define ESP_LOGE(...) (++errors)
#define ESP_LOGW(...) ((void)0)
constexpr int ESP_OK=0, ESP_TIMER_TASK=1, HIGH=1, LOW=0;
constexpr int IOE_PIN_MOTOR=1, BUTTON1_GPIO=2, BUTTON2_GPIO=3, STOPWATCH_HOME_HOLD_MS=1500;
constexpr int LV_OBJ_FLAG_HIDDEN=1, LV_OBJ_FLAG_SCROLLABLE=2, LV_OBJ_FLAG_CLICKABLE=4;
constexpr int LV_OPA_COVER=255, LV_TEXT_ALIGN_CENTER=0, LV_ALIGN_TOP_MID=0;
constexpr int LV_LABEL_LONG_WRAP=1, LV_RADIUS_CIRCLE=100, LV_EVENT_CLICKED=1;
struct lv_font_t {};
lv_font_t MontserratSemiBold26, CommissionerMedium108, font_noto_sans_basic_20_4;
struct lv_obj_t;
struct lv_event_t { void* user_data; };
using lv_event_cb_t=void(*)(lv_event_t*);
struct lv_obj_t {
    lv_obj_t* parent=nullptr; int flags=LV_OBJ_FLAG_SCROLLABLE, foreground=0;
    int width=0,height=0;std::string text;lv_event_cb_t callback=nullptr;void* user_data=nullptr;
};
lv_obj_t screen,top_layer;
std::vector<std::unique_ptr<lv_obj_t>> objects;
lv_obj_t* lv_screen_active(){return &screen;}
lv_obj_t* lv_display_get_layer_top(void*){return &top_layer;}
lv_obj_t* NewObject(lv_obj_t* parent){auto object=std::make_unique<lv_obj_t>();object->parent=parent;auto* raw=object.get();objects.push_back(std::move(object));return raw;}
lv_obj_t* lv_obj_create(lv_obj_t* parent){return NewObject(parent);}
lv_obj_t* lv_label_create(lv_obj_t* parent){return NewObject(parent);}
lv_obj_t* lv_button_create(lv_obj_t* parent){return NewObject(parent);}
void lv_obj_set_parent(lv_obj_t* object,lv_obj_t* parent){object->parent=parent;}
void lv_obj_remove_style_all(lv_obj_t*){}
void lv_obj_set_size(lv_obj_t* object,int width,int height){object->width=width;object->height=height;}
void lv_obj_set_width(lv_obj_t* object,int width){object->width=width;}
void lv_obj_set_style_bg_color(lv_obj_t*,int,int){}
void lv_obj_set_style_bg_opa(lv_obj_t*,int,int){}
void lv_obj_set_style_text_color(lv_obj_t*,int,int){}
void lv_obj_set_style_text_font(lv_obj_t*,const lv_font_t*,int){}
void lv_obj_set_style_text_align(lv_obj_t*,int,int){}
void lv_obj_set_style_radius(lv_obj_t*,int,int){}
void lv_obj_align(lv_obj_t*,int,int,int){}
void lv_obj_center(lv_obj_t*){}
void lv_label_set_long_mode(lv_obj_t*,int){}
int lv_color_black(){return 0;}int lv_color_white(){return 0xffffff;}int lv_color_hex(int value){return value;}
void lv_label_set_text(lv_obj_t* object,const char* text){object->text=text?text:"";}
void lv_label_set_text_fmt(lv_obj_t* object,const char* format,...){char text[128];va_list args;va_start(args,format);std::vsnprintf(text,sizeof(text),format,args);va_end(args);object->text=text;}
void lv_obj_remove_flag(lv_obj_t* object,int flag){object->flags&=~flag;}
void lv_obj_add_flag(lv_obj_t* object,int flag){object->flags|=flag;}
void lv_obj_move_foreground(lv_obj_t* object){++object->foreground;}
void lv_obj_add_event_cb(lv_obj_t* object,lv_event_cb_t callback,int code,void* data){assert(code==LV_EVENT_CLICKED);object->callback=callback;object->user_data=data;}
void* lv_event_get_user_data(lv_event_t* event){return event->user_data;}
bool Hidden(lv_obj_t* object){return (object->flags&LV_OBJ_FLAG_HIDDEN)!=0;}
void ClickOverlay(){for(auto& object:objects)if(object->callback){lv_event_t event{object->user_data};object->callback(&event);return;}assert(false);}

struct DisplayLockGuard {
    bool acquired;
    template<class T> explicit DisplayLockGuard(T*):acquired(lock_allowed){
        if(acquired){assert(display_depth==0);++display_depth;trace.push_back("lock");}
    }
    ~DisplayLockGuard(){
        if(acquired){
            trace.push_back("unlock");--display_depth;
            auto after=std::exchange(after_display_unlock,{});if(after)after();
        }
    }
    explicit operator bool()const{return acquired;}
};
struct AudioService {
    bool processing=false,idle=true;
    bool IsAudioProcessorRunning(){return processing;}
    bool IsPlaybackIdle(){return idle;}
};
struct EventGroup {uint32_t bits=0;};
constexpr uint32_t MAIN_EVENT_SCHEDULE=1;
void xEventGroupSetBits(EventGroup* group,uint32_t bits){group->bits|=bits;}
struct Application {
    std::mutex mutex_;std::deque<std::function<void()>> main_tasks_;
    EventGroup event_group;EventGroup* event_group_=&event_group;
    DeviceState state=kDeviceStateIdle;AudioService audio;
    bool conversation=true,text_accepted=true,complete_synchronously=false;
    int toggles=0,requests=0;std::function<void(bool)> pending_text;
    std::vector<int> conversation_lock_depths;
    std::function<bool()> wake_handler,listening_start_guard;
    int listening_guard_registrations=0;
    static Application& GetInstance(){static Application instance;return instance;}
    AudioService& GetAudioService(){return audio;}
    DeviceState GetDeviceState(){return state;}
    void SetConversationEnabled(bool enabled){
        conversation=enabled;conversation_lock_depths.push_back(display_depth);trace.push_back(enabled?"conversation_on":"conversation_off");
        if(!enabled&&pending_text){auto callback=std::move(pending_text);Schedule([callback=std::move(callback)](){assert(on_main&&display_depth==0);callback(false);});}
    }
    bool RequestTextInteraction(const std::string& text,std::function<void(bool)> completion){
        assert(on_main&&!on_timer&&display_depth==0&&!text.empty());++requests;trace.push_back("request_text");
        if(!text_accepted)return false;
        if(complete_synchronously)completion(false);else pending_text=std::move(completion);return true;
    }
    void FailPending(){assert(on_main&&display_depth==0&&pending_text);auto callback=std::move(pending_text);callback(false);}
    void SetWakeWordActivationHandler(std::function<bool()> handler){wake_handler=std::move(handler);}
    void SetListeningStartGuard(std::function<bool()> guard){++listening_guard_registrations;listening_start_guard=std::move(guard);}
    void ToggleChatState(){assert(on_main&&display_depth==0);++toggles;}
    void Schedule(std::function<void()>&& callback);
    void FlushTurn();
};
struct Button {
    std::function<void()> click,down,up,long_press,double_click;
    explicit Button(int,bool=true,int=0){}
    void OnClick(std::function<void()> callback){click=std::move(callback);}
    void OnPressDown(std::function<void()> callback){down=std::move(callback);}
    void OnPressUp(std::function<void()> callback){up=std::move(callback);}
    void OnLongPress(std::function<void()> callback){long_press=std::move(callback);}
    void OnDoubleClick(std::function<void()> callback){double_click=std::move(callback);}
};
struct AudioCodec {int volume=40,sets=0;int output_volume(){return volume;}void SetOutputVolume(int value){assert(on_main&&display_depth==0);volume=value;++sets;}};
struct WifiManager {bool connected=true;static WifiManager& GetInstance(){static WifiManager instance;return instance;}bool IsConnected(){return connected;}};
namespace Lang::Strings {constexpr const char* VOLUME="Volume";}
struct HomeChord {bool held[2]={false,false};void Press(int index){held[index]=true;}void Release(int index){held[index]=false;}bool LongPress(int){return held[0]&&held[1];}};
struct IoExpander {int writes=0,last_pin=0,last_value=0;void digitalWrite(int pin,int value){++writes;last_pin=pin;last_value=value;}};
struct esp_timer_create_args_t {void(*callback)(void*);void* arg;int dispatch_method;const char* name;bool skip_unhandled_events;};
struct Timer {esp_timer_create_args_t args{};uint64_t period=0;};
using esp_timer_handle_t=Timer*;
Timer timer;bool fail_timer_create=false,fail_timer_start=false;int64_t monotonic_us=1234000;
int64_t esp_timer_get_time(){return monotonic_us;}
int esp_timer_create(const esp_timer_create_args_t* args,esp_timer_handle_t* result){if(fail_timer_create)return -1;timer.args=*args;*result=&timer;return ESP_OK;}
int esp_timer_start_periodic(esp_timer_handle_t handle,uint64_t period){assert(handle==&timer);if(fail_timer_start)return -1;handle->period=period;return ESP_OK;}
void FireTimer(){assert(timer.args.callback);on_timer=true;timer.args.callback(timer.args.arg);on_timer=false;}

#if CONFIG_STOPWATCH_LOCAL_APPS
namespace stopwatch_alarm {struct Alarm {uint32_t id=1;int hour=7,minute=5;bool enabled=true;std::string reminder,date,last_triggered_date;};}
struct StopwatchWeatherService {};
struct StopwatchAlarmService {int initializes=0;bool Initialize(){++initializes;return true;}};
struct StopwatchAlarmPlayer {
    AudioService* audio;std::function<void(bool)> motor;
    StopwatchAlarmPlayer(AudioService& audio,std::function<void(bool)> motor):audio(&audio),motor(std::move(motor)){}
};
struct StopwatchAlarmRuntime {
    struct Hooks {
        std::function<bool()> ready;
        std::function<void(const std::vector<stopwatch_alarm::Alarm>&,const char*)> present;
        std::function<void()> hide,suspend_voice,restore_voice;
        std::function<bool()> activate_voice,voice_foreground,audio_quiet;
        std::function<bool(const std::string&,std::function<void(bool)>)> request_text;
    };
    StopwatchAlarmService* service;StopwatchAlarmPlayer* player;Hooks hooks;
    bool active=false,audio_released=true,request_on_tick=false;int ticks=0,acks=0,cancels=0,failed=0;
    time_t last_time=0;int64_t last_monotonic=0;std::function<void()> during_tick,during_cancel;
    StopwatchAlarmRuntime(StopwatchAlarmService& service,StopwatchAlarmPlayer& player,Hooks hooks):service(&service),player(&player),hooks(std::move(hooks)){}
    bool IsActive()const{return active;}
    bool IsAudioReleased()const{return audio_released;}
    void Tick(time_t now,int64_t monotonic){
        assert(on_main&&!on_timer&&display_depth==0);++ticks;trace.push_back("tick");last_time=now;last_monotonic=monotonic;
        if(during_tick)during_tick();
        if(request_on_tick&&hooks.ready()&&hooks.audio_quiet()){
            request_on_tick=false;assert(hooks.activate_voice());
            if(!hooks.request_text("reminder",[this](bool started){assert(on_main&&display_depth==0);if(!started){++failed;hooks.present({stopwatch_alarm::Alarm{}},"fallback");}})){
                ++failed;hooks.present({stopwatch_alarm::Alarm{}},"fallback");
            }
        }
    }
    void Acknowledge(){assert(on_main&&!on_timer&&display_depth==0);++acks;trace.push_back("ack");active=false;hooks.hide();hooks.restore_voice();}
    void CancelForUser(){assert(on_main&&!on_timer&&display_depth==0);++cancels;trace.push_back("cancel");active=false;hooks.suspend_voice();hooks.hide();if(during_cancel)during_cancel();}
};
struct StopwatchApps {
    enum class NavigationResult {Accepted,AlreadyOpen,Busy,Unavailable};
    StopwatchAlarmService* service;StopwatchWeatherService* weather;
    std::function<void(bool)> conversation;
    bool ready=true,voice=false,handles=false;int buttons=0,homes=0,wakes=0,observations=0,navigations=0;uint32_t generation=0;
    template<class Display> StopwatchApps(Display*,std::function<void(bool)> conversation,std::function<bool()>,StopwatchWeatherService* weather,StopwatchAlarmService* service):service(service),weather(weather),conversation(std::move(conversation)){}
    void Setup(lv_obj_t*){}
    bool CanPresentAlarm(){assert(display_depth==1);return ready;}
    bool IsVoiceForeground(){assert(display_depth==1);trace.push_back("voice_foreground");return voice;}
    bool ActivateVoiceForAlarm(){assert(display_depth==1);voice=true;conversation(true);trace.push_back("activate_voice");return ready;}
    bool HandleButton(int){assert(display_depth==1);++buttons;return handles;}
    void ShowHome(){assert(display_depth==1);++homes;++generation;voice=false;trace.push_back("home");conversation(false);}
    bool ActivateVoiceByWakeWord(){assert(display_depth==1);++wakes;++generation;voice=true;trace.push_back("wake");conversation(true);return ready;}
    uint32_t navigation_generation()const{return generation;}
    NavigationResult NavigateToApp(int){assert(display_depth==1);++navigations;++generation;voice=false;trace.push_back("navigate");conversation(false);return NavigationResult::Accepted;}
    void ObserveVoiceState(DeviceState,bool){assert(display_depth==1);++observations;}
};
#endif
'''

DISPLAY = r'''
struct RoundLcdDisplay {
    void* display_=nullptr;lv_obj_t popup;
    lv_obj_t* low_battery_popup_=&popup;int notifications=0;
    void ShowNotification(const std::string&){++notifications;}
#if CONFIG_STOPWATCH_LOCAL_APPS
    std::unique_ptr<StopwatchApps> apps_;
    StopwatchWeatherService* weather_service_=nullptr;
    // ALARM_DISPLAY_MEMBERS
    std::function<bool()> configure_wifi_;
    // DISPLAY_METHODS
    void SetupLocalUi(){
        // SYSTEM_POPUP
        // LOCAL_UI
    }
#endif
};
'''

BOARD_CLASS = r'''
struct M5StackStopwatchBoard {
    void* i2c_bus_;Button button1_,button2_;RoundLcdDisplay* display_;void* backlight_;
    RoundLcdDisplay display;AudioCodec codec;IoExpander ioe_;HomeChord home_chord_;int wifi_configs=0;
#if CONFIG_STOPWATCH_LOCAL_APPS
    StopwatchWeatherService weather_service_;
    // ALARM_BOARD_MEMBERS
    StopwatchAlarmService* registered_service=nullptr;
    void InitializeTools(){registered_service=&alarm_service_;}
    void InitializeTouch(){}
#endif
    void InitializeI2c(){}void InitializeSpi(){}void InitializeDisplay(){display_=&display;}
    bool EnterWifiConfigMode(){++wifi_configs;return true;}
    AudioCodec* GetAudioCodec(){return &codec;}RoundLcdDisplay* GetDisplay(){return display_;}
    // BOARD_METHODS
};
'''

CHECKS = r'''
void Drain(){auto& app=Application::GetInstance();for(int i=0;!app.main_tasks_.empty();++i){assert(i<20);app.FlushTurn();}}
struct Main {Main(){assert(!on_main);on_main=true;}~Main(){assert(display_depth==0);on_main=false;}};
#if CONFIG_STOPWATCH_LOCAL_APPS
void Setup(M5StackStopwatchBoard& board){Main main;board.display.SetupLocalUi();trace.clear();}
void Injection(){
    M5StackStopwatchBoard board;Setup(board);auto& app=Application::GetInstance();
    assert(board.alarm_service_.initializes==1&&board.registered_service==&board.alarm_service_);
    assert(board.display.alarm_service_==&board.alarm_service_&&board.display.apps_->service==&board.alarm_service_);
    assert(board.alarm_runtime_->service==&board.alarm_service_&&board.display.alarm_runtime_==board.alarm_runtime_.get());
    assert(board.alarm_runtime_->player==board.alarm_player_.get()&&board.alarm_player_->audio==&app.audio);
    assert(board.display.apps_->weather==&board.weather_service_&&board.display.weather_service_==&board.weather_service_);
    assert(timer.args.arg==&board&&timer.args.dispatch_method==ESP_TIMER_TASK&&timer.args.skip_unhandled_events&&timer.period==100000);
    assert(std::string(timer.args.name)=="sw_alarm_tick"&&app.wake_handler);
    board.alarm_player_->motor(true);assert(board.ioe_.last_pin==IOE_PIN_MOTOR&&board.ioe_.last_value==HIGH);
    board.alarm_player_->motor(false);assert(board.ioe_.last_value==LOW);
    assert(board.display.low_battery_popup_->parent==&top_layer&&board.display.alarm_overlay_->parent==&screen);
}
void CoalescedTimer(){
    M5StackStopwatchBoard board;Setup(board);auto& app=Application::GetInstance();auto& runtime=*board.alarm_runtime_;
    for(int i=0;i<100;++i)FireTimer();assert(app.main_tasks_.size()==1&&board.alarm_tick_pending_&&runtime.ticks==0);
    runtime.during_tick=[](){for(int i=0;i<5;++i)FireTimer();};
    app.FlushTurn();assert(runtime.ticks==1&&runtime.last_monotonic==monotonic_us&&runtime.last_time>0&&!board.alarm_tick_pending_);
    assert(app.main_tasks_.empty());runtime.during_tick={};FireTimer();app.FlushTurn();assert(runtime.ticks==2);
    assert(!on_main&&!on_timer&&display_depth==0);
}
void TouchAck(){
    M5StackStopwatchBoard board;Setup(board);auto& app=Application::GetInstance();auto& runtime=*board.alarm_runtime_;
    runtime.active=true;
    {DisplayLockGuard lock(&board.display);ClickOverlay();ClickOverlay();}
    assert(runtime.acks==0&&app.main_tasks_.empty());FireTimer();assert(runtime.acks==0);
    app.FlushTurn();assert(runtime.acks==1&&runtime.ticks==1&&!runtime.active&&!board.display.alarm_acknowledged_);
    auto ack=std::find(trace.begin(),trace.end(),"ack"),tick=std::find(trace.begin(),trace.end(),"tick");assert(ack<tick);
    FireTimer();app.FlushTurn();assert(runtime.acks==1&&runtime.ticks==2);
    board.display.alarm_acknowledged_=true;assert(board.display.TakeAlarmAcknowledgement());assert(!board.display.TakeAlarmAcknowledgement());
}
void Buttons(){
    M5StackStopwatchBoard board;Setup(board);auto& app=Application::GetInstance();auto& runtime=*board.alarm_runtime_;
    for(auto* button:{&board.button1_,&board.button2_}){
        runtime.active=true;int before=runtime.acks;button->click();assert(runtime.acks==before);app.FlushTurn();
        assert(runtime.acks==before+1&&app.toggles==0&&board.codec.sets==0&&board.display.notifications==0&&board.display.apps_->buttons==0);
    }
    runtime.active=true;board.button2_.double_click();app.FlushTurn();assert(runtime.acks==3&&app.toggles==0&&board.codec.sets==0);
    board.display.apps_->handles=true;board.button1_.click();app.FlushTurn();assert(app.toggles==0&&board.display.apps_->buttons==1);
    board.display.apps_->handles=false;board.button1_.click();app.FlushTurn();assert(app.toggles==1);
    board.button2_.click();app.FlushTurn();assert(board.codec.volume==50&&board.codec.sets==1&&board.display.notifications==1);
    runtime.active=true;board.button1_.down();board.button1_.long_press();assert(app.main_tasks_.empty());
    board.button2_.down();board.button2_.long_press();app.FlushTurn();assert(runtime.cancels==1&&board.display.apps_->homes==1);
    board.button1_.up();board.button2_.up();
}
void HomeWake(){
    M5StackStopwatchBoard board;Setup(board);auto& runtime=*board.alarm_runtime_;
    for(bool wake:{false,true}){
        trace.clear();runtime.active=true;Main main;
        if(wake)assert(Application::GetInstance().wake_handler());else board.display.ShowHome();
        assert(!runtime.active&&display_depth==0&&trace.front()=="cancel");
        auto cancel=std::find(trace.begin(),trace.end(),"cancel"),lock=std::find(trace.begin(),trace.end(),"lock");assert(cancel<lock);
    }
    assert(runtime.cancels==2&&board.display.apps_->homes==1&&board.display.apps_->wakes==1);
    lock_allowed=false;{Main main;assert(!board.display.ActivateVoiceByWakeWord());board.display.ShowHome();}lock_allowed=true;
    assert(runtime.cancels==4&&board.display.apps_->homes==1&&board.display.apps_->wakes==1);
}
void NavigationCancel(){
    M5StackStopwatchBoard board;Setup(board);auto& app=Application::GetInstance();auto& runtime=*board.alarm_runtime_;
    {Main main;assert(board.display.ScheduleAppNavigation(4));}
    assert(app.main_tasks_.size()==1&&runtime.cancels==0);app.FlushTurn();assert(runtime.cancels==0&&app.main_tasks_.size()==1);
    trace.clear();runtime.active=true;app.FlushTurn();assert(runtime.cancels==1&&board.display.apps_->navigations==1&&display_depth==0);
    assert(std::find(trace.begin(),trace.end(),"cancel")<std::find(trace.begin(),trace.end(),"navigate"));
    // A user Home between the queued hops invalidates navigation before Cancel.
    {Main main;assert(board.display.ScheduleAppNavigation(2));}app.FlushTurn();{Main main;board.display.ShowHome();}
    const int cancellations=runtime.cancels;app.FlushTurn();assert(runtime.cancels==cancellations&&board.display.apps_->navigations==1);
    // Cancellation deliberately releases the display mutex. If another UI
    // action changes generation during that gap, the second check must reject.
    {Main main;assert(board.display.ScheduleAppNavigation(2));}app.FlushTurn();
    runtime.during_cancel=[&](){++board.display.apps_->generation;};app.FlushTurn();runtime.during_cancel={};
    assert(board.display.apps_->navigations==1&&runtime.cancels==cancellations+1);
    // Missing/failed display access at the first check must not cancel an alarm.
    for(bool missing:{false,true}){
        {Main main;assert(board.display.ScheduleAppNavigation(3));}app.FlushTurn();
        const auto before=runtime.cancels;auto apps=missing?std::move(board.display.apps_):nullptr;
        lock_allowed=missing;app.FlushTurn();lock_allowed=true;
        if(missing)board.display.apps_=std::move(apps);
        assert(runtime.cancels==before&&board.display.apps_->navigations==1);
    }
    // The final reacquire may also fail after a successful unlocked Cancel.
    {Main main;assert(board.display.ScheduleAppNavigation(2));}app.FlushTurn();
    runtime.during_cancel=[](){lock_allowed=false;};app.FlushTurn();lock_allowed=true;runtime.during_cancel={};
    assert(board.display.apps_->navigations==1);
}
void AudioQuiet(){
    M5StackStopwatchBoard board;Setup(board);auto& app=Application::GetInstance();auto& hooks=board.alarm_runtime_->hooks;
    for(auto state:{kDeviceStateIdle,kDeviceStateConnecting,kDeviceStateListening,kDeviceStateSpeaking,kDeviceStateNotifying})
        for(bool processing:{false,true})for(bool idle:{false,true}){
            app.state=state;app.audio.processing=processing;app.audio.idle=idle;Main main;
            assert(hooks.audio_quiet()==(state!=kDeviceStateSpeaking&&state!=kDeviceStateNotifying&&!processing&&idle));
        }
    app.state=kDeviceStateIdle;app.audio.processing=false;app.audio.idle=false;
    board.alarm_runtime_->request_on_tick=true;FireTimer();app.FlushTurn();assert(app.requests==0);
    app.audio.idle=true;FireTimer();app.FlushTurn();assert(app.requests==1);
    {Main main;app.FailPending();}assert(board.alarm_runtime_->failed==1);
}
void PendingVoiceFailure(){
    M5StackStopwatchBoard board;Setup(board);auto& app=Application::GetInstance();auto& runtime=*board.alarm_runtime_;
    runtime.request_on_tick=true;FireTimer();app.FlushTurn();assert(app.requests==1&&app.pending_text&&board.display.apps_->voice);
    {Main main;board.display.ShowHome();}assert(runtime.failed==0);Drain();assert(runtime.failed==1&&display_depth==0);
    // request_text may reject immediately or call failure synchronously. Neither
    // is allowed to inherit ActivateVoiceForAlarm's display guard.
    for(bool synchronous:{false,true}){
        app.text_accepted=synchronous;app.complete_synchronously=synchronous;
        runtime.request_on_tick=true;FireTimer();app.FlushTurn();assert(display_depth==0);
    }
    assert(app.requests==3&&runtime.failed==3);
}
void Overlay(){
    M5StackStopwatchBoard board;Setup(board);auto& display=board.display;auto& app=Application::GetInstance();
    assert(Hidden(display.alarm_overlay_)&&display.alarm_overlay_->width==466&&display.alarm_overlay_->height==466);
    {Main main;display.PresentAlarm({},"ignored");}assert(Hidden(display.alarm_overlay_));
    {Main main;display.PresentAlarm({stopwatch_alarm::Alarm{}},"ringing");}
    assert(!Hidden(display.alarm_overlay_)&&display.alarm_time_->text=="07:05"&&display.alarm_count_->text=="Alarm"&&display.alarm_status_->text=="ringing");
    {Main main;display.PresentAlarm({stopwatch_alarm::Alarm{},stopwatch_alarm::Alarm{}},"voice");}
    assert(display.alarm_count_->text=="2 alarms"&&display.alarm_overlay_->foreground==2);
    for(auto state:{kDeviceStateWifiConfiguring,kDeviceStateUpgrading,kDeviceStateFatalError}){
        app.state=state;{Main main;display.ObserveVoiceState();}assert(Hidden(display.alarm_overlay_));
        app.state=kDeviceStateIdle;{Main main;display.ObserveVoiceState();}assert(Hidden(display.alarm_overlay_));
        {Main main;display.PresentAlarm({stopwatch_alarm::Alarm{}},"ringing");}
    }
    {Main main;display.HideAlarm();}assert(Hidden(display.alarm_overlay_));
    lock_allowed=false;{Main main;display.PresentAlarm({stopwatch_alarm::Alarm{}},"ignored");assert(!display.CanPresentAlarm()&&!display.IsVoiceForeground()&&!display.ActivateVoiceForAlarm());}lock_allowed=true;
    assert(Hidden(display.alarm_overlay_));display.apps_->ready=false;{Main main;assert(!display.CanPresentAlarm());}
    auto* overlay=display.alarm_overlay_;display.alarm_overlay_=nullptr;
    {Main main;display.PresentAlarm({stopwatch_alarm::Alarm{}},"optional overlay");display.HideAlarm();display.ObserveVoiceState();}
    display.alarm_overlay_=overlay;
    display.apps_.reset();{Main main;display.ObserveVoiceState();assert(!display.HandleAppButton(0)&&!display.CanPresentAlarm());}
}
void RestoreConversationAtomic(){
    M5StackStopwatchBoard board;Setup(board);auto& app=Application::GetInstance();
    auto& display=board.display;auto& hooks=board.alarm_runtime_->hooks;
    for(bool voice:{false,true}){
        display.apps_->voice=voice;app.conversation=!voice;
        app.conversation_lock_depths.clear();trace.clear();
        {Main main;hooks.restore_voice();}
        assert(app.conversation==voice&&app.conversation_lock_depths==std::vector<int>{1});
        const std::vector<std::string> expected={"lock","voice_foreground",voice?"conversation_on":"conversation_off","unlock"};
        assert(trace==expected);
    }
    // Deliver a touch-style Voice entry immediately after restore unlocks.
    // Its foreground/policy update takes the next display lock, so an older
    // foreground result must not write false after the new true policy.
    display.apps_->voice=false;app.conversation=true;
    app.conversation_lock_depths.clear();trace.clear();
    after_display_unlock=[&](){
        assert(display_depth==0&&!app.conversation);
        DisplayLockGuard lock(&display);assert(lock);
        assert(display.apps_->ActivateVoiceForAlarm());trace.push_back("touch_voice");
    };
    {Main main;hooks.restore_voice();}
    assert(!after_display_unlock&&app.conversation&&display.apps_->voice);
    assert((app.conversation_lock_depths==std::vector<int>{1,1}));
    const std::vector<std::string> expected={"lock","voice_foreground","conversation_off","unlock",
        "lock","conversation_on","activate_voice","touch_voice","unlock"};
    assert(trace==expected);
    // A missing app or failed lock is not evidence that Voice is inactive:
    // preserve either prior conversation policy without querying or writing.
    for(bool missing:{false,true})for(bool previous:{false,true}){
        auto apps=missing?std::move(display.apps_):nullptr;
        lock_allowed=missing;app.conversation=previous;
        app.conversation_lock_depths.clear();trace.clear();
        {Main main;hooks.restore_voice();}
        lock_allowed=true;if(missing)display.apps_=std::move(apps);
        assert(app.conversation==previous&&app.conversation_lock_depths.empty());
        assert(std::find(trace.begin(),trace.end(),"voice_foreground")==trace.end());
    }
    assert(display_depth==0&&!after_display_unlock);
}
void ListeningTailGuard(){
    M5StackStopwatchBoard board;Setup(board);auto& app=Application::GetInstance();auto& runtime=*board.alarm_runtime_;
    assert(app.listening_guard_registrations==1&&app.listening_start_guard);
    // UI/runtime activity is not audio ownership: even an acknowledged or
    // cancelled alarm may still have buffered tone playback to drain.
    for(bool active:{true,false}){
        runtime.active=active;runtime.audio_released=false;
        assert(runtime.IsActive()==active&&!app.listening_start_guard());
        runtime.audio_released=true;
        assert(runtime.IsActive()==active&&app.listening_start_guard());
    }
    assert(app.requests==0&&app.toggles==0&&display_depth==0);
}
void TimerFailure(){
    for(bool create:{false,true}){fail_timer_create=create;fail_timer_start=!create;M5StackStopwatchBoard board;assert(board.alarm_service_.initializes==1&&errors>0);}
}
#endif
int main(int argc,char** argv){
    assert(argc==2);const std::string test=argv[1];
#if CONFIG_STOPWATCH_LOCAL_APPS
    if(test=="injection")Injection();else if(test=="coalesced")CoalescedTimer();else if(test=="touch")TouchAck();
    else if(test=="buttons")Buttons();else if(test=="home_wake")HomeWake();else if(test=="navigation")NavigationCancel();
    else if(test=="audio_quiet")AudioQuiet();else if(test=="pending_failure")PendingVoiceFailure();
    else if(test=="restore_voice")RestoreConversationAtomic();else if(test=="overlay")Overlay();
    else if(test=="tail_guard")ListeningTailGuard();else if(test=="timer_failure")TimerFailure();else assert(false);
#else
    assert(test=="disabled");M5StackStopwatchBoard board;auto& app=Application::GetInstance();
    board.button1_.click();assert(app.toggles==0);app.FlushTurn();assert(app.toggles==1);
    board.button2_.click();app.FlushTurn();assert(board.codec.volume==50&&board.codec.sets==1&&board.display.notifications==1);
    assert(!board.button1_.down&&!board.button1_.long_press&&!board.button2_.double_click&&timer.args.callback==nullptr);
    assert(!app.listening_start_guard&&app.listening_guard_registrations==0);
#endif
    assert(display_depth==0&&!on_main&&!on_timer);std::puts("production board alarm wiring passed");
}
'''


def extracted_source():
    board = (BOARD / "m5stack_stopwatch.cc").read_text(encoding="utf-8")
    application = (ROOT / "main/application.cc").read_text(encoding="utf-8")
    display_source = DISPLAY.replace("// ALARM_DISPLAY_MEMBERS", "\n".join(
        next(line.strip() for line in board.splitlines() if line.strip().startswith(token))
        for token in ("StopwatchAlarmService* alarm_service_", "StopwatchAlarmRuntime* alarm_runtime_",
                      "lv_obj_t* alarm_overlay_", "lv_obj_t* alarm_time_", "lv_obj_t* alarm_count_",
                      "lv_obj_t* alarm_status_", "std::atomic<bool> alarm_acknowledged_")))
    signatures = (
        "void SetWeatherService(", "void SetAlarmService(", "void SetAlarmRuntime(",
        "bool CanPresentAlarm()", "bool IsVoiceForeground()", "bool ActivateVoiceForAlarm()",
        "void RestoreConversationForAlarm()", "void PresentAlarm(", "void HideAlarm()", "bool TakeAlarmAcknowledgement()",
        "void SetWifiConfigCallback(", "bool HandleAppButton(", "void ShowHome()",
        "bool ActivateVoiceByWakeWord()", "bool ScheduleAppNavigation(",
        "void SetupAlarmOverlay()", "void ObserveVoiceState(",
    )
    display_source = display_source.replace("// DISPLAY_METHODS", "\n".join(
        method(board, signature) for signature in signatures))
    setup = method(board, "void SetupUI()")
    display_source = display_source.replace("// SYSTEM_POPUP", method(
        setup, "if (low_battery_popup_ != nullptr)"))
    start = setup.index("apps_ = std::make_unique<StopwatchApps>")
    end = setup.index("#endif", start)
    display_source = display_source.replace("// LOCAL_UI", setup[start:end])
    board_source = BOARD_CLASS.replace("// ALARM_BOARD_MEMBERS", "\n".join(
        next(line.strip() for line in board.splitlines() if line.strip().startswith(token))
        for token in ("StopwatchAlarmService alarm_service_", "std::unique_ptr<StopwatchAlarmPlayer> alarm_player_",
                      "std::unique_ptr<StopwatchAlarmRuntime> alarm_runtime_", "esp_timer_handle_t alarm_timer_",
                      "std::atomic<bool> alarm_tick_pending_")))
    board_source = board_source.replace("// BOARD_METHODS", "\n".join((
        "#if CONFIG_STOPWATCH_LOCAL_APPS\n" + method(board, "void InitializeAlarm()") + "#endif\n",
        method(board, "void InitializeButtons()"), method(board, "M5StackStopwatchBoard()"))))
    source = PREAMBLE + display_source + board_source
    source += method(application, "void Application::Schedule(")
    source += "void Application::FlushTurn(){assert(!on_main&&!main_tasks_.empty());on_main=true;const auto bits=event_group.bits;event_group.bits=0;\n"
    source += method(application, "if (bits & MAIN_EVENT_SCHEDULE)")
    source += "on_main=false;}\n"
    return source + CHECKS


@unittest.skipUnless(COMPILER, "requires native MinGW/g++ C++23")
class StopwatchAlarmWiringTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory(prefix="stopwatch-alarm-wiring-")
        cls.addClassCleanup(cls.directory.cleanup)
        build = Path(cls.directory.name)
        source = build / "wiring.cc"
        source.write_text(extracted_source(), encoding="utf-8")
        cls.binaries = {}
        for enabled in (0, 1):
            binary = build / f"wiring-{enabled}.exe"
            result = subprocess.run(
                [COMPILER, "-std=c++23", "-fno-exceptions", "-fno-rtti", "-Wall", "-Wextra",
                 "-Werror", "-Wno-misleading-indentation", f"-DCONFIG_STOPWATCH_LOCAL_APPS={enabled}",
                 f"-I{ROOT / 'main'}", str(source), "-o", str(binary)],
                capture_output=True, text=True, encoding="utf-8", errors="replace", env=ENV,
                timeout=90)
            if result.returncode:
                raise AssertionError(result.stdout + result.stderr)
            cls.binaries[enabled] = binary

    def run_case(self, name, enabled=1):
        result = subprocess.run([str(self.binaries[enabled]), name], capture_output=True,
                                text=True, encoding="utf-8", errors="replace", env=ENV, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("production board alarm wiring passed", result.stdout)

    def test_board_and_display_receive_one_shared_service_runtime_and_player(self):
        self.run_case("injection")

    def test_timer_bursts_coalesce_until_scheduled_main_tick_finishes(self):
        self.run_case("coalesced")

    def test_touch_ack_is_atomic_then_consumed_before_main_tick(self):
        self.run_case("touch")

    def test_physical_buttons_ack_alarm_without_chat_volume_or_page_input(self):
        self.run_case("buttons")

    def test_home_and_wake_cancel_before_any_display_lock(self):
        self.run_case("home_wake")

    def test_navigation_cancels_outside_display_lock_and_keeps_generation_guard(self):
        self.run_case("navigation")

    def test_audio_quiet_hook_requires_idle_playback_and_stopped_processor(self):
        self.run_case("audio_quiet")

    def test_pending_voice_failure_and_sync_rejection_never_request_while_display_locked(self):
        self.run_case("pending_failure")

    def test_overlay_visibility_text_optional_ui_and_system_foreground(self):
        self.run_case("overlay")

    def test_restore_conversation_queries_and_sets_under_one_display_lock(self):
        self.run_case("restore_voice")

    def test_listening_start_guard_tracks_audio_release_even_after_alarm_inactive(self):
        self.run_case("tail_guard")

    def test_timer_create_and_start_errors_are_reported(self):
        self.run_case("timer_failure")

    def test_stock_configuration_retains_original_buttons_without_alarm_timer(self):
        self.run_case("disabled", enabled=0)


if __name__ == "__main__":
    unittest.main()
