const $ = (id) => document.getElementById(id);
const state = { config: null, devices: [], ws: null, running: false, microphoneTestRunning: false, startedAt: null, timer: null, history: [] };

function toast(message, isError = false) {
  const node = $("toast");
  node.textContent = message;
  node.className = `toast show${isError ? " error" : ""}`;
  clearTimeout(node._timer);
  node._timer = setTimeout(() => node.className = "toast", 3400);
}

async function request(url, options = {}) {
  const response = await fetch(url, { headers: { "Content-Type": "application/json" }, ...options });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.detail || `请求失败 (${response.status})`);
  return body;
}

function setValue(id, value) { const node = $(id); if (node) node.value = value ?? ""; }
function setChecked(id, value) { const node = $(id); if (node) node.checked = Boolean(value); }

async function loadConfig() {
  const config = await request("/api/config");
  state.config = config;
  setValue("region", config.region);
  setValue("workspaceId", config.workspace_id);
  setValue("model", config.model);
  setValue("mode", config.mode);
  setValue("languagePreset", config.language_preset);
  setValue("voiceId", config.voice_id);
  setChecked("microphoneDefault", config.microphone.use_default);
  setValue("chunkMs", config.audio.chunk_ms);
  setValue("silenceDbfs", config.audio.silence_dbfs);
  setValue("silenceReportMs", config.audio.silence_report_after_ms);
  setValue("vadThreshold", config.vad.threshold);
  setValue("vadSilence", config.vad.silence_duration_ms);
  setValue("queueChunks", config.queues.input_chunks);
  setChecked("remotePlaybackDefault", config.remote_playback.use_default);
  setValue("virtualOutputName", config.virtual_output.name);
  setChecked("virtualOutputEnabled", config.virtual_output.enabled);
  setChecked("showSource", config.subtitle.show_source);
  setChecked("showTranslation", config.subtitle.show_translation);
  setValue("subtitleFontSize", config.subtitle.font_size_px);
  setValue("subtitleMaxSegments", config.subtitle.max_segments);
  $("keyState").textContent = config.api_key_configured ? `已配置 · 来源：${config.api_key_source === "environment" ? "环境变量" : "配置文件"}` : "尚未配置";
  $("configPath").textContent = `配置文件：${config.config_path}`;
  $("microphoneName").dataset.selected = config.microphone.name;
  $("remotePlaybackName").dataset.selected = config.remote_playback.name;
  updateDerived();
  applySubtitleSettings();
}

function configPayload() {
  return {
    api_key: $("apiKey").value || null,
    clear_api_key: false,
    region: $("region").value,
    workspace_id: $("workspaceId").value.trim(),
    mode: $("mode").value,
    language_preset: $("languagePreset").value,
    voice_id: $("voiceId").value.trim(),
    microphone_name: $("microphoneDefault").checked ? "" : $("microphoneName").value,
    microphone_use_default: $("microphoneDefault").checked,
    remote_playback_name: $("remotePlaybackDefault").checked ? "" : $("remotePlaybackName").value,
    remote_playback_use_default: $("remotePlaybackDefault").checked,
    virtual_output_name: $("virtualOutputName").value.trim() || "CABLE Input",
    virtual_output_enabled: $("virtualOutputEnabled").checked,
    input_chunk_ms: Number($("chunkMs").value),
    silence_dbfs: Number($("silenceDbfs").value),
    silence_report_after_ms: Number($("silenceReportMs").value),
    vad_threshold: Number($("vadThreshold").value),
    vad_silence_ms: Number($("vadSilence").value),
    input_queue_chunks: Number($("queueChunks").value),
    subtitle_show_source: $("showSource").checked,
    subtitle_show_translation: $("showTranslation").checked,
    subtitle_font_size_px: Number($("subtitleFontSize").value),
    subtitle_max_segments: Number($("subtitleMaxSegments").value),
  };
}

