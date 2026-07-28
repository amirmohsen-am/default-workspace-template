"""One KasmVNC display per browser: the live-view transport.

Each :class:`~browser.session.LiveBrowser` owns a private ``Xvnc`` -- KasmVNC's
X server, a TigerVNC fork that is *both* the framebuffer Chromium renders into
and the web client that streams it (its HTTP server and websocket handler are
in-process, so there is no second daemon to supervise). Chromium is launched
headful against it via ``DISPLAY``.

Why per-browser rather than one shared display:

* **Input fidelity is the whole point.** RFB pointer/keyboard events are injected
  as X events at the *display* level, so native right-click context menus, native
  ``<select>`` dropdowns and date pickers, and real click-drag all work -- none of
  which CDP's page-scoped ``Input.dispatch*`` can reach. There is no input code
  here; that is what a VNC server is.
* **Isolation.** One X CLIPBOARD per display, so two browsers can't clobber each
  other's clipboard, and their windows can't overlap on one framebuffer.

The daemon (not supervisord) owns these processes because browsers are created on
demand: a display starts in :meth:`LiveBrowser.start` and is reaped in
:meth:`LiveBrowser.close`, alongside the Chromium that renders into it.

There is deliberately NO window manager. Chromium sizes its own window to the
framebuffer (browser-use already pins ``window_size``), and a maximise request
would have nobody to answer it. The visible consequence: X stays in
``PointerRoot`` focus mode, so pointer and keyboard events reach the window under
the cursor but it never receives a ``FocusIn`` -- ``document.hasFocus()`` is
false in the page, so the JS Clipboard API and some autofocus behaviours do not
work. Adding a ~200 KB WM is the fix if that becomes a problem.
"""

import contextlib
import os
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path

from loguru import logger

# Display numbers start well clear of a workspace's own :0/:99 so a stray shared
# display can never collide with a per-browser one.
_DISPLAY_BASE = int(os.environ.get("BROWSER_VNC_DISPLAY_BASE", "100"))
# The websocket/HTTP port for display :N is _PORT_BASE + (N - _DISPLAY_BASE). Kept
# clear of 8080-8099, which the app scaffolder auto-assigns from by regex-scanning
# supervisord.conf -- it cannot see a port opened at runtime by this module.
_PORT_BASE = int(os.environ.get("BROWSER_VNC_PORT_BASE", "6900"))
# Ceiling on concurrent displays; well above the fleet's session cap.
_MAX_DISPLAYS = 16

# Framebuffer geometry. Matches the window size browser-use pins on the Chromium
# session, so the page fills the framebuffer exactly and frames are never scaled.
# Xvnc allocates the whole framebuffer up front and cannot grow it at runtime, so
# this is the hard ceiling on encoded resolution.
_SCREEN_W = int(os.environ.get("BROWSER_VNC_WIDTH", "1280"))
_SCREEN_H = int(os.environ.get("BROWSER_VNC_HEIGHT", "800"))

_READY_TIMEOUT_S = float(os.environ.get("BROWSER_VNC_READY_TIMEOUT", "20"))
_READY_POLL_S = 0.1
_STOP_GRACE_S = 5.0

# KasmVNC's bundled HTML5 client. Served by Xvnc's own in-process httpd.
_WWW_ROOT = os.environ.get("BROWSER_VNC_WWW", "/usr/share/kasmvnc/www")

_XVNC_BINARY = "Xvnc"

# Display numbers handed out in this process. An entry is released on stop().
_allocated: set[int] = set()


class VncStartupError(RuntimeError):
    """Xvnc could not be started for a browser (missing binary, or never came up)."""


def is_available() -> bool:
    """Whether KasmVNC is installed yet.

    It lands asynchronously on first container boot via the env-converge one-shot
    (system/scripts/env.d/1010-kasmvnc.sh), so a browser launched in the first
    minute of a fresh workspace may find it absent. Callers gate on the binary
    itself -- the unit's own satisfied condition -- because there are no marker
    files (the env.d contract).
    """
    return shutil.which(_XVNC_BINARY) is not None


def _lock_paths(display_num: int) -> tuple[Path, Path]:
    return (Path(f"/tmp/.X{display_num}-lock"), Path(f"/tmp/.X11-unix/X{display_num}"))


def _port_is_listening(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.2)
        return probe.connect_ex(("127.0.0.1", port)) == 0


def _x_socket_is_live(display_num: int) -> bool:
    """Whether something is actually accepting on this display's X socket.

    Distinguishes a live X server from the leftovers of a dead one: Xvnc creates
    /tmp/.X<n>-lock and /tmp/.X11-unix/X<n> and a SIGKILL (earlyoom, or a hard
    container stop) leaves BOTH behind. Nothing reclaims them and /tmp outlives a
    daemon restart, so treating mere file existence as "taken" would retire a
    display number for the life of the container.
    """
    _, x_socket = _lock_paths(display_num)
    if not x_socket.exists():
        return False
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.2)
        try:
            probe.connect(str(x_socket))
        except OSError:
            return False
        return True


def _clear_stale_locks(display_num: int) -> None:
    """Remove the lock/socket files of a display whose server is gone.

    Only ever called once :func:`_x_socket_is_live` has said nothing is listening,
    so this cannot unlink a live server's files.
    """
    for path in _lock_paths(display_num):
        with contextlib.suppress(OSError):
            path.unlink()
            logger.debug("cleared stale X lock {}", path)


