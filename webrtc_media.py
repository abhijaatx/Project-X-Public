"""Low-latency WebRTC media with platform hardware H.264 acceleration."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
import fractions
import ipaddress
import json
import logging
import sys
import threading
import time
import weakref

import av
from aiortc import (
    RTCPeerConnection,
    RTCRtpSender,
    RTCSessionDescription,
    VideoStreamTrack,
)
import aiortc.codecs
from aiortc.codecs.h264 import H264Encoder, VIDEO_TIME_BASE, convert_timebase


logger = logging.getLogger("app")


def _candidate_address(line: str):
    """Return (address, candidate type) for an SDP ICE candidate line."""
    fields = line.split()
    if not fields or not fields[0].startswith("a=candidate:") or len(fields) < 8:
        return None, None
    try:
        type_index = fields.index("typ")
        return fields[4], fields[type_index + 1]
    except (ValueError, IndexError):
        return None, None


def _prefer_direct_lan_sdp(sdp: str, client_address: str | None):
    """Remove competing VPN host candidates when a same-LAN route exists.

    Campus and office networks commonly route several Wi-Fi subnets together.
    Pick the private host candidate with the longest address-prefix match to
    the viewer, provided at least the first 16 bits match. This recognizes
    10.2.8.x -> 10.2.21.x as local while rejecting a competing 10.8.x VPN.
    Server-reflexive and relay candidates remain as fallbacks.
    """
    try:
        client_ip = ipaddress.ip_address(client_address or "")
    except ValueError:
        return sdp, None
    if client_ip.version != 4 or not client_ip.is_private:
        return sdp, None

    lines = sdp.splitlines()
    candidates = set()
    for line in lines:
        address, candidate_type = _candidate_address(line)
        if candidate_type != "host":
            continue
        try:
            candidate_ip = ipaddress.ip_address(address)
        except ValueError:
            continue
        if (
            candidate_ip.version == 4
            and candidate_ip.is_private
            and not candidate_ip.is_loopback
            and not candidate_ip.is_link_local
        ):
            candidates.add(str(candidate_ip))

    if not candidates:
        return sdp, None

    def shared_prefix_bits(address):
        difference = int(ipaddress.ip_address(address)) ^ int(client_ip)
        return 32 if difference == 0 else 32 - difference.bit_length()

    preferred = max(
        candidates,
        key=lambda address: (shared_prefix_bits(address), -abs(
            int(ipaddress.ip_address(address)) - int(client_ip)
        )),
    )
    if shared_prefix_bits(preferred) < 16:
        return sdp, None
    filtered = []
    for line in lines:
        address, candidate_type = _candidate_address(line)
        if candidate_type == "host" and address != preferred:
            continue
        filtered.append(line)
    trailing_newline = "\r\n" if sdp.endswith(("\r\n", "\n")) else ""
    return "\r\n".join(filtered) + trailing_newline, preferred


def _videotoolbox_available() -> bool:
    if sys.platform != "darwin":
        return False
    try:
        av.Codec("h264_videotoolbox", "w")
        return True
    except Exception:
        return False


VIDEO_TOOLBOX_AVAILABLE = _videotoolbox_available()


def _windows_h264_candidates():
    if sys.platform != "win32":
        return ()
    available = []
    for codec_name in ("h264_nvenc", "h264_qsv", "h264_amf", "h264_mf"):
        try:
            av.Codec(codec_name, "w")
            available.append(codec_name)
        except Exception:
            continue
    return tuple(available)


WINDOWS_H264_CANDIDATES = _windows_h264_candidates()
WINDOWS_HARDWARE_H264_AVAILABLE = bool(WINDOWS_H264_CANDIDATES)


class _SharedVideoToolboxState:
    def __init__(self, bitrate):
        self.lock = threading.Lock()
        self.codec = None
        self.bitrate = bitrate
        # Each sender reports its own REMB target.  The shared codec follows
        # the most constrained live sender, then recovers when that sender's
        # target recovers or disappears.
        self.requested_bitrates = weakref.WeakKeyDictionary()
        self.cache = OrderedDict()
        self.encode_count = 0
        self.cache_hits = 0


class VideoToolboxH264Encoder(H264Encoder):
    """aiortc-compatible H.264 packetizer backed by Apple's VideoToolbox."""

    MIN_BITRATE = 500_000
    INITIAL_BITRATE = 1_200_000
    MAX_BITRATE = 2_000_000
    DOWNSHIFT_INTERVAL = 1.0
    UPSHIFT_INTERVAL = 8.0
    CACHE_FRAMES = 8
    _pool_lock = threading.Lock()
    _pool = {}

    def __init__(self):
        self._videotoolbox_target_bitrate = self.INITIAL_BITRATE
        self._last_bitrate_update = 0.0
        # REMB is often deliberately conservative during the first seconds of
        # a browser session.  The server enables downshifts only after its
        # media stats confirm sustained loss/jitter/FPS degradation.
        self.allow_bitrate_downshift = False
        super().__init__()
        self._videotoolbox_target_bitrate = self.INITIAL_BITRATE

    @classmethod
    def clear_shared_pool(cls):
        with cls._pool_lock:
            cls._pool.clear()

    @classmethod
    def shared_pool_stats(cls):
        with cls._pool_lock:
            states = list(cls._pool.values())
        return {
            "encodes": sum(state.encode_count for state in states),
            "cache_hits": sum(state.cache_hits for state in states),
            "variants": len(states),
        }

    @property
    def target_bitrate(self):
        return self._videotoolbox_target_bitrate

    @target_bitrate.setter
    def target_bitrate(self, bitrate):
        requested = max(self.MIN_BITRATE, min(self.MAX_BITRATE, int(bitrate)))
        current = self._videotoolbox_target_bitrate
        now = time.monotonic()
        elapsed = now - self._last_bitrate_update

        # Reduce quickly when REMB reports congestion, but recover slowly to
        # avoid repeatedly reconfiguring VideoToolbox and creating frame gaps.
        if (
            requested <= current * 0.85
            and elapsed >= self.DOWNSHIFT_INTERVAL
            and self.allow_bitrate_downshift
        ):
            updated = max(requested, int(current * 0.65), self.MIN_BITRATE)
        elif requested >= current * 1.25 and elapsed >= self.UPSHIFT_INTERVAL:
            updated = min(requested, int(current * 1.20), self.MAX_BITRATE)
        else:
            return

        if updated == current:
            return
        self._videotoolbox_target_bitrate = updated
        self._last_bitrate_update = now
        with self._pool_lock:
            states = list(self._pool.values())
        for state in states:
            with state.lock:
                if self not in state.requested_bitrates:
                    continue
                state.requested_bitrates[self] = updated
                desired = min(
                    state.requested_bitrates.values(),
                    default=updated,
                )
                if desired == state.bitrate:
                    continue
                state.bitrate = desired
                # VideoToolbox accepts a live bitrate update on supported
                # macOS releases.  Keep the codec instance (and its GOP) so a
                # REMB report cannot create a visible frame gap.  Fall back to
                # reopening only when a particular codec build rejects it.
                if state.codec is not None:
                    try:
                        state.codec.bit_rate = desired
                    except (AttributeError, ValueError):
                        state.codec = None
                # Cached packets were produced at the previous bitrate.
                state.cache.clear()
        logger.info("VideoToolbox bitrate adapted to %.2f Mbps.", updated / 1_000_000)

    def _new_codec(self, frame, bitrate=None):
        codec = av.CodecContext.create("h264_videotoolbox", "w")
        codec.width = frame.width
        codec.height = frame.height
        codec.bit_rate = bitrate or self.target_bitrate
        codec.pix_fmt = "nv12"
        codec.framerate = fractions.Fraction(30, 1)
        codec.time_base = fractions.Fraction(1, 30)
        codec.max_b_frames = 0
        codec.gop_size = 60
        codec.options = {
            "realtime": "1",
            "allow_sw": "1",
            "prio_speed": "1",
            "profile": "baseline",
            "level": "4.2",
        }
        return codec

    def encode(self, frame, force_keyframe=False):
        variant = (frame.width, frame.height)
        with self._pool_lock:
            state = self._pool.setdefault(
                variant,
                _SharedVideoToolboxState(self.target_bitrate),
            )

        frame_key = (
            int(frame.pts or 0),
            int(frame.time_base.numerator),
            int(frame.time_base.denominator),
        )
        with state.lock:
            state.requested_bitrates[self] = self.target_bitrate
            desired = min(
                state.requested_bitrates.values(),
                default=self.target_bitrate,
            )
            if desired != state.bitrate:
                state.bitrate = desired
                if state.codec is not None:
                    try:
                        state.codec.bit_rate = desired
                    except (AttributeError, ValueError):
                        state.codec = None
                state.cache.clear()
            cached = state.cache.get(frame_key)
            if cached is not None and (not force_keyframe or cached[2]):
                state.cache_hits += 1
                return list(cached[0]), cached[1]

            if self.target_bitrate < state.bitrate:
                state.bitrate = self.target_bitrate
                state.codec = None
            if state.codec is None:
                state.codec = self._new_codec(frame, state.bitrate)

            frame.pict_type = (
                av.video.frame.PictureType.I
                if force_keyframe
                else av.video.frame.PictureType.NONE
            )
            nal_units = []
            timestamp = None
            for packet in state.codec.encode(frame):
                nal_units.extend(self._split_bitstream(bytes(packet)))
                if timestamp is None:
                    timestamp = convert_timebase(
                        packet.pts, packet.time_base, VIDEO_TIME_BASE
                    )
            if not nal_units:
                fallback_timestamp = convert_timebase(
                    frame.pts, frame.time_base, VIDEO_TIME_BASE
                )
                return [], fallback_timestamp

            payloads = tuple(self._packetize(nal_units))
            state.encode_count += 1
            state.cache[frame_key] = (payloads, timestamp, bool(force_keyframe))
            state.cache.move_to_end(frame_key)
            while len(state.cache) > self.CACHE_FRAMES:
                state.cache.popitem(last=False)
            return list(payloads), timestamp