async function saveConfig() {
  try {
    const result = await request("/api/config", { method: "PUT", body: JSON.stringify(configPayload()) });
    $("apiKey").value = "";
    state.config = result;
    $("keyState").textContent = result.api_key_configured ? `已配置 · 来源：${result.api_key_source === "environment" ? "环境变量" : "配置文件"}` : "尚未配置";
    if (result.validation_error) {
      $("cloudBadge").textContent = "配置待完善";
      $("cloudBadge").className = "section-status error";
      toast(`配置已保存，但暂不能启动：${result.validation_error}`, true);
    } else {
      $("cloudBadge").textContent = "配置有效";
      $("cloudBadge").className = "section-status ok";
      toast("配置已安全写入本地 YAML");
    }
    applySubtitleSettings();
    return !result.validation_error;
  } catch (error) { toast(error.message, true); return false; }
}

async function loadDevices() {
  $("deviceResult").textContent = "扫描中…";
  try {
    const result = await request("/api/devices");
    state.devices = result.devices;
    renderDeviceSelectors();
    renderDeviceTable();
    $("deviceResult").textContent = `已读取 ${result.devices.length} 个设备`;
    $("captureBadge").textContent = "设备已读取";
    $("captureBadge").className = "section-status ok";
  } catch (error) {
    $("deviceResult").textContent = "读取失败";
    $("captureBadge").textContent = "设备异常";
    $("captureBadge").className = "section-status error";
    toast(error.message, true);
  }
}

function renderDeviceSelectors() {
  const isWasapi = device => device.host_api.toLowerCase().endsWith("wasapi");
  const inputs = state.devices.filter(d => isWasapi(d) && d.inputs > 0 && !d.loopback);
  const outputs = state.devices.filter(d => isWasapi(d) && d.outputs > 0);
  fillSelect($("microphoneName"), inputs, state.config?.microphone.name, "选择物理麦克风");
  fillSelect($("remotePlaybackName"), outputs, state.config?.remote_playback.name, "选择会议播放设备");
  updateDefaultControls();
}

function fillSelect(select, devices, selected, placeholder) {
  select.innerHTML = "";
  const empty = document.createElement("option"); empty.value = ""; empty.textContent = placeholder; select.appendChild(empty);
  devices.forEach(device => { const option = document.createElement("option"); option.value = device.name; option.textContent = `${device.name} · ${device.sample_rate} Hz`; select.appendChild(option); });
  if (selected) select.value = selected;
}

function renderDeviceTable() {
  const tbody = $("deviceTable"); tbody.innerHTML = "";
  if (!state.devices.length) { tbody.innerHTML = '<tr><td colspan="6" class="empty-cell">未发现音频设备</td></tr>'; return; }
  state.devices.forEach(device => {
    const row = document.createElement("tr");
    [device.name, device.host_api, device.inputs, device.outputs, `${device.sample_rate} Hz`, device.loopback ? "Loopback" : (device.inputs ? "录音/物理" : "播放")].forEach(value => { const td = document.createElement("td"); td.textContent = value; row.appendChild(td); });
    tbody.appendChild(row);
  });
}

function updateDefaultControls() {
  const busy = state.running || state.microphoneTestRunning;
  $("microphoneName").disabled = $("microphoneDefault").checked || busy;
  $("remotePlaybackName").disabled = $("remotePlaybackDefault").checked || busy;
}

function updateDerived() {
  $("vadThresholdOutput").textContent = Number($("vadThreshold").value).toFixed(2);
  $("vadSilenceOutput").textContent = `${$("vadSilence").value} ms`;
  const seconds = Number($("chunkMs").value || 0) * Number($("queueChunks").value || 0) / 1000;
  $("bufferDuration").textContent = `${seconds.toFixed(1)} 秒`;
}

function applySubtitleSettings() {
  const size = Number($("subtitleFontSize").value || 28);
  $("translationLine").style.fontSize = `${size}px`;
  $("remoteTranslationLine").style.fontSize = `${size}px`;
  ["sourceLine", "remoteSourceLine"].forEach(id => $(id).style.display = $("showSource").checked ? "block" : "none");
  ["translationLine", "remoteTranslationLine"].forEach(id => $(id).style.display = $("showTranslation").checked ? "block" : "none");
  trimHistory();
}

