"""Cross-platform Project X launcher with managed server/tunnel lifecycle."""

from __future__ import annotations

import argparse
import ipaddress
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import time


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SERVER_SCRIPT = os.path.join(BASE_DIR, "server.py")
TUNNEL_URL_PATTERN = re.compile(r"https://[a-zA-Z0-9-]+\.trycloudflare\.com")


def find_cloudflared() -> str:
    executable = "cloudflared.exe" if sys.platform == "win32" else "cloudflared"
    local_path = os.path.join(BASE_DIR, executable)
    discovered = shutil.which("cloudflared")
    if os.path.isfile(local_path):
        return local_path
    if discovered:
        return discovered

    if sys.platform == "darwin":
        install_hint = "Install it with: brew install cloudflared"
    elif sys.platform == "win32":
        install_hint = "Install it with: winget install Cloudflare.cloudflared"
    else:
        install_hint = "Install cloudflared using your operating system package manager."
    raise RuntimeError(f"cloudflared was not found. {install_hint}")


def wait_for_server(process: subprocess.Popen, port: int, timeout: float = 120.0) -> None:
    """Wait through slow first-run native media-library loading."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"Project X server exited with code {process.returncode}")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.25):
                return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError(f"Project X did not become ready on port {port} within {timeout:g}s")


def terminate(process: subprocess.Popen | None) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def generated_pin() -> str:
    return f"{secrets.randbelow(100_000_000):08d}"


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Start Project X and its HTTPS tunnel")
    parser.add_argument("--port", type=int, default=5000)
    parser.add_argument(
        "--pin",
        default=os.environ.get("WEBREMOTE_PIN"),
        help="Connection PIN; defaults to a random eight-digit PIN",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Server bind address; keep the default when using the tunnel",
    )
    parser.add_argument("--no-tunnel", action="store_true")
    return parser.parse_args(argv)


def server_command(args: argparse.Namespace, pin: str) -> list[str]:
    options = [
        "--host",
        args.host,
        "--port",
        str(args.port),
        "--pin",
        pin,
    ]
    if getattr(sys, "frozen", False):
        return [sys.executable, "--server-child", *options]
    return [sys.executable, SERVER_SCRIPT, *options]


def lan_address(port: int) -> str:
    """Return a useful LAN URL without sending application data."""
    address = "127.0.0.1"
    if sys.platform == "darwin":
        # The default route may be a full-tunnel VPN (utun), so a UDP route
        # probe advertises an unreachable 10.8.x address. Prefer physical
        # Ethernet/Wi-Fi interfaces before consulting the default route.
        for interface in ("en0", "en1", "en2"):
            try:
                result = subprocess.run(
                    ["/usr/sbin/ipconfig", "getifaddr", interface],
                    capture_output=True,
                    text=True,
                    timeout=1,
                    check=False,
                )
                candidate = result.stdout.strip()
                candidate_ip = ipaddress.ip_address(candidate)
                if candidate_ip.version == 4 and candidate_ip.is_private:
                    return f"http://{candidate}:{port}"
            except (OSError, subprocess.SubprocessError, ValueError):
                continue
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("1.1.1.1", 80))
        address = probe.getsockname()[0]
    except OSError:
        try:
            address = socket.gethostbyname(socket.gethostname())
        except OSError:
            pass
    finally:
        probe.close()
    return f"http://{address}:{port}"


def main(argv=None) -> int:
    args = parse_args(argv)
    pin = str(args.pin or generated_pin())
    server = None
    tunnel = None

    print("=" * 64, flush=True)
    print("  Project X host is starting", flush=True)
    print(f"  Platform: {sys.platform} | Port: {args.port}", flush=True)
    print("=" * 64, flush=True)

    try:
        popen_kwargs = {}
        if sys.platform == "win32":
            _stealth = os.environ.get("PROJECTX_STEALTH_CAPTURE", "1").strip()
            if _stealth not in ("0", "false", "no", "off"):
                popen_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        server = subprocess.Popen(
            server_command(args, pin),
            cwd=BASE_DIR,
            **popen_kwargs,
        )
        wait_for_server(server, args.port)

        if args.no_tunnel:
            url = (
                lan_address(args.port)
                if args.host == "0.0.0.0"
                else f"http://127.0.0.1:{args.port}"
            )
            print(f"Local URL: {url}", flush=True)
            print(f"PIN: {pin}", flush=True)
            return server.wait()

        cloudflared = find_cloudflared()
        tunnel = subprocess.Popen(
            [cloudflared, "tunnel", "--url", f"http://127.0.0.1:{args.port}"],
            cwd=BASE_DIR,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )

        tunnel_url = None
        assert tunnel.stdout is not None
        for line in tunnel.stdout:
            if tunnel_url is None:
                match = TUNNEL_URL_PATTERN.search(line)
                if match:
                    tunnel_url = match.group(0)
                    print("\n" + "=" * 64, flush=True)
                    print(f"  Remote URL: {tunnel_url}", flush=True)
                    print(f"  PIN: {pin}", flush=True)
                    print("  Keep this window open. Press Ctrl+C to stop.", flush=True)
                    print("=" * 64 + "\n", flush=True)

        if tunnel_url is None:
            raise RuntimeError("Cloudflare tunnel exited before publishing a URL")
        return tunnel.wait()
    except KeyboardInterrupt:
        return 0
    except RuntimeError as error:
        print(f"[!] {error}", file=sys.stderr, flush=True)
        return 1
    finally:
        terminate(tunnel)
        terminate(server)


if __name__ == "__main__":
    # In stealth mode, hide all console windows on Windows.
    if sys.platform == "win32":
        _stealth = os.environ.get("PROJECTX_STEALTH_CAPTURE", "1").strip()
        if _stealth not in ("0", "false", "no", "off"):
            try:
                import ctypes
                ctypes.windll.user32.ShowWindow(
                    ctypes.windll.kernel32.GetConsoleWindow(), 0  # SW_HIDE
                )
            except Exception:
                pass
    raise SystemExit(main())
