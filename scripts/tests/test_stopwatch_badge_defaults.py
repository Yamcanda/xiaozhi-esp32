"""Default badges: extracted production initialization and real-file regressions.

Uses only stdlib Python and a host C++23 compiler; CMake isolation checks are
optional when CMake is unavailable. The actual JPEG parser, seeding helper and
store initialization run against the six original assets and temporary host
files. JPEG pixel decoding, ESP partition mounting and NVS are stubbed, so these
tests do not validate the real decoder, FAT/wear levelling, flash size or hardware.
"""
import os
import pathlib
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
BOARD = ROOT / "main/boards/m5stack/stopwatch"
BADGE = BOARD / "stopwatch_badge.cc"
COMPILER = shutil.which("c++") or shutil.which("g++")
CMAKE = shutil.which("cmake")
ASSETS = (
    "01-status-dnd.jpg",
    "02-status-focus.jpg",
    "03-status-chill.jpg",
    "04-pet-shiba.jpg",
    "05-mars.jpg",
    "06-earth.jpg",
)


def function(source, signature):
    """Extract a complete production function, without reimplementing its logic."""
    start = source.index(signature)
    brace = source.index("{", start)
    depth, end = 1, brace + 1
    while depth:
        depth += (source[end] == "{") - (source[end] == "}")
        end += 1
    return source[start:end] + "\n"


PREFIX = r'''
#include <algorithm>
#include <array>
#include <cassert>
#include <cerrno>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <fcntl.h>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <map>
#include <memory>
#include <mutex>
#include <string>
#include <sys/stat.h>
#include <unistd.h>
#include <vector>
#ifdef _WIN32
#include <io.h>
#endif
using esp_err_t = int;
constexpr int ESP_OK = 0, ESP_ERR_INVALID_ARG = 1, ESP_ERR_NO_MEM = 2;
constexpr int ESP_PARTITION_TYPE_DATA = 0, ESP_PARTITION_SUBTYPE_DATA_FAT = 0;
constexpr int WL_INVALID_HANDLE = -1;
constexpr char kMountPath[] = ".", kPartition[] = "badge", kSettingsNamespace[] = "sw_badge";
constexpr size_t kPartitionSize = 2 * 1024 * 1024;
#define ESP_LOGW(...) ((void)0)
#define ESP_LOGI(...) ((void)0)

int persisted_active = -1, selection_writes = 0, mounts = 0;
struct esp_partition_t { size_t size = kPartitionSize; bool readonly = false; } partition;
const esp_partition_t* esp_partition_find_first(int, int, const char*) { return &partition; }
bool FullyErased(const esp_partition_t*) { return true; }
struct esp_vfs_fat_mount_config_t {
    bool format_if_mount_failed = false;
    int max_files = 0;
    size_t allocation_unit_size = 0;
};
esp_vfs_fat_mount_config_t VFS_FAT_MOUNT_DEFAULT_CONFIG() { return {}; }
int esp_vfs_fat_spiflash_mount_rw_wl(const char*, const char*,
                                   const esp_vfs_fat_mount_config_t*, int32_t* handle) {
    ++mounts;
    *handle = 1;
    return ESP_OK;
}
int wl_unmount(int32_t) { return ESP_OK; }
class Settings {
public:
    explicit Settings(const char*) {}
    int GetInt(const char*, int) { return persisted_active; }
};

// The constants below are inserted from the actual public header.
class StopwatchBadgeStore {
public:
'''