class WindowsH264Encoder(H264Encoder):
    """Stable low-delay H.264 with NVIDIA/Intel/AMD/MF probing and x264 fallback."""

    MIN_BITRATE = 750_000
    INITIAL_BITRATE = 2_000_000
    MAX_BITRATE = 4_000_000
    DOWNSHIFT_INTERVAL = 1.0
    UPSHIFT_INTERVAL = 8.0

    def __init__(self, codec_name=None):
        self._windows_target_bitrate = self.INITIAL_BITRATE
        self._last_bitrate_update = 0.0
        self.allow_bitrate_downshift = False
        self._codec_override = codec_name
        self.active_codec_name = None
        self._hardware_failed = set()
        super().__init__()
        self._windows_target_bitrate = self.INITIAL_BITRATE

    @property
    def target_bitrate(self):
        return self._windows_target_bitrate

    @target_bitrate.setter
    def target_bitrate(self, bitrate):
        requested = max(self.MIN_BITRATE, min(self.MAX_BITRATE, int(bitrate)))
        current = self._windows_target_bitrate
        now = time.monotonic()
        elapsed = now - self._last_bitrate_update
        if (
            requested <= current * 0.85
            and elapsed >= self.DOWNSHIFT_INTERVAL
            and self.allow_bitrate_downshift
        ):
            updated = max(requested, int(current * 0.65), self.MIN_BITRATE)
        elif requested >= current * 1.25 and elapsed >= self.UPSHIFT_INTERVAL:
            updated = min(requested, int(current * 1.20), self.MAX_BITRATE)
        else:
            return
        if updated == current:
            return
        self._windows_target_bitrate = updated
        self._last_bitrate_update = now
        logger.info("Windows H.264 bitrate adapted to %.2f Mbps.", updated / 1_000_000)

    @staticmethod
    def _codec_options(codec_name):
        if codec_name == "h264_nvenc":
            return {
                "preset": "p1",
                "tune": "ull",
                "rc": "cbr",
                "zerolatency": "1",
            }
        if codec_name == "h264_qsv":
            return {
                "preset": "veryfast",
                "look_ahead": "0",
                "async_depth": "1",
            }
        if codec_name == "h264_amf":
            return {
                "usage": "ultralowlatency",
                "quality": "speed",
                "rc": "cbr",
                "preanalysis": "false",
            }
        if codec_name == "h264_mf":
            return {
                "rate_control": "cbr",
                "scenario": "video_conference",
                "hw_encoding": "1",
            }
        return {
            "preset": "ultrafast",
            "tune": "zerolatency",
            "profile": "baseline",
            "level": "4.2",
            "x264-params": "scenecut=0:sync-lookahead=0:sliced-threads=1",
        }

    def _candidate_names(self):
        if self._codec_override:
            candidates = [self._codec_override]
        else:
            candidates = list(WINDOWS_H264_CANDIDATES)
        if "libx264" not in candidates:
            candidates.append("libx264")
        return [name for name in candidates if name not in self._hardware_failed]

    def _open_codec(self, codec_name, frame):
        codec = av.CodecContext.create(codec_name, "w")
        codec.width = frame.width
        codec.height = frame.height
        codec.bit_rate = self.target_bitrate
        codec.pix_fmt = "yuv420p" if codec_name == "libx264" else "nv12"
        codec.framerate = fractions.Fraction(30, 1)
        codec.time_base = fractions.Fraction(1, 30)
        codec.max_b_frames = 0
        codec.gop_size = 60
        codec.options = self._codec_options(codec_name)
        codec.open()
        return codec

    def _new_codec(self, frame):
        errors = []
        for codec_name in self._candidate_names():
            try:
                codec = self._open_codec(codec_name, frame)
                self.active_codec_name = codec_name
                logger.info(
                    "Windows H.264 encoder active: %s at %.1f Mbps.",
                    codec_name,
                    self.target_bitrate / 1_000_000,
                )
                return codec
            except Exception as error:
                errors.append(f"{codec_name}: {error}")
                if codec_name != "libx264":
                    self._hardware_failed.add(codec_name)
                    logger.warning(
                        "Windows hardware encoder %s unavailable: %s",
                        codec_name,
                        error,
                    )
        raise RuntimeError("No usable Windows H.264 encoder: " + "; ".join(errors))

    def _encode_frame(self, frame, force_keyframe):
        if self.codec and (
            frame.width != self.codec.width or frame.height != self.codec.height
        ):
            self.codec = None
            self.buffer_data = b""
            self.buffer_pts = None

        if self.codec is None:
            self.codec = self._new_codec(frame)

        encode_frame = frame
        if frame.format.name != self.codec.pix_fmt:
            encode_frame = frame.reformat(format=self.codec.pix_fmt)
            encode_frame.pts = frame.pts
            encode_frame.time_base = frame.time_base
        encode_frame.pict_type = (
            av.video.frame.PictureType.I
            if force_keyframe
            else av.video.frame.PictureType.NONE
        )

        try:
            data = b"".join(bytes(packet) for packet in self.codec.encode(encode_frame))
        except Exception as error:
            failed_name = self.active_codec_name
            if failed_name and failed_name != "libx264":
                logger.warning(
                    "Windows encoder %s failed during capture; switching: %s",
                    failed_name,
                    error,
                )
                self._hardware_failed.add(failed_name)
                self.codec = None
                self.active_codec_name = None
                yield from self._encode_frame(frame, True)
                return
            raise
        if data:
            yield from self._split_bitstream(data)


