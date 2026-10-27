#include "sdkconfig.h"

#if CONFIG_STOPWATCH_LOCAL_APPS

#include "stopwatch_badge.h"

#include <esp_heap_caps.h>
#include <esp_http_server.h>
#include <esp_log.h>
#include <esp_netif.h>
#include <esp_partition.h>
#include <esp_random.h>
#include <esp_timer.h>
#include <esp_vfs_fat.h>
#include <esp_wifi.h>
#include <fcntl.h>
#include <nvs.h>
#include <sys/stat.h>
#include <unistd.h>

#include <algorithm>
#include <cerrno>
#include <cstdio>
#include <cstring>
#include <new>
#include <string_view>
#include <utility>

#include "display/lvgl_display/jpg/jpeg_to_image.h"
#include "display/lvgl_display/lvgl_image.h"
#include "settings.h"
#include "stopwatch_badge_editor.html.h"

namespace {
constexpr char kTag[] = "StopwatchBadge";
constexpr char kMountPath[] = "/badge";
constexpr char kPartition[] = "badge";
constexpr char kSettingsNamespace[] = "sw_badge";
constexpr size_t kPartitionSize = 2 * 1024 * 1024;
constexpr int64_t kUploadTimeoutUs = 25 * 1000 * 1000;

extern const uint8_t dnd_start[] asm("_binary_01_status_dnd_jpg_start");
extern const uint8_t dnd_end[] asm("_binary_01_status_dnd_jpg_end");
extern const uint8_t focus_start[] asm("_binary_02_status_focus_jpg_start");
extern const uint8_t focus_end[] asm("_binary_02_status_focus_jpg_end");
extern const uint8_t chill_start[] asm("_binary_03_status_chill_jpg_start");
extern const uint8_t chill_end[] asm("_binary_03_status_chill_jpg_end");
extern const uint8_t shiba_start[] asm("_binary_04_pet_shiba_jpg_start");
extern const uint8_t shiba_end[] asm("_binary_04_pet_shiba_jpg_end");
extern const uint8_t mars_start[] asm("_binary_05_mars_jpg_start");
extern const uint8_t mars_end[] asm("_binary_05_mars_jpg_end");
extern const uint8_t earth_start[] asm("_binary_06_earth_jpg_start");
extern const uint8_t earth_end[] asm("_binary_06_earth_jpg_end");

struct DefaultBadge {
    const uint8_t* start;
    const uint8_t* end;
};
const std::array<DefaultBadge, StopwatchBadgeStore::kSlotCount> kDefaultBadges{{
    {dnd_start, dnd_end},
    {focus_start, focus_end},
    {chill_start, chill_end},
    {shiba_start, shiba_end},
    {mars_start, mars_end},
    {earth_start, earth_end},
}};

struct HeapDeleter {
    void operator()(uint8_t* data) const { heap_caps_free(data); }
};
using HeapBytes = std::unique_ptr<uint8_t, HeapDeleter>;

uint8_t* AllocateBytes(size_t size) {
    auto* data = static_cast<uint8_t*>(heap_caps_malloc(size, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT));
    if (!data) {
        data = static_cast<uint8_t*>(heap_caps_malloc(size, MALLOC_CAP_8BIT));
    }
    return data;
}

bool ValidSlot(int slot) {
    return slot >= 0 && slot < StopwatchBadgeStore::kSlotCount;
}

void SlotPath(int slot, char* path, size_t size, const char* extension = "jpg") {
    snprintf(path, size, "%s/slot_%d.%s", kMountPath, slot, extension);
}

bool ReadFile(const char* path, uint8_t** data, size_t* size) {
    *data = nullptr;
    *size = 0;
    int fd = open(path, O_RDONLY);
    if (fd < 0) {
        return false;
    }
    struct stat info {};
    if (fstat(fd, &info) != 0 || !S_ISREG(info.st_mode) || info.st_size <= 0 ||
        static_cast<size_t>(info.st_size) > StopwatchBadgeStore::kMaxJpegSize) {
        close(fd);
        return false;
    }
    size_t length = static_cast<size_t>(info.st_size);
    HeapBytes buffer(AllocateBytes(length));
    if (!buffer) {
        close(fd);
        return false;
    }
    size_t received = 0;
    while (received < length) {
        ssize_t count = read(fd, buffer.get() + received, length - received);
        if (count <= 0) {
            close(fd);
            return false;
        }
        received += static_cast<size_t>(count);
    }
    if (close(fd) != 0) {
        return false;
    }
    *data = buffer.release();
    *size = length;
    return true;
}

// Parse the entire bounded stream before giving it to any decoder. Accept only one
// 8-bit baseline frame and one complete scan (the format produced by browser canvas).
// In particular, reject later SOF/DNL markers that could change the checked dimensions.
bool ValidJpeg(const uint8_t* data, size_t size) {
    if (!data || size < 4 || size > StopwatchBadgeStore::kMaxJpegSize || data[0] != 0xff ||
        data[1] != 0xd8) {
        return false;
    }
    bool frame = false;
    uint8_t components = 0;
    std::array<uint8_t, 3> ids{};
    size_t offset = 2;
    auto word = [data](size_t pos) -> unsigned { return (data[pos] << 8) | data[pos + 1]; };
    while (offset < size) {
        if (data[offset++] != 0xff) {
            return false;
        }
        while (offset < size && data[offset] == 0xff) {
            ++offset;
        }
        if (offset >= size) {
            return false;
        }
        uint8_t marker = data[offset++];
        if (size - offset < 2) {
            return false;
        }
        size_t length = word(offset);
        if (length < 2 || length > size - offset) {
            return false;
        }
        const uint8_t* segment = data + offset;
        if (marker == 0xc0) {
            if (frame || length < 8 || segment[2] != 8 ||
                word(offset + 3) != StopwatchBadgeStore::kHeight ||
                word(offset + 5) != StopwatchBadgeStore::kWidth) {
                return false;
            }
            components = segment[7];
            if ((components != 1 && components != 3) || length != 8u + 3u * components) {
                return false;
            }
            unsigned blocks = 0;
            for (unsigned i = 0; i < components; ++i) {
                ids[i] = segment[8 + 3 * i];
                unsigned sampling = segment[9 + 3 * i];
                unsigned horizontal = sampling >> 4;
                unsigned vertical = sampling & 15;
                if (horizontal < 1 || horizontal > 4 || vertical < 1 || vertical > 4 ||
                    segment[10 + 3 * i] > 3) {
                    return false;
                }
                blocks += horizontal * vertical;
                for (unsigned j = 0; j < i; ++j) {
                    if (ids[j] == ids[i]) {
                        return false;
                    }
                }
            }
            if (blocks > 10) {
                return false;
            }
            frame = true;
        } else if (marker == 0xda) {
            if (!frame || length != 6u + 2u * components || segment[2] != components ||
                segment[length - 3] != 0 || segment[length - 2] != 63 ||
                segment[length - 1] != 0) {
                return false;
            }
            unsigned seen = 0;
            for (unsigned i = 0; i < components; ++i) {
                unsigned index = 0;
                while (index < components && ids[index] != segment[3 + 2 * i]) {
                    ++index;
                }
                unsigned tables = segment[4 + 2 * i];
                if (index == components || (seen & (1u << index)) || (tables >> 4) > 3 ||
                    (tables & 15) > 3) {
                    return false;
                }
                seen |= 1u << index;
            }
            offset += length;
            bool entropy = false;
            while (offset < size) {
                if (data[offset++] != 0xff) {
                    entropy = true;
                    continue;
                }
                while (offset < size && data[offset] == 0xff) {
                    ++offset;
                }
                if (offset >= size) {
                    return false;
                }
                uint8_t code = data[offset++];
                if (code == 0) {
                    entropy = true;
                } else if (code >= 0xd0 && code <= 0xd7) {
                    continue;
                } else {
                    return code == 0xd9 && offset == size && entropy;
                }
            }
            return false;
        } else if (marker != 0xc4 && marker != 0xdb && marker != 0xdd && marker != 0xfe &&
                   !(marker >= 0xe0 && marker <= 0xef)) {
            return false;
        }
        offset += length;
    }
    return false;
}

esp_err_t DecodeJpeg(const uint8_t* data, size_t size, uint8_t** pixels, size_t* pixel_size,
                     size_t* stride) {
    *pixels = nullptr;
    *pixel_size = 0;
    *stride = 0;
    if (!ValidJpeg(data, size)) {
        return ESP_ERR_INVALID_ARG;
    }
    size_t width = 0;
    size_t height = 0;
    esp_err_t result = jpeg_to_image(data, size, pixels, pixel_size, &width, &height, stride);
    if (result != ESP_OK) {
        return result;
    }
    if (!*pixels || width != StopwatchBadgeStore::kWidth || height != StopwatchBadgeStore::kHeight ||
        *stride < width * 2 || *stride > (width + 16) * 2 || *pixel_size < *stride * height) {
        heap_caps_free(*pixels);
        *pixels = nullptr;
        return ESP_ERR_INVALID_SIZE;
    }
    return ESP_OK;
}

bool UsableFile(const char* path) {
    uint8_t* data = nullptr;
    size_t size = 0;
    if (!ReadFile(path, &data, &size)) {
        return false;
    }
    HeapBytes jpeg(data);
    uint8_t* pixels = nullptr;
    size_t pixel_size = 0;
    size_t stride = 0;
    bool valid = DecodeJpeg(data, size, &pixels, &pixel_size, &stride) == ESP_OK;
    heap_caps_free(pixels);
    return valid;
}

// Called on the storage worker while Initialize holds the store lock. Install only
// once per filesystem; uploaded badges and recoverable backups always take priority.
bool InstallDefaultBadges() {
    char marker[40], marker_temporary[40];
    snprintf(marker, sizeof(marker), "%s/defaults-v1", kMountPath);
    snprintf(marker_temporary, sizeof(marker_temporary), "%s/defaults.tmp", kMountPath);
    struct stat info {};
    if (stat(marker, &info) == 0) {
        return S_ISREG(info.st_mode);
    }
    if (errno != ENOENT) {
        return false;
    }
    auto install = [](const char* path, const char* temporary, const uint8_t* data, size_t size) {
        int fd = open(temporary, O_WRONLY | O_CREAT | O_TRUNC, 0600);
        if (fd < 0) {
            return false;
        }
        size_t written = 0;
        bool ok = true;
        while (written < size) {
            ssize_t count = write(fd, data + written, size - written);
            if (count <= 0) {
                ok = false;
                break;
            }
            written += static_cast<size_t>(count);
        }
        if (ok && fsync(fd) != 0) {
            ok = false;
        }
        if (close(fd) != 0) {
            ok = false;
        }
        if (!ok || rename(temporary, path) != 0) {
            unlink(temporary);
            return false;
        }
        return true;
    };
    for (int slot = 0; slot < StopwatchBadgeStore::kSlotCount; ++slot) {
        char path[40], backup[40], temporary[40];
        SlotPath(slot, path, sizeof(path));
        SlotPath(slot, backup, sizeof(backup), "bak");
        SlotPath(slot, temporary, sizeof(temporary), "tmp");
        if (stat(path, &info) == 0) {
            continue;
        }
        if (errno != ENOENT) {
            return false;
        }
        if (stat(backup, &info) == 0) {
            continue;
        }
        if (errno != ENOENT) {
            return false;
        }
        const auto& image = kDefaultBadges[slot];
        size_t size = static_cast<size_t>(image.end - image.start);
        uint8_t* pixels = nullptr;
        size_t pixel_size = 0;
        size_t stride = 0;
        esp_err_t result = DecodeJpeg(image.start, size, &pixels, &pixel_size, &stride);
        heap_caps_free(pixels);
        if (result != ESP_OK || !install(path, temporary, image.start, size)) {
            return false;
        }
    }
    // Commit this last so an interrupted first pass retries only the missing files.
    // Once committed, deleting a slot in the editor must remain effective on reboot.
    const uint8_t version = 1;
    return install(marker, marker_temporary, &version, sizeof(version));
}

bool FullyErased(const esp_partition_t* partition) {
    uint8_t buffer[4096];
    for (size_t offset = 0; offset < partition->size; offset += sizeof(buffer)) {
        size_t size = std::min<size_t>(sizeof(buffer), partition->size - offset);
        if (esp_partition_read_raw(partition, offset, buffer, size) != ESP_OK) {
            return false;
        }
        for (size_t i = 0; i < size; ++i) {
            if (buffer[i] != 0xff) {
                return false;
            }
        }
    }
    return true;
}

void SecurityHeaders(httpd_req_t* request) {
    httpd_resp_set_hdr(request, "Cache-Control", "no-store");
    httpd_resp_set_hdr(request, "Referrer-Policy", "no-referrer");
    httpd_resp_set_hdr(request, "X-Content-Type-Options", "nosniff");
    httpd_resp_set_hdr(request, "X-Frame-Options", "DENY");
    httpd_resp_set_hdr(request, "Connection", "close");
    httpd_resp_set_hdr(request, "Content-Security-Policy",
                       "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
                       "img-src 'self' blob: data:; connect-src 'self'; base-uri 'none'; "
                       "form-action 'none'; frame-ancestors 'none'");
}

int Error(httpd_req_t* request, const char* status, const char* message) {
    httpd_resp_set_status(request, status);
    httpd_resp_set_type(request, "text/plain; charset=utf-8");
    httpd_resp_send(request, message, HTTPD_RESP_USE_STRLEN);
    // Also close the session when rejecting a request with an unread body.
    return ESP_FAIL;
}

// Strict and bounded query grammar: token=<32 hex chars>[&slot=<0..5>], either order.
// No decoding, duplicate parameters, trailing data or arbitrary filesystem paths.
bool ParseQuery(httpd_req_t* request, const char* expected, bool require_slot, int* slot) {
    char query[96];
    size_t length = httpd_req_get_url_query_len(request);
    if (length == 0 || length >= sizeof(query) ||
        httpd_req_get_url_query_str(request, query, sizeof(query)) != ESP_OK) {
        return false;
    }
    bool token_seen = false;
    bool slot_seen = false;
    std::string_view remaining(query, length);
    while (!remaining.empty()) {
        size_t end = remaining.find('&');
        std::string_view part = remaining.substr(0, end);
        if (part.starts_with("token=")) {
            if (token_seen || part.size() != 38 || expected[0] == '\0') {
                return false;
            }
            unsigned difference = 0;
            for (size_t i = 0; i < 32; ++i) {
                difference |= static_cast<unsigned char>(part[6 + i]) ^
                              static_cast<unsigned char>(expected[i]);
            }
            if (difference != 0) {
                return false;
            }
            token_seen = true;
        } else if (part.starts_with("slot=")) {
            if (slot_seen || part.size() != 6 || part[5] < '0' || part[5] > '5') {
                return false;
            }
            *slot = part[5] - '0';
            slot_seen = true;
        } else {
            return false;
        }
        if (end == std::string_view::npos) {
            break;
        }
        remaining.remove_prefix(end + 1);
        if (remaining.empty()) {
            return false;
        }
    }
    return token_seen && slot_seen == require_slot;
}

bool SameOrigin(httpd_req_t* request, const std::string& origin) {
    char value[80];
    size_t length = httpd_req_get_hdr_value_len(request, "Origin");
    if (length && (length >= sizeof(value) ||
                   httpd_req_get_hdr_value_str(request, "Origin", value, sizeof(value)) != ESP_OK ||
                   origin != value)) {
        return false;
    }
    length = httpd_req_get_hdr_value_len(request, "Sec-Fetch-Site");
    if (length &&
        (length >= sizeof(value) ||
         httpd_req_get_hdr_value_str(request, "Sec-Fetch-Site", value, sizeof(value)) != ESP_OK ||
         (strcmp(value, "same-origin") != 0 && strcmp(value, "none") != 0))) {
        return false;
    }
    return true;
}
}  // namespace