STORE = r'''
    bool Initialize();
    bool DeleteSlot(int);
    int ResolveActiveLocked();
    void SetActiveLocked(int slot) {
        if (active_ != slot) {
            active_ = persisted_active = slot;
            ++selection_writes;
        }
    }
    std::mutex mutex_;
    bool available_ = false;
    int32_t wl_handle_ = WL_INVALID_HANDLE;
    int active_ = -1;
    std::array<bool, kSlotCount> occupied_{};
};

struct Fault {
    std::string operation, filename;
    bool triggered = false;
} fault;
std::map<int, std::string> opened;
int allocations = 0, decode_calls = 0, write_calls = 0;
size_t short_write_limit = 0;
bool Fails(const char* operation, const char* path) {
    if (!fault.triggered && fault.operation == operation &&
        fault.filename == std::filesystem::path(path).filename().string()) {
        fault.triggered = true;
        errno = EIO;
        return true;
    }
    return false;
}
int test_stat(const char* path, struct stat* info) {
    if (Fails("stat", path)) { errno = EACCES; return -1; }
    return ::stat(path, info);
}
int test_open(const char* path, int flags, int mode = 0) {
    if (Fails("open", path)) return -1;
#ifdef _WIN32
    // ESP-IDF has binary POSIX files; match that on the Windows host CRT.
    flags |= O_BINARY;
#endif
    int fd = ::open(path, flags, mode);
    if (fd >= 0) opened[fd] = path;
    return fd;
}
ssize_t test_write(int fd, const void* data, size_t size) {
    ++write_calls;
    if (Fails("write", opened.at(fd).c_str())) { errno = ENOSPC; return -1; }
    if (Fails("zero-write", opened.at(fd).c_str())) return 0;
    if (short_write_limit) size = std::min(size, short_write_limit);
    return ::write(fd, data, size);
}
int test_fsync(int fd) {
    if (Fails("fsync", opened.at(fd).c_str())) return -1;
#ifdef _WIN32
    return ::_commit(fd);
#else
    return ::fsync(fd);
#endif
}
int test_close(int fd) {
    bool fail = Fails("close", opened.at(fd).c_str());
    opened.erase(fd);
    int result = ::close(fd);
    return fail ? -1 : result;
}
int test_rename(const char* from, const char* to) {
    if (Fails("rename", to)) return -1;
    // FatFs does not replace existing destinations; retain that behavior here.
    if (std::filesystem::exists(to)) { errno = EEXIST; return -1; }
    return std::rename(from, to);
}
uint8_t* AllocateBytes(size_t size) {
    auto* bytes = static_cast<uint8_t*>(std::malloc(size));
    if (bytes) ++allocations;
    return bytes;
}
void heap_caps_free(void* bytes) {
    if (bytes) { --allocations; assert(allocations >= 0); }
    std::free(bytes);
}
struct HeapDeleter { void operator()(uint8_t* bytes) const { heap_caps_free(bytes); } };
using HeapBytes = std::unique_ptr<uint8_t, HeapDeleter>;
std::vector<uint8_t> Bytes(const std::string& path) {
    std::ifstream stream(path, std::ios::binary);
    return {std::istreambuf_iterator<char>(stream), {}};
}
'''

DECODER = r'''
esp_err_t DecodeJpeg(const uint8_t* data, size_t size, uint8_t** pixels,
                     size_t* pixel_size, size_t* stride) {
    ++decode_calls;
    *pixels = nullptr;
    *pixel_size = *stride = 0;
    for (int slot = 0; slot < StopwatchBadgeStore::kSlotCount; ++slot) {
        if (data == kDefaultBadges[slot].start &&
            Fails("decode", ("slot_" + std::to_string(slot) + ".jpg").c_str())) {
            return ESP_ERR_NO_MEM;
        }
    }
    if (!ValidJpeg(data, size)) return ESP_ERR_INVALID_ARG;
    *pixels = AllocateBytes(8);
    assert(*pixels);
    *pixel_size = *stride = 8;
    return ESP_OK;
}
#define stat(...) test_stat(__VA_ARGS__)
#define open test_open
#define write test_write
#define fsync test_fsync
#define close test_close
#define rename test_rename
'''