if VIDEO_TOOLBOX_AVAILABLE:
    # aiortc's encoder factory resolves this module global at call time.
    aiortc.codecs.H264Encoder = VideoToolboxH264Encoder
elif sys.platform == "win32":
    aiortc.codecs.H264Encoder = WindowsH264Encoder


if VIDEO_TOOLBOX_AVAILABLE:
    SCREEN_ENCODER_NAME = "h264_videotoolbox"
    SCREEN_ENCODER_HARDWARE = True
elif WINDOWS_H264_CANDIDATES:
    SCREEN_ENCODER_NAME = WINDOWS_H264_CANDIDATES[0]
    SCREEN_ENCODER_HARDWARE = True
else:
    SCREEN_ENCODER_NAME = "libx264"
    SCREEN_ENCODER_HARDWARE = False


class SharedScreenTrack(VideoStreamTrack):
    def __init__(self, capture_hub, client_session):
        super().__init__()
        self.capture_hub = capture_hub
        self.client_session = client_session
        self.sequence = 0
        self._next_frame_at = None

    async def _wait_for_frame_slot(self):
        fps = max(5, min(30, int(self.client_session.target_fps)))
        interval = 1.0 / fps
        now = asyncio.get_running_loop().time()
        if self._next_frame_at is None or now - self._next_frame_at > interval:
            self._next_frame_at = now
        delay = self._next_frame_at - now
        if delay > 0:
            await asyncio.sleep(delay)
        self._next_frame_at += interval

    async def recv(self):
        while True:
            await self._wait_for_frame_slot()
            snapshot = await asyncio.to_thread(
                self.capture_hub.wait_for_frame,
                self.client_session.monitor_id,
                self.sequence,
                1.0,
            )
            if snapshot is not None:
                break
            await asyncio.sleep(0.01)
        self.sequence = snapshot.sequence

        frame = av.VideoFrame.from_numpy_buffer(snapshot.bgra, format="bgra")
        scale = self.client_session.webrtc_scale
        width = max(320, round(frame.width * scale)) & ~1
        height = max(240, round(frame.height * scale)) & ~1
        if scale < 0.995 or width != frame.width or height != frame.height:
            frame = frame.reformat(width=width, height=height, format="nv12")
        # All viewers of this captured frame receive the same timestamp. This
        # allows identical media profiles to reuse an encoded VideoToolbox frame.
        # Keep timestamps relative to one process-wide epoch.  Feeding the
        # absolute monotonic clock into the encoder creates very large RTP
        # timestamps and makes a recovered capture stream look discontinuous.
        # A shared epoch keeps every viewer aligned while remaining monotonic
        # if ScreenCaptureKit is restarted.
        epoch = getattr(self.capture_hub, "media_epoch", None)
        if epoch is None:
            frame.pts = int(snapshot.sequence * 3_000)
        else:
            frame.pts = max(0, round((snapshot.captured_at - epoch) * 90_000))
        frame.time_base = fractions.Fraction(1, 90_000)
        return frame