StopwatchBadgeStore::~StopwatchBadgeStore() {
    std::lock_guard lock(mutex_);
    if (available_) {
        esp_vfs_fat_spiflash_unmount_rw_wl(kMountPath, wl_handle_);
    }
}

bool StopwatchBadgeStore::Initialize() {
    std::lock_guard lock(mutex_);
    if (available_) {
        return true;
    }
    const esp_partition_t* partition = esp_partition_find_first(
        ESP_PARTITION_TYPE_DATA, ESP_PARTITION_SUBTYPE_DATA_FAT, kPartition);
    if (!partition || partition->size != kPartitionSize || partition->readonly) {
        ESP_LOGW(kTag, "A writable 2 MiB FAT partition labelled badge is required");
        return false;
    }
    // Check the entire raw partition BEFORE wl_mount can initialize its metadata.
    // Formatting is permitted only when every byte was erased on entry. Never
    // retry a corrupt/nonempty volume with an unconditional formatting option.
    const bool erased = FullyErased(partition);
    esp_vfs_fat_mount_config_t config = VFS_FAT_MOUNT_DEFAULT_CONFIG();
    config.format_if_mount_failed = erased;
    config.max_files = 3;
    config.allocation_unit_size = 4096;
    esp_err_t result = esp_vfs_fat_spiflash_mount_rw_wl(kMountPath, kPartition, &config, &wl_handle_);
    if (result != ESP_OK) {
        // IDF's failed FAT mount may still leave the wear-level handle allocated.
        if (wl_handle_ != WL_INVALID_HANDLE) {
            wl_unmount(wl_handle_);
            wl_handle_ = WL_INVALID_HANDLE;
        }
        ESP_LOGW(kTag, "Badge mount failed (no existing data formatted): %s", esp_err_to_name(result));
        return false;
    }
    available_ = true;
    for (int slot = 0; slot < kSlotCount; ++slot) {
        char path[40], backup[40], temporary[40];
        SlotPath(slot, path, sizeof(path));
        SlotPath(slot, backup, sizeof(backup), "bak");
        SlotPath(slot, temporary, sizeof(temporary), "tmp");
        struct stat info {};
        if (stat(backup, &info) == 0) {
            if (UsableFile(path)) {
                unlink(backup);  // A completed replacement with interrupted backup cleanup.
            } else if (UsableFile(backup)) {
                // Preserve the old badge after an interrupted/failed replacement.
                if (unlink(path) == 0 || errno == ENOENT) {
                    if (rename(backup, path) != 0) {
                        ESP_LOGW(kTag, "Badge %d rollback remains in backup", slot);
                    }
                }
            }
        }
        unlink(temporary);  // A temporary file was never committed.
    }
    if (!InstallDefaultBadges()) {
        ESP_LOGW(kTag, "Default badge setup incomplete; will retry missing slots on next boot");
    }
    for (int slot = 0; slot < kSlotCount; ++slot) {
        char path[40], backup[40];
        SlotPath(slot, path, sizeof(path));
        SlotPath(slot, backup, sizeof(backup), "bak");
        occupied_[slot] = UsableFile(path) || UsableFile(backup);
    }
    Settings settings(kSettingsNamespace);
    active_ = settings.GetInt("active", -1);
    ResolveActiveLocked();
    return true;
}