async function startSession() {
  const valid = await saveConfig();
  if (!valid) return;
  try {
    await request("/api/session/start", { method: "POST", body: "{}" });
    setRunning(true);
    toast("双路同声传译正在启动");
  } catch (error) { setRunning(false); toast(error.message, true); }
}

async function stopSession() {
  try { await request("/api/session/stop", { method: "POST", body: "{}" }); toast("会话已停止并清空音频队列"); }
  catch (error) { toast(error.message, true); }
  finally { setRunning(false); }
}

function setRunning(running) {
  state.running = running;
  applyBusyState();
  document.querySelector(".live-indicator").classList.toggle("on", running);
  if (running && !state.startedAt) { state.startedAt = Date.now(); state.timer = setInterval(updateClock, 1000); }
  if (!running) { state.startedAt = null; clearInterval(state.timer); $("sessionClock").textContent = "00:00:00"; }
}

function setMicrophoneTestRunning(running) {
  state.microphoneTestRunning = running;
  applyBusyState();
  $("microphoneTestState").textContent = running ? "RUNNING" : "IDLE";
  $("microphoneTestState").className = `section-status ${running ? "ok" : "neutral"}`;
}

function applyBusyState() {
  const busy = state.running || state.microphoneTestRunning;
  ["startTop", "startHero"].forEach(id => $(id).disabled = busy);
  $("stopHero").disabled = !state.running;
  $("startMicrophoneTest").disabled = busy;
  $("stopMicrophoneTest").disabled = !state.microphoneTestRunning;
  $("saveTop").disabled = busy;
  document.querySelectorAll("input,select").forEach(node => { node.disabled = busy; });
  updateDefaultControls();
}

async function startMicrophoneTest() {
  const valid = await saveConfig();
  if (!valid) return;
  const audio = $("microphoneTestAudio");
  audio.pause(); audio.removeAttribute("src"); audio.hidden = true;
  $("microphoneTestSource").textContent = "等待麦克风原文…";
  $("microphoneTestTranslation").textContent = "等待翻译…";
  $("microphoneTestHint").textContent = "正在建立本地麦克风翻译会话…";
  try {
    const result = await request("/api/microphone-test/start", {
      method: "POST",
      body: JSON.stringify({ duration_seconds: Number($("microphoneTestDuration").value) }),
    });
    setMicrophoneTestRunning(true);
    $("microphoneTestHint").textContent = `正在采集：${result.microphone}。说完后点击“停止并生成音频”。`;
    toast("麦克风测试已开始，请对着物理麦克风说话");
  } catch (error) { setMicrophoneTestRunning(false); toast(error.message, true); }
}

async function stopMicrophoneTest() {
  $("stopMicrophoneTest").disabled = true;
  $("microphoneTestState").textContent = "FINISHING";
  $("microphoneTestHint").textContent = "正在结束云端会话并封装 WAV…";
  try {
    const result = await request("/api/microphone-test/stop", { method: "POST", body: "{}" });
    applyMicrophoneTestResult(result);
    toast(result.recording_url ? "翻译克隆音频已生成，可以试听" : "测试结束，但未收到克隆音频", !result.recording_url);
  } catch (error) { toast(error.message, true); await refreshMicrophoneTest(); }
}

async function refreshMicrophoneTest() {
  try { applyMicrophoneTestResult(await request("/api/microphone-test/status")); }
  catch (error) { toast(error.message, true); }
}