TESTS = r'''
#undef stat
#undef open
#undef write
#undef fsync
#undef close
#undef rename
const char* kMarker = "defaults-v1";
std::string Slot(int slot, const char* extension = "jpg") {
    return "slot_" + std::to_string(slot) + "." + extension;
}
void Put(const std::string& path, const std::vector<uint8_t>& bytes) {
    std::ofstream stream(path, std::ios::binary);
    assert(stream);
    stream.write(reinterpret_cast<const char*>(bytes.data()), bytes.size());
    assert(stream);
}
void Complete() {
    assert(std::filesystem::is_regular_file(kMarker));
    assert(!std::filesystem::exists("defaults.tmp"));
    for (int slot = 0; slot < StopwatchBadgeStore::kSlotCount; ++slot) {
        assert(Bytes(Slot(slot)) == kDefaultData[slot]);
        assert(!std::filesystem::exists(Slot(slot, "tmp")));
    }
    assert(allocations == 0 && opened.empty());
}
int main(int argc, char** argv) {
    assert(argc >= 2);
    const std::string test = argv[1];
    if (test == "assets") {
        size_t total = 0;
        for (const auto& bytes : kDefaultData) {
            assert(ValidJpeg(bytes.data(), bytes.size()));
            total += bytes.size();
        }
        assert(total < kPartitionSize);
    } else if (test == "fresh" || test == "short-writes") {
        if (test == "short-writes") short_write_limit = 17;
        StopwatchBadgeStore store;
        assert(store.Initialize() && store.available_);
        Complete();
        for (bool occupied : store.occupied_) assert(occupied);
        assert(store.active_ == 0 && persisted_active == 0 && selection_writes == 1);
        int writes_before = write_calls;
        assert(store.Initialize() && mounts == 1 && write_calls == writes_before);
    } else if (test == "preserve") {
        // Different valid user files, a corrupt file, a recoverable rollback copy,
        // a completed replacement with an old backup, and an invalid backup.
        Put(Slot(0), kDefaultData[5]);
        Put(Slot(1), {1, 2, 3});
        Put(Slot(2, "bak"), kDefaultData[4]);
        Put(Slot(3), kDefaultData[0]);
        Put(Slot(3, "bak"), kDefaultData[1]);
        Put(Slot(4, "bak"), {4, 5, 6});
        persisted_active = 2;
        StopwatchBadgeStore store;
        assert(store.Initialize());
        assert(Bytes(Slot(0)) == kDefaultData[5]);
        assert(Bytes(Slot(1)) == std::vector<uint8_t>({1, 2, 3}));
        assert(Bytes(Slot(2)) == kDefaultData[4]);
        assert(Bytes(Slot(3)) == kDefaultData[0]);
        assert(!std::filesystem::exists(Slot(4)));
        assert(Bytes(Slot(4, "bak")) == std::vector<uint8_t>({4, 5, 6}));
        assert(Bytes(Slot(5)) == kDefaultData[5]);
        assert(std::filesystem::is_regular_file(kMarker));
        const std::array<bool, 6> expected = {true, false, true, true, false, true};
        assert(store.occupied_ == expected);
        assert(store.active_ == 2 && persisted_active == 2 && selection_writes == 0);
    } else if (test == "backup-rename") {
        Put(Slot(2, "bak"), kDefaultData[4]);
        fault = {"rename", Slot(2), false};
        persisted_active = 2;
        StopwatchBadgeStore store;
        assert(store.Initialize() && fault.triggered);
        assert(Bytes(Slot(2, "bak")) == kDefaultData[4]);
        assert(!std::filesystem::exists(Slot(2)));
        assert(store.occupied_[2] && store.active_ == 2 && selection_writes == 0);
        assert(std::filesystem::is_regular_file(kMarker));
    } else if (test == "delete-reboot") {
        persisted_active = 4;
        StopwatchBadgeStore first;
        assert(first.Initialize() && first.active_ == 4 && selection_writes == 0);
        assert(first.DeleteSlot(4));
        assert(!std::filesystem::exists(Slot(4)) && first.active_ == 0);
        int writes_before = write_calls;
        StopwatchBadgeStore reboot;
        assert(reboot.Initialize() && write_calls == writes_before);
        assert(!reboot.occupied_[4] && !std::filesystem::exists(Slot(4)));
        assert(reboot.active_ == 0 && std::filesystem::is_regular_file(kMarker));
        // Erasing storage also erases the marker and allows factory seeding again.
        for (const auto& entry : std::filesystem::directory_iterator(".")) {
            std::filesystem::remove_all(entry.path());
        }
        StopwatchBadgeStore erased;
        assert(erased.Initialize());
        Complete();
    } else if (test == "retry-initialize") {
        fault = {"write", Slot(2, "tmp"), false};
        StopwatchBadgeStore interrupted;
        assert(interrupted.Initialize() && interrupted.available_ && fault.triggered);
        assert(!std::filesystem::exists(kMarker) && !std::filesystem::exists(Slot(2)));
        assert(Bytes(Slot(0)) == kDefaultData[0] && interrupted.occupied_[0]);
        Put(Slot(1), kDefaultData[5]);
        persisted_active = 1;
        selection_writes = 0;
        fault = {};
        StopwatchBadgeStore reboot;
        assert(reboot.Initialize() && reboot.active_ == 1 && selection_writes == 0);
        assert(Bytes(Slot(1)) == kDefaultData[5]);
        for (int slot = 2; slot < StopwatchBadgeStore::kSlotCount; ++slot) {
            assert(Bytes(Slot(slot)) == kDefaultData[slot] && reboot.occupied_[slot]);
        }
        assert(std::filesystem::is_regular_file(kMarker));
    } else if (test == "interrupted-temporaries") {
        // Simulate a power loss, not a handled I/O error: committed slot 0 plus
        // truncated slot/marker temporaries that never reached their final names.
        Put(Slot(0), kDefaultData[5]);
        Put(Slot(1, "tmp"), {0xff, 0xd8});
        Put("defaults.tmp", {1});
        persisted_active = 0;
        StopwatchBadgeStore reboot;
        assert(reboot.Initialize() && reboot.active_ == 0 && selection_writes == 0);
        assert(Bytes(Slot(0)) == kDefaultData[5]);
        for (int slot = 1; slot < StopwatchBadgeStore::kSlotCount; ++slot) {
            assert(Bytes(Slot(slot)) == kDefaultData[slot]);
            assert(!std::filesystem::exists(Slot(slot, "tmp")));
        }
        assert(std::filesystem::is_regular_file(kMarker));
        assert(!std::filesystem::exists("defaults.tmp"));
    } else if (test == "fault") {
        assert(argc == 4);
        fault = {argv[2], argv[3], false};
        assert(!InstallDefaultBadges() && fault.triggered);
        assert(!std::filesystem::exists(kMarker));
        assert(!std::filesystem::exists("defaults.tmp"));
        assert(allocations == 0 && opened.empty());
        // Whatever was already committed must survive byte-for-byte on retry.
        std::map<std::string, std::vector<uint8_t>> committed;
        for (int slot = 0; slot < StopwatchBadgeStore::kSlotCount; ++slot) {
            if (std::filesystem::exists(Slot(slot))) committed[Slot(slot)] = Bytes(Slot(slot));
            assert(!std::filesystem::exists(Slot(slot, "tmp")));
        }
        fault = {};
        int decodes_before = decode_calls;
        assert(InstallDefaultBadges());
        assert(decode_calls - decodes_before ==
               StopwatchBadgeStore::kSlotCount - static_cast<int>(committed.size()));
        Complete();
        for (const auto& [path, bytes] : committed) assert(Bytes(path) == bytes);
    } else if (test == "marker-file") {
        // The committed regular marker is authoritative even with no live images.
        Put(kMarker, {});
        StopwatchBadgeStore store;
        assert(store.Initialize() && store.active_ == -1 && selection_writes == 0);
        for (bool occupied : store.occupied_) assert(!occupied);
        assert(decode_calls == 0 && write_calls == 0);
    } else if (test == "marker-directory") {
        std::filesystem::create_directory(kMarker);
        assert(!InstallDefaultBadges());
        for (int slot = 0; slot < StopwatchBadgeStore::kSlotCount; ++slot) {
            assert(!std::filesystem::exists(Slot(slot)));
        }
        assert(std::filesystem::is_directory(kMarker));
    } else {
        assert(false);
    }
    assert(allocations == 0 && opened.empty());
    std::cout << test << ": default badge checks passed\n";
}
'''