bool StopwatchBadgeStore::available() const {
    std::lock_guard lock(mutex_);
    return available_;
}

std::array<bool, StopwatchBadgeStore::kSlotCount> StopwatchBadgeStore::OccupiedSlots() {
    std::lock_guard lock(mutex_);
    return occupied_;
}

void StopwatchBadgeStore::SetActiveLocked(int slot) {
    if (active_ == slot) {
        return;
    }
    active_ = slot;
    // Same namespace/key and integer type as Settings. Use checked NVS writes here
    // because Settings::SetInt aborts on storage errors (e.g. a full NVS partition).
    nvs_handle_t handle = 0;
    esp_err_t result = nvs_open(kSettingsNamespace, NVS_READWRITE, &handle);
    if (result == ESP_OK) {
        result = nvs_set_i32(handle, "active", slot);
        if (result == ESP_OK) {
            result = nvs_commit(handle);
        }
        nvs_close(handle);
    }
    if (result != ESP_OK) {
        ESP_LOGW(kTag, "Badge selection is RAM-only: %s", esp_err_to_name(result));
    }
}

int StopwatchBadgeStore::ResolveActiveLocked() {
    if (ValidSlot(active_) && occupied_[active_]) {
        return active_;
    }
    for (int slot = 0; slot < kSlotCount; ++slot) {
        if (occupied_[slot]) {
            SetActiveLocked(slot);
            return slot;
        }
    }
    SetActiveLocked(-1);
    return -1;
}