function applyMicrophoneTestResult(result = {}) {
  setMicrophoneTestRunning(Boolean(result.running || result.stopping));
  if (result.source) $("microphoneTestSource").textContent = result.source;
  if (result.translation) $("microphoneTestTranslation").textContent = result.translation;
  if (result.stopping) {
    $("microphoneTestState").textContent = "FINISHING";
    $("microphoneTestHint").textContent = "正在等待 session.finished…";
    return;
  }
  if (result.recording_url) {
    const audio = $("microphoneTestAudio");
    audio.src = `${result.recording_url}?v=${Date.now()}`;
    audio.hidden = false;
    $("microphoneTestState").textContent = "READY";
    $("microphoneTestState").className = "section-status ok";
    $("microphoneTestHint").textContent = `已收到 ${(Number(result.bytes_written || 0) / 1024).toFixed(1)} KB 克隆音频。`;
  } else if (!result.running && result.recording_name) {
    $("microphoneTestState").textContent = "NO AUDIO";
    $("microphoneTestState").className = "section-status error";
    $("microphoneTestHint").textContent = result.error || "未收到克隆音频，请检查说话音量、语言方向和 voice_id。";
  }
}

function updateClock() {
  if (!state.startedAt) return;
  const total = Math.floor((Date.now() - state.startedAt) / 1000);
  const h = String(Math.floor(total / 3600)).padStart(2, "0");
  const m = String(Math.floor(total % 3600 / 60)).padStart(2, "0");
  const s = String(total % 60).padStart(2, "0");
  $("sessionClock").textContent = `${h}:${m}:${s}`;
}

function connectEvents() {
  const protocol = location.protocol === "https:" ? "wss:" : "ws:";
  const ws = new WebSocket(`${protocol}//${location.host}/ws`); state.ws = ws;
  ws.onopen = () => { $("browserState").className = "connection-pill online"; $("browserState").querySelector("span").textContent = "本地服务已连接"; };
  ws.onclose = () => { $("browserState").className = "connection-pill offline"; $("browserState").querySelector("span").textContent = "本地服务已断开"; setTimeout(connectEvents, 1500); };
  ws.onmessage = event => handleEvent(JSON.parse(event.data));
}

function handleEvent(event) {
  if (event.type === "snapshot") { setRunning(event.running); applyMicrophoneTestResult(event.microphone_test || {}); Object.values(event.statuses || {}).forEach(handleStatus); Object.values(event.subtitles || {}).forEach(handleSubtitle); return; }
  if (event.type === "status") handleStatus(event);
  if (event.type === "subtitle") handleSubtitle(event);
  if (event.type === "microphone_test_status") handleMicrophoneTestStatus(event);
  if (event.type === "microphone_test_subtitle") {
    $("microphoneTestSource").textContent = event.source || "等待麦克风原文…";
    $("microphoneTestTranslation").textContent = event.translation || "等待翻译…";
  }
}

function handleMicrophoneTestStatus(event) {
  addEvent({ ...event, key: `test:${event.component}` });
  $("microphoneTestState").textContent = labelState(event.state).toUpperCase();
  if (event.component === "audio_capture" && typeof event.counters?.dbfs === "number") updateMeter(event.counters.dbfs);
  if (["finished", "no_audio"].includes(event.state)) void refreshMicrophoneTest();
}

function handleStatus(event) {
  addEvent(event);
  if (event.key === "remote:audio_capture") {
    $("remoteCaptureState").textContent = event.state;
    $("remoteCaptureDetail").textContent = event.message || (event.category === "none" ? "WASAPI 整机回环状态正常" : event.category);
  }
  if (event.key === "remote:cloud_push") {
    $("remotePushState").textContent = event.state;
    $("remotePushDetail").textContent = event.category === "none" ? "远端文本会话状态正常" : event.category;
  }
  if (event.key === "local:audio_capture") {
    $("captureState").textContent = event.state;
    $("captureDetail").textContent = event.category === "none" ? "物理麦克风状态正常" : event.category;
    $("captureSummary").textContent = labelState(event.state);
    if (typeof event.counters?.dbfs === "number") updateMeter(event.counters.dbfs);
  }
  if (event.key === "local:cloud_push") {
    $("pushState").textContent = event.state;
    $("pushDetail").textContent = event.category === "none" ? "LiveTranslate 推流状态正常" : event.category;
    $("cloudSummary").textContent = labelState(event.state);
    if (["error", "auth_failed"].includes(event.state)) setRunning(false);
  }
  if (event.key === "local:cloud_vad") {
    $("speechState").textContent = event.state;
    $("speechDetail").textContent = event.state === "speech_started" ? "服务端检测到正在说话" : "本轮语音已结束";
    $("vadSummary").textContent = event.state === "speech_started" ? "说话中" : "句尾";
  }
  if (event.key === "local:virtual_output") {
    $("outputState").textContent = event.state;
    $("outputDetail").textContent = event.message || (event.category === "none" ? "虚拟麦克风状态正常" : event.category);
  }
  if (event.key === "local:cloud_audio") {
    $("outputState").textContent = event.state;
    $("outputDetail").textContent = event.state === "received_no_output" ? "已接收克隆译音，虚拟输出未启用" : "24 kHz PCM 正在输出";
  }
  if (event.component === "latency") {
    const value = event.counters?.server_vad_to_first_ms;
    $("latencyState").textContent = typeof value === "number" ? `${value.toFixed(0)} ms` : "—";
    $("latencyDetail").textContent = `${event.channel === "remote" ? "远端字幕" : (event.state === "audio_first" ? "本地首块译音" : "本地译文")} 首响`;
  }
  if (event.key === "controller" && event.state === "stopped") setRunning(false);
}

