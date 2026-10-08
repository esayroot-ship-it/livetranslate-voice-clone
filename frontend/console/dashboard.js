let mode = "subtitle_only";
let runtimeRunning = false;
let appConfig = null;
let pttTimer = null;
let pttActive = false;
let pttShortcut = "Ctrl+Space";
let recordingShortcut = false;

const latencyFields = {
  remote_capture_to_vad_ms: "latency-remote-capture-vad",
  remote_vad_to_subtitle_ms: "latency-remote-subtitle",
  local_capture_to_vad_ms: "latency-local-capture-vad",
  local_vad_to_translation_ms: "latency-local-translation",
  local_vad_to_audio_ms: "latency-local-audio",
  cloud_audio_to_cable_ms: "latency-audio-cable",
  local_vad_to_cable_ms: "latency-local-cable",
};

function microphoneCloneEnabled() {
  return mode === "full_interpretation" || (
    mode === "microphone_subtitle" && Boolean(appConfig?.microphone?.clone_audio_enabled)
  );
}

function setMode(value) {
  mode = value;
  $$(".mode-option").forEach((item) => {
    item.classList.toggle("active", item.dataset.mode === value);
    item.classList.toggle("locked", runtimeRunning);
    item.setAttribute("aria-disabled", String(runtimeRunning));
  });
  const microphoneMode = value === "microphone_subtitle";
  $("#microphone-options").classList.toggle("visible", microphoneMode);
  $("#meeting-subtitle-options").classList.toggle(
    "visible",
    value === "full_interpretation",
  );
  const explanations = {
    subtitle_only: {
      title: "远端字幕是什么？",
      text: "电脑里正在播放的会议、视频或网页声音会被识别并翻译成字幕，不会使用你的麦克风。",
    },
    microphone_subtitle: {
      title: "麦克风字幕是什么？",
      text: "你对着麦克风讲话，程序生成原文或翻译字幕；需要时还能把克隆译音送入录屏或虚拟麦克风。",
    },
    full_interpretation: {
      title: "会议同传是什么？",
      text: "对方讲话可显示字幕，你的讲话会被翻译并用克隆声音发给对方；字幕仍可只显示其中一方。",
    },
  };
  $("#mode-explanation-title").textContent = explanations[value].title;
  $("#mode-explanation-text").textContent = explanations[value].text;
  $("#microphone-translate").disabled = runtimeRunning;
  $("#microphone-clone").disabled = runtimeRunning;

  const remoteEnabled = value !== "microphone_subtitle";
  const localEnabled = value !== "subtitle_only";
  $("#ptt-panel").classList.toggle("visible", localEnabled);
  $("#ptt-enabled").disabled = runtimeRunning;
  updatePttControls();
  const cloneEnabled = microphoneCloneEnabled();
  if (!remoteEnabled) {
    $("#remote-capture").textContent = "未启用";
    $("#remote-cloud").textContent = "未启用";
  }
  if (!localEnabled) {
    $("#local-cloud").textContent = "未启用";
    $("#local-capture").textContent = "未启用";
  }
  if (!cloneEnabled) $("#virtual-output").textContent = "未启用";
  $("#local-cloud-note").textContent = cloneEnabled
    ? "WebSocket · text + audio"
    : "WebSocket · text";
}

function applyDisplayConfig() {
  if (!appConfig) return;
  const setting = appConfig.subtitle;
  const sourceMode = setting.source_mode || "remote";
  $$("[data-subtitle-source]").forEach((button) => {
    button.classList.toggle("active", button.dataset.subtitleSource === sourceMode);
    button.setAttribute(
      "aria-pressed",
      String(button.dataset.subtitleSource === sourceMode),
    );
  });
  const showRemote = sourceMode === "remote" || sourceMode === "both";
  const showLocal = sourceMode === "local" || sourceMode === "both";
  $("#remote-subtitle-panel").style.display = showRemote ? "block" : "none";
  $("#local-subtitle-panel").style.display = showLocal ? "block" : "none";
  $$(".subtitle-channel .source").forEach((element) => {
    element.style.display = setting.show_source ? "block" : "none";
  });
  $$(".subtitle-channel .channel-label").forEach((element) => {
    element.style.display = setting.show_channel_labels ? "block" : "none";
  });
  $("#remote-translation").style.display = setting.show_translation ? "block" : "none";
  const localTranslationEnabled = mode === "full_interpretation" ||
    Boolean(appConfig.microphone.translate_enabled);
  $("#local-translation").style.display =
    setting.show_translation && localTranslationEnabled ? "block" : "none";
}

