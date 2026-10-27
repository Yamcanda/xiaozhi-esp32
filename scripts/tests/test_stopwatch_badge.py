"""Extracted-production badge parser/query/upload regression tests.

Requires only a host C++23 compiler. JPEG decode, heap freeing, HTTP query access
and selection persistence are stubbed; these tests do NOT exercise the real JPEG
codec, NVS, ESP-IDF FAT/wear levelling, HTTP sockets or physical hardware. Upload
writes/renames use real temporary host files with injected syscall failures.
The miniature JPEG fixture is structural, not a fully decodable photograph.
"""
import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
BADGE = ROOT / "main/boards/m5stack/stopwatch/stopwatch_badge.cc"
COMPILER = shutil.which("c++") or shutil.which("g++")

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
#include <fstream>
#include <iostream>
#include <mutex>
#include <string>
#include <string_view>
#include <sys/stat.h>
#include <unistd.h>
#include <vector>
#ifdef _WIN32
#include <io.h>
#endif
using esp_err_t = int;
constexpr int ESP_OK = 0, ESP_ERR_INVALID_ARG = 1, ESP_ERR_NO_MEM = 2;
#define ESP_LOGW(...) ((void)0)

// Only query access is stubbed: ParseQuery itself is extracted verbatim below.
struct httpd_req_t { std::string query; };
size_t httpd_req_get_url_query_len(httpd_req_t* request) {
    return request->query.size();
}
int httpd_req_get_url_query_str(httpd_req_t* request, char* buffer, size_t size) {
    if (request->query.size() >= size) return -1;
    std::memcpy(buffer, request->query.c_str(), request->query.size() + 1);
    return ESP_OK;
}

const char* mount_path = nullptr;
class StopwatchBadgeStore {
public:
    static constexpr int kSlotCount = 6, kWidth = 466, kHeight = 466;
    static constexpr size_t kMaxJpegSize = 256 * 1024;
    bool SaveJpegInternal(int, const uint8_t*, size_t, std::string&, bool*);
    std::mutex mutex_;
    bool available_ = true;
    int active_ = -1, selection_writes_ = 0;
    std::array<bool, kSlotCount> occupied_{};