function labelState(value) {
  const labels = { streaming:"推流中", capturing:"采集中", speech:"有语音", silent:"持续静音", connecting:"连接中", configuring:"配置中", configured:"已配置", error:"错误", stopped:"已停止", ready:"就绪" };
  return labels[value] || value;
}

function updateMeter(dbfs) {
  const percent = Math.max(0, Math.min(100, (dbfs + 60) / 60 * 100));
  $("inputMeter").style.width = `${percent}%`;
  $("dbfsValue").textContent = `${dbfs.toFixed(1)} dBFS`;
}

function handleSubtitle(event) {
  const source = (event.source_confirmed || "") + (event.source_stash || "");
  const translation = (event.translation_confirmed || "") + (event.translation_stash || "");
  const prefix = event.channel === "remote" ? "remote" : "";
  $(`${prefix ? prefix + "SourceLine" : "sourceLine"}`).textContent = source || "正在等待原文…";
  $(`${prefix ? prefix + "TranslationLine" : "translationLine"}`).textContent = translation || "正在等待翻译…";
  if (event.translation_final) {
    const id = `${event.channel}:${event.source_item_id}`;
    const existing = state.history.findIndex(item => item.id === id);
    const item = { id, channel: event.channel, source, translation, time: new Date() };
    if (existing >= 0) state.history[existing] = item;
    else state.history.push(item);
    renderHistory();
  }
}

function renderHistory() {
  trimHistory(); const root = $("subtitleHistory"); root.innerHTML = "";
  if (!state.history.length) { root.innerHTML = '<div class="history-empty">最终字幕会按语句出现在这里，并在刷新页面或停止后清空。</div>'; }
  [...state.history].reverse().forEach(item => {
    const card = document.createElement("div"); card.className = "history-item";
    const time = document.createElement("small"); time.textContent = item.time.toLocaleTimeString(); card.appendChild(time);
    const channel = document.createElement("b"); channel.className = "history-channel"; channel.textContent = item.channel === "remote" ? "对方 / 媒体声" : "本地麦克风"; card.appendChild(channel);
    if ($("showSource").checked) { const source = document.createElement("p"); source.textContent = item.source; card.appendChild(source); }
    if ($("showTranslation").checked) { const translated = document.createElement("p"); translated.className = "translated"; translated.textContent = item.translation; card.appendChild(translated); }
    root.appendChild(card);
  });
  $("historyCount").textContent = `${state.history.length} 条最终字幕`;
}

function trimHistory() { const max = Number($("subtitleMaxSegments").value || 50); if (state.history.length > max) state.history = state.history.slice(-max); }
function clearHistory() { state.history = []; renderHistory(); }