async function saveMeetingSubtitleSource(sourceMode) {
  const buttons = $$('[data-subtitle-source]');
  buttons.forEach((button) => { button.disabled = true; });
  try {
    appConfig = await api("/api/config", {
      method: "PUT",
      body: JSON.stringify({
        allow_pending_restart: true,
        subtitle: { source_mode: sourceMode },
      }),
    });
    applyDisplayConfig();
    toast("会议字幕显示来源已保存；总览立即生效，悬浮窗重新打开后同步");
  } finally {
    buttons.forEach((button) => { button.disabled = false; });
  }
}

function setRunning(running) {
  runtimeRunning = Boolean(running);
  $("#start").disabled = runtimeRunning;
  $("#stop").disabled = !runtimeRunning;
  $("#validate").disabled = runtimeRunning;
  setMode(mode);
}

function updatePttControls() {
  if (!appConfig) return;
  const enabled = Boolean(appConfig.microphone.push_to_talk_enabled);
  $("#ptt-enabled").checked = enabled;
  const button = $("#ptt-hold");
  $("#ptt-shortcut-value").textContent = pttShortcut;
  $("#ptt-record-shortcut").disabled = runtimeRunning;
  button.disabled = !runtimeRunning || !enabled || mode === "subtitle_only";
  if (!pttActive) {
    button.textContent = runtimeRunning && enabled
      ? `按住说话（${pttShortcut}）`
      : "启动程序后按住说话";
  }
}

async function savePttOption() {
  const enabled = $("#ptt-enabled").checked;
  try {
    appConfig = await api("/api/config", {
      method: "PUT",
      body: JSON.stringify({ microphone: { push_to_talk_enabled: enabled } }),
    });
    updatePttControls();
    toast(enabled ? "按住说话保护已启用" : "按住说话保护已关闭");
  } catch (error) {
    $("#ptt-enabled").checked = Boolean(appConfig.microphone.push_to_talk_enabled);
    throw error;
  }
}

function shortcutFromEvent(event) {
  let key = null;
  if (event.code === "Space") key = "Space";
  else if (/^Key[A-Z]$/.test(event.code)) key = event.code.slice(3);
  else if (/^F(?:[1-9]|1[0-2])$/.test(event.code)) key = event.code;
  const modifiers = [];
  if (event.ctrlKey) modifiers.push("Ctrl");
  if (event.altKey) modifiers.push("Alt");
  if (event.shiftKey) modifiers.push("Shift");
  if (!key || modifiers.length === 0) return null;
  return [...modifiers, key].join("+");
}

function shortcutMatches(event) {
  return shortcutFromEvent(event) === pttShortcut;
}

function shortcutMainKeyMatches(event) {
  const key = pttShortcut.split("+").at(-1);
  if (key === "Space") return event.code === "Space";
  if (/^[A-Z]$/.test(key)) return event.code === `Key${key}`;
  return event.code === key;
}

async function savePttShortcut(shortcut) {
  appConfig = await api("/api/config", {
    method: "PUT",
    body: JSON.stringify({ microphone: { push_to_talk_shortcut: shortcut } }),
  });
  pttShortcut = appConfig.microphone.push_to_talk_shortcut;
  recordingShortcut = false;
  $("#ptt-record-shortcut").classList.remove("recording");
  $("#ptt-record-shortcut").textContent = "自定义快捷键";
  updatePttControls();
  toast(`按住说话快捷键已保存为 ${pttShortcut}`);
}

async function sendPttState(active) {
  await api("/api/runtime/microphone-transmit", {
    method: "POST",
    body: JSON.stringify({ active }),
  });
}

function beginPtt() {
  if (pttActive || $("#ptt-hold").disabled) return;
  pttActive = true;
  $("#ptt-hold").classList.add("transmitting");
  $("#ptt-hold").textContent = "正在传输麦克风…松开即静音";
  sendPttState(true).catch((error) => { endPtt(); toast(error.message, true); });
  pttTimer = window.setInterval(() => {
    sendPttState(true).catch(() => endPtt());
  }, 500);
}

function endPtt() {
  if (pttTimer !== null) window.clearInterval(pttTimer);
  pttTimer = null;
  const wasActive = pttActive;
  pttActive = false;
  $("#ptt-hold").classList.remove("transmitting");
  updatePttControls();
  if (wasActive && runtimeRunning) sendPttState(false).catch(() => {});
}

