"""Exercise StopWatch's actual battery getter with noisy PMIC readings."""
import pathlib
import shutil
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
BOARD = ROOT / "main/boards/m5stack/stopwatch/m5stack_stopwatch.cc"
COMPILER = shutil.which("c++") or shutil.which("g++")


def function(source, signature):
    start = source.index(signature)
    brace = source.index("{", start)
    depth, end = 1, brace + 1
    while depth:
        depth += (source[end] == "{") - (source[end] == "}")
        end += 1
    return source[start:end]


@unittest.skipUnless(COMPILER, "requires a host C++ compiler")
class StopwatchBatteryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="stopwatch-battery-test-")
        cls.addClassCleanup(cls.tmp.cleanup)
        binary_name = "battery-test.exe" if COMPILER.endswith(".exe") else "battery-test"
        cls.binary = str(pathlib.Path(cls.tmp.name) / binary_name)
        board = BOARD.read_text(encoding="utf-8")
        source = r'''
#include <algorithm>
#include <cassert>
#include <cstdint>
#include <mutex>
#include <string>
constexpr int M5PM1_OK = 0, PMIC_PIN_CHARGE_STATE = 2;
struct Pmic {
    uint16_t voltage_mv = 3800;
    int charge_state = 1;
    bool voltage_valid = true;
    int readVbat(uint16_t* voltage) {
        if (!voltage_valid) return -1;
        *voltage = voltage_mv;
        return M5PM1_OK;
    }
    int digitalRead(int pin) {
        assert(pin == PMIC_PIN_CHARGE_STATE);
        return charge_state;
    }
};
struct BatteryProvider {
    virtual bool GetBatteryLevel(int&, bool&, bool&) = 0;
};
class TestBoard : public BatteryProvider {
public:
    Pmic pmic_;
'''
        source += "    std::mutex battery_mutex_;\n"
        source += "    " + next(line.strip() for line in board.splitlines()
                               if line.strip().startswith("int last_battery_level_")) + "\n"
        source += function(board, "bool GetBatteryLevel(int& level,")
        source += r'''
    void Expect(int voltage_mv, int charge_state, int expected) {
        pmic_.voltage_mv = voltage_mv;
        pmic_.charge_state = charge_state;
        int level = -1;
        bool charging = true, discharging = true;
        assert(GetBatteryLevel(level, charging, discharging));
        assert(level == expected);
        assert(charging == (charge_state == 0));
        assert(discharging == (charge_state > 0));
    }
    void FailRead() {
        pmic_.voltage_valid = false;
        int level = -1;
        bool charging = false, discharging = false;
        assert(!GetBatteryLevel(level, charging, discharging));
        pmic_.voltage_valid = true;
    }
};
int main(int argc, char** argv) {
    assert(argc == 2);
    std::string test = argv[1];
    TestBoard board;
    if (test == "rebound") {
        board.Expect(4000, 1, 75);
        board.Expect(3992, 1, 74);
        board.Expect(4000, 1, 74);
        board.Expect(4008, 1, 74);
        board.Expect(3984, 1, 73);
        for (int i = 0; i < 100; ++i) {
            board.Expect(i % 2 ? 4000 : 3984, 1, 73);
        }
        board.Expect(3960, 1, 70);
        board.Expect(3992, 1, 70);
    } else if (test == "charging_and_unplug") {
        board.Expect(3800, 1, 50);
        board.Expect(3840, 0, 55);
        board.Expect(3880, 0, 60);
        board.Expect(3872, 1, 59);
        board.Expect(3880, 1, 59);
        board.Expect(3960, 0, 70);
        board.Expect(3952, 1, 69);
        board.Expect(3960, 1, 69);
    } else if (test == "voltage_failure") {
        board.FailRead();
        board.Expect(4000, 1, 75);
        board.Expect(3992, 1, 74);
        board.FailRead();
        board.Expect(4008, 1, 74);
        board.Expect(4040, 0, 80);
        board.FailRead();
        board.Expect(4048, 1, 80);
    } else if (test == "unknown_charge_state") {
        board.Expect(3800, -1, 50);
        board.Expect(3840, -1, 50);
        board.Expect(3792, -1, 49);
        board.Expect(3880, 0, 60);
        board.Expect(3888, -1, 60);
        board.Expect(3872, 1, 59);
        board.Expect(3880, 1, 59);
    } else if (test == "bounds_and_boot") {
        board.Expect(4300, 1, 100);
        board.Expect(4200, 1, 100);
        board.Expect(4192, 1, 99);
        board.Expect(4300, 1, 99);
        board.Expect(3400, 1, 0);
        board.Expect(3300, 1, 0);
        board.Expect(3800, 1, 0);
        board.Expect(4200, 0, 100);
        TestBoard rebooted;
        rebooted.Expect(3800, 1, 50);
    } else {
        assert(false);
    }
}
'''
        subprocess.run([COMPILER, "-std=c++17", "-Wall", "-Wextra", "-x", "c++", "-",
                        "-o", cls.binary], input=source, text=True, check=True, timeout=60)

    def test_discharge_readings_never_rebound(self):
        subprocess.run([self.binary, "rebound"], check=True, timeout=10)

    def test_charging_allows_increase_and_unplug_resumes_filter(self):
        subprocess.run([self.binary, "charging_and_unplug"], check=True, timeout=10)

    def test_failed_voltage_reads_preserve_history(self):
        subprocess.run([self.binary, "voltage_failure"], check=True, timeout=10)

    def test_unknown_charge_state_does_not_allow_increase(self):
        subprocess.run([self.binary, "unknown_charge_state"], check=True, timeout=10)

    def test_bounds_and_new_boot(self):
        subprocess.run([self.binary, "bounds_and_boot"], check=True, timeout=10)