@unittest.skipUnless(COMPILER, "requires a host C++23 compiler")
class StopwatchBadgeDefaultsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="stopwatch-badge-defaults-")
        cls.addClassCleanup(cls.tmp.cleanup)
        cls.binary = pathlib.Path(cls.tmp.name) / (
            "badge-defaults.exe" if os.name == "nt" else "badge-defaults"
        )
        production = BADGE.read_text(encoding="utf-8")
        header = (BOARD / "stopwatch_badge.h").read_text(encoding="utf-8")
        constants = "\n".join(re.findall(r"    static constexpr [^\n]+;", header))
        badge_struct = re.search(r"struct DefaultBadge\s*\{.*?\};", production, re.S).group()
        assets = ",\n".join(
            f'    Bytes(R"({(ROOT / "image" / name).as_posix()})")' for name in ASSETS
        )
        fixtures = badge_struct + "\n" + (
            "const std::array<std::vector<uint8_t>, StopwatchBadgeStore::kSlotCount> "
            "kDefaultData = {{\n" + assets + "\n}};\n"
            "const std::array<DefaultBadge, StopwatchBadgeStore::kSlotCount> kDefaultBadges = [] {\n"
            "    std::array<DefaultBadge, StopwatchBadgeStore::kSlotCount> result{};\n"
            "    for (size_t slot = 0; slot < result.size(); ++slot) {\n"
            "        const auto& bytes = kDefaultData[slot];\n"
            "        result[slot] = {bytes.data(), bytes.data() + bytes.size()};\n"
            "    }\n    return result;\n}();\n"
        )
        source = PREFIX + constants + STORE + fixtures
        source += function(production, "bool ValidSlot(")
        source += function(production, "void SlotPath(")
        source += function(production, "bool ValidJpeg(")
        source += DECODER
        source += function(production, "bool ReadFile(")
        source += function(production, "bool UsableFile(")
        source += function(production, "bool InstallDefaultBadges(")
        source += function(production, "bool StopwatchBadgeStore::Initialize(")
        source += function(production, "int StopwatchBadgeStore::ResolveActiveLocked(")
        source += function(production, "bool StopwatchBadgeStore::DeleteSlot(")
        source += TESTS
        subprocess.run(
            [COMPILER, "-std=c++23", "-fno-exceptions", "-Wall", "-Wextra", "-Werror",
             "-x", "c++", "-", "-o", str(cls.binary)],
            input=source, text=True, check=True, timeout=60,
        )

    def run_case(self, *args):
        # Keep the mount path '.' short enough for production's 40-byte paths.
        with tempfile.TemporaryDirectory(dir=self.tmp.name) as directory:
            subprocess.run([str(self.binary), *args], cwd=directory, check=True, timeout=20)

    def test_all_original_assets_pass_the_production_jpeg_parser(self):
        self.run_case("assets")

    def test_fresh_storage_gets_six_defaults_and_selects_once(self):
        self.run_case("fresh")

    def test_partial_writes_are_completed(self):
        self.run_case("short-writes")

    def test_existing_files_backups_and_selection_are_preserved(self):
        self.run_case("preserve")

    def test_recoverable_backup_is_not_overwritten_when_recovery_rename_fails(self):
        self.run_case("backup-rename")

    def test_deletion_survives_reboot_and_filesystem_erasure_allows_reseed(self):
        self.run_case("delete-reboot")

    def test_failed_seeding_is_nonfatal_and_retries_only_missing_slots(self):
        self.run_case("retry-initialize")

    def test_uncommitted_temporaries_from_power_loss_are_recovered(self):
        self.run_case("interrupted-temporaries")

    def test_slot_validation_io_and_stat_failures_do_not_commit_marker(self):
        for operation, path in (
            ("decode", "slot_2.jpg"),
            ("open", "slot_2.tmp"),
            ("write", "slot_2.tmp"),
            ("zero-write", "slot_2.tmp"),
            ("fsync", "slot_2.tmp"),
            ("close", "slot_2.tmp"),
            ("rename", "slot_2.jpg"),
            ("stat", "slot_2.jpg"),
            ("stat", "slot_2.bak"),
            ("stat", "defaults-v1"),
        ):
            with self.subTest(operation=operation, path=path):
                self.run_case("fault", operation, path)

    def test_marker_io_failures_are_retried_without_replacing_committed_slots(self):
        for operation, path in (
            ("open", "defaults.tmp"),
            ("write", "defaults.tmp"),
            ("zero-write", "defaults.tmp"),
            ("fsync", "defaults.tmp"),
            ("close", "defaults.tmp"),
            ("rename", "defaults-v1"),
        ):
            with self.subTest(operation=operation, path=path):
                self.run_case("fault", operation, path)

    def test_a_committed_marker_alone_prevents_reseeding(self):
        self.run_case("marker-file")

    def test_nonregular_marker_is_not_treated_as_committed(self):
        self.run_case("marker-directory")