function stateText(value) {
  return ({
    streaming: "推流中", capturing: "捕获中", speech: "检测到语音", silent: "静音",
    connecting: "连接中", configuring: "配置中", started: "已启动", stopped: "已停止",
    playing: "输出中", underrun: "等待音频", error: "错误", auth_failed: "认证失败",
    reconnect_wait: "等待重连", draining: "正在播放剩余译音",
  })[value] || value || "待机";
}

function applyStatus(payload) {
  const map = {
    "remote:audio_capture": "remote-capture", "remote:cloud_push": "remote-cloud",
    "local:cloud_push": "local-cloud", "local:audio_capture": "local-capture",
    "local:virtual_output": "virtual-output",
  };
  const id = map[`${payload.channel || "global"}:${payload.component}`];
  if (!id) return;
  $(`#${id}`).textContent = stateText(payload.state);
  const note = $(`#${id}-note`);
  if (note && payload.message) note.textContent = payload.message;
}

function formatLatency(value) {
  if (typeof value !== "number" || !Number.isFinite(value)) return "—";
  return value < 100 ? value.toFixed(1) : Math.round(value).toString();
}

function applyLatencies(latencies = {}) {
  Object.entries(latencyFields).forEach(([key, id]) => {
    const element = $(`#${id}`);
    const value = latencies[key];
    element.textContent = formatLatency(value);
    element.classList.toggle("has-value", typeof value === "number" && Number.isFinite(value));
  });
}

function applySubtitle(subtitle) {
  if (!subtitle || !["remote", "local"].includes(subtitle.channel)) return;
  const channel = subtitle.channel;
  const source = `${subtitle.source_confirmed || ""}${subtitle.source_stash || ""}`;
  const translation = `${subtitle.translation_confirmed || ""}${subtitle.translation_stash || ""}`;
  $(`#${channel}-source`).textContent = source || (channel === "local" ? "正在识别麦克风…" : "正在识别媒体…");
  $(`#${channel}-translation`).textContent = translation || "正在翻译…";
}

function applySnapshot(snapshot) {
  setRunning(snapshot.running);
  setMode(snapshot.mode);
  Object.values(snapshot.statuses || {}).forEach(applyStatus);
  Object.values(snapshot.subtitles || {}).forEach(applySubtitle);
  applyLatencies(snapshot.latencies);
  applyDisplayConfig();
}

async function load() {
  const [snapshot, config] = await Promise.all([
    api("/api/runtime/status"),
    api("/api/config"),
  ]);
  appConfig = config;
  mode = config.app.mode;
  pttShortcut = config.microphone.push_to_talk_shortcut || "Ctrl+Space";
  $("#microphone-translate").checked = Boolean(config.microphone.translate_enabled);
  $("#microphone-clone").checked = Boolean(config.microphone.clone_audio_enabled);
  $("#ptt-enabled").checked = Boolean(config.microphone.push_to_talk_enabled);
  applySnapshot(snapshot);
}

async function saveMicrophoneOptions() {
  const previous = { ...appConfig.microphone };
  let translateEnabled = $("#microphone-translate").checked;
  let cloneEnabled = $("#microphone-clone").checked;
  if (cloneEnabled && !translateEnabled) {
    translateEnabled = true;
    $("#microphone-translate").checked = true;
  }
  if (!translateEnabled) {
    cloneEnabled = false;
    $("#microphone-clone").checked = false;
  }
  try {
    appConfig = await api("/api/config", {
      method: "PUT",
      body: JSON.stringify({
        microphone: {
          translate_enabled: translateEnabled,
          clone_audio_enabled: cloneEnabled,
        },
      }),
    });
  } catch (error) {
    $("#microphone-translate").checked = Boolean(previous.translate_enabled);
    $("#microphone-clone").checked = Boolean(previous.clone_audio_enabled);
    throw error;
  }
  setMode(mode);
  applyDisplayConfig();
  toast("麦克风字幕输出设置已保存");
}

