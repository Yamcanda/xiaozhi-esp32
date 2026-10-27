#pragma once

// Page selection is deliberately independent of the voice assistant's DeviceState.
class StopwatchAppState {
public:
    enum class Page { Home, Voice, Badge, Settings, Weather, Alarm };
    static constexpr int kAppCount = 5;
    // Home positions are separate from the stable app IDs used by Open().
    static constexpr int kHomeOrder[kAppCount] = {0, 4, 1, 3, 2};

    Page page() const { return page_; }
    int selection() const { return selection_; }

    void ShowHome() { page_ = Page::Home; }
    void ShowVoice() { Open(0); }
    void OpenSelected() { Open(selection_); }
    void Open(int index) {
        if (index < 0 || index >= kAppCount) {
            return;
        }
        static constexpr Page pages[] = {Page::Voice, Page::Badge, Page::Settings, Page::Weather,
                                         Page::Alarm};
        selection_ = index;
        page_ = pages[index];
    }
    void Step(int direction) {
        for (int position = 0; position < kAppCount; ++position) {
            if (kHomeOrder[position] == selection_) {
                const int next = (position + (direction < 0 ? kAppCount - 1 : 1)) % kAppCount;
                selection_ = kHomeOrder[next];
                return;
            }
        }
    }

private:
    Page page_ = Page::Home;
    int selection_ = 0;
};

// Called only by the button driver's serialized callbacks. Each button must reach
// its long-press threshold while both remain down; rearm only after both release.
class StopwatchHomeChord {
public:
    void Press(int button) {
        if (button < 0 || button > 1) {
            return;
        }
        pressed_[button] = true;
        long_pressed_[button] = false;
    }
    void Release(int button) {
        if (button < 0 || button > 1) {
            return;
        }
        pressed_[button] = false;
        long_pressed_[button] = false;
        if (!pressed_[0] && !pressed_[1]) {
            triggered_ = false;
        }
    }
    bool LongPress(int button) {
        if (button < 0 || button > 1 || !pressed_[button]) {
            return false;
        }
        long_pressed_[button] = true;
        if (triggered_ || !pressed_[0] || !pressed_[1] || !long_pressed_[0] || !long_pressed_[1]) {
            return false;
        }
        triggered_ = true;
        return true;
    }

private:
    bool pressed_[2] = {};
    bool long_pressed_[2] = {};
    bool triggered_ = false;
};