int StopwatchBadgeStore::ActiveSlot() {
    std::lock_guard lock(mutex_);
    return available_ ? ResolveActiveLocked() : -1;
}

bool StopwatchBadgeStore::SetActiveSlot(int slot) {
    std::lock_guard lock(mutex_);
    if (!available_ || !ValidSlot(slot) || !occupied_[slot]) {
        return false;
    }
    SetActiveLocked(slot);
    return true;
}

int StopwatchBadgeStore::StepSlot(int direction) {
    std::lock_guard lock(mutex_);
    if (!available_) {
        return -1;
    }
    int active = ResolveActiveLocked();
    if (active < 0 || direction == 0) {
        return active;
    }
    int step = direction > 0 ? 1 : -1;
    for (int count = 1; count <= kSlotCount; ++count) {
        int slot = (active + step * count + kSlotCount) % kSlotCount;
        if (occupied_[slot]) {
            SetActiveLocked(slot);
            return slot;
        }
    }
    return active;
}

bool StopwatchBadgeStore::ReadJpegLocked(int slot, uint8_t** data, size_t* size) {
    *data = nullptr;
    *size = 0;
    if (!available_ || !ValidSlot(slot) || !occupied_[slot]) {
        return false;
    }
    char path[40];
    for (const char* extension : {"jpg", "bak"}) {
        SlotPath(slot, path, sizeof(path), extension);
        if (ReadFile(path, data, size)) {
            if (ValidJpeg(*data, *size)) {
                return true;
            }
            heap_caps_free(*data);
            *data = nullptr;
            *size = 0;
        }
    }
    return false;
}

