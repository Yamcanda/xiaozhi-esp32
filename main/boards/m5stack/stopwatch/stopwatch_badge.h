#pragma once

#include <array>
#include <cstddef>
#include <cstdint>
#include <functional>
#include <memory>
#include <mutex>
#include <string>

class LvglImage;
struct httpd_req;

// All filesystem, decoding and NVS work belongs on a worker/server task, not the UI task.
// Keep the store alive until the editor has stopped. Returned images own their pixel memory.
class StopwatchBadgeStore {
public:
    static constexpr int kSlotCount = 6;
    static constexpr int kWidth = 466;
    static constexpr int kHeight = 466;
    static constexpr size_t kMaxJpegSize = 256 * 1024;

    ~StopwatchBadgeStore();
    bool Initialize();
    bool available() const;
    std::array<bool, kSlotCount> OccupiedSlots();
    int ActiveSlot();  // Zero-based; -1 when no usable badge exists.
    bool SetActiveSlot(int slot);
    int StepSlot(int direction);  // The sign selects the direction; skips empty slots.
    bool DeleteSlot(int slot);
    bool SaveJpeg(int slot, const uint8_t* data, size_t size, std::string& error);
    std::unique_ptr<LvglImage> LoadActiveImage();

    // Checked, PSRAM-preferred allocation. On success the caller owns *data and must
    // heap_caps_free() it. On failure outputs are reset. Used by the HTTP image endpoint.
    bool ReadJpeg(int slot, uint8_t** data, size_t* size);

private:
    friend class StopwatchBadgeEditor;

    int ResolveActiveLocked();
    void SetActiveLocked(int slot);
    bool ReadJpegLocked(int slot, uint8_t** data, size_t* size);
    bool SaveJpegInternal(int slot, const uint8_t* data, size_t size, std::string& error,
                          bool* invalid_image);

    mutable std::mutex mutex_;
    bool available_ = false;
    int32_t wl_handle_ = -1;
    int active_ = -1;
    std::array<bool, kSlotCount> occupied_{};
};

// Start/Stop/destruction must be serialized on the parent's dedicated worker task.
// on_changed runs on the HTTP task: it must only signal the parent, never touch UI/voice.
class StopwatchBadgeEditor {
public:
    explicit StopwatchBadgeEditor(StopwatchBadgeStore& store, std::function<void()> on_changed);
    ~StopwatchBadgeEditor();
    std::string Start();  // Empty on failure; otherwise a temporary, secret bearer-token URL.
    void Stop();

private:
    static int HandleRequest(httpd_req* request);
    int Handle(httpd_req* request);

    StopwatchBadgeStore& store_;
    std::function<void()> on_changed_;
    void* server_ = nullptr;
    std::array<char, 33> token_{};
    std::string origin_;
    std::string url_;
};