function addEvent(event) {
  const root = $("eventList"); if (root.querySelector(".console-empty")) root.innerHTML = "";
  const row = document.createElement("div"); row.className = "event-row";
  const values = [new Date().toLocaleTimeString(), event.key, event.state, event.category === "none" ? formatCounters(event.counters) : `${event.category} ${formatCounters(event.counters)}`];
  values.forEach((value, index) => { const span = document.createElement("span"); span.textContent = value || "—"; span.className = ["time","component","state", event.category === "none" ? "category" : "category error"][index]; row.appendChild(span); });
  root.prepend(row); while (root.children.length > 100) root.lastElementChild.remove();
}

function formatCounters(counters = {}) { return Object.entries(counters).map(([k,v]) => `${k}=${v}`).join(" "); }

function wireUi() {
  ["saveTop"].forEach(id => $(id).addEventListener("click", saveConfig));
  ["startTop", "startHero"].forEach(id => $(id).addEventListener("click", startSession));
  $("stopHero").addEventListener("click", stopSession);
  $("startMicrophoneTest").addEventListener("click", startMicrophoneTest);
  $("stopMicrophoneTest").addEventListener("click", stopMicrophoneTest);
  $("openOverlay").addEventListener("click", async () => {
    try { const result = await request("/api/overlay/open", { method: "POST", body: "{}" }); toast(result.already_running ? "悬浮字幕已在运行" : "已打开置顶中文字幕；拖动移位，Esc 或右键关闭"); }
    catch (error) { toast(error.message, true); }
  });
  $("closeOverlay").addEventListener("click", async () => {
    try { await request("/api/overlay/close", { method: "POST", body: "{}" }); toast("已关闭悬浮字幕"); }
    catch (error) { toast(error.message, true); }
  });
  $("probeRemote").addEventListener("click", async () => {
    const resultNode = $("probeRemoteResult");
    resultNode.textContent = "正在监测，请保持视频播放…";
    try {
      const result = await request("/api/devices/probe-remote", {
        method: "POST",
        body: JSON.stringify({
          remote_playback_name: $("remotePlaybackDefault").checked ? "" : $("remotePlaybackName").value,
          remote_playback_use_default: $("remotePlaybackDefault").checked,
          duration_ms: 2500,
          detection_dbfs: Number($("silenceDbfs").value),
        }),
      });
      resultNode.textContent = result.audio_detected
        ? `已捕获媒体声：${result.device}，峰值 ${result.peak_dbfs} dBFS`
        : `未检测到有效声音：${result.device}，峰值 ${result.peak_dbfs} dBFS。请换一个播放设备。`;
      toast(result.audio_detected ? "媒体回环捕获正常" : "当前端点没有捕获到视频声音", !result.audio_detected);
    } catch (error) { resultNode.textContent = error.message; toast(error.message, true); }
  });
  ["refreshDevices", "refreshDevicesTable"].forEach(id => $(id).addEventListener("click", loadDevices));
  $("microphoneDefault").addEventListener("change", updateDefaultControls);
  $("remotePlaybackDefault").addEventListener("change", updateDefaultControls);
  ["vadThreshold","vadSilence","chunkMs","queueChunks"].forEach(id => $(id).addEventListener("input", updateDerived));
  ["showSource","showTranslation","subtitleFontSize","subtitleMaxSegments"].forEach(id => $(id).addEventListener("change", () => { applySubtitleSettings(); renderHistory(); }));
  $("toggleSecret").addEventListener("click", () => { const input = $("apiKey"); input.type = input.type === "password" ? "text" : "password"; $("toggleSecret").textContent = input.type === "password" ? "显示" : "隐藏"; });
  $("closeNotice").addEventListener("click", () => $("notice").remove());
  $("clearHistory").addEventListener("click", clearHistory);
  $("clearEvents").addEventListener("click", () => $("eventList").innerHTML = '<div class="console-empty">等待状态事件…</div>');
  document.querySelectorAll(".nav-item").forEach(item => item.addEventListener("click", () => { document.querySelectorAll(".nav-item").forEach(x => x.classList.remove("active")); item.classList.add("active"); }));
}

async function boot() {
  wireUi();
  try { await loadConfig(); await loadDevices(); } catch (error) { toast(error.message, true); }
  connectEvents();
}

boot();