class StopwatchBadgeDefaultAssetsTests(unittest.TestCase):
    def test_embedded_symbol_mapping_matches_original_six_asset_order(self):
        source = BADGE.read_text(encoding="utf-8")
        symbols = re.findall(
            r'extern const uint8_t (\w+)\[\] asm\("(_binary_[^"]+)"\);', source
        )
        symbols = dict(symbols)
        array = source[source.index("const std::array<DefaultBadge,"):
                       source.index(";", source.index("const std::array<DefaultBadge,"))]
        pairs = re.findall(r"\{(\w+),\s*(\w+)\}", array)
        expected = []
        for name in ASSETS:
            symbol = "_binary_" + re.sub(r"[^A-Za-z0-9_]", "_", name)
            expected.append((symbol + "_start", symbol + "_end"))
        self.assertEqual([(symbols[start], symbols[end]) for start, end in pairs], expected)
        self.assertGreater(source.index("extern const uint8_t"),
                           source.index("#if CONFIG_STOPWATCH_LOCAL_APPS"))
        self.assertLess(source.index("const std::array<DefaultBadge,"),
                        source.rindex("#endif"))

    @unittest.skipUnless(CMAKE, "requires CMake for isolated feature-condition evaluation")
    def test_cmake_embeds_only_for_stopwatch_apps_and_not_early_expansion(self):
        source = (ROOT / "main/CMakeLists.txt").read_text(encoding="utf-8")
        # Execute the production conditional only; do not configure/build firmware.
        start = source.rfind("\nif(", 0, source.index("set(STOPWATCH_BADGE_IMAGES")) + 1
        end = source.index("\nendif()", start) + len("\nendif()")
        block = source[start:end]
        self.assertIn("target_add_binary_data", block)
        self.assertGreater(start, source.index("idf_component_register("))
        with tempfile.TemporaryDirectory(prefix="stopwatch-badge-cmake-") as directory:
            script = pathlib.Path(directory) / "check.cmake"
            result = pathlib.Path(directory) / "images.txt"
            script.write_text(
                'set(COMPONENT_LIB host-main)\n'
                f'set(PROJECT_DIR "{ROOT.as_posix()}")\n'
                f'file(WRITE "{result.as_posix()}" "")\n'
                'function(target_add_binary_data target path kind)\n'
                '  if(NOT target STREQUAL "host-main" OR NOT kind STREQUAL "BINARY")\n'
                '    message(FATAL_ERROR "Wrong embedding target or asset mode")\n'
                '  endif()\n'
                f'  file(APPEND "{result.as_posix()}" "${{path}}\\n")\n'
                'endfunction()\n' + block + '\n',
                encoding="utf-8",
            )
            for board in (0, 1):
                for apps in (0, 1):
                    for early in (0, 1):
                        with self.subTest(board=board, apps=apps, early=early):
                            subprocess.run(
                                [CMAKE, f"-DCONFIG_BOARD_TYPE_M5STACK_STOPWATCH={board}",
                                 f"-DCONFIG_STOPWATCH_LOCAL_APPS={apps}",
                                 f"-DCMAKE_BUILD_EARLY_EXPANSION={early}", "-P", str(script)],
                                check=True, capture_output=True, text=True, timeout=15,
                            )
                            expected = [str(ROOT / "image" / name) for name in ASSETS]
                            actual = result.read_text(encoding="utf-8").splitlines()
                            self.assertEqual(
                                [pathlib.Path(path) for path in actual],
                                [pathlib.Path(path) for path in expected]
                                if board and apps and not early else [],
                            )


if __name__ == "__main__":
    unittest.main()
