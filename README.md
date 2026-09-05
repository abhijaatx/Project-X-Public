# Project X

A lightweight browser-based remote desktop host for Windows and macOS, built
with Python, native platform capture APIs, WebRTC, and a browser client.

## Desktop application

Project X can be distributed as a setup-free background application. The macOS
build runs in the menu bar without a Dock or Terminal window. The Windows build
runs in the system tray without a console window. Both provide controls to start
a secure tunnel or LAN host, open the viewer, copy the connection URL and PIN,
stop the host, and quit Project X.

Project X starts automatically in **LAN mode** on port `5001`. Devices on the
same network connect to `http://HOST_IP:5001`. A secure Cloudflare tunnel can be
started manually from the tray/menu-bar menu when internet access is required.

Download the appropriate artifact from a `Build Project X desktop apps` GitHub
Actions run, or build it locally:

```bash
# macOS (creates release/Project-X-macOS-<architecture>.dmg)
chmod +x packaging/build-macos.sh
packaging/build-macos.sh
```

```powershell
# Windows (creates an installer and portable ZIP)
.\packaging\build-windows.ps1
```

The build scripts bundle Cloudflare Tunnel, Python, the native capture stack,
and the browser assets. Python and Cloudflare Tunnel do not need to be installed
on the destination computer. Unsigned builds may still show macOS Gatekeeper or
Windows SmartScreen warnings.

On macOS, drag Project X onto the Applications shortcut in the DMG before opening
it. Project X deliberately does not start capture from a mounted DMG or build
folder because macOS privacy permissions are tied to that exact application copy.

## Features

- Windows and macOS host support
- Browser clients on desktop, phone, and tablet
- Native ScreenCaptureKit capture on macOS with automatic MSS fallback
- Borderless DXGI Desktop Duplication with optional Windows Graphics Capture/MSS fallback
- Hardware H.264 through VideoToolbox, NVENC, Intel QSV, AMD AMF, or Media Foundation
- JPEG/WebSocket fallback for clients where direct WebRTC cannot connect
- Bounded latest-frame back-pressure: slow links drop stale JPEG frames instead
  of waiting a full render round-trip before sending the next one
- WebRTC DataChannels for low-latency pointer, click, and keyboard input
- Adaptive 0.5–2.0 Mbps VideoToolbox bitrate with true 15/30 FPS pacing
- Shared VideoToolbox frame encoding for viewers using the same media profile
- Multiple displays, including displays positioned left or above the primary one
- Mouse, keyboard, scrolling, clipboard, file transfer, and host camera streaming
- Low-latency 320×240 camera mode with latest-frame-only browser rendering
- Muted-by-default host microphone listening with bounded low-latency PCM audio
- Host-specific shortcuts and an on-screen mobile keyboard
- Optional Cloudflare HTTPS tunnel with a random PIN per launch

## macOS setup

Install Python 3.10 or newer and Cloudflare Tunnel:

```bash
brew install python cloudflared
git clone https://github.com/abhijaatx/Project-X.git
cd Project-X
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python start.py
```

Alternatively, double-click `run-macos.command`. It creates the virtual
environment, installs dependencies, and launches the host.

For the lowest latency on the same network, double-click
`run-macos-lan.command`, then connect to the displayed LAN address on port 5001.

On first launch, macOS will request **Screen Recording** permission. Remote mouse
and keyboard control also require **Accessibility** permission. Host audio requires
**Microphone** permission, requested only after you press **Listen**. Grant these to
the application that launches Python (normally Terminal or your terminal app) in:

> System Settings → Privacy & Security → Screen Recording / Accessibility / Microphone

Restart Project X after changing either permission. Camera permission is requested
only when the host camera is enabled.

## Windows setup

Install Python 3.10 or newer and Cloudflare Tunnel, then double-click `run.bat`.
It creates `.venv`, installs the native Windows capture stack, and launches the
managed tunnel. For the lowest latency on the same network, double-click
`run-windows-lan.bat` and connect to the displayed address on port 5001.