bool StopwatchBadgeStore::ReadJpeg(int slot, uint8_t** data, size_t* size) {
    if (!data || !size) {
        return false;
    }
    std::lock_guard lock(mutex_);
    return ReadJpegLocked(slot, data, size);
}

bool StopwatchBadgeStore::DeleteSlot(int slot) {
    std::lock_guard lock(mutex_);
    if (!available_ || !ValidSlot(slot)) {
        return false;
    }
    // Remove any rollback copy first; a failed removal must not delete the live image.
    char path[40];
    SlotPath(slot, path, sizeof(path), "bak");
    if (unlink(path) != 0 && errno != ENOENT) {
        return false;
    }
    SlotPath(slot, path, sizeof(path));
    if (unlink(path) != 0 && errno != ENOENT) {
        return false;
    }
    occupied_[slot] = false;
    ResolveActiveLocked();
    return true;
}

bool StopwatchBadgeStore::SaveJpeg(int slot, const uint8_t* data, size_t size, std::string& error) {
    return SaveJpegInternal(slot, data, size, error, nullptr);
}

bool StopwatchBadgeStore::SaveJpegInternal(int slot, const uint8_t* data, size_t size,
                                           std::string& error, bool* invalid_image) {
    std::lock_guard lock(mutex_);
    error.clear();
    if (invalid_image) {
        *invalid_image = false;
    }
    if (!available_ || !ValidSlot(slot)) {
        error = "Badge storage or slot is unavailable.";
        return false;
    }
    uint8_t* pixels = nullptr;
    size_t pixel_size = 0;
    size_t stride = 0;
    esp_err_t decoded = DecodeJpeg(data, size, &pixels, &pixel_size, &stride);
    heap_caps_free(pixels);  // Validation only; do not retain another RGB565 copy.
    if (decoded != ESP_OK) {
        if (invalid_image) {
            *invalid_image = decoded != ESP_ERR_NO_MEM;
        }
        error = decoded == ESP_ERR_NO_MEM ? "Not enough memory to validate the JPEG."
                                          : "Expected a complete baseline 466 x 466 JPEG, at most 256 KiB.";
        return false;
    }
    char path[40], temporary[40], backup[40];
    SlotPath(slot, path, sizeof(path));
    SlotPath(slot, temporary, sizeof(temporary), "tmp");
    SlotPath(slot, backup, sizeof(backup), "bak");
    struct stat info {};
    if (stat(backup, &info) == 0 || errno != ENOENT) {
        error = "A previous badge needs recovery. Restart before replacing this slot.";
        return false;
    }
    bool previous = stat(path, &info) == 0;
    if (!previous && errno != ENOENT) {
        error = "Cannot inspect the previous badge.";
        return false;
    }
    int fd = open(temporary, O_WRONLY | O_CREAT | O_TRUNC, 0600);
    if (fd < 0) {
        error = "Cannot create the temporary badge. Storage may be full.";
        return false;
    }
    size_t written = 0;
    bool ok = true;
    while (written < size) {
        ssize_t count = write(fd, data + written, size - written);
        if (count <= 0) {
            ok = false;
            break;
        }
        written += static_cast<size_t>(count);
    }
    if (ok && fsync(fd) != 0) {
        ok = false;
    }
    if (close(fd) != 0) {
        ok = false;
    }
    if (!ok) {
        unlink(temporary);
        error = "Badge write failed. The previous badge was kept.";
        return false;
    }
    // FatFs f_rename does NOT replace an existing destination. Never unlink the
    // old image to make room: retain it in a rollback file until commit succeeds.
    if (previous && rename(path, backup) != 0) {
        unlink(temporary);
        error = "Cannot preserve the previous badge; upload was not committed.";
        return false;
    }
    if (rename(temporary, path) != 0) {
        if (previous && rename(backup, path) != 0) {
            ESP_LOGW(kTag, "Badge %d preserved in rollback backup", slot);
        }
        unlink(temporary);
        error = "Cannot commit the badge. The previous badge was kept.";
        return false;
    }
    if (previous && unlink(backup) != 0) {
        ESP_LOGW(kTag, "Badge %d committed; backup cleanup deferred until restart", slot);
    }
    occupied_[slot] = true;
    SetActiveLocked(slot);  // Match the watch: a successful upload becomes the active badge.
    return true;
}