    // No NVS on the host. Record calls that change the selected slot instead.
    void SetActiveLocked(int slot) {
        if (active_ != slot) {
            active_ = slot;
            ++selection_writes_;
        }
    }
};
bool ValidSlot(int slot) { return slot >= 0 && slot < StopwatchBadgeStore::kSlotCount; }
void SlotPath(int slot, char* buffer, size_t size, const char* extension = "jpg") {
    std::snprintf(buffer, size, "%s/slot_%d.%s", mount_path, slot, extension);
}
void heap_caps_free(void* buffer) { std::free(buffer); }
'''

STUBS = r'''
// Real decoding is tested on the device, not by this structural JPEG fixture.
esp_err_t DecodeJpeg(const uint8_t* data, size_t size, uint8_t** pixels,
                     size_t* length, size_t* stride) {
    *pixels = nullptr;
    if (!ValidJpeg(data, size)) return ESP_ERR_INVALID_ARG;
    *pixels = static_cast<uint8_t*>(std::malloc(8));
    assert(*pixels);
    *length = *stride = 8;
    return ESP_OK;
}
int fail_write = 0, fail_sync = 0, fail_close = 0, rename_count = 0;
unsigned fail_renames = 0;
ssize_t test_write(int fd, const void* data, size_t size) {
    if (fail_write) { errno = ENOSPC; return -1; }
    return ::write(fd, data, size);
}
int test_fsync(int fd) {
    if (fail_sync) { errno = EIO; return -1; }
#ifdef _WIN32
    return ::_commit(fd);
#else
    return ::fsync(fd);
#endif
}
int test_close(int fd) {
    int result = ::close(fd);
    if (fail_close) { errno = EIO; return -1; }
    return result;
}
int test_rename(const char* from, const char* to) {
    ++rename_count;
    if (fail_renames & (1u << rename_count)) { errno = EIO; return -1; }
    return std::rename(from, to);
}
#define write test_write
#define fsync test_fsync
#define close test_close
#define rename test_rename
'''

TESTS = r'''
#undef write
#undef fsync
#undef close
#undef rename
std::vector<uint8_t> SampleJpeg() {
    return {255,216,255,192,0,11,8,1,210,1,210,1,1,17,0,
            255,218,0,8,1,1,0,0,63,0,18,255,217};
}
std::vector<uint8_t> LoadFile(const char* extension = "jpg") {
    char path[4096];
    SlotPath(0, path, sizeof(path), extension);
    std::ifstream file(path, std::ios::binary);
    return {std::istreambuf_iterator<char>(file), {}};
}
void Seed(const std::vector<uint8_t>& data) {
    for (auto extension : {"jpg", "bak", "tmp"}) {
        char path[4096];
        SlotPath(0, path, sizeof(path), extension);
        std::remove(path);
    }
    char path[4096];
    SlotPath(0, path, sizeof(path));
    std::ofstream file(path, std::ios::binary);
    assert(file);
    file.write(reinterpret_cast<const char*>(data.data()), data.size());
    assert(file);
}
int main(int argc, char** argv) {
    assert(argc == 3);
    mount_path = argv[2];
    const std::string test = argv[1];
    auto good = SampleJpeg();
    int cases = 0;
    if (test == "parser") {
        assert(ValidJpeg(good.data(), good.size()));
        ++cases;
        for (size_t length = 0; length < good.size(); ++length) {
            assert(!ValidJpeg(good.data(), length));
            ++cases;
        }
        // Bad length, progressive SOF, precision, dimensions, sampling, scan,
        // missing EOI and invalid SOI, respectively.
        for (auto change : std::array<std::pair<int, int>, 8>{{
                 {5,0}, {3,194}, {6,12}, {8,211}, {13,0}, {23,62}, {27,192}, {1,0}}}) {
            auto bad = good;
            bad[change.first] = change.second;
            assert(!ValidJpeg(bad.data(), bad.size()));
            ++cases;
        }
        auto bad = good;
        bad.push_back(0);
        assert(!ValidJpeg(bad.data(), bad.size()));
        ++cases;
        bad.resize(256 * 1024 + 1);
        assert(!ValidJpeg(bad.data(), bad.size()));
        ++cases;
        assert(cases == 39);
    } else if (test == "query") {
        std::string token(32, 'a');
        httpd_req_t request;
        int slot = -1;
        for (auto query : {"token=" + token, "token=" + token + "&slot=5",
                           "slot=0&token=" + token}) {
            request.query = query;
            assert(ParseQuery(&request, token.c_str(),
                              query.find("slot=") != std::string::npos, &slot));
            ++cases;
        }
        for (auto query : {std::string(), "token=" + token + "&",
                           "token=" + token + "&token=" + token,
                           "token=" + token + "&slot=6", "token=" + token + "&slot=-1",
                           "token=" + token + "&slot=00",
                           "token=" + token + "&slot=0&slot=1",
                           "token=" + token + "&x=1", "token=" + std::string(32, 'b'),
                           "token=" + token + "%00"}) {
            request.query = query;
            assert(!ParseQuery(&request, token.c_str(),
                               query.find("slot=") != std::string::npos, &slot));
            ++cases;
        }
        assert(cases == 13);
    } else if (test == "storage") {
        StopwatchBadgeStore store;
        std::string error;
        bool invalid = false;
        auto old = good;
        old[25] = 19;
        auto reset = [&] {
            Seed(old);
            fail_write = fail_sync = fail_close = rename_count = 0;
            fail_renames = 0;
            store.active_ = 2;
            store.occupied_[0] = true;
        };
        reset();
        assert(store.SaveJpegInternal(0, good.data(), good.size(), error, &invalid));
        assert(LoadFile() == good && store.active_ == 0 && store.selection_writes_ == 1);
        ++cases;
        assert(store.SaveJpegInternal(0, good.data(), good.size(), error, &invalid));
        assert(store.selection_writes_ == 1);
        ++cases;
        for (int failure = 0; failure < 5; ++failure) {
            reset();
            if (failure == 0) fail_write = 1;
            if (failure == 1) fail_sync = 1;
            if (failure == 2) fail_close = 1;
            if (failure == 3) fail_renames = 1u << 1;
            if (failure == 4) fail_renames = 1u << 2;
            assert(!store.SaveJpegInternal(0, good.data(), good.size(), error, &invalid));
            assert(LoadFile() == old && store.active_ == 2);
            ++cases;
        }
        reset();
        fail_renames = (1u << 2) | (1u << 3);
        assert(!store.SaveJpegInternal(0, good.data(), good.size(), error, &invalid));
        assert(LoadFile("bak") == old);
        ++cases;
        reset();
        auto bad = good;
        bad[3] = 194;
        assert(!store.SaveJpegInternal(0, bad.data(), bad.size(), error, &invalid));
        assert(invalid && LoadFile() == old);
        ++cases;
        reset();
        assert(!store.SaveJpegInternal(-1, good.data(), good.size(), error, &invalid));
        assert(LoadFile() == old);
        ++cases;
        assert(cases == 10);
    } else {
        assert(false);
    }
    std::cout << test << ": " << cases << " badge cases passed\n";
}
'''


@unittest.skipUnless(COMPILER, "requires a host C++23 compiler")
class StopwatchBadgeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="stopwatch-badge-test-")
        cls.addClassCleanup(cls.tmp.cleanup)
        binary_name = "badge-test.exe" if os.name == "nt" else "badge-test"
        cls.binary = str(pathlib.Path(cls.tmp.name) / binary_name)
        production = BADGE.read_text(encoding="utf-8")
        # Slice complete adjacent production functions, without copying their logic.
        parser = production[production.index("bool ValidJpeg("):
                            production.index("\nesp_err_t DecodeJpeg(")]
        query = production[production.index("bool ParseQuery("):
                           production.index("\nbool SameOrigin(")]
        save = production[production.index("bool StopwatchBadgeStore::SaveJpegInternal("):
                          production.index("\nstd::unique_ptr<LvglImage> "
                                           "StopwatchBadgeStore::LoadActiveImage(")]
        source = PREFIX + parser + query + STUBS + save + TESTS
        subprocess.run(
            [COMPILER, "-std=c++23", "-fno-exceptions", "-Wall", "-Wextra", "-Werror",
             "-x", "c++", "-", "-o", cls.binary],
            input=source, text=True, check=True, timeout=60,
        )

    def run_case(self, name):
        # Production paths fit a fixed 40-byte buffer. Keep the host mount prefix
        # short too, rather than placing a long Windows temp path in that buffer.
        subprocess.run([self.binary, name, "."], cwd=self.tmp.name,
                       check=True, timeout=15)

    def test_baseline_dimensions_size_and_truncations(self):
        self.run_case("parser")

    def test_token_query_and_slot_validation(self):
        self.run_case("query")

    def test_upload_selection_and_write_rollback_failures(self):
        self.run_case("storage")


if __name__ == "__main__":
    unittest.main()