For a fresh Windows machine, open PowerShell or a PowerShell tab in Windows
Terminal and run this one command. It clones or updates the checkout, installs
Git and Python through WinGet when needed, creates `.venv`, installs the
dependencies, configures the private-network firewall rule when PowerShell is
elevated, and starts the LAN host:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://raw.githubusercontent.com/abhijaatx/Project-X-Public/main/scripts/bootstrap-windows.ps1 | iex"
```

The command uses a random PIN by default. To choose a PIN or a different
directory, append parameters such as `-Pin 12345678 -ProjectDir C:\Project-X`.
Run it from PowerShell (a prompt beginning with `PS`).

If Git and Python are already installed, the manual equivalent is:

```powershell
$projectDir = Join-Path $env:USERPROFILE "Project-X-Public"; if (-not (Test-Path (Join-Path $projectDir ".git"))) { git clone https://github.com/abhijaatx/Project-X-Public.git $projectDir }; Set-Location $projectDir; .\run-windows-lan.bat
```

From **Command Prompt** instead of PowerShell, use this one line when Git and
Python are already installed:

```bat
git clone https://github.com/abhijaatx/Project-X-Public.git "%USERPROFILE%\Project-X-Public" && cd /d "%USERPROFILE%\Project-X-Public" && run-windows-lan.bat
```

From a terminal, the equivalent commands are:

```powershell
python -m venv .venv
.venv\Scripts\activate
python -m pip install -r requirements.txt
python start.py
```

## Local-only mode

To run without creating a public tunnel:

```bash
python start.py --no-tunnel
```

The default bind address is `127.0.0.1`. For intentional LAN access:

```bash
python start.py --no-tunnel --host 0.0.0.0
```

Open `http://HOST_IP:5000` from another device on the same network. Ensure your
firewall allows the connection.

## Options

```text
python start.py --port 5000 --pin 12345678
python server.py --host 127.0.0.1 --port 5000 --pin 12345678
```

If `--pin` is omitted from `start.py`, a random eight-digit PIN is generated.
`WEBREMOTE_PIN` can provide the PIN without placing it in shell history.

## Performance guidance

- **Turbo H.264 + Sharp & Fast / 30 FPS** is the recommended same-AP default.
- Use **Fast / 15 FPS** over routed, mobile, or higher-jitter Wi-Fi.
- Use **Native / 30 FPS** when maximum text detail matters more than cadence.
- Every connected client has independent monitor, quality, scale, and FPS state.

Each monitor has one continuous capture producer shared by every client. The hub
uses double-buffered latest-frame state and caches identical JPEG variants instead
of recapturing or re-encoding them. Identical macOS H.264 frames at the same
resolution also reuse one VideoToolbox encode across viewers. On macOS,
ScreenCaptureKit uses a dedicated serial callback queue, a two-frame WindowServer
queue, and complete-frame filtering before feeding hardware H.264
through VideoToolbox over WebRTC. On Windows, DXcam uses borderless DXGI Desktop
Duplication first, then MSS if DXGI is unavailable. Windows Graphics Capture is
available only when explicitly selected with `PROJECTX_WINDOWS_CAPTURE_BACKEND=winrt`
and may show Windows' system yellow privacy border. H.264 probes NVIDIA NVENC, Intel Quick Sync, AMD AMF,
and Media Foundation before using a tuned zero-latency x264 fallback. A dedicated
control WebSocket carries authentication and status. Once WebRTC connects,
unordered zero-retry pointer motion and reliable click/keyboard input move to
separate DataChannels. A separate authenticated media WebSocket carries
JPEG fallback frames, camera frames, and microphone data. JPEG scaling uses
Accelerate/vImage on macOS and PyAV/libswscale on Windows before the Pillow fallback. Camera
capture runs in an isolated process so OpenCV cannot conflict with PyAV's FFmpeg
runtime. Camera and microphone data are captured once and broadcast to subscribers.

WebRTC is the default for compatible viewers on every host/viewer pairing. Any
WebRTC viewer reporting a
decode stall automatically falls back to JPEG. The capture hub also restarts an
individual ScreenCaptureKit producer if macOS stops delivering callbacks after a
long sleep/wake or extended host session. Adaptive quality responds to packet loss,
jitter, and rendered FPS while preserving the selected quality as its upper bound.

### Optional TURN route comparison

Project X always prefers the matching physical LAN candidate and excludes competing
VPN host candidates. A TURN server can be supplied for networks whose direct AP
backhaul is unstable:

```bash
export PROJECTX_TURN_URLS="turn:relay.example.com:3478,turns:relay.example.com:5349"
export PROJECTX_TURN_USERNAME="projectx"
export PROJECTX_TURN_CREDENTIAL="replace-me"
```

When direct RTT remains above 80 ms, the viewer samples the relay and retains it
only when its measured RTT is materially better. Without TURN configuration, media
remains direct peer-to-peer.

### Reproducible benchmark

```bash
.venv/bin/python benchmarks/performance_benchmark.py --network-rtt-ms 142
```

The JSON report measures actual VideoToolbox reuse, 15/30 FPS pacing, frame-copy
cost, and realtime-input dispatch overhead on the current host.

## Security note

A remote-desktop host can control the keyboard, mouse, clipboard, camera, and file
system of the computer running it. Keep the URL and PIN private, stop the process
when it is not needed, and do not expose the local port directly to the internet.