std::unique_ptr<LvglImage> StopwatchBadgeStore::LoadActiveImage() {
    std::lock_guard lock(mutex_);
    if (!available_) {
        return nullptr;
    }
    // Revalidate disk data, not just its filename/header. If damaged, skip it and
    // recover selection without deleting anything that may be recoverable later.
    for (int attempts = 0; attempts < kSlotCount; ++attempts) {
        int slot = ResolveActiveLocked();
        if (slot < 0) {
            return nullptr;
        }
        uint8_t* data = nullptr;
        size_t size = 0;
        if (!ReadJpegLocked(slot, &data, &size)) {
            // A transient allocation/I/O failure must not erase occupied state.
            return nullptr;
        }
        HeapBytes jpeg(data);
        uint8_t* pixels = nullptr;
        size_t pixel_size = 0;
        size_t stride = 0;
        esp_err_t decoded = DecodeJpeg(data, size, &pixels, &pixel_size, &stride);
        HeapBytes buffer(pixels);
        if (decoded == ESP_ERR_NO_MEM) {
            return nullptr;
        }
        if (decoded != ESP_OK) {
            occupied_[slot] = false;
            continue;
        }
        auto* image = new (std::nothrow) LvglAllocatedImage(
            buffer.get(), pixel_size, kWidth, kHeight, stride, LV_COLOR_FORMAT_RGB565);
        if (!image) {
            return nullptr;
        }
        buffer.release();
        return std::unique_ptr<LvglImage>(image);
    }
    ResolveActiveLocked();
    return nullptr;
}

