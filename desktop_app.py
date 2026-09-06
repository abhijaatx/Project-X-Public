"""Project X menu-bar/system-tray desktop host."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import webbrowser
from pathlib import Path

from PIL import Image, ImageDraw
import pyperclip
import pystray

import start


APP_NAME = "Project X"
DEFAULT_MODE = "lan"
URL_PATTERN = re.compile(r"(?:Remote|Local) URL:\s*(https?://\S+)")
PIN_PATTERN = re.compile(r"PIN:\s*(\d+)")
_desktop_lock_handle = None


def acquire_desktop_lock() -> None:
    """Allow only one Project X tray/menu-bar process per user session."""
    global _desktop_lock_handle
    lock_path = os.path.join(tempfile.gettempdir(), "project-x-desktop.lock")
    handle = open(lock_path, "a+", encoding="utf-8")
    try:
        if sys.platform == "win32":
            import msvcrt

            handle.seek(0)
            if not handle.read(1):
                handle.write("0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (OSError, BlockingIOError) as error:
        handle.close()
        raise RuntimeError("Project X is already running") from error

    handle.seek(0)
    handle.truncate()
    handle.write(str(os.getpid()))
    handle.flush()
    _desktop_lock_handle = handle


def app_log_path() -> str:
    if sys.platform == "darwin":
        directory = os.path.expanduser("~/Library/Logs/Project X")
    elif sys.platform == "win32":
        directory = os.path.join(
            os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "Project X"
        )
    else:
        directory = os.path.expanduser("~/.local/state/project-x")
    os.makedirs(directory, exist_ok=True)
    return os.path.join(directory, "project-x.log")


def macos_bundle_path(executable=None) -> Path | None:
    """Return the containing .app bundle for a frozen macOS executable."""
    path = Path(executable or sys.executable).resolve()
    for parent in (path, *path.parents):
        if parent.suffix == ".app":
            return parent
    return None


def is_macos_app_installed(executable=None) -> bool:
    if sys.platform != "darwin" or not getattr(sys, "frozen", False):
        return True
    bundle = macos_bundle_path(executable)
    if bundle is None:
        return False
    allowed_roots = (Path("/Applications"), Path.home() / "Applications")
    return any(root == bundle.parent for root in allowed_roots)


logging.basicConfig(
    filename=app_log_path(),
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("ProjectXDesktop")


def child_command(mode: str, pin: str) -> list[str]:
    options = ["--host-child", "--pin", pin]
    if mode == "lan":
        options.extend(["--no-tunnel", "--host", "0.0.0.0", "--port", "5001"])
    else:
        options.extend(["--host", "127.0.0.1", "--port", "5000"])
    if getattr(sys, "frozen", False):
        return [sys.executable, *options]
    return [sys.executable, os.path.abspath(__file__), *options]


def hidden_process_options() -> dict:
    if sys.platform != "win32":
        return {"start_new_session": True}
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = subprocess.SW_HIDE
    return {
        "startupinfo": startupinfo,
        "creationflags": subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP,
    }


def terminate_process_tree(process: subprocess.Popen) -> None:
    """Stop the launcher plus its server and tunnel children."""
    if process.poll() is not None:
        return
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            **hidden_process_options(),
        )
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=6)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=3)


def create_icon_image() -> Image.Image:
    size = 64
    image = Image.new("RGBA", (size, size), (15, 23, 42, 255))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((3, 3, 60, 60), radius=14, outline=(96, 165, 250), width=4)
    draw.line((18, 17, 46, 47), fill=(248, 250, 252), width=7)
    draw.line((46, 17, 18, 47), fill=(248, 250, 252), width=7)
    return image


class ProjectXController:
    def __init__(self):
        self.icon = None
        self.process = None
        self.mode = None
        self.pin = ""
        self.url = ""
        self.status = "Stopped"
        self._lock = threading.RLock()
        self._stopping = False
        self.missing_accessibility = False
        self._server_thread = None
        self._server_loop = None
        self._server_stop_event = None
        self._permission_notice_shown = False

    def attach(self, icon):
        self.icon = icon

    def _refresh(self):
        if self.icon:
            self.icon.update_menu()

    def _notify(self, message):
        if self.icon:
            try:
                self.icon.notify(message, APP_NAME)
            except (NotImplementedError, OSError):
                logger.info("Notification unavailable: %s", message)

    def start(self, mode="tunnel"):
        with self._lock:
            self.stop()
            self.mode = mode
            self.pin = start.generated_pin()
            self.url = ""
            self.missing_accessibility = False
            self.status = "Starting tunnel…" if mode == "tunnel" else "Starting LAN host…"
            if sys.platform == "darwin" and getattr(sys, "frozen", False):
                self._start_macos_in_process(mode)
                return
            command = child_command(mode, self.pin)
            logger.info("Starting %s host", mode)
            try:
                self.process = subprocess.Popen(
                    command,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                    cwd=os.path.dirname(os.path.abspath(__file__)),
                    **hidden_process_options(),
                )
            except OSError as error:
                logger.exception("Could not start host")
                self.status = f"Start failed: {error}"
                self._notify("Project X could not start. Open the log for details.")
                self._refresh()
                return
            threading.Thread(target=self._read_output, daemon=True).start()
            self._refresh()

    def _start_macos_in_process(self, mode):
        from macos_permissions import check_macos_permissions

        # Never trigger macOS privacy prompts from automatic startup. Doing so
        # creates a prompt/relaunch loop when an ad-hoc development build has a
        # stale TCC entry. Permission requests remain an explicit menu action.
        permissions = check_macos_permissions(request=False)
        if not permissions["screen_recording"]:
            self.status = "Screen Recording permission required"
            if not self._permission_notice_shown:
                self._notify(
                    "Screen Recording is unavailable. Use the Project X menu to "
                    "open its privacy setting, then restart once."
                )
                self._permission_notice_shown = True
            self._refresh()
            return
        self.missing_accessibility = not permissions["accessibility"]
        if self.missing_accessibility:
            self._notify(
                "Enable Accessibility for Project X to use remote mouse and keyboard control."
            )

        self._server_thread = threading.Thread(
            target=self._macos_server_thread,
            args=(mode,),
            name="project-x-host",
            daemon=True,
        )
        self._server_thread.start()
        self._refresh()

    def _macos_server_thread(self, mode):
        try:
            asyncio.run(self._serve_macos(mode))
        except Exception as error:
            logger.exception("In-process macOS host failed")
            if not self._stopping:
                self.status = f"Start failed: {error}"
                self._notify("Project X could not start. Open the log for details.")
                self._refresh()

    async def _serve_macos(self, mode):
        from aiohttp import web
        from server import acquire_host_lock, create_app, release_host_lock

        runner = None
        tunnel = None
        tunnel_reader = None
        host = "0.0.0.0" if mode == "lan" else "127.0.0.1"
        port = 5001 if mode == "lan" else 5000
        try:
            acquire_host_lock()
            app, _server = create_app(pin=self.pin, port=port)
            runner = web.AppRunner(app, handle_signals=False)
            await runner.setup()
            await web.TCPSite(runner, host=host, port=port).start()

            self._server_loop = asyncio.get_running_loop()
            self._server_stop_event = asyncio.Event()
            if mode == "lan":
                self.url = start.lan_address(port)
                self.status = (
                    "Running on LAN · Accessibility required"
                    if self.missing_accessibility
                    else "Running on LAN"
                )
                self._notify(f"Ready: {self.url}\nPIN: {self.pin}")
                self._refresh()
            else:
                tunnel = await asyncio.create_subprocess_exec(
                    start.find_cloudflared(),
                    "tunnel",
                    "--url",
                    f"http://127.0.0.1:{port}",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                )
                tunnel_reader = asyncio.create_task(
                    self._read_async_tunnel_output(tunnel)
                )

            await self._server_stop_event.wait()
        finally:
            if tunnel_reader:
                tunnel_reader.cancel()
            if tunnel and tunnel.returncode is None:
                tunnel.terminate()
                try:
                    await asyncio.wait_for(tunnel.wait(), timeout=5)
                except asyncio.TimeoutError:
                    tunnel.kill()
                    await tunnel.wait()
            if runner:
                await runner.cleanup()
            release_host_lock()
            self._server_loop = None
            self._server_stop_event = None

    async def _read_async_tunnel_output(self, tunnel):
        assert tunnel.stdout is not None
        while True:
            raw_line = await tunnel.stdout.readline()
            if not raw_line:
                return
            line = raw_line.decode("utf-8", errors="replace").rstrip()
            logger.info("tunnel: %s", line)
            match = start.TUNNEL_URL_PATTERN.search(line)
            if match:
                self.url = match.group(0)
                self.status = (
                    "Running via tunnel · Accessibility required"
                    if self.missing_accessibility
                    else "Running via tunnel"
                )
                self._notify(f"Ready: {self.url}\nPIN: {self.pin}")
                self._refresh()

    def is_active(self):
        child_active = self.process is not None and self.process.poll() is None
        thread_active = (
            self._server_thread is not None and self._server_thread.is_alive()
        )
        return child_active or thread_active

    def _read_output(self):
        process = self.process
        if process is None or process.stdout is None:
            return
        for raw_line in process.stdout:
            line = raw_line.rstrip()
            logger.info("host: %s", line)
            self._handle_output_line(line)
        return_code = process.wait()
        with self._lock:
            if self.process is process:
                self.process = None
                if not self._stopping and "permission required" not in self.status.lower():
                    self.status = f"Stopped unexpectedly ({return_code})"
                    self._notify("Project X stopped unexpectedly. Open the log for details.")
                self._refresh()

    def _handle_output_line(self, line):
        if line == "APP_PERM:screen_recording":
            self.status = "Screen Recording permission required"
            self._notify(
                "Enable Screen Recording for Project X, then quit and reopen the app."
            )
            self._refresh()
            return
        if line == "APP_PERM:accessibility":
            self.missing_accessibility = True
            self._notify(
                "Enable Accessibility for Project X to use remote mouse and keyboard control."
            )
            self._refresh()
            return

        pin_match = PIN_PATTERN.search(line)
        if pin_match:
            self.pin = pin_match.group(1)
        url_match = URL_PATTERN.search(line)
        if url_match:
            self.url = url_match.group(1)
            base_status = "Running via tunnel" if self.mode == "tunnel" else "Running on LAN"
            self.status = (
                f"{base_status} · Accessibility required"
                if self.missing_accessibility
                else base_status
            )
            self._notify(f"Ready: {self.url}\nPIN: {self.pin}")
            self._refresh()

    def stop(self):
        with self._lock:
            if self._server_thread is not None:
                self._stopping = True
                try:
                    if self._server_loop and self._server_stop_event:
                        self._server_loop.call_soon_threadsafe(
                            self._server_stop_event.set
                        )
                    self._server_thread.join(timeout=10)
                finally:
                    self._server_thread = None
                    self._server_loop = None
                    self._server_stop_event = None
                    self._stopping = False
                    self.status = "Stopped"
                    self.url = ""
                    self._refresh()
                return
            process = self.process
            if process is None or process.poll() is not None:
                self.process = None
                if self.status.startswith("Running") or self.status.startswith("Starting"):
                    self.status = "Stopped"
                return
            self._stopping = True
            try:
                terminate_process_tree(process)
            finally:
                self.process = None
                self._stopping = False
                self.status = "Stopped"
                self._refresh()

    def copy_url(self):
        if self.url:
            pyperclip.copy(self.url)
            self._notify("Connection URL copied.")

    def copy_pin(self):
        if self.pin:
            pyperclip.copy(self.pin)
            self._notify("Connection PIN copied.")

    def open_viewer(self):
        if self.url:
            webbrowser.open(self.url)


def run_tray() -> int:
    try:
        acquire_desktop_lock()
    except RuntimeError as error:
        logger.info("%s", error)
        return 0

    controller = ProjectXController()

    def status_text(_item):
        return f"Status: {controller.status}"

    def start_tunnel(_icon, _item):
        controller.start("tunnel")

    def start_lan(_icon, _item):
        controller.start("lan")

    def copy_url(_icon, _item):
        controller.copy_url()

    def copy_pin(_icon, _item):
        controller.copy_pin()

    def open_viewer(_icon, _item):
        controller.open_viewer()

    def stop_host(_icon, _item):
        controller.stop()

    def open_screen_recording_settings(_icon, _item):
        # This is deliberately user initiated. Automatic startup only checks
        # permission and never invokes the native request dialog.
        try:
            from macos_permissions import check_macos_permissions

            check_macos_permissions(request=True, open_settings=False)
        except Exception as error:
            logger.info("Could not request Screen Recording permission: %s", error)
        subprocess.Popen(
            [
                "open",
                "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def open_accessibility_settings(_icon, _item):
        subprocess.Popen(
            [
                "open",
                "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def quit_app(icon, _item):
        controller.stop()
        icon.stop()

    menu = pystray.Menu(
        pystray.MenuItem(status_text, None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Open viewer", open_viewer, enabled=lambda _item: bool(controller.url)),
        pystray.MenuItem("Copy connection URL", copy_url, enabled=lambda _item: bool(controller.url)),
        pystray.MenuItem("Copy PIN", copy_pin, enabled=lambda _item: bool(controller.pin)),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Start secure tunnel", start_tunnel),
        pystray.MenuItem("Start LAN host", start_lan),
        pystray.MenuItem("Stop host", stop_host, enabled=lambda _item: controller.is_active()),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(
            "Request / Open Screen Recording",
            open_screen_recording_settings,
            visible=sys.platform == "darwin",
        ),
        pystray.MenuItem(
            "Open Accessibility settings",
            open_accessibility_settings,
            visible=sys.platform == "darwin",
        ),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit Project X", quit_app),
    )
    icon = pystray.Icon("project-x", create_icon_image(), APP_NAME, menu)
    controller.attach(icon)

    def setup(_icon):
        if not is_macos_app_installed():
            controller.status = "Move Project X to Applications"
            controller._notify(
                "Quit Project X, drag it into Applications, then open that installed copy."
            )
            controller._refresh()
            return
        controller.start(DEFAULT_MODE)

    try:
        icon.run(setup=setup)
    finally:
        controller.stop()
    return 0


def parse_internal_args(argv=None):
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--host-child", action="store_true")
    parser.add_argument("--server-child", action="store_true")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5000)
    parser.add_argument("--pin", default="1234")
    parser.add_argument("--no-tunnel", action="store_true")
    parser.add_argument("--camera-worker", action="store_true")
    parser.add_argument("--index", type=int, default=0)
    parser.add_argument("--width", type=int, default=320)
    parser.add_argument("--height", type=int, default=240)
    parser.add_argument("--fps", type=int, default=20)
    parser.add_argument("--quality", type=int, default=35)
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_internal_args(argv)
    if args.camera_worker:
        from camera_process import camera_worker

        return camera_worker(
            args.index, args.width, args.height, args.fps, args.quality
        )
    if args.server_child:
        from server import run_server

        run_server(host=args.host, port=args.port, pin=args.pin)
        return 0
    if args.host_child:
        options = ["--host", args.host, "--port", str(args.port), "--pin", args.pin]
        if args.no_tunnel:
            options.append("--no-tunnel")
        return start.main(options)
    return run_tray()


if __name__ == "__main__":
    raise SystemExit(main())
