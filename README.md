# waydroid-tray

A small system tray icon for Linux that starts and stops **Waydroid** on
demand — nothing runs in the background until *you* ask for it.

![states](https://img.shields.io/badge/states-4-blue) ![license](https://img.shields.io/badge/license-MIT-green)

## Features

- **Tray icon with four states**
  - grey — Waydroid stopped
  - green — Waydroid running
  - orange — starting or stopping (transitional)
  - red — error (failed or timed-out start/stop, inconsistent state)
- **Double-click** toggles start/stop.
  Clicks are never queued: anything pressed while the icon is orange is
  ignored. Start is only possible from *stopped* or *error*, stop only
  from *running*.
- **Right-click context menu** with *Start Waydroid* / *Stop Waydroid* /
  *Quit* — unavailable entries are **greyed out** instead of silently
  swallowing clicks.
- **Tooltip with live runtime info**: RAM and CPU usage of the Android
  container, its IP address, session uptime and the current state
  (including the error reason, when red).
- **Autostarts with the desktop session** — the tray icon, not Waydroid.
  Waydroid itself is only ever started by you.
- Runs entirely as your desktop user — no root, no polkit prompts.
  Starting the session brings up the LXC container, the Android session
  and (on the [waydroid-nvidia](https://github.com/CinQwQeggs01/waydroid-nvidia)
  fork) the `wd-venus` render service; stopping takes all of it down again.

## Requirements

- Linux with a freedesktop system tray (KDE Plasma, GNOME with appindicator support, …)
- [Waydroid](https://waydro.id)
- `python-pyqt6` (Arch) / `python3-pyqt6` (Debian) — Qt 6 Python bindings

## Install

```bash
git clone https://github.com/mmeissner/waydroid-tray.git
cd waydroid-tray
sudo ./install.sh          # system-wide
# or
./install.sh --user        # just for your user
```

The tray icon appears on your next login (or run `waydroid-tray` now).
Waydroid is started and stopped **only** through the tray icon.

Uninstall with `sudo ./install.sh --uninstall` (or `--uninstall --user`).

### Arch package

A `PKGBUILD` is provided in [`packaging/`](packaging/) for local builds or
AUR submission:

```bash
cd packaging && makepkg -si
```

## How it works

The app polls `waydroid status` every 2 seconds and runs a small state
machine:

- starting/stopping must settle within 90 s / 45 s, otherwise the icon
  turns red with the reason;
- a container/session mismatch (one up, one down) turns orange briefly
  and red after 20 s if it doesn't resolve;
- red always stays visible for at least 10 s before resolving.

Runtime numbers come from the container's cgroup
(`/sys/fs/cgroup/lxc.payload.waydroid`) — RAM, CPU, plus the IP address
and uptime reported by Waydroid itself.

The waydroid container *manager* service (`waydroid-container.service`)
may stay enabled at boot: it is only the idle control daemon and does
**not** start Android. Everything Android-related is started/stopped by
the tray icon on your request.

## Troubleshooting

- **Icon doesn't appear** — your desktop needs StatusNotifierItem tray
  support (Plasma has it built in; GNOME needs an appindicator extension).
- **Red icon** — hover the tooltip for the reason; the session manager's
  log is at `~/.local/state/waydroid-tray/session-start.log`.
- **No internet in Waydroid behind Docker** — Docker's iptables FORWARD
  policy drops traffic from other bridges; allow waydroid0 in the
  `DOCKER-USER` chain (see
  [waydroid wiki](https://docs.waydro.id/net/internet) for details).

## License

[MIT](LICENSE)