StopwatchBadgeEditor::StopwatchBadgeEditor(StopwatchBadgeStore& store,
                                           std::function<void()> on_changed)
    : store_(store), on_changed_(std::move(on_changed)) {}

StopwatchBadgeEditor::~StopwatchBadgeEditor() {
    Stop();
}

std::string StopwatchBadgeEditor::Start() {
    if (server_) {
        return url_;
    }
    if (!store_.available()) {
        return {};
    }
    esp_netif_t* station = esp_netif_get_handle_from_ifkey("WIFI_STA_DEF");
    esp_netif_ip_info_t info {};
    wifi_ap_record_t access_point {};
    if (!station || !esp_netif_is_netif_up(station) ||
        esp_wifi_sta_get_ap_info(&access_point) != ESP_OK ||
        esp_netif_get_ip_info(station, &info) != ESP_OK || info.ip.addr == 0) {
        return {};
    }
    uint8_t random[16];
    esp_fill_random(random, sizeof(random));
    constexpr char hex[] = "0123456789abcdef";
    for (size_t i = 0; i < sizeof(random); ++i) {
        token_[i * 2] = hex[random[i] >> 4];
        token_[i * 2 + 1] = hex[random[i] & 15];
    }
    token_.back() = '\0';
    char origin[64];
    snprintf(origin, sizeof(origin), "http://" IPSTR ":8080", IP2STR(&info.ip));
    origin_ = origin;
    httpd_config_t config = HTTPD_DEFAULT_CONFIG();
    config.server_port = 8080;
    config.ctrl_port = 32769;  // Separate from the Wi-Fi provisioning server.
    config.stack_size = 10240;
    config.max_open_sockets = 4;
    config.max_uri_handlers = 1;
    config.max_resp_headers = 10;
    config.recv_wait_timeout = 3;
    config.send_wait_timeout = 3;
    config.lru_purge_enable = true;
    config.uri_match_fn = httpd_uri_match_wildcard;
    esp_err_t result = httpd_start(&server_, &config);
    if (result != ESP_OK) {
        server_ = nullptr;
        token_.fill(0);
        origin_.clear();
        ESP_LOGW(kTag, "Badge editor start failed: %s", esp_err_to_name(result));
        return {};
    }
    httpd_uri_t handler {};
    handler.uri = "/*";
    handler.method = static_cast<httpd_method_t>(HTTP_ANY);
    handler.handler = &StopwatchBadgeEditor::HandleRequest;
    handler.user_ctx = this;
    if (httpd_register_uri_handler(server_, &handler) != ESP_OK) {
        Stop();
        return {};
    }
    url_ = origin_ + "/?token=" + token_.data();
    return url_;
}

void StopwatchBadgeEditor::Stop() {
    if (server_) {
        // Wait for the HTTP task before clearing fields it reads. Never call Stop
        // from an HTTP callback or while holding the store's mutex.
        httpd_stop(server_);
        server_ = nullptr;
    }
    token_.fill(0);
    origin_.clear();
    url_.clear();
}

int StopwatchBadgeEditor::HandleRequest(httpd_req* request) {
    return static_cast<StopwatchBadgeEditor*>(request->user_ctx)->Handle(request);
}