document.addEventListener("DOMContentLoaded", () => {
  load().catch((error) => toast(error.message, true));

  $$(".mode-option").forEach((item) => {
    item.onclick = async () => {
      if (runtimeRunning) {
        toast("请先停止程序，再切换运行模式", true);
        return;
      }
      try {
        const body = { app: { mode: item.dataset.mode } };
        if (item.dataset.mode === "full_interpretation") {
          body.microphone = { translate_enabled: true, clone_audio_enabled: true };
        }
        appConfig = await api("/api/config", { method: "PUT", body: JSON.stringify(body) });
        $("#microphone-translate").checked = Boolean(appConfig.microphone.translate_enabled);
        $("#microphone-clone").checked = Boolean(appConfig.microphone.clone_audio_enabled);
        setMode(item.dataset.mode);
        applyDisplayConfig();
        toast("运行模式已保存");
      } catch (error) { toast(error.message, true); }
    };
  });

  $("#microphone-translate").onchange = () => {
    if (!$("#microphone-translate").checked) $("#microphone-clone").checked = false;
    saveMicrophoneOptions().catch((error) => toast(error.message, true));
  };
  $("#microphone-clone").onchange = () => {
    if ($("#microphone-clone").checked) $("#microphone-translate").checked = true;
    saveMicrophoneOptions().catch((error) => toast(error.message, true));
  };
  $("#ptt-enabled").onchange = () => savePttOption().catch((error) => toast(error.message, true));
  $("#ptt-record-shortcut").onclick = () => {
    recordingShortcut = true;
    $("#ptt-record-shortcut").classList.add("recording");
    $("#ptt-record-shortcut").textContent = "请按组合键…";
  };
  $("#ptt-hold").onpointerdown = (event) => {
    event.currentTarget.setPointerCapture(event.pointerId);
    beginPtt();
  };
  $("#ptt-hold").onpointerup = endPtt;
  $("#ptt-hold").onpointercancel = endPtt;
  $("#ptt-hold").onlostpointercapture = endPtt;
  window.addEventListener("blur", endPtt);
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) endPtt();
  });
  document.addEventListener("keydown", (event) => {
    if (recordingShortcut) {
      event.preventDefault();
      if (event.code === "Escape") {
        recordingShortcut = false;
        $("#ptt-record-shortcut").classList.remove("recording");
        $("#ptt-record-shortcut").textContent = "自定义快捷键";
        return;
      }
      const shortcut = shortcutFromEvent(event);
      if (shortcut) savePttShortcut(shortcut).catch((error) => toast(error.message, true));
      return;
    }
    if (shortcutMatches(event) && !event.repeat) {
      event.preventDefault();
      beginPtt();
    }
  });
  document.addEventListener("keyup", (event) => {
    if (shortcutMainKeyMatches(event)) endPtt();
  });
  $$("[data-subtitle-source]").forEach((button) => {
    button.onclick = () => saveMeetingSubtitleSource(button.dataset.subtitleSource)
      .catch((error) => toast(error.message, true));
  });

  $("#start").onclick = (event) => busy(event.currentTarget, async () => {
    try {
      const result = await api("/api/runtime/start", { method: "POST" });
      toast(`已启动：${result.mode}`);
      await load();
    } catch (error) { toast(error.message, true); }
  });
  $("#stop").onclick = (event) => busy(event.currentTarget, async () => {
    try {
      endPtt();
      await api("/api/runtime/stop", { method: "POST" });
      toast("程序已停止");
      await load();
    }
    catch (error) { toast(error.message, true); }
  });
  $("#validate").onclick = (event) => busy(event.currentTarget, async () => {
    try {
      await api("/api/config/validate", { method: "POST" });
      const result = await api("/api/devices/validate", { method: "POST" });
      toast(`检查通过：${Object.values(result.resolved).join(" / ")}`);
    } catch (error) { toast(error.message, true); }
  });
  $("#overlay-open").onclick = async () => {
    try { await api("/api/overlay/open", { method: "POST" }); toast("悬浮窗已打开"); }
    catch (error) { toast(error.message, true); }
  };
  $("#overlay-close").onclick = async () => {
    try { await api("/api/overlay/close", { method: "POST" }); toast("悬浮窗已关闭"); }
    catch (error) { toast(error.message, true); }
  };

  const scheme = location.protocol === "https:" ? "wss" : "ws";
  const socket = new WebSocket(`${scheme}://${location.host}/ws`);
  socket.onmessage = (event) => {
    const payload = JSON.parse(event.data);
    if (payload.type === "snapshot") applySnapshot(payload);
    else if (payload.type === "status") applyStatus(payload);
    else if (payload.type === "latency_snapshot") applyLatencies(payload.latencies);
    else if (payload.type === "subtitle") applySubtitle(payload);
  };
});