class WebRTCManager:
    def __init__(self, capture_hub, input_handler=None):
        self.capture_hub = capture_hub
        self.input_handler = input_handler
        self.peer_connections = set()
        self.session_peers = {}

    def set_session_media_health(self, client_session, degraded: bool):
        """Gate encoder downshifts on observed receiver health.

        aiortc keeps the encoder private to each RTP sender, so this small
        bridge updates the opt-in flag without coupling the media loop to the
        control WebSocket.  Recovery (upshifts) remains available at all times.
        """
        peers = self.session_peers.get(id(client_session), ())
        for peer in tuple(peers):
            for sender in peer.getSenders():
                encoder = getattr(sender, "_RTCRtpSender__encoder", None)
                if encoder is not None and hasattr(encoder, "allow_bitrate_downshift"):
                    encoder.allow_bitrate_downshift = bool(degraded)

    async def offer(self, payload, client_session):
        peer = RTCPeerConnection()
        self.peer_connections.add(peer)
        session_key = id(client_session)
        self.session_peers.setdefault(session_key, set()).add(peer)
        track = SharedScreenTrack(self.capture_hub, client_session)
        sender = peer.addTrack(track)
        # A shared VideoToolbox encoder can already be producing P-frames for
        # another viewer.  Request an IDR for this sender so a newly connected
        # browser can decode immediately instead of waiting for the next GOP.
        request_keyframe = getattr(sender, "_send_keyframe", None)
        if callable(request_keyframe):
            request_keyframe()

        @peer.on("datachannel")
        def data_channel_received(channel):
            if channel.label not in {"projectx-pointer", "projectx-control"}:
                return

            @channel.on("message")
            def data_channel_message(message):
                if self.input_handler is None or not isinstance(message, str):
                    return
                if len(message) > 4096:
                    return
                try:
                    data = json.loads(message)
                except (json.JSONDecodeError, TypeError):
                    return
                if not isinstance(data, dict):
                    return
                asyncio.create_task(
                    self.input_handler(data, client_session, channel)
                )

        codecs = RTCRtpSender.getCapabilities("video").codecs
        h264 = [codec for codec in codecs if codec.mimeType.lower() == "video/h264"]
        others = [codec for codec in codecs if codec.mimeType.lower() != "video/h264"]
        for transceiver in peer.getTransceivers():
            if transceiver.sender == sender and h264:
                transceiver.setCodecPreferences(h264 + others)

        @peer.on("connectionstatechange")
        async def connection_state_changed():
            logger.info("WebRTC connection state: %s", peer.connectionState)
            if peer.connectionState in {"failed", "closed", "disconnected"}:
                await peer.close()
                self.peer_connections.discard(peer)
                session_set = self.session_peers.get(session_key)
                if session_set is not None:
                    session_set.discard(peer)
                    if not session_set:
                        self.session_peers.pop(session_key, None)
                client_session.use_webrtc = False

        await peer.setRemoteDescription(
            RTCSessionDescription(sdp=payload["sdp"], type=payload["type"])
        )
        answer = await peer.createAnswer()
        await peer.setLocalDescription(answer)
        client_session.use_webrtc = True
        response_sdp, preferred_lan_address = _prefer_direct_lan_sdp(
            peer.localDescription.sdp,
            getattr(client_session, "remote_address", None),
        )
        if preferred_lan_address:
            logger.info(
                "WebRTC answer pinned to direct LAN interface %s for viewer %s.",
                preferred_lan_address,
                client_session.remote_address,
            )
        return {
            "sdp": response_sdp,
            "type": peer.localDescription.type,
            "codec": SCREEN_ENCODER_NAME,
            "hardware": SCREEN_ENCODER_HARDWARE,
        }

    async def close_session(self, client_session):
        peers = list(self.session_peers.pop(id(client_session), set()))
        for peer in peers:
            self.peer_connections.discard(peer)
        if peers:
            await asyncio.gather(
                *(peer.close() for peer in peers),
                return_exceptions=True,
            )
        client_session.use_webrtc = False
        if not self.peer_connections and VIDEO_TOOLBOX_AVAILABLE:
            stats = VideoToolboxH264Encoder.shared_pool_stats()
            if stats["encodes"]:
                logger.info(
                    "Shared VideoToolbox pool: %d encodes, %d reused frames, %d variants.",
                    stats["encodes"],
                    stats["cache_hits"],
                    stats["variants"],
                )
            VideoToolboxH264Encoder.clear_shared_pool()

    async def close(self):
        peers = list(self.peer_connections)
        self.peer_connections.clear()
        self.session_peers.clear()
        await asyncio.gather(*(peer.close() for peer in peers), return_exceptions=True)