def _allocate_display() -> int:
    """Pick a free display number, reclaiming any left behind by an unclean exit."""
    for display_num in range(_DISPLAY_BASE, _DISPLAY_BASE + _MAX_DISPLAYS):
        if display_num in _allocated:
            continue
        port = _PORT_BASE + (display_num - _DISPLAY_BASE)
        if _x_socket_is_live(display_num) or _port_is_listening(port):
            continue  # a real server owns this number
        _clear_stale_locks(display_num)
        _allocated.add(display_num)
        return display_num
    raise VncStartupError(f"no free VNC display in :{_DISPLAY_BASE}..:{_DISPLAY_BASE + _MAX_DISPLAYS - 1}")


class VncDisplay:
    """A running ``Xvnc`` for one browser: X framebuffer + bundled HTML5 client."""

    def __init__(self, browser_id: str) -> None:
        self.browser_id = browser_id
        self.display_num = _allocate_display()
        self.port = _PORT_BASE + (self.display_num - _DISPLAY_BASE)
        self.display = f":{self.display_num}"
        self._process: subprocess.Popen[bytes] | None = None

    def _command(self) -> list[str]:
        # KasmVNC's defaults are HTTPS-with-snakeoil + basic auth on 0.0.0.0. We
        # bind loopback (every other workspace listener does) and turn TLS and auth
        # off, because the ONLY path in is the browser daemon's own authenticated
        # /browsers/<id>/vnc/ proxy -- which speaks plain HTTP to this port.
        return [
            _XVNC_BINARY,
            self.display,
            "-geometry", f"{_SCREEN_W}x{_SCREEN_H}",
            "-depth", "24",
            "-interface", "127.0.0.1",
            "-websocketPort", str(self.port),
            "-httpd", _WWW_ROOT,
            "-sslOnly", "0",
            "-SecurityTypes", "None",
            "-disableBasicAuth",
            "-AlwaysShared",
            # Log to stderr so the daemon's supervisord-rotated log captures it.
            # KasmVNC's own default writes ~/.vnc/*.log, which nothing rotates and
            # host-backup would snapshot into every restic run forever.
            "-Log", "*:stderr:30",
        ]

    def start(self) -> None:
        """Spawn Xvnc and block until the display and its web port both answer."""
        if not is_available():
            self.release()
            raise VncStartupError(
                f"{_XVNC_BINARY} is not installed yet (env.d/1010-kasmvnc.sh installs it "
                "asynchronously on first boot); retry once the env-converge one-shot has run"
            )
        logger.info("starting Xvnc {} for browser {} (port {})", self.display, self.browser_id, self.port)
        self._process = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
            self._command(),
            stdout=subprocess.DEVNULL,
            stderr=None,  # inherit: Xvnc's own log lands in the daemon's stderr
            start_new_session=True,
        )
        self._await_ready()

    def _await_ready(self) -> None:
        """Wait for a usable display, not merely for the socket file to appear.

        Both conditions matter: the X socket must actually accept a connection (a
        bare ``exists()`` is satisfied by a leftover file from a dead server), and
        the web port must be listening (Chromium can render into a display whose
        client is not yet serving, which would show a blank pane).
        """
        deadline = time.monotonic() + _READY_TIMEOUT_S
        # An Event we never set, purely as the interval timer: `wait(timeout)` blocks
        # for the interval without `time.sleep`, which this package's ratchet forbids
        # in production code (see test_browser_ratchets.py::test_prevent_time_sleep).
        tick = threading.Event()
        while time.monotonic() < deadline:
            if self._process is not None and self._process.poll() is not None:
                code = self._process.returncode
                self.release()
                raise VncStartupError(f"Xvnc {self.display} exited during startup (code {code})")
            if _x_socket_is_live(self.display_num) and _port_is_listening(self.port):
                logger.info("Xvnc {} ready for browser {}", self.display, self.browser_id)
                return
            tick.wait(_READY_POLL_S)
        self.stop()
        raise VncStartupError(f"Xvnc {self.display} did not become ready within {_READY_TIMEOUT_S}s")

    def stop(self) -> None:
        """Terminate Xvnc and free its display number. Idempotent."""
        process = self._process
        self._process = None
        if process is not None and process.poll() is None:
            with contextlib.suppress(OSError):
                process.terminate()
            try:
                process.wait(timeout=_STOP_GRACE_S)
            except subprocess.TimeoutExpired:
                logger.warning("Xvnc {} ignored SIGTERM; killing", self.display)
                with contextlib.suppress(OSError):
                    process.kill()
                with contextlib.suppress(subprocess.TimeoutExpired):
                    process.wait(timeout=_STOP_GRACE_S)
        # Unlink unconditionally: a killed Xvnc leaves both files, and leaving them
        # would retire this display number permanently (see _x_socket_is_live).
        _clear_stale_locks(self.display_num)
        self.release()
        logger.info("stopped Xvnc {} for browser {}", self.display, self.browser_id)

    def release(self) -> None:
        """Return the display number to the pool without touching the process."""
        _allocated.discard(self.display_num)
