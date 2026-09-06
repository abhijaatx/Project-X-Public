(() => {
  const canvas = document.getElementById("screen-canvas");
  const ctx = canvas.getContext("2d");
  const screenVideo = document.getElementById("screen-video");
  const localCursor = document.getElementById("local-cursor");
  const authModal = document.getElementById("auth-modal");
  const pinInput = document.getElementById("pin-input");
  const btnConnect = document.getElementById("btn-connect");
  const authError = document.getElementById("auth-error");
  const statPing = document.getElementById("stat-ping");
  const statFps = document.getElementById("stat-fps");
  const qualitySelect = document.getElementById("quality-select");
  const fpsSelect = document.getElementById("fps-select");
  const transportSelect = document.getElementById("transport-select");
  const micVolumeSelect = document.getElementById("mic-volume-select");
  const monitorSelect = document.getElementById("monitor-select");
  const monitorGroup = document.getElementById("monitor-group");
  const btnWin = document.getElementById("btn-win");
  const btnCAD = document.getElementById("btn-cad");
  const btnAltTab = document.getElementById("btn-alttab");
  const btnFullscreen = document.getElementById("btn-fullscreen");
  const btnDisconnect = document.getElementById("btn-disconnect");
  const toolbar = document.getElementById("toolbar");
  const mobileKeyInput = document.getElementById("mobile-key-input");
  const btnKbd = document.getElementById("btn-kbd");
  const btnCam = document.getElementById("btn-cam");
  const btnMic = document.getElementById("btn-mic");
  const pipCam = document.getElementById("pip-cam");
  const pipClose = document.getElementById("pip-close");
  const pipDragHandle = document.getElementById("pip-drag-handle");
  const camCanvas = document.getElementById("cam-canvas");
  const camCtx = camCanvas ? camCanvas.getContext("2d") : null;
  const btnPaste = document.getElementById("btn-paste");
  const typeTextModal = document.getElementById("type-text-modal");
  const typeTextDialog = typeTextModal?.querySelector(".type-text-dialog");
  const typeTextDragHandle = document.getElementById("type-text-drag-handle");
  const typeTextResizeHandle = document.getElementById("type-text-resize-handle");
  const typeTextInput = document.getElementById("type-text-input");
  const typeTextSend = document.getElementById("type-text-send");
  const typeTextCancel = document.getElementById("type-text-cancel");
  const typeTextClose = document.getElementById("type-text-close");
  const typeTextStatus = document.getElementById("type-text-status");
  const typeSpeedInput = document.getElementById("type-speed-input");
  const typeCodeMode = document.getElementById("type-code-mode");
  const typeSpeedHint = document.getElementById("type-speed-hint");

  let ws = null;
  let mediaWs = null;
  let isConnected = false;
  let isCameraOn = false;
  let frameCount = 0;
  let lastFpsTime = performance.now();
  let pendingPointerMove = null;
  let pointerMoveScheduled = false;
  let pendingWheelDelta = 0;
  let wheelScheduled = false;
  let pingInterval = null;
  let remoteWidth = 1920;
  let remoteHeight = 1080;
  let hostPlatform = "win32";
  let reconnectTimer = null;
  let mediaReconnectTimer = null;
  let manualDisconnect = false;
  let uploadToken = null;
  let cameraRenderBusy = false;
  let pendingCameraBlob = null;
  let screenRenderBusy = false;
  let pendingScreenBlob = null;
  let isMicrophoneOn = false;
  let audioContext = null;
  let audioGainNode = null;
  let audioCompressorNode = null;
  let nextAudioTime = 0;
  const activeAudioSources = new Set();
  let peerConnection = null;
  let webRTCGeneration = 0;
  let webRTCAbortController = null;
  let mediaStatsInterval = null;
  let webRTCActive = false;
  let videoFrameCallbackActive = false;
  let videoCallbackGeneration = 0;
  let lastPacketsReceived = 0;
  let lastPacketsLost = 0;
  let lastDecodedFrames = 0;
  let lastMediaProgressAt = performance.now();
  let lastControlRttMs = 0;
  let mobileDefaultsApplied = false;
  let activeTypingRequest = null;
  let pointerDataChannel = null;
  let controlDataChannel = null;
  let inputSequence = 0;
  const pendingInputTimes = new Map();
  let iceServers = [];
  let activeIcePolicy = "all";
  let routeSamples = 0;
  let routeBenchmark = {
    directRtt: null,
    relayTried: false,
    finalized: false
  };

  // Quality presets: quality (0-100), scale (0.3-1.0)
  const QUALITY_PRESETS = {
    low: { quality: 32, scale: 0.70 },
    med: { quality: 55, scale: 0.90 },
    high: { quality: 70, scale: 1.0 },
    max: { quality: 85, scale: 1.0 }
  };

  function initWebSocket() {
    clearTimeout(reconnectTimer);
    if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) {
      return;
    }
    const protocol = location.protocol === "https:" ? "wss:" : "ws:";
    const wsUrl = `${protocol}//${location.host}/ws`;

    const socket = new WebSocket(wsUrl);
    ws = socket;
    socket.onopen = () => {
      // Send PIN
      const pin = pinInput.value.trim();
      socket.send(JSON.stringify({ type: "auth", pin: pin }));
    };

    socket.onmessage = async (event) => {
      if (typeof event.data === "string") {
        const msg = JSON.parse(event.data);
        handleServerMessage(msg);
      }
    };

    socket.onclose = () => {
      if (ws !== socket) return;
      isConnected = false;
      uploadToken = null;
      pendingCameraBlob = null;
      clearTimeout(mediaReconnectTimer);
      if (mediaWs) {
        mediaWs.close();
        mediaWs = null;
      }
      stopWebRTC();
      clearInterval(pingInterval);
      authModal.style.display = "flex";
      if (isCameraOn) {
        btnCam.disabled = false;
        btnCam.textContent = "📹 Camera (reconnecting)";
      }
      if (isMicrophoneOn) {
        btnMic.disabled = false;
        btnMic.textContent = "🎤 Reconnecting...";
      }
      if (!manualDisconnect) {
        authError.textContent = "Disconnected from host. Reconnecting...";
        reconnectTimer = setTimeout(initWebSocket, 2500);
      } else {
        authError.textContent = "Disconnected.";
      }
    };

    socket.onerror = (err) => {
      console.error("WebSocket error:", err);
      authError.textContent = "Connection error. Ensure host server is running.";
    };
  }

  function initMediaWebSocket() {
    clearTimeout(mediaReconnectTimer);
    if (!isConnected || !uploadToken) return;
    if (mediaWs && [WebSocket.OPEN, WebSocket.CONNECTING].includes(mediaWs.readyState)) {
      return;
    }
    const protocol = location.protocol === "https:" ? "wss:" : "ws:";
    const mediaUrl = `${protocol}//${location.host}/ws/media`;
    const socket = new WebSocket(mediaUrl);
    mediaWs = socket;
    socket.binaryType = "arraybuffer";

    socket.onopen = () => {
      if (mediaWs !== socket) return;
      socket.send(JSON.stringify({ type: "media_auth", token: uploadToken }));
      sendFrameAck();
    };

    socket.onmessage = async (event) => {
      if (!(event.data instanceof ArrayBuffer || event.data instanceof Blob)) return;
      const buffer = event.data instanceof ArrayBuffer
        ? event.data
        : await event.data.arrayBuffer();
      const bytes = new Uint8Array(buffer);
      const tag = bytes[0];
      if (tag === 1) {
        queueScreenFrame(new Blob([bytes.subarray(1)], { type: "image/jpeg" }));
      } else if (tag === 2) {
        queueCamFrame(new Blob([bytes.subarray(1)], { type: "image/jpeg" }));
      } else if (tag === 3) {
        playMicrophoneFrame(bytes);
      }
    };

    socket.onclose = () => {
      if (mediaWs !== socket) return;
      mediaWs = null;
      if (isConnected && !manualDisconnect) {
        mediaReconnectTimer = setTimeout(initMediaWebSocket, 700);
      }
    };

    socket.onerror = () => {
      // onclose owns the bounded reconnect loop.
    };
  }

  function handleServerMessage(msg) {
    if (msg.type === "auth_ok") {
      isConnected = true;
      authModal.style.display = "none";
      remoteWidth = msg.width || 1920;
      remoteHeight = msg.height || 1080;
      hostPlatform = msg.platform || "win32";
      uploadToken = msg.upload_token || null;
      iceServers = Array.isArray(msg.ice_servers) ? msg.ice_servers : [];
      routeSamples = 0;
      routeBenchmark = { directRtt: null, relayTried: false, finalized: false };
      if (
        !mobileDefaultsApplied
        && window.matchMedia?.("(pointer: coarse)").matches
        && Math.min(window.innerWidth, window.innerHeight) < 1024
      ) {
        // A phone decoder and Wi-Fi radio benefit more from bounded latency
        // than from desktop-sized detail. The user can still raise either
        // setting after the connection proves healthy.
        qualitySelect.value = "low";
        fpsSelect.value = "15";
        mobileDefaultsApplied = true;
      }
      // Try WebRTC on every modern browser, including Windows viewers of a Mac
      // host. The previous unconditional Windows->Mac JPEG fallback caused
      // multi-second latency on routed/campus Wi-Fi. Runtime stall detection
      // below still falls back safely if a particular decoder cannot progress.
      transportSelect.value = "webrtc";
      canvas.width = remoteWidth;
      canvas.height = remoteHeight;

      if (hostPlatform === "darwin") {
        btnWin.textContent = "⌘ Desktop";
        btnWin.title = "Show Desktop";
        btnAltTab.textContent = "⌘ Tab";
        btnCAD.style.display = "none";
      } else {
        btnWin.textContent = "⊞ Win";
        btnAltTab.textContent = "Alt+Tab";
        btnCAD.style.display = "";
      }

      // Handle multi-monitors
      if (msg.monitors && msg.monitors.length > 1) {
        monitorGroup.style.display = "flex";
        monitorSelect.innerHTML = "";
        msg.monitors.forEach((m) => {
          const opt = document.createElement("option");
          opt.value = m.id;
          opt.textContent = `Screen ${m.id} (${m.width}x${m.height})`;
          monitorSelect.appendChild(opt);
        });
      }

      startPing();
      sendQualitySettings();
      initMediaWebSocket();
      if (isCameraOn && ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ type: "toggle_camera", enabled: true }));
      }
      if (isMicrophoneOn && ws && ws.readyState === WebSocket.OPEN) {
        ensureAudioContext();
        ws.send(JSON.stringify({ type: "toggle_microphone", enabled: true }));
      }
      applyMediaTransport();
      canvas.focus();
    } else if (msg.type === "monitor_changed") {
      remoteWidth = msg.monitor.width || remoteWidth;
      remoteHeight = msg.monitor.height || remoteHeight;
    } else if (msg.type === "camera_status") {
      if (msg.status === "starting") {
        btnCam.disabled = true;
        btnCam.textContent = "📹 Starting...";
      } else if (msg.status === "on") {
        btnCam.disabled = false;
        pipCam.style.display = "block";
        btnCam.classList.add("active");
        btnCam.textContent = "📹 Camera (ON)";
      } else if (msg.status === "off") {
        btnCam.disabled = false;
        pipCam.style.display = "none";
        btnCam.classList.remove("active");
        btnCam.textContent = "📹 Camera";
      } else if (msg.status === "error") {
        isCameraOn = false;
        btnCam.disabled = false;
        pipCam.style.display = "none";
        btnCam.classList.remove("active");
        btnCam.textContent = "📹 Camera unavailable";
      }
    } else if (msg.type === "microphone_status") {
      if (msg.status === "starting") {
        btnMic.disabled = true;
        btnMic.textContent = "🎤 Starting...";
      } else if (msg.status === "on") {
        btnMic.disabled = false;
        btnMic.classList.add("active");
        btnMic.textContent = "🔊 Listening";
      } else if (msg.status === "off") {
        btnMic.disabled = false;
        btnMic.classList.remove("active");
        btnMic.textContent = "🎤 Listen";
      } else if (msg.status === "error") {
        isMicrophoneOn = false;
        stopMicrophonePlayback();
        btnMic.disabled = false;
        btnMic.classList.remove("active");
        btnMic.textContent = "🎤 Mic unavailable";
      }
    } else if (msg.type === "typing_status") {
      if (msg.request_id !== activeTypingRequest) return;
      if (msg.status === "started") {
        const mode = msg.code_mode ? "Code-Safe · " : "";
        typeTextStatus.textContent = `${mode}Typing ${msg.total} character${msg.total === 1 ? "" : "s"} at ${msg.chars_per_second} chars/sec…`;
      } else if (msg.status === "complete") {
        const skipped = msg.skipped || 0;
        typeTextStatus.textContent = skipped
          ? `Typed ${msg.typed}; ${skipped} unsupported character${skipped === 1 ? "" : "s"} skipped.`
          : `Typed ${msg.typed} character${msg.typed === 1 ? "" : "s"}.`;
        typeTextSend.disabled = false;
        activeTypingRequest = null;
      } else if (msg.status === "cancelled") {
        typeTextStatus.textContent = "Typing was cancelled.";
        typeTextSend.disabled = false;
        activeTypingRequest = null;
      } else if (msg.status === "error") {
        typeTextStatus.textContent = msg.message || "Text could not be typed on the host.";
        typeTextStatus.classList.add("is-error");
        typeTextSend.disabled = false;
        activeTypingRequest = null;
      }
    } else if (msg.type === "auth_fail") {
      authError.textContent = msg.message || "Invalid PIN. Try again.";
    } else if (msg.type === "pong") {
      const rtt = Math.round(performance.now() - msg.time);
      lastControlRttMs = rtt;
      statPing.textContent = `${rtt} ms`;
    }
  }

  async function renderFrame(blob) {
    try {
      if (window.createImageBitmap) {
        try {
          const bitmap = await createImageBitmap(blob);
          if (canvas.width !== bitmap.width || canvas.height !== bitmap.height) {
            canvas.width = bitmap.width;
            canvas.height = bitmap.height;
          }
          ctx.drawImage(bitmap, 0, 0);
          bitmap.close();
          trackFps();
          sendFrameAck();
          return;
        } catch (bitmapErr) {
          // Fallback to Image element
        }
      }

      await new Promise((resolve) => {
        const img = new Image();
        const url = URL.createObjectURL(blob);
        img.onload = () => {
          if (canvas.width !== img.width || canvas.height !== img.height) {
            canvas.width = img.width;
            canvas.height = img.height;
          }
          ctx.drawImage(img, 0, 0);
          URL.revokeObjectURL(url);
          trackFps();
          sendFrameAck();
          resolve();
        };
        img.onerror = () => {
          URL.revokeObjectURL(url);
          sendFrameAck();
          resolve();
        };
        img.src = url;
      });
    } catch (e) {
      console.error("Frame render error:", e);
      sendFrameAck();
    }
  }

  async function queueScreenFrame(blob) {
    // Decode at most one screen frame while retaining only the newest pending
    // frame. Acknowledge a superseded frame so the host's bounded send window
    // remains accurate without ever rendering stale work.
    if (pendingScreenBlob) sendFrameAck();
    pendingScreenBlob = blob;
    if (screenRenderBusy) return;
    screenRenderBusy = true;
    try {
      while (pendingScreenBlob) {
        const latestFrame = pendingScreenBlob;
        pendingScreenBlob = null;
        await renderFrame(latestFrame);
      }
    } finally {
      screenRenderBusy = false;
    }
  }

  async function renderCamFrame(blob) {
    if (!camCtx) return;
    try {
      if (window.createImageBitmap) {
        try {
          const bitmap = await createImageBitmap(blob);
          if (camCanvas.width !== bitmap.width || camCanvas.height !== bitmap.height) {
            camCanvas.width = bitmap.width;
            camCanvas.height = bitmap.height;
          }
          camCtx.drawImage(bitmap, 0, 0);
          bitmap.close();
          return;
        } catch (e) {}
      }
      const img = new Image();
      const url = URL.createObjectURL(blob);
      await new Promise((resolve) => {
        img.onload = () => {
          if (camCanvas.width !== img.width || camCanvas.height !== img.height) {
            camCanvas.width = img.width;
            camCanvas.height = img.height;
          }
          camCtx.drawImage(img, 0, 0);
          URL.revokeObjectURL(url);
          resolve();
        };
        img.onerror = () => {
          URL.revokeObjectURL(url);
          resolve();
        };
        img.src = url;
      });
    } catch (err) {
      console.error("Camera render error:", err);
    }
  }

  async function queueCamFrame(blob) {
    // Keep the newest frame only. If decoding falls behind, stale camera frames
    // are discarded instead of delaying the desktop stream and input handling.
    pendingCameraBlob = blob;
    if (cameraRenderBusy) return;
    cameraRenderBusy = true;
    try {
      while (pendingCameraBlob) {
        const latestFrame = pendingCameraBlob;
        pendingCameraBlob = null;
        await renderCamFrame(latestFrame);
      }
    } finally {
      cameraRenderBusy = false;
    }
  }

  function ensureAudioContext() {
    if (!audioContext || audioContext.state === "closed") {
      const AudioContextClass = window.AudioContext || window.webkitAudioContext;
      if (!AudioContextClass) return false;
      audioContext = new AudioContextClass({ latencyHint: "interactive" });
      audioGainNode = audioContext.createGain();
      audioCompressorNode = audioContext.createDynamicsCompressor();
      audioGainNode.gain.value = Number(micVolumeSelect?.value) || 3;
      audioCompressorNode.threshold.value = -28;
      audioCompressorNode.knee.value = 20;
      audioCompressorNode.ratio.value = 6;
      audioCompressorNode.attack.value = 0.003;
      audioCompressorNode.release.value = 0.25;
      audioGainNode.connect(audioCompressorNode);
      audioCompressorNode.connect(audioContext.destination);
      nextAudioTime = 0;
    }
    if (audioContext.state === "suspended") {
      audioContext.resume().catch(() => {});
    }
    return true;
  }

  function clearScheduledAudio() {
    for (const source of activeAudioSources) {
      try { source.stop(); } catch (_error) {}
    }
    activeAudioSources.clear();
    nextAudioTime = audioContext ? audioContext.currentTime : 0;
  }

  function stopMicrophonePlayback() {
    clearScheduledAudio();
    if (audioContext) {
      audioContext.close().catch(() => {});
      audioContext = null;
      audioGainNode = null;
      audioCompressorNode = null;
    }
  }

  function playMicrophoneFrame(bytes) {
    if (!isMicrophoneOn || bytes.byteLength <= 6 || !ensureAudioContext()) return;
    const header = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
    const sampleRate = header.getUint32(2, true);
    const sampleCount = Math.floor((bytes.byteLength - 6) / 2);
    if (!sampleRate || !sampleCount) return;

    const pcm = new Int16Array(bytes.buffer, bytes.byteOffset + 6, sampleCount);
    const samples = new Float32Array(sampleCount);
    for (let index = 0; index < sampleCount; index++) {
      samples[index] = pcm[index] / 32768;
    }

    const buffer = audioContext.createBuffer(1, sampleCount, sampleRate);
    buffer.copyToChannel(samples, 0);
    const source = audioContext.createBufferSource();
    source.buffer = buffer;
    source.connect(audioGainNode || audioContext.destination);

    const now = audioContext.currentTime;
    if (nextAudioTime < now || nextAudioTime > now + 0.2) {
      clearScheduledAudio();
      nextAudioTime = now + 0.03;
    }
    source.onended = () => activeAudioSources.delete(source);
    activeAudioSources.add(source);
    source.start(nextAudioTime);
    nextAudioTime += sampleCount / sampleRate;
  }

  function waitForIceGathering(connection) {
    if (connection.iceGatheringState === "complete") return Promise.resolve();
    return new Promise((resolve) => {
      const listener = () => {
        if (connection.iceGatheringState === "complete") {
          connection.removeEventListener("icegatheringstatechange", listener);
          resolve();
        }
      };
      connection.addEventListener("icegatheringstatechange", listener);
      setTimeout(resolve, 3000);
    });
  }

  function useWebSocketVideoFallback() {
    webRTCActive = false;
    videoFrameCallbackActive = false;
    videoCallbackGeneration++;
    screenVideo.pause();
    screenVideo.srcObject = null;
    screenVideo.style.display = "none";
    canvas.style.opacity = "1";
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: "media_transport", transport: "websocket" }));
      initMediaWebSocket();
      sendFrameAck();
    }
  }

  function stopWebRTC() {
    webRTCGeneration++;
    if (webRTCAbortController) {
      webRTCAbortController.abort();
      webRTCAbortController = null;
    }
    clearInterval(mediaStatsInterval);
    mediaStatsInterval = null;
    if (peerConnection) {
      peerConnection.close();
      peerConnection = null;
    }
    pointerDataChannel = null;
    controlDataChannel = null;
    pendingInputTimes.clear();
    useWebSocketVideoFallback();
  }

  function applyMediaTransport() {
    if (!isConnected) return;
    if (transportSelect.value === "webrtc") {
      startWebRTC().catch((error) => {
        if (error && error.name === "AbortError") return;
        console.warn("WebRTC unavailable; keeping JPEG fallback:", error);
        useWebSocketVideoFallback();
      });
    } else {
      stopWebRTC();
    }
  }

  async function sendWebRTCStats() {
    if (!peerConnection || !webRTCActive || !ws || ws.readyState !== WebSocket.OPEN) return;
    const reports = await peerConnection.getStats();
    let selectedPair = null;
    let selectedLocalCandidate = null;
    for (const report of reports.values()) {
      if (
        report.type === "candidate-pair"
        && report.state === "succeeded"
        && (report.nominated || report.selected)
      ) {
        selectedPair = report;
        selectedLocalCandidate = reports.get(report.localCandidateId) || null;
        break;
      }
    }
    const routeRtt = selectedPair?.currentRoundTripTime;
    const routeType = selectedLocalCandidate?.candidateType || "unknown";

    for (const report of reports.values()) {
      if (report.type === "inbound-rtp" && report.kind === "video") {
        const received = report.packetsReceived || 0;
        const lost = report.packetsLost || 0;
        const receivedDelta = Math.max(0, received - lastPacketsReceived);
        const lostDelta = Math.max(0, lost - lastPacketsLost);
        const total = Math.max(1, receivedDelta + lostDelta);
        lastPacketsReceived = received;
        lastPacketsLost = lost;
        const renderedFps = report.framesPerSecond || 0;
        const decodedFrames = report.framesDecoded || report.framesReceived || 0;
        if (decodedFrames > lastDecodedFrames) {
          lastMediaProgressAt = performance.now();
        }
        lastDecodedFrames = Math.max(lastDecodedFrames, decodedFrames);
        ws.send(JSON.stringify({
          type: "media_stats",
          packetLoss: lostDelta / total,
          jitter: report.jitter || 0,
          fps: renderedFps,
          framesDropped: report.framesDropped || 0,
          routeRtt: Number.isFinite(routeRtt) ? routeRtt : null,
          routeType,
          controlRttMs: lastControlRttMs
        }));
        maybeBenchmarkRoute(routeRtt, routeType);
        if (performance.now() - lastMediaProgressAt > 7000) {
          // Keep the low-bandwidth H.264 path and renegotiate it. Falling
          // directly to full-frame JPEG under congestion compounds the queue.
          lastMediaProgressAt = performance.now();
          restartWebRTCWithPolicy(activeIcePolicy);
        }
        break;
      }
    }
  }

  function hasTurnServer() {
    return iceServers.some((server) => {
      const urls = Array.isArray(server.urls) ? server.urls : [server.urls];
      return urls.some((url) => typeof url === "string" && url.startsWith("turn"));
    });
  }

  function restartWebRTCWithPolicy(policy) {
    if (transportSelect.value !== "webrtc") return;
    webRTCGeneration++;
    clearInterval(mediaStatsInterval);
    mediaStatsInterval = null;
    if (webRTCAbortController) webRTCAbortController.abort();
    webRTCAbortController = null;
    const previous = peerConnection;
    peerConnection = null;
    if (previous) previous.close();
    pointerDataChannel = null;
    controlDataChannel = null;
    webRTCActive = false;
    routeSamples = 0;
    setTimeout(() => {
      startWebRTC(policy).catch(() => useWebSocketVideoFallback());
    }, 50);
  }

  function maybeBenchmarkRoute(rtt, routeType) {
    if (!hasTurnServer() || !Number.isFinite(rtt) || routeBenchmark.finalized) return;
    routeSamples++;
    if (
      activeIcePolicy === "all"
      && !routeBenchmark.relayTried
      && routeSamples >= 3
      && rtt > 0.08
      && routeType !== "relay"
    ) {
      routeBenchmark.directRtt = rtt;
      routeBenchmark.relayTried = true;
      restartWebRTCWithPolicy("relay");
    } else if (activeIcePolicy === "relay" && routeSamples >= 3) {
      const directRtt = routeBenchmark.directRtt;
      routeBenchmark.finalized = true;
      if (directRtt !== null && rtt >= directRtt * 0.9) {
        restartWebRTCWithPolicy("all");
      }
    }
  }

  function startWebRTCFrameTracking() {
    if (!screenVideo.requestVideoFrameCallback) return;
    const generation = ++videoCallbackGeneration;
    videoFrameCallbackActive = true;
    const onFrame = () => {
      if (!webRTCActive || generation !== videoCallbackGeneration) {
        videoFrameCallbackActive = false;
        return;
      }
      lastMediaProgressAt = performance.now();
      trackFps();
      screenVideo.requestVideoFrameCallback(onFrame);
    };
    screenVideo.requestVideoFrameCallback(onFrame);
  }

  async function startWebRTC(icePolicy = "all") {
    if (!uploadToken || !window.RTCPeerConnection || peerConnection) return;
    const generation = ++webRTCGeneration;
    const abortController = new AbortController();
    webRTCAbortController = abortController;
    activeIcePolicy = icePolicy;
    const connection = new RTCPeerConnection({
      bundlePolicy: "max-bundle",
      iceServers,
      iceTransportPolicy: icePolicy
    });
    lastPacketsReceived = 0;
    lastPacketsLost = 0;
    lastDecodedFrames = 0;
    lastMediaProgressAt = performance.now();
    peerConnection = connection;
    pointerDataChannel = connection.createDataChannel("dc0", {
      ordered: false,
      maxRetransmits: 0
    });
    controlDataChannel = connection.createDataChannel("dc1", {
      ordered: true
    });
    controlDataChannel.onmessage = (event) => {
      try {
        const message = JSON.parse(event.data);
        if (message.type !== "input_ack") return;
        const sentAt = pendingInputTimes.get(message.input_id);
        if (sentAt === undefined) return;
        pendingInputTimes.delete(message.input_id);
        statPing.textContent = `${Math.round(performance.now() - sentAt)} ms`;
      } catch (_error) {}
    };
    connection.addTransceiver("video", { direction: "recvonly" });

    connection.ontrack = (event) => {
      if (
        event.track.kind !== "video"
        || generation !== webRTCGeneration
        || peerConnection !== connection
      ) return;
      if ("playoutDelayHint" in event.receiver) {
        event.receiver.playoutDelayHint = 0;
      }
      if ("jitterBufferTarget" in event.receiver) {
        event.receiver.jitterBufferTarget = 0;
      }
      screenVideo.srcObject = new MediaStream([event.track]);
      screenVideo.onloadedmetadata = () => {
        remoteWidth = screenVideo.videoWidth || remoteWidth;
        remoteHeight = screenVideo.videoHeight || remoteHeight;
        canvas.width = remoteWidth;
        canvas.height = remoteHeight;
        screenVideo.style.display = "block";
        canvas.style.opacity = "0";
        webRTCActive = true;
        screenVideo.play().catch(() => {});
        startWebRTCFrameTracking();
        clearInterval(mediaStatsInterval);
        mediaStatsInterval = setInterval(() => {
          sendWebRTCStats().catch(() => {});
        }, 2000);
      };
    };

    connection.onconnectionstatechange = () => {
      if (peerConnection !== connection) return;
      if (["failed", "disconnected", "closed"].includes(connection.connectionState)) {
        if (peerConnection === connection) peerConnection = null;
        if (connection.connectionState !== "closed") connection.close();
        if (activeIcePolicy === "relay" && routeBenchmark.directRtt !== null) {
          routeBenchmark.finalized = true;
          restartWebRTCWithPolicy("all");
        } else {
          useWebSocketVideoFallback();
        }
      }
    };

    const offer = await connection.createOffer();
    await connection.setLocalDescription(offer);
    await waitForIceGathering(connection);
    if (generation !== webRTCGeneration || peerConnection !== connection) {
      connection.close();
      return;
    }
    const response = await fetch("/api/webrtc/offer", {
      method: "POST",
      signal: abortController.signal,
      headers: {
        "Authorization": `Bearer ${uploadToken}`,
        "Content-Type": "application/json"
      },
      body: JSON.stringify({
        sdp: connection.localDescription.sdp,
        type: connection.localDescription.type
      })
    });
    if (!response.ok) throw new Error(`WebRTC offer failed (${response.status})`);
    const answer = await response.json();
    if (generation !== webRTCGeneration || peerConnection !== connection) {
      connection.close();
      return;
    }
    await connection.setRemoteDescription(answer);
    if (webRTCAbortController === abortController) {
      webRTCAbortController = null;
    }
  }

  function sendFrameAck() {
    if (mediaWs && mediaWs.readyState === WebSocket.OPEN) {
      mediaWs.send(JSON.stringify({ type: "frame_ack" }));
    }
  }

  function trackFps() {
    frameCount++;
    const now = performance.now();
    if (now - lastFpsTime >= 1000) {
      statFps.textContent = `${frameCount} fps`;
      frameCount = 0;
      lastFpsTime = now;
    }
  }

  function startPing() {
    clearInterval(pingInterval);
    pingInterval = setInterval(() => {
      if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ type: "ping", time: performance.now() }));
      }
    }, 2000);
  }

  function sendQualitySettings() {
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    const presetKey = qualitySelect.value;
    const preset = QUALITY_PRESETS[presetKey] || QUALITY_PRESETS.med;
    const fps = parseInt(fpsSelect.value, 10) || 30;

    ws.send(JSON.stringify({
      type: "set_quality",
      quality: preset.quality,
      scale: preset.scale,
      fps: fps
    }));
  }

  function getNormalizedCoords(e) {
    const rect = canvas.getBoundingClientRect();
    const sourceRatio = canvas.width / canvas.height;
    const elementRatio = rect.width / rect.height;
    let contentLeft = rect.left;
    let contentTop = rect.top;
    let contentWidth = rect.width;
    let contentHeight = rect.height;

    if (elementRatio > sourceRatio) {
      contentWidth = rect.height * sourceRatio;
      contentLeft += (rect.width - contentWidth) / 2;
    } else if (elementRatio < sourceRatio) {
      contentHeight = rect.width / sourceRatio;
      contentTop += (rect.height - contentHeight) / 2;
    }

    const x = Math.max(0, Math.min(1, (e.clientX - contentLeft) / contentWidth));
    const y = Math.max(0, Math.min(1, (e.clientY - contentTop) / contentHeight));
    return { x, y };
  }

  function showOptimisticCursor(event) {
    if (!localCursor) return;
    localCursor.style.left = `${event.clientX}px`;
    localCursor.style.top = `${event.clientY}px`;
    localCursor.style.display = "block";
  }

  function sendRealtimeInput(payload, reliable = false) {
    const channel = reliable ? controlDataChannel : pointerDataChannel;
    const controlChannelBackedUp = reliable && channel?.bufferedAmount > 8 * 1024;
    if (channel && channel.readyState === "open" && !controlChannelBackedUp) {
      if (!reliable && channel.bufferedAmount > 16 * 1024) return;
      if (reliable) {
        payload.input_id = ++inputSequence;
        pendingInputTimes.set(payload.input_id, performance.now());
        if (pendingInputTimes.size > 128) {
          pendingInputTimes.delete(pendingInputTimes.keys().next().value);
        }
      }
      channel.send(JSON.stringify(payload));
      return;
    }
    if (controlChannelBackedUp) {
      // Old acknowledgements from the congested channel no longer describe
      // the active control path and would make the latency badge keep rising.
      pendingInputTimes.clear();
    }
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify(payload));
    }
  }

  // Mouse Input Events
  canvas.addEventListener("mousemove", (e) => {
    if (!isConnected) return;
    showOptimisticCursor(e);
    pendingPointerMove = getNormalizedCoords(e);
    if (pointerMoveScheduled) return;
    pointerMoveScheduled = true;
    requestAnimationFrame(() => {
      pointerMoveScheduled = false;
      const latest = pendingPointerMove;
      pendingPointerMove = null;
      if (latest) {
        sendRealtimeInput({ type: "mouse_move", ...latest });
      }
    });
  });

  canvas.addEventListener("mousedown", (e) => {
    if (!isConnected) return;
    e.preventDefault();
    canvas.focus();
    const btn = e.button === 0 ? "left" : (e.button === 2 ? "right" : "middle");
    const { x, y } = getNormalizedCoords(e);
    sendRealtimeInput({ type: "mouse_down", button: btn, x, y }, true);
  });

  canvas.addEventListener("mouseup", (e) => {
    if (!isConnected) return;
    e.preventDefault();
    const btn = e.button === 0 ? "left" : (e.button === 2 ? "right" : "middle");
    const { x, y } = getNormalizedCoords(e);
    sendRealtimeInput({ type: "mouse_up", button: btn, x, y }, true);
  });

  canvas.addEventListener("dblclick", (e) => {
    if (!isConnected) return;
    e.preventDefault();
    const { x, y } = getNormalizedCoords(e);
    sendRealtimeInput({ type: "mouse_dblclick", x, y }, true);
  });

  canvas.addEventListener("contextmenu", (e) => e.preventDefault());

  canvas.addEventListener("wheel", (e) => {
    if (!isConnected) return;
    e.preventDefault();
    const pixelMultiplier = e.deltaMode === WheelEvent.DOM_DELTA_LINE
      ? 16
      : e.deltaMode === WheelEvent.DOM_DELTA_PAGE
        ? window.innerHeight
        : 1;
    pendingWheelDelta += e.deltaY * pixelMultiplier;
    if (wheelScheduled) return;
    wheelScheduled = true;
    requestAnimationFrame(() => {
      wheelScheduled = false;
      const deltaY = pendingWheelDelta;
      pendingWheelDelta = 0;
      if (deltaY) {
        sendRealtimeInput({ type: "mouse_wheel", deltaY, deltaMode: 0 }, true);
      }
    });
  }, { passive: false });

  // Keyboard Input Events
  window.addEventListener("keydown", (e) => {
    if (!isConnected || authModal.style.display !== "none") return;
    // Don't capture when typing inside the PIN field
    if (document.activeElement === pinInput || document.activeElement === mobileKeyInput || !typeTextModal.hidden) return;

    // Prevent default browser shortcuts (Ctrl+S, Ctrl+P, F5, Tab)
    if (["Tab", "Alt", "F5"].includes(e.key) || (e.ctrlKey && ["s", "p", "f", "w"].includes(e.key.toLowerCase()))) {
      e.preventDefault();
    }

    sendRealtimeInput({
      type: "key_down",
      key: e.key,
      code: e.code
    }, true);
  });

  window.addEventListener("keyup", (e) => {
    if (!isConnected || authModal.style.display !== "none") return;
    if (document.activeElement === pinInput || document.activeElement === mobileKeyInput || !typeTextModal.hidden) return;

    sendRealtimeInput({
      type: "key_up",
      key: e.key,
      code: e.code
    }, true);
  });

  // Touch Support for Mobile / Tablet
  let touchStartX = 0, touchStartY = 0;
  let touchMoved = false;

  canvas.addEventListener("touchstart", (e) => {
    if (!isConnected || e.touches.length !== 1) return;
    e.preventDefault();
    const touch = e.touches[0];
    touchStartX = touch.clientX;
    touchStartY = touch.clientY;
    touchMoved = false;
    const { x, y } = getNormalizedCoords(touch);
    showOptimisticCursor(touch);
    sendRealtimeInput({ type: "mouse_move", x, y });
  }, { passive: false });

  canvas.addEventListener("touchmove", (e) => {
    if (!isConnected || e.touches.length !== 1) return;
    e.preventDefault();
    touchMoved = true;
    const touch = e.touches[0];
    const { x, y } = getNormalizedCoords(touch);
    showOptimisticCursor(touch);
    sendRealtimeInput({ type: "mouse_move", x, y });
  }, { passive: false });

  canvas.addEventListener("touchend", (e) => {
    if (!isConnected) return;
    if (!touchMoved) {
      // Tap = Left Click
      const touch = e.changedTouches[0];
      const { x, y } = getNormalizedCoords(touch);
      sendRealtimeInput({ type: "mouse_click", button: "left", x, y }, true);
    }
  });

  // UI Event Handlers
  btnConnect.addEventListener("click", () => {
    authError.textContent = "";
    manualDisconnect = false;
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: "auth", pin: pinInput.value.trim() }));
    } else {
      initWebSocket();
    }
  });

  pinInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      btnConnect.click();
    }
  });

  qualitySelect.addEventListener("change", sendQualitySettings);
  fpsSelect.addEventListener("change", sendQualitySettings);
  transportSelect.addEventListener("change", applyMediaTransport);
  if (micVolumeSelect) {
    micVolumeSelect.addEventListener("change", () => {
      if (audioGainNode && audioContext) {
        audioGainNode.gain.setTargetAtTime(
          Number(micVolumeSelect.value) || 3,
          audioContext.currentTime,
          0.02
        );
      }
    });
  }

  monitorSelect.addEventListener("change", () => {
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: "set_monitor", monitor: parseInt(monitorSelect.value, 10) }));
    }
  });

  btnWin.addEventListener("click", () => {
    if (ws) ws.send(JSON.stringify({ type: "special_combo", combo: "win_d" }));
  });

  btnCAD.addEventListener("click", () => {
    if (ws) ws.send(JSON.stringify({ type: "special_combo", combo: "ctrl_alt_del" }));
  });

  btnAltTab.addEventListener("click", () => {
    if (ws) ws.send(JSON.stringify({ type: "special_combo", combo: "alt_tab" }));
  });

  const btnUpload = document.getElementById("btn-upload");
  const fileUploader = document.getElementById("file-uploader");

  if (btnPaste) {
    const TYPE_DIALOG_MARGIN = 12;
    let typeDialogOffsetX = 0;
    let typeDialogOffsetY = 0;
    let typeDialogDrag = null;
    let typeDialogAnimationFrame = 0;
    let typeDialogResize = null;
    let typeDialogResizeAnimationFrame = 0;

    const typeDialogSizeLimits = () => ({
      minWidth: Math.min(340, window.innerWidth - TYPE_DIALOG_MARGIN * 2),
      minHeight: Math.min(420, window.innerHeight - TYPE_DIALOG_MARGIN * 2),
      maxWidth: window.innerWidth - TYPE_DIALOG_MARGIN * 2,
      maxHeight: window.innerHeight - TYPE_DIALOG_MARGIN * 2
    });

    const applyTypeDialogOffset = (offsetX, offsetY) => {
      if (!typeTextDialog) return;
      const rect = typeTextDialog.getBoundingClientRect();
      const baseLeft = (window.innerWidth - rect.width) / 2;
      const baseTop = (window.innerHeight - rect.height) / 2;
      typeDialogOffsetX = Math.max(
        TYPE_DIALOG_MARGIN - baseLeft,
        Math.min(window.innerWidth - rect.width - TYPE_DIALOG_MARGIN - baseLeft, offsetX)
      );
      typeDialogOffsetY = Math.max(
        TYPE_DIALOG_MARGIN - baseTop,
        Math.min(window.innerHeight - rect.height - TYPE_DIALOG_MARGIN - baseTop, offsetY)
      );
      typeTextDialog.style.transform = `translate3d(${Math.round(typeDialogOffsetX)}px, ${Math.round(typeDialogOffsetY)}px, 0)`;
    };

    const resetTypeDialogPosition = () => {
      typeDialogOffsetX = 0;
      typeDialogOffsetY = 0;
      typeTextDialog?.style.removeProperty("transform");
    };

    const applyTypeDialogSize = (width, height, anchorLeft, anchorTop) => {
      if (!typeTextDialog) return;
      const limits = typeDialogSizeLimits();
      const nextWidth = Math.max(limits.minWidth, Math.min(limits.maxWidth, width));
      const nextHeight = Math.max(limits.minHeight, Math.min(limits.maxHeight, height));
      typeTextDialog.style.width = `${Math.round(nextWidth)}px`;
      typeTextDialog.style.height = `${Math.round(nextHeight)}px`;
      const baseLeft = (window.innerWidth - nextWidth) / 2;
      const baseTop = (window.innerHeight - nextHeight) / 2;
      applyTypeDialogOffset(anchorLeft - baseLeft, anchorTop - baseTop);
    };

    const resetTypeDialogSize = () => {
      if (!typeTextDialog) return;
      typeTextDialog.style.removeProperty("width");
      typeTextDialog.style.removeProperty("height");
      applyTypeDialogOffset(typeDialogOffsetX, typeDialogOffsetY);
    };

    const scheduleTypeDialogMove = (clientX, clientY) => {
      if (!typeDialogDrag) return;
      typeDialogDrag.clientX = clientX;
      typeDialogDrag.clientY = clientY;
      if (typeDialogAnimationFrame) return;
      typeDialogAnimationFrame = requestAnimationFrame(() => {
        typeDialogAnimationFrame = 0;
        if (!typeDialogDrag) return;
        applyTypeDialogOffset(
          typeDialogDrag.offsetX + typeDialogDrag.clientX - typeDialogDrag.startX,
          typeDialogDrag.offsetY + typeDialogDrag.clientY - typeDialogDrag.startY
        );
      });
    };

    const finishTypeDialogMove = (event) => {
      if (!typeDialogDrag || event.pointerId !== typeDialogDrag.pointerId) return;
      scheduleTypeDialogMove(event.clientX, event.clientY);
      if (typeDialogAnimationFrame) {
        cancelAnimationFrame(typeDialogAnimationFrame);
        typeDialogAnimationFrame = 0;
        applyTypeDialogOffset(
          typeDialogDrag.offsetX + event.clientX - typeDialogDrag.startX,
          typeDialogDrag.offsetY + event.clientY - typeDialogDrag.startY
        );
      }
      typeDialogDrag = null;
      typeTextDialog?.classList.remove("is-dragging");
    };

    const scheduleTypeDialogResize = (clientX, clientY) => {
      if (!typeDialogResize) return;
      typeDialogResize.clientX = clientX;
      typeDialogResize.clientY = clientY;
      if (typeDialogResizeAnimationFrame) return;
      typeDialogResizeAnimationFrame = requestAnimationFrame(() => {
        typeDialogResizeAnimationFrame = 0;
        if (!typeDialogResize) return;
        applyTypeDialogSize(
          typeDialogResize.width + typeDialogResize.clientX - typeDialogResize.startX,
          typeDialogResize.height + typeDialogResize.clientY - typeDialogResize.startY,
          typeDialogResize.left,
          typeDialogResize.top
        );
      });
    };

    const finishTypeDialogResize = (event) => {
      if (!typeDialogResize || event.pointerId !== typeDialogResize.pointerId) return;
      scheduleTypeDialogResize(event.clientX, event.clientY);
      if (typeDialogResizeAnimationFrame) {
        cancelAnimationFrame(typeDialogResizeAnimationFrame);
        typeDialogResizeAnimationFrame = 0;
        applyTypeDialogSize(
          typeDialogResize.width + event.clientX - typeDialogResize.startX,
          typeDialogResize.height + event.clientY - typeDialogResize.startY,
          typeDialogResize.left,
          typeDialogResize.top
        );
      }
      typeDialogResize = null;
      typeTextDialog?.classList.remove("is-resizing");
    };

    if (typeTextDialog && typeTextDragHandle) {
      typeTextDragHandle.addEventListener("pointerdown", (event) => {
        if (event.button !== undefined && event.button !== 0) return;
        if (event.target.closest("button, input, textarea, select, a")) return;
        event.preventDefault();
        typeDialogDrag = {
          pointerId: event.pointerId,
          startX: event.clientX,
          startY: event.clientY,
          clientX: event.clientX,
          clientY: event.clientY,
          offsetX: typeDialogOffsetX,
          offsetY: typeDialogOffsetY
        };
        typeTextDialog.classList.add("is-dragging");
        typeTextDragHandle.setPointerCapture?.(event.pointerId);
      });
      window.addEventListener("pointermove", (event) => {
        if (typeDialogDrag && event.pointerId === typeDialogDrag.pointerId) {
          scheduleTypeDialogMove(event.clientX, event.clientY);
        }
      });
      window.addEventListener("pointerup", finishTypeDialogMove);
      window.addEventListener("pointercancel", finishTypeDialogMove);
      typeTextDragHandle.addEventListener("dblclick", resetTypeDialogPosition);
      typeTextDragHandle.addEventListener("keydown", (event) => {
        if (event.key === "Home") {
          event.preventDefault();
          resetTypeDialogPosition();
          return;
        }
        if (!["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown"].includes(event.key)) return;
        event.preventDefault();
        const amount = event.shiftKey ? 40 : 10;
        applyTypeDialogOffset(
          typeDialogOffsetX + (event.key === "ArrowLeft" ? -amount : event.key === "ArrowRight" ? amount : 0),
          typeDialogOffsetY + (event.key === "ArrowUp" ? -amount : event.key === "ArrowDown" ? amount : 0)
        );
      });
      window.addEventListener("resize", () => {
        if (typeTextModal.hidden) return;
        const rect = typeTextDialog.getBoundingClientRect();
        if (typeTextDialog.style.width || typeTextDialog.style.height) {
          applyTypeDialogSize(rect.width, rect.height, rect.left, rect.top);
        } else {
          applyTypeDialogOffset(typeDialogOffsetX, typeDialogOffsetY);
        }
      });
    }

    if (typeTextDialog && typeTextResizeHandle) {
      typeTextResizeHandle.addEventListener("pointerdown", (event) => {
        if (event.button !== undefined && event.button !== 0) return;
        event.preventDefault();
        event.stopPropagation();
        const rect = typeTextDialog.getBoundingClientRect();
        typeDialogResize = {
          pointerId: event.pointerId,
          startX: event.clientX,
          startY: event.clientY,
          clientX: event.clientX,
          clientY: event.clientY,
          left: rect.left,
          top: rect.top,
          width: rect.width,
          height: rect.height
        };
        typeTextDialog.classList.add("is-resizing");
        typeTextResizeHandle.setPointerCapture?.(event.pointerId);
      });
      window.addEventListener("pointermove", (event) => {
        if (typeDialogResize && event.pointerId === typeDialogResize.pointerId) {
          scheduleTypeDialogResize(event.clientX, event.clientY);
        }
      });
      window.addEventListener("pointerup", finishTypeDialogResize);
      window.addEventListener("pointercancel", finishTypeDialogResize);
      typeTextResizeHandle.addEventListener("dblclick", resetTypeDialogSize);
      typeTextResizeHandle.addEventListener("keydown", (event) => {
        if (event.key === "Home") {
          event.preventDefault();
          resetTypeDialogSize();
          return;
        }
        if (!["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown"].includes(event.key)) return;
        event.preventDefault();
        const amount = event.shiftKey ? 80 : 20;
        const rect = typeTextDialog.getBoundingClientRect();
        applyTypeDialogSize(
          rect.width + (event.key === "ArrowLeft" ? -amount : event.key === "ArrowRight" ? amount : 0),
          rect.height + (event.key === "ArrowUp" ? -amount : event.key === "ArrowDown" ? amount : 0),
          rect.left,
          rect.top
        );
      });
    }

    const closeTypeTextDialog = () => {
      if (activeTypingRequest && ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ type: "cancel_typing" }));
      }
      typeTextModal.hidden = true;
      typeTextSend.disabled = false;
      activeTypingRequest = null;
      canvas.focus();
    };

    const submitTypeText = () => {
      const text = typeTextInput.value;
      const charsPerSecond = Number(typeSpeedInput.value);
      const codeMode = typeCodeMode.checked;
      typeTextStatus.classList.remove("is-error");
      if (!text) {
        typeTextStatus.textContent = "Enter some text first.";
        typeTextStatus.classList.add("is-error");
        typeTextInput.focus();
        return;
      }
      if (!Number.isInteger(charsPerSecond) || charsPerSecond < 1 || charsPerSecond > 1000) {
        typeTextStatus.textContent = "Set typing speed between 1 and 1,000 chars/sec.";
        typeTextStatus.classList.add("is-error");
        typeSpeedInput.focus();
        return;
      }
      if (codeMode && charsPerSecond > 120) {
        typeTextStatus.textContent = "Code-Safe Mode supports 1–120 chars/sec. Use 40 for code.";
        typeTextStatus.classList.add("is-error");
        typeSpeedInput.focus();
        return;
      }
      if (!ws || ws.readyState !== WebSocket.OPEN || !isConnected) {
        typeTextStatus.textContent = "Connect to the host before sending text.";
        typeTextStatus.classList.add("is-error");
        return;
      }
      activeTypingRequest = `${Date.now()}-${Math.random().toString(16).slice(2)}`;
      typeTextSend.disabled = true;
      typeTextStatus.textContent = "Sending to host…";
      ws.send(JSON.stringify({
        type: "type_text",
        text,
        chars_per_second: charsPerSecond,
        code_mode: codeMode,
        request_id: activeTypingRequest
      }));
    };

    btnPaste.addEventListener("click", () => {
      typeTextModal.hidden = false;
      typeTextStatus.textContent = "Ctrl/⌘ + Enter to send";
      typeTextStatus.classList.remove("is-error");
      try {
        const savedCodeMode = localStorage.getItem("app_csm");
        typeCodeMode.checked = savedCodeMode === null ? true : savedCodeMode === "true";
        const savedSpeed = Number(localStorage.getItem("app_ts"));
        if (savedCodeMode === null) {
          typeSpeedInput.value = "40";
        } else if (Number.isInteger(savedSpeed) && savedSpeed >= 1 && savedSpeed <= 1000) {
          typeSpeedInput.value = String(savedSpeed);
        }
      } catch (_) {
        // Storage may be disabled; the default remains available.
      }
      typeSpeedHint.textContent = typeCodeMode.checked
        ? "1–120 in Code-Safe Mode · 40 recommended"
        : "1–1000 · intended for ordinary prose";
      requestAnimationFrame(() => typeTextInput.focus());
    });
    typeSpeedInput.addEventListener("change", () => {
      const speed = Math.max(1, Math.min(1000, Math.round(Number(typeSpeedInput.value) || 300)));
      typeSpeedInput.value = String(speed);
      try {
        localStorage.setItem("app_ts", String(speed));
      } catch (_) {
        // Preference persistence is optional.
      }
    });
    typeCodeMode.addEventListener("change", () => {
      if (typeCodeMode.checked && Number(typeSpeedInput.value) > 120) {
        typeSpeedInput.value = "40";
      }
      typeSpeedHint.textContent = typeCodeMode.checked
        ? "1–120 in Code-Safe Mode · 40 recommended"
        : "1–1000 · intended for ordinary prose";
      try {
        localStorage.setItem("app_csm", String(typeCodeMode.checked));
        localStorage.setItem("app_ts", typeSpeedInput.value);
      } catch (_) {
        // Preference persistence is optional.
      }
    });
    typeTextSend.addEventListener("click", submitTypeText);
    typeTextCancel.addEventListener("click", closeTypeTextDialog);
    typeTextClose.addEventListener("click", closeTypeTextDialog);
    typeTextModal.addEventListener("mousedown", (event) => {
      if (event.target === typeTextModal) closeTypeTextDialog();
    });
    typeTextInput.addEventListener("keydown", (event) => {
      if (event.key === "Escape") {
        event.preventDefault();
        closeTypeTextDialog();
      } else if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
        event.preventDefault();
        submitTypeText();
      }
    });
  }

  if (btnUpload && fileUploader) {
    btnUpload.addEventListener("click", () => {
      fileUploader.click();
    });

    fileUploader.addEventListener("change", async () => {
      const file = fileUploader.files[0];
      if (!file) return;

      btnUpload.textContent = "⏳ Sending...";
      btnUpload.disabled = true;

      const formData = new FormData();
      formData.append("file", file);

      try {
        const res = await fetch("/api/upload", {
          method: "POST",
          headers: uploadToken ? { Authorization: `Bearer ${uploadToken}` } : {},
          body: formData
        });
        const data = await res.json();
        if (data.status === "ok") {
          alert(`✅ File "${data.filename}" saved to your PC's Downloads folder!`);
        } else {
          alert(`❌ Upload failed: ${data.message}`);
        }
      } catch (err) {
        alert("❌ Error uploading file: " + err.message);
      } finally {
        btnUpload.textContent = "📁 Send File";
        btnUpload.disabled = false;
        fileUploader.value = "";
      }
    });
  }

  btnFullscreen.addEventListener("click", () => {
    if (!document.fullscreenElement) {
      document.documentElement.requestFullscreen().catch(() => {});
    } else {
      document.exitFullscreen().catch(() => {});
    }
  });

  btnDisconnect.addEventListener("click", () => {
    manualDisconnect = true;
    isMicrophoneOn = false;
    stopMicrophonePlayback();
    clearTimeout(reconnectTimer);
    clearTimeout(mediaReconnectTimer);
    if (mediaWs) mediaWs.close();
    if (ws) ws.close();
    authModal.style.display = "flex";
  });

  if (btnKbd) {
    btnKbd.addEventListener("click", () => {
      mobileKeyInput.focus();
    });

    mobileKeyInput.addEventListener("input", () => {
      if (!isConnected || !mobileKeyInput.value) return;
      for (const character of mobileKeyInput.value) {
        sendRealtimeInput({ type: "key_down", key: character, code: "" }, true);
        sendRealtimeInput({ type: "key_up", key: character, code: "" }, true);
      }
      mobileKeyInput.value = "";
    });

    mobileKeyInput.addEventListener("keydown", (event) => {
      if (event.key === "Backspace" && isConnected) {
        sendRealtimeInput({ type: "key_down", key: "Backspace", code: "Backspace" }, true);
        sendRealtimeInput({ type: "key_up", key: "Backspace", code: "Backspace" }, true);
      }
    });
  }

  if (btnCam) {
    function toggleCamera() {
      isCameraOn = !isCameraOn;
      if (isCameraOn) {
        pipCam.style.display = "block";
        btnCam.classList.add("active");
        btnCam.textContent = "📹 Starting...";
      } else {
        pipCam.style.display = "none";
        pendingCameraBlob = null;
        btnCam.classList.remove("active");
        btnCam.textContent = "📹 Camera";
      }
      if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ type: "toggle_camera", enabled: isCameraOn }));
      }
    }

    btnCam.addEventListener("click", toggleCamera);
    if (pipClose) pipClose.addEventListener("click", toggleCamera);
  }

  if (btnMic) {
    btnMic.addEventListener("click", () => {
      isMicrophoneOn = !isMicrophoneOn;
      if (isMicrophoneOn) {
        if (!ensureAudioContext()) {
          isMicrophoneOn = false;
          btnMic.textContent = "🎤 Unsupported";
          return;
        }
        btnMic.classList.add("active");
        btnMic.textContent = "🎤 Starting...";
      } else {
        stopMicrophonePlayback();
        btnMic.classList.remove("active");
        btnMic.textContent = "🎤 Listen";
      }
      if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({
          type: "toggle_microphone",
          enabled: isMicrophoneOn
        }));
      }
    });
  }

  // Draggable PiP Camera Box
  let isPipDragging = false, pipStartX = 0, pipStartY = 0, pipInitialX = 0, pipInitialY = 0;
  if (pipDragHandle && pipCam) {
    pipDragHandle.addEventListener("mousedown", (e) => {
      if (e.target === pipClose) return;
      isPipDragging = true;
      pipStartX = e.clientX;
      pipStartY = e.clientY;
      pipInitialX = pipCam.offsetLeft;
      pipInitialY = pipCam.offsetTop;
      pipCam.style.transition = "none";
    });

    window.addEventListener("mousemove", (e) => {
      if (!isPipDragging) return;
      const dx = e.clientX - pipStartX;
      const dy = e.clientY - pipStartY;
      pipCam.style.right = "auto";
      pipCam.style.bottom = "auto";
      pipCam.style.left = `${pipInitialX + dx}px`;
      pipCam.style.top = `${pipInitialY + dy}px`;
    });

    window.addEventListener("mouseup", () => {
      isPipDragging = false;
    });
  }

  // Movable and resizable toolbar. Explicit viewport coordinates avoid the
  // old left + translateX interaction that made the dock jump while dragging.
  const dockMoveHandle = toolbar.querySelector(".toolbar-drag-handle");
  const dockResizeHandle = toolbar.querySelector(".toolbar-resize-handle");
  const DOCK_LAYOUT_KEY = "app_layout_v2";
  const DOCK_MARGIN = 8;
  let dockInteraction = null;
  let dockAnimationFrame = 0;

  function dockWidthLimits() {
    const available = Math.max(160, window.innerWidth - DOCK_MARGIN * 2);
    return {
      min: Math.min(320, available),
      max: available
    };
  }

  function clampDockLayout(left, top, width = toolbar.getBoundingClientRect().width) {
    const limits = dockWidthLimits();
    const clampedWidth = Math.max(limits.min, Math.min(limits.max, width));
    toolbar.classList.toggle("is-compact-width", clampedWidth < 600);
    toolbar.style.width = `${Math.round(clampedWidth)}px`;
    const height = toolbar.getBoundingClientRect().height;
    return {
      left: Math.max(DOCK_MARGIN, Math.min(window.innerWidth - clampedWidth - DOCK_MARGIN, left)),
      top: Math.max(DOCK_MARGIN, Math.min(window.innerHeight - height - DOCK_MARGIN, top)),
      width: clampedWidth
    };
  }

  function applyDockLayout(left, top, width) {
    toolbar.style.transform = "none";
    toolbar.style.right = "auto";
    toolbar.style.bottom = "auto";
    const layout = clampDockLayout(left, top, width);
    toolbar.style.left = `${Math.round(layout.left)}px`;
    toolbar.style.top = `${Math.round(layout.top)}px`;
    return layout;
  }

  function saveDockLayout() {
    const rect = toolbar.getBoundingClientRect();
    try {
      localStorage.setItem(DOCK_LAYOUT_KEY, JSON.stringify({
        left: Math.round(rect.left),
        top: Math.round(rect.top),
        width: Math.round(rect.width)
      }));
    } catch (_error) {}
  }

  function resetDockLayout() {
    toolbar.classList.remove("is-manipulating");
    toolbar.classList.remove("is-compact-width");
    for (const property of ["left", "top", "right", "bottom", "width", "transform"]) {
      toolbar.style.removeProperty(property);
    }
    try { localStorage.removeItem(DOCK_LAYOUT_KEY); } catch (_error) {}
  }

  function restoreDockLayout() {
    try {
      const saved = JSON.parse(localStorage.getItem(DOCK_LAYOUT_KEY));
      if (
        saved
        && Number.isFinite(saved.left)
        && Number.isFinite(saved.top)
        && Number.isFinite(saved.width)
      ) {
        applyDockLayout(saved.left, saved.top, saved.width);
      }
    } catch (_error) {
      resetDockLayout();
    }
  }

  function scheduleDockUpdate(clientX, clientY) {
    if (!dockInteraction) return;
    dockInteraction.clientX = clientX;
    dockInteraction.clientY = clientY;
    if (dockAnimationFrame) return;
    dockAnimationFrame = requestAnimationFrame(() => {
      dockAnimationFrame = 0;
      if (!dockInteraction) return;
      const dx = dockInteraction.clientX - dockInteraction.startX;
      const dy = dockInteraction.clientY - dockInteraction.startY;
      if (dockInteraction.type === "move") {
        applyDockLayout(
          dockInteraction.left + dx,
          dockInteraction.top + dy,
          dockInteraction.width
        );
      } else {
        applyDockLayout(
          dockInteraction.left,
          dockInteraction.top,
          dockInteraction.width + dx
        );
      }
    });
  }

  function beginDockInteraction(event, type) {
    if (event.button !== undefined && event.button !== 0) return;
    event.preventDefault();
    const rect = toolbar.getBoundingClientRect();
    applyDockLayout(rect.left, rect.top, rect.width);
    dockInteraction = {
      type,
      pointerId: event.pointerId,
      startX: event.clientX,
      startY: event.clientY,
      clientX: event.clientX,
      clientY: event.clientY,
      left: rect.left,
      top: rect.top,
      width: rect.width
    };
    toolbar.classList.add("is-manipulating");
    event.currentTarget.setPointerCapture?.(event.pointerId);
  }

  function finishDockInteraction(event) {
    if (!dockInteraction || event.pointerId !== dockInteraction.pointerId) return;
    if (event.type === "pointercancel") {
      if (dockAnimationFrame) cancelAnimationFrame(dockAnimationFrame);
      dockAnimationFrame = 0;
      dockInteraction = null;
      toolbar.classList.remove("is-manipulating");
      return;
    }
    scheduleDockUpdate(event.clientX, event.clientY);
    if (dockAnimationFrame) {
      cancelAnimationFrame(dockAnimationFrame);
      dockAnimationFrame = 0;
      const interaction = dockInteraction;
      const dx = event.clientX - interaction.startX;
      const dy = event.clientY - interaction.startY;
      if (interaction.type === "move") {
        applyDockLayout(interaction.left + dx, interaction.top + dy, interaction.width);
      } else {
        applyDockLayout(interaction.left, interaction.top, interaction.width + dx);
      }
    }
    dockInteraction = null;
    toolbar.classList.remove("is-manipulating");
    saveDockLayout();
  }

  function nudgeDock(event, type) {
    if (!["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown", "Home"].includes(event.key)) return;
    event.preventDefault();
    if (event.key === "Home") {
      resetDockLayout();
      return;
    }
    const amount = event.shiftKey ? 40 : 10;
    const rect = toolbar.getBoundingClientRect();
    if (type === "resize") {
      const direction = event.key === "ArrowLeft" || event.key === "ArrowDown" ? -1 : 1;
      applyDockLayout(rect.left, rect.top, rect.width + direction * amount);
    } else {
      const dx = event.key === "ArrowLeft" ? -amount : event.key === "ArrowRight" ? amount : 0;
      const dy = event.key === "ArrowUp" ? -amount : event.key === "ArrowDown" ? amount : 0;
      applyDockLayout(rect.left + dx, rect.top + dy, rect.width);
    }
    saveDockLayout();
  }

  if (dockMoveHandle && dockResizeHandle) {
    dockMoveHandle.addEventListener("pointerdown", (event) => beginDockInteraction(event, "move"));
    dockResizeHandle.addEventListener("pointerdown", (event) => beginDockInteraction(event, "resize"));
    window.addEventListener("pointermove", (event) => {
      if (dockInteraction && event.pointerId === dockInteraction.pointerId) {
        scheduleDockUpdate(event.clientX, event.clientY);
      }
    });
    window.addEventListener("pointerup", finishDockInteraction);
    window.addEventListener("pointercancel", finishDockInteraction);
    dockMoveHandle.addEventListener("dblclick", resetDockLayout);
    dockMoveHandle.addEventListener("keydown", (event) => nudgeDock(event, "move"));
    dockResizeHandle.addEventListener("keydown", (event) => nudgeDock(event, "resize"));
    window.addEventListener("resize", () => {
      if (toolbar.style.left) {
        const rect = toolbar.getBoundingClientRect();
        applyDockLayout(rect.left, rect.top, rect.width);
        saveDockLayout();
      }
    });
    restoreDockLayout();
  }

  // Check if PIN already saved in localStorage for quick reconnect
  const savedPin = localStorage.getItem("app_key");
  if (savedPin) {
    pinInput.value = savedPin;
  }
  pinInput.addEventListener("input", () => {
    localStorage.setItem("app_key", pinInput.value);
  });
})();