int StopwatchBadgeEditor::Handle(httpd_req* request) {
    SecurityHeaders(request);
    std::string_view uri(request->uri);
    std::string_view path = uri.substr(0, uri.find('?'));
    bool slot_endpoint = path == "/badge/image" || path == "/badge/active" || path == "/upload";
    int slot = -1;
    if (!ParseQuery(request, token_.data(), slot_endpoint, &slot)) {
        return Error(request, "403 Forbidden", "Invalid editor token or query.");
    }
    if (!SameOrigin(request, origin_)) {
        return Error(request, "403 Forbidden", "Cross-origin requests are not allowed.");
    }
    if (request->content_len > StopwatchBadgeStore::kMaxJpegSize) {
        return Error(request, "413 Content Too Large", "Maximum JPEG size is 256 KiB.");
    }
    bool upload = path == "/upload" && request->method == HTTP_POST;
    if (!upload && request->content_len != 0) {
        return Error(request, "400 Bad Request", "This endpoint does not accept a body.");
    }
    if (path == "/" && request->method == HTTP_GET) {
        httpd_resp_set_type(request, "text/html; charset=utf-8");
        return httpd_resp_send(request, kStopwatchBadgeEditorHtml,
                               sizeof(kStopwatchBadgeEditorHtml) - 1);
    }
    if (path == "/badge/state" && request->method == HTTP_GET) {
        char response[192];
        {
            std::lock_guard lock(store_.mutex_);
            const auto& slots = store_.occupied_;
            snprintf(response, sizeof(response),
                     "{\"available\":%s,\"active\":%d,\"slots\":[%s,%s,%s,%s,%s,%s]}",
                     store_.available_ ? "true" : "false", store_.ResolveActiveLocked(),
                     slots[0] ? "true" : "false", slots[1] ? "true" : "false",
                     slots[2] ? "true" : "false", slots[3] ? "true" : "false",
                     slots[4] ? "true" : "false", slots[5] ? "true" : "false");
        }
        httpd_resp_set_type(request, "application/json");
        return httpd_resp_send(request, response, HTTPD_RESP_USE_STRLEN);
    }
    if (path == "/badge/image" && request->method == HTTP_GET) {
        uint8_t* data = nullptr;
        size_t size = 0;
        if (!store_.ReadJpeg(slot, &data, &size)) {
            return Error(request, "404 Not Found", "Badge is empty or could not be read.");
        }
        HeapBytes jpeg(data);
        httpd_resp_set_type(request, "image/jpeg");
        return httpd_resp_send(request, reinterpret_cast<const char*>(data), size);
    }
    if (path == "/badge/active" && request->method == HTTP_POST) {
        if (!store_.SetActiveSlot(slot)) {
            return Error(request, "409 Conflict", "Select an occupied badge slot.");
        }
    } else if (path == "/badge/image" && request->method == HTTP_DELETE) {
        if (!store_.DeleteSlot(slot)) {
            return Error(request, "500 Internal Server Error", "Could not delete the badge.");
        }
    } else if (upload) {
        if (request->content_len == 0) {
            return Error(request, "400 Bad Request", "JPEG body is required.");
        }
        char type[32];
        if (httpd_req_get_hdr_value_str(request, "Content-Type", type, sizeof(type)) != ESP_OK ||
            strcmp(type, "image/jpeg") != 0) {
            return Error(request, "415 Unsupported Media Type", "Upload a canvas-exported JPEG.");
        }
        HeapBytes data(AllocateBytes(request->content_len));
        if (!data) {
            return Error(request, "503 Service Unavailable", "Not enough memory for this upload.");
        }
        size_t received = 0;
        int64_t deadline = esp_timer_get_time() + kUploadTimeoutUs;
        while (received < request->content_len) {
            if (esp_timer_get_time() >= deadline) {
                return Error(request, "408 Request Timeout", "Upload deadline exceeded.");
            }
            int count = httpd_req_recv(request, reinterpret_cast<char*>(data.get() + received),
                                       std::min<size_t>(4096, request->content_len - received));
            if (count <= 0) {
                return Error(request,
                             count == HTTPD_SOCK_ERR_TIMEOUT ? "408 Request Timeout"
                                                             : "400 Bad Request",
                             "Upload interrupted; previous badge was kept.");
            }
            received += static_cast<size_t>(count);
        }
        std::string error;
        bool invalid_image = false;
        if (!store_.SaveJpegInternal(slot, data.get(), received, error, &invalid_image)) {
            return Error(request,
                         invalid_image ? "422 Unprocessable Content" : "500 Internal Server Error",
                         error.c_str());
        }
    } else {
        bool known = path == "/" || path == "/badge/state" || slot_endpoint;
        if (known) {
            httpd_resp_set_hdr(request, "Allow", path == "/badge/image" ? "GET, DELETE"
                                                : path == "/badge/active" || path == "/upload"
                                                    ? "POST"
                                                    : "GET");
        }
        return Error(request, known ? "405 Method Not Allowed" : "404 Not Found",
                     known ? "Method not allowed." : "Unknown endpoint.");
    }
    if (on_changed_) {
        on_changed_();
    }
    httpd_resp_set_type(request, "application/json");
    return httpd_resp_send(request, "{\"ok\":true}", HTTPD_RESP_USE_STRLEN);
}

#endif  // CONFIG_STOPWATCH_LOCAL_APPS
