#!/usr/bin/env python3
"""waydroid-tray — a system tray icon to start and stop Waydroid on demand.

States and colors:
    grey    stopped
    green   running
    orange  starting / stopping (transitional — clicks are swallowed)
    red     error (start/stop failed, timed out, or inconsistent state)

Interaction:
    double-click  toggle start/stop (only from a stable state, never queued)
    right-click   context menu: Start / Stop / Quit — unavailable entries greyed

The tray app runs entirely as the desktop user: "start" launches
`waydroid session start`, which boots the LXC container, the Android
session and (on the waydroid-nvidia fork) the wd-venus render service.
"stop" runs `waydroid session stop`, which takes all of it down again.

License: MIT — see the LICENSE file.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from PyQt6.QtCore import QLockFile, QObject, QStandardPaths, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QAction, QColor, QIcon, QImage, QPainter, QPixmap
from PyQt6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

# ---------------------------------------------------------------- constants

POLL_SECONDS = 2.0
START_TIMEOUT_S = 90.0      # start must settle within this, else error
STOP_TIMEOUT_S = 45.0       # stop must settle within this, else error
MIXED_GRACE_S = 20.0        # container/session mismatch without action -> error
MIN_TRANSITION_S = 1.5      # orange always shows at least this long
ERROR_LATCH_S = 10.0        # red stays visible at least this long
DOUBLE_CLICK_MS = 400       # two clicks within this window count as a toggle
CPU_WINDOW_S = 2.0          # cgroup cpu.stat sampling window

STATE_STOPPED = "stopped"
STATE_RUNNING = "running"
STATE_TRANSITION = "transition"
STATE_ERROR = "error"

STATE_COLORS = {
    STATE_STOPPED: "#8a8f98",      # grey
    STATE_RUNNING: "#22c55e",      # green
    STATE_TRANSITION: "#f59e0b",   # orange
    STATE_ERROR: "#ef4444",        # red
}

STATE_LABELS = {
    STATE_STOPPED: "Stopped",
    STATE_RUNNING: "Running",
    STATE_TRANSITION: "Starting/stopping…",
    STATE_ERROR: "Error",
}

CGROUP_BASE = Path("/sys/fs/cgroup/lxc.payload.waydroid")
LOG_DIR = Path(
    os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")
) / "waydroid-tray"


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------- status


@dataclass
class Status:
    session: str | None          # "RUNNING"/"STOPPED"; None means not reported
    container: str | None        # absent from output when stopped
    ip: str | None

    @property
    def session_up(self) -> bool:
        return self.session == "RUNNING"

    @property
    def container_up(self) -> bool:
        return self.container == "RUNNING"


def parse_status(text: str) -> Status:
    def grab(key: str) -> str | None:
        m = re.search(rf"^{key}:\s*(\S+)", text, re.MULTILINE)
        return m.group(1) if m else None

    return Status(
        session=grab("Session"),
        container=grab("Container"),      # absent when stopped
        ip=grab("IP address"),
    )


def read_container_stats(prev: tuple[float, int] | None):
    """Return (ram_bytes, cpu_percent, new_sample); (None, None, prev) if n/a."""
    try:
        ram = int((CGROUP_BASE / "memory.current").read_text().strip())
        usage = None
        for line in (CGROUP_BASE / "cpu.stat").read_text().splitlines():
            key, val = line.split()
            if key == "usage_usec":
                usage = int(val)
                break
        if usage is None:
            return ram, None, prev
        now = time.monotonic()
        cpu = None
        if prev is not None:
            dt = now - prev[0]
            if dt > 0:
                cpu = (usage - prev[1]) / (dt * 1e6) * 100.0
        return ram, cpu, (now, usage)
    except (OSError, ValueError):
        return None, None, prev


def human_ram(num: int | None) -> str:
    if num is None:
        return "?"
    value = float(num)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            return f"{value:.0f} {unit}" if unit in ("B", "KiB") else f"{value:.1f} {unit}"
        value /= 1024
    return "?"


# ---------------------------------------------------------------- icons

_ICON_CACHE: dict[str, QIcon] = {}


class DoubleClickGate:
    """Converts a stream of single clicks into double-click events.

    feed() returns True exactly on the second click of a pair (clicks
    closer than the window apart); singles return False and are never
    queued across longer gaps.
    """

    def __init__(self, window_ms: float = DOUBLE_CLICK_MS) -> None:
        self._window = window_ms
        self._last = 0.0

    def feed(self, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        if (now - self._last) * 1000.0 <= self._window:
            self._last = 0.0
            return True
        self._last = now
        return False


def _base_pixmap() -> QPixmap:
    """The Waydroid logo, downscaled to a sane working size for tinting."""
    icon = QIcon.fromTheme("waydroid")
    if not icon.isNull():
        pm = icon.pixmap(128, 128)
        if not pm.isNull():
            return pm
    for path in ("/usr/share/icons/hicolor/512x512/apps/waydroid.png",
                 "/usr/share/pixmaps/waydroid.png"):
        if os.path.exists(path):
            return QPixmap(path).scaled(
                128, 128, Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation)
    # Fallback: a plain disc with "W"
    pm = QPixmap(128, 128)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setBrush(QColor("#4a5568"))
    p.setPen(Qt.PenStyle.NoPen)
    p.drawEllipse(8, 8, 112, 112)
    p.setPen(QColor("#ffffff"))
    f = p.font()
    f.setPixelSize(64)
    f.setBold(True)
    p.setFont(f)
    p.drawText(pm.rect(), Qt.AlignmentFlag.AlignCenter, "W")
    p.end()
    return pm


def state_icon(state: str) -> QIcon:
    if state in _ICON_CACHE:
        return _ICON_CACHE[state]
    base = _base_pixmap().toImage().convertToFormat(QImage.Format.Format_ARGB32)
    color = QColor(STATE_COLORS[state])
    cr, cg, cb = color.redF(), color.greenF(), color.blueF()
    for y in range(base.height()):
        for x in range(base.width()):
            px = base.pixelColor(x, y)
            a = px.alphaF()
            if a == 0.0:
                continue
            lum = (0.2126 * px.red() + 0.7152 * px.green() + 0.0722 * px.blue()) / 255.0
            shade = 0.45 + 0.55 * lum          # keep dark strokes dark
            base.setPixelColor(x, y, QColor.fromRgbF(cr * shade, cg * shade, cb * shade, a))
    icon = QIcon(QPixmap.fromImage(base))
    _ICON_CACHE[state] = icon
    return icon


# ---------------------------------------------------------------- monitor


class Monitor(QObject):
    """Polls `waydroid status` in a worker thread and runs the state machine."""

    stateChanged = pyqtSignal(str, str)   # (state, detail)
    tooltipChanged = pyqtSignal(str)

    def __init__(self, verbose: bool = False):
        super().__init__()
        self._verbose = verbose
        self._lock = threading.RLock()   # reentrant: _launch may report errors
        self._pending_action: str | None = None   # "start"/"stop" from the UI
        self._target: str | None = None           # desired final state, if any
        self._action_at = 0.0
        self._mixed_since: float | None = None
        self._error_until = 0.0
        self._error_detail = ""
        self.state = STATE_STOPPED
        self._cpu_sample: tuple[float, int] | None = None
        self._status: Status | None = None
        self._session_proc: subprocess.Popen | None = None
        self._running_since: float | None = None
        self._stop_worker = threading.Event()
        self._thread = threading.Thread(target=self._worker, daemon=True)
        self._thread.start()

    # ---- UI-facing API (main thread)

    def can_start(self) -> bool:
        return self.state in (STATE_STOPPED, STATE_ERROR)

    def can_stop(self) -> bool:
        return self.state == STATE_RUNNING

    def can_show_ui(self) -> bool:
        return self.state == STATE_RUNNING

    def request_toggle(self) -> str:
        """Returns a short reason string for logging; never queues clicks."""
        with self._lock:
            if self.can_start():
                self._pending_action = "start"
                return "start requested"
            if self.can_stop():
                self._pending_action = "stop"
                return "stop requested"
            return f"ignored: state is '{self.state}' (clicks are not queued)"

    # ---- worker thread

    def _worker(self) -> None:
        while not self._stop_worker.is_set():
            started = time.monotonic()
            try:
                out = subprocess.run(
                    ["waydroid", "status"],
                    capture_output=True, text=True, timeout=5,
                )
                status = parse_status(out.stdout) if out.returncode == 0 else None
                failed = out.returncode != 0
                stderr_tail = (out.stderr or "").strip().splitlines()[-1:] or [""]
            except FileNotFoundError:
                status, failed, stderr_tail = None, True, ["waydroid binary not found"]
            except subprocess.TimeoutExpired:
                status, failed, stderr_tail = None, True, ["status check timed out"]

            ram, cpu, self._cpu_sample = read_container_stats(self._cpu_sample)
            state, detail = self._advance(status, failed, stderr_tail[0] if stderr_tail else "")
            if state != self.state:
                if self._verbose:
                    log(f"state: {self.state} -> {state}  ({detail})")
                self.state = state
                self.stateChanged.emit(state, detail)
            self.tooltipChanged.emit(self._tooltip(status, ram, cpu))
            self._status = status

            self._stop_worker.wait(max(0.0, POLL_SECONDS - (time.monotonic() - started)))

    def _advance(self, s: Status | None, failed: bool, err: str) -> tuple[str, str]:
        now = time.monotonic()
        launch_action = None
        with self._lock:
            # consume a UI request
            if self._pending_action:
                launch_action = self._pending_action
                self._pending_action = None
                self._target = "running" if launch_action == "start" else "stopped"
                self._action_at = now
                self._error_until = 0.0
                if self._verbose:
                    log(f"action: {launch_action}")
        if launch_action:
            self._launch(launch_action)   # outside the lock; may take seconds
        with self._lock:

            # error latch: keep red visible at least ERROR_LATCH_S
            if self.state == STATE_ERROR and now < self._error_until:
                return STATE_ERROR, self._error_detail

            session_up = bool(s and s.session_up)
            container_up = bool(s and s.container_up)
            both_up = session_up and container_up
            both_down = (not session_up) and (not container_up)

            if failed and s is None:
                # transient status failure: tolerate 3 misses before red
                self._fail_count = getattr(self, "_fail_count", 0) + 1
                if self._fail_count >= 3:
                    self._enter_error(now, f"status check failed: {err}")
                    return STATE_ERROR, self._error_detail
                return (STATE_ERROR if now < self._error_until else self.state), self._error_detail
            self._fail_count = 0

            if self._target:
                settled = ((self._target == "running" and both_up) or
                           (self._target == "stopped" and both_down))
                if settled and now - self._action_at >= MIN_TRANSITION_S:
                    self._target = None
                    self._running_since = now if both_up else None
                    return (STATE_RUNNING if both_up else STATE_STOPPED), ""
                if settled:
                    return STATE_TRANSITION, "settling…"   # hold orange briefly
                limit = START_TIMEOUT_S if self._target == "running" else STOP_TIMEOUT_S
                if now - self._action_at > limit:
                    target = self._target
                    self._target = None
                    self._enter_error(now, f"{target} did not complete within {limit:.0f}s")
                    return STATE_ERROR, self._error_detail
                return STATE_TRANSITION, f"{self._target}…"

            # no action in flight
            if both_up:
                self._mixed_since = None
                if self._running_since is None:
                    self._running_since = now
                return STATE_RUNNING, ""
            if both_down:
                self._mixed_since = None
                self._running_since = None
                return STATE_STOPPED, ""

            # mixed state without action: graceful, then error
            if self._mixed_since is None:
                self._mixed_since = now
            if now - self._mixed_since > MIXED_GRACE_S:
                self._enter_error(
                    now,
                    f"inconsistent state (container={'up' if container_up else 'down'}, "
                    f"session={'up' if session_up else 'down'})",
                )
                return STATE_ERROR, self._error_detail
            return STATE_TRANSITION, "changing…"

    def _enter_error(self, now: float, detail: str) -> None:
        self._error_until = now + ERROR_LATCH_S
        self._error_detail = detail
        log(f"ERROR: {detail}")

    # ---- command execution (worker thread)

    def _launch(self, action: str) -> None:
        try:
            if action == "start":
                if self._session_proc and self._session_proc.poll() is None:
                    return  # session manager already alive
                LOG_DIR.mkdir(parents=True, exist_ok=True)
                log_file = open(LOG_DIR / "session-start.log", "ab")
                self._session_proc = subprocess.Popen(
                    ["waydroid", "session", "start"],
                    stdout=subprocess.DEVNULL, stderr=log_file,
                    start_new_session=True,
                )
                # fail fast if it dies immediately (e.g. container error)
                time.sleep(2.0)
                if self._session_proc.poll() not in (None, 0):
                    with self._lock:
                        self._enter_error(
                            time.monotonic(),
                            f"session start exited with code {self._session_proc.returncode}"
                            " (see ~/.local/state/waydroid-tray/session-start.log)",
                        )
            else:  # stop
                subprocess.run(
                    ["waydroid", "session", "stop"],
                    capture_output=True, text=True, timeout=STOP_TIMEOUT_S,
                )
        except FileNotFoundError:
            with self._lock:
                self._enter_error(time.monotonic(), "waydroid binary not found")
        except subprocess.TimeoutExpired:
            with self._lock:
                self._enter_error(time.monotonic(), "session stop timed out")

    # ---- tooltip

    def _tooltip(self, s: Status | None, ram: int | None, cpu: float | None) -> str:
        lines = [f"Waydroid: {STATE_LABELS[self.state]}"]
        if self.state == STATE_ERROR and self._error_detail:
            lines.append(self._error_detail)
        if self.state in (STATE_RUNNING, STATE_TRANSITION) or ram is not None:
            parts = []
            if ram is not None:
                parts.append(f"RAM {human_ram(ram)}")
            if cpu is not None:
                parts.append(f"CPU {cpu:.0f}%")
            if parts:
                lines.append(" · ".join(parts))
        if s and s.ip:
            lines.append(f"IP {s.ip}")
        if self._running_since and self.state == STATE_RUNNING:
            lines.append(f"Uptime {human_uptime(time.monotonic() - self._running_since)}")
        return "\n".join(lines)


def human_uptime(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h}h {m:02d}m" if h else f"{m}m {s:02d}s"


# ---------------------------------------------------------------- tray


class Tray(QSystemTrayIcon):
    def __init__(self, monitor: Monitor, verbose: bool = False):
        super().__init__(state_icon(STATE_STOPPED))
        self._monitor = monitor
        self._verbose = verbose
        self._click_gate = DoubleClickGate()

        self.setToolTip("Waydroid: starting…")

        menu = QMenu()
        self._start_action = QAction("Start Waydroid")
        self._start_action.triggered.connect(self._menu_start)
        self._stop_action = QAction("Stop Waydroid")
        self._stop_action.triggered.connect(self._menu_stop)
        self._ui_action = QAction("Show Waydroid UI")
        self._ui_action.triggered.connect(self._menu_show_ui)
        quit_action = QAction("Quit")
        quit_action.triggered.connect(self._quit)
        menu.addAction(self._start_action)
        menu.addAction(self._stop_action)
        menu.addSeparator()
        menu.addAction(self._ui_action)
        menu.addSeparator()
        menu.addAction(quit_action)
        self.setContextMenu(menu)

        self.activated.connect(self._on_activated)
        monitor.stateChanged.connect(self._on_state)
        monitor.tooltipChanged.connect(self.setToolTip)

        self._on_state(STATE_STOPPED, "")   # sync menu to initial state
        self.show()

    # ---- interactions

    def _on_activated(self, reason) -> None:
        # Plasma sends one Trigger per physical click; Qt may additionally
        # synthesize DoubleClick. Both paths below yield exactly one toggle.
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self._toggle()
            return
        if reason != QSystemTrayIcon.ActivationReason.Trigger:
            return
        if self._click_gate.feed():
            self._toggle()

    def _toggle(self) -> None:
        reason = self._monitor.request_toggle()
        if self._verbose:
            log(f"toggle: {reason}")

    def _menu_start(self) -> None:
        if self._monitor.can_start():
            self._toggle()

    def _menu_stop(self) -> None:
        if self._monitor.can_stop():
            self._toggle()

    def _menu_show_ui(self) -> None:
        if self._monitor.can_show_ui():
            subprocess.Popen(
                ["waydroid", "show-full-ui"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True)

    def _quit(self) -> None:
        # never touches the running/stopped waydroid state
        QApplication.quit()

    # ---- state sync

    def _on_state(self, state: str, detail: str) -> None:
        self.setIcon(state_icon(state))
        self._start_action.setEnabled(self._monitor.can_start())
        self._stop_action.setEnabled(self._monitor.can_stop())
        self._ui_action.setEnabled(self._monitor.can_show_ui())


# ---------------------------------------------------------------- main


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="log state transitions to stderr")
    args = parser.parse_args()

    app = QApplication(sys.argv)
    app.setApplicationName("waydroid-tray")
    app.setApplicationDisplayName("Waydroid Tray")
    app.setQuitOnLastWindowClosed(False)     # tray-only application

    lock = QLockFile(str(Path(QStandardPaths.writableLocation(
        QStandardPaths.StandardLocation.RuntimeLocation)) / "waydroid-tray.lock"))
    if not lock.tryLock(100):
        log("another waydroid-tray instance is already running")
        return 0

    monitor = Monitor(verbose=args.verbose)
    tray = Tray(monitor, verbose=args.verbose)
    tray.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
