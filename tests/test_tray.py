#!/usr/bin/env python3
"""Logic tests for waydroid-tray.

Run:  QT_QPA_PLATFORM=offscreen python3 tests/test_tray.py          # stub waydroid
      QT_QPA_PLATFORM=offscreen python3 tests/test_tray.py --real   # real waydroid

The --real run actually starts and stops Waydroid on this machine
(and leaves it running, which is where it starts from when Waydroid
was already up).
"""
import importlib.util
import os
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("wt", REPO / "waydroid-tray.py")
wt = importlib.util.module_from_spec(spec)
sys.modules["wt"] = wt
spec.loader.exec_module(wt)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PyQt6.QtCore import QCoreApplication
from PyQt6.QtWidgets import QApplication  # noqa: E402

app = QApplication([])
REAL = "--real" in sys.argv

results: list[tuple[str, bool]] = []


def check(name: str, cond: bool) -> None:
    results.append((name, bool(cond)))
    print(("PASS  " if cond else "FAIL  ") + name, flush=True)


def wait_state(monitor, want, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if monitor.state == want:
            return True
        QCoreApplication.processEvents()
        time.sleep(0.05)
    return monitor.state == want


if not REAL:
    stub_dir = Path("/tmp/waydroid-tray-stub")
    stub_dir.mkdir(exist_ok=True)
    stub = stub_dir / "waydroid"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        'case "$1" in\n'
        "  status)\n"
        '    if [ "${STUB_FAIL_STATUS:-0}" = "1" ]; then echo "stub failure" >&2; exit 1; fi\n'
        '    state=$(cat "$STUB_STATE_FILE" 2>/dev/null || echo stopped)\n'
        '    if [ "$state" = stopped ]; then\n'
        "      printf 'Session:\\tSTOPPED\\nVendor type:\\tMAINLINE\\n'\n"
        "    else\n"
        "      printf 'Session:\\tRUNNING\\nContainer:\\tRUNNING\\n"
        "Vendor type:\\tMAINLINE\\nIP address:\\t192.168.240.112\\n'\n"
        "    fi ;;\n"
        "  session)\n"
        '    case "$2" in\n'
        '      start) sleep "${STUB_BOOT_S:-2}"; echo RUNNING > "$STUB_STATE_FILE" ;;\n'
        '      stop)  sleep 1; echo stopped > "$STUB_STATE_FILE" ;;\n'
        "    esac ;;\n"
        "esac\n"
    )
    stub.chmod(0o755)
    os.environ["STUB_STATE_FILE"] = str(stub_dir / "state")
    Path(os.environ["STUB_STATE_FILE"]).write_text("stopped")
    os.environ["PATH"] = f"{stub_dir}:{os.environ['PATH']}"

monitor = wt.Monitor(verbose=True)
T0 = time.monotonic()
transitions = []
monitor.stateChanged.connect(
    lambda s, d: transitions.append((round(time.monotonic() - T0, 1), s, d)))
tray = wt.Tray(monitor)

check("initial state settles to stopped (grey)", wait_state(monitor, wt.STATE_STOPPED, 8))

gate = wt.DoubleClickGate()
check("click gate: single click is not a toggle", gate.feed() is False)
check("click gate: quick second click toggles", gate.feed() is True)
check("click gate: later click is a fresh single",
      gate.feed(time.monotonic() + 5) is False)

# --- start -------------------------------------------------------------
check("start allowed when stopped", monitor.can_start())
check("toggle requests start", monitor.request_toggle() == "start requested")
print(f"  t={time.monotonic() - T0:.1f} waiting for transition...", flush=True)
ok = wait_state(monitor, wt.STATE_TRANSITION, 8)
print(f"  t={time.monotonic() - T0:.1f} transition={ok} state={monitor.state} "
      f"transitions={transitions}", flush=True)
check("orange transition shown", ok)
check("clicks swallowed during transition", "ignored" in monitor.request_toggle())
check("start settles to green", wait_state(monitor, wt.STATE_RUNNING, 60))

# --- gating while running ----------------------------------------------
check("start NOT allowed while running", monitor.can_start() is False)
check("stop allowed while running", monitor.can_stop())

# --- stop --------------------------------------------------------------
check("toggle requests stop", monitor.request_toggle() == "stop requested")
check("stop settles to grey", wait_state(monitor, wt.STATE_STOPPED, 60))
check("stop NOT allowed while stopped", monitor.can_stop() is False)
for _ in range(5):
    QCoreApplication.processEvents(); time.sleep(0.02)
check("menu: Start enabled when stopped", tray._start_action.isEnabled())
check("menu: Start enabled when stopped", tray._start_action.isEnabled())
check("menu: Stop greyed out when stopped", tray._stop_action.isEnabled() is False)

# --- transition timeout -> red (stub only; real timeouts are 45-90 s) --
if not REAL:
    monitor._stop_worker.set()
    wt.START_TIMEOUT_S = 3.0
    os.environ["STUB_BOOT_S"] = "60"          # boot hangs
    m2 = wt.Monitor(verbose=True)
    m2.request_toggle()
    check("start that never settles turns red", wait_state(m2, wt.STATE_ERROR, 15))
    check("start allowed on error (retry)", m2.can_start())
    check("menu: Start enabled on error", m2._pending_action is None and True)
    os.environ["STUB_BOOT_S"] = "1"
    Path(os.environ["STUB_STATE_FILE"]).write_text("stopped")
    check("red resolves to grey after error latch",
          wait_state(m2, wt.STATE_STOPPED, 25))
    m2._stop_worker.set()

    # --- status command failing -> red ---------------------------------
    os.environ["STUB_FAIL_STATUS"] = "1"
    m3 = wt.Monitor(verbose=True)
    check("failing status checks turn red", wait_state(m3, wt.STATE_ERROR, 15))
    os.environ["STUB_FAIL_STATUS"] = "0"
    m3._stop_worker.set()

# --- real waydroid integration ----------------------------------------
if REAL:
    check("real: initial state resolves", wait_state(
        monitor, wt.STATE_RUNNING, 15) or wait_state(monitor, wt.STATE_STOPPED, 15))
    was_running = monitor.state == wt.STATE_RUNNING
    if was_running:
        check("real: stop -> grey", (monitor.request_toggle() == "stop requested")
              and wait_state(monitor, wt.STATE_STOPPED, 60))
    check("real: start -> orange -> green",
          (monitor.request_toggle() == "start requested")
          and wait_state(monitor, wt.STATE_TRANSITION, 10)
          and wait_state(monitor, wt.STATE_RUNNING, 90))
    ok = False
    deadline = time.monotonic() + 6
    while time.monotonic() < deadline:
        QCoreApplication.processEvents(); time.sleep(0.1)
        tip = tray.toolTip()
        if "IP 192.168" in tip or "Uptime" in tip:
            ok = True
            break
    check("real: tooltip reports IP/uptime", ok)

monitor._stop_worker.set()

failed = [name for name, ok in results if not ok]
if failed:
    print("observed transitions:", transitions, flush=True)
print(f"\n{len(results) - len(failed)}/{len(results)} passed", flush=True)
sys.exit(1 if failed else 0)
