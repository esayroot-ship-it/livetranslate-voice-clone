let testConfig = null;
let cloneWasRunning = false;
let subtitleWasRunning = false;

function eventRows(events, emptyText) {
  const rows = (events || []).slice(-14).reverse();
  if (!rows.length) return `<div class="empty">${emptyText}</div>`;
  return rows.map((item) => {
    const good = ["streaming", "capturing", "speech", "subtitle_first"].includes(item.state);
    const bad = ["error", "auth_failed", "device_lost", "network_error"].includes(item.state);
    return `<div class="event"><code>${esc(item.component)}</code><span class="tag ${bad ? "bad" : good ? "good" : ""}">${esc(item.state)}</span><span>${esc(item.message || item.category || "")}</span></div>`;
  }).join("");
}

function statusName(value, fallback) {
  return ({
    streaming: "云端推流中", connecting: "正在连接", configuring: "正在配置",
    capturing: "正在捕获", speech: "检测到语音", silent: "当前静音",
    reconnect_wait: "等待重连", stopped: "已停止", error: "错误",
  })[value] || value || fallback;
}

function applySubtitleStyle() {
  if (!testConfig) return;
  const setting = testConfig.subtitle;
  const preview = $("#subtitle-preview");
  preview.style.background = setting.background_color;
  preview.style.opacity = setting.background_opacity;
  $("#subtitle-translation").style.color = setting.font_color;
  $("#subtitle-translation").style.fontSize = `${setting.font_size_px}px`;
  $("#subtitle-source").style.display = setting.show_source ? "block" : "none";
  $("#subtitle-translation").style.display = setting.show_translation ? "block" : "none";
}

function renderSubtitleStatus(snapshot) {
  const running = Boolean(snapshot.running);
  $("#subtitle-run-state").textContent = running ? "RUNNING" : "IDLE";
  $("#subtitle-run-state").className = `tag ${running ? "good" : ""}`;
  $("#subtitle-start").disabled = running;
  $("#subtitle-stop").disabled = !running;
  const capture = snapshot.statuses?.["remote:audio_capture"] || {};
  const cloud = snapshot.statuses?.["remote:cloud_push"] || {};
  $("#subtitle-capture-state").textContent = statusName(capture.state, "等待启动");
  $("#subtitle-cloud-state").textContent = statusName(cloud.state, "等待连接");
  $("#subtitle-capture-dot").className = ["capturing", "speech", "silent"].includes(capture.state) ? "on" : capture.state === "error" ? "error" : "";
  $("#subtitle-cloud-dot").className = cloud.state === "streaming" ? "on" : ["error", "auth_failed"].includes(cloud.state) ? "error" : "";
  $("#subtitle-source").textContent = snapshot.subtitle?.source || "等待系统媒体原文…";
  $("#subtitle-translation").textContent = snapshot.subtitle?.translation || "目标语言字幕将在这里实时显示";
  const metrics = snapshot.metrics || {};
  $("#subtitle-latency").textContent = fmtMs(metrics.server_vad_to_first_subtitle_ms);
  $("#subtitle-speech").textContent = metrics.speech_detected ? "是" : running ? "等待声音" : "—";
  $("#subtitle-count").textContent = String(metrics.final_segments || 0);
  $("#subtitle-events").innerHTML = eventRows(snapshot.events, "尚无字幕测试事件");
  const history = snapshot.history || [];
  $("#subtitle-history").innerHTML = history.length ? history.slice().reverse().map((item) => `<article><small>${esc(item.source || "（无原文）")}</small><strong>${esc(item.translation || "（无译文）")}</strong></article>`).join("") : '<div class="empty">等待最终字幕…</div>';
  if (!running && subtitleWasRunning) toast("字幕效果测试已停止");
  subtitleWasRunning = running;
  applySubtitleStyle();
}

function renderCloneMetrics(metrics = {}, running = false) {
  const waiting = (value, text) => value === null || value === undefined ? (running ? text : "—") : fmtMs(value);
  $("#m-cloud").textContent = waiting(metrics.mic_to_cloud_audio_ms, "等待语音");
  $("#m-output").textContent = waiting(metrics.cloud_to_virtual_output_ms, "等待云端音频");
  $("#m-total").textContent = waiting(metrics.mic_to_virtual_output_ms, "等待写入");
  $("#m-route").textContent = waiting(metrics.virtual_output_to_cable_recording_ms, "等待回录");
  $("#m-end").textContent = waiting(metrics.mic_to_virtual_mic_ms, "等待回录");
  $("#m-detected").textContent = metrics.cable_return_detected ? "检测通过" : running ? "等待推流" : "—";
}

function renderCloneStatus(snapshot) {
  const running = Boolean(snapshot.running);
  $("#clone-run-state").textContent = running ? "RUNNING" : "IDLE";
  $("#clone-run-state").className = `tag ${running ? "good" : ""}`;
  $("#cloud-start").disabled = running;
  $("#cloud-stop").disabled = !running;
  renderCloneMetrics(snapshot.metrics || {}, running);
  $("#test-source").textContent = snapshot.subtitle?.source || "等待麦克风输入…";
  $("#test-translation").textContent = snapshot.subtitle?.translation || "目标语言译文";
  $("#test-events").innerHTML = eventRows(snapshot.events, "尚无测试事件");
  $("#test-message").textContent = running
    ? "正在实时采集：说话后先出现原文/译文，再出现克隆音频和 CABLE Output 回录指标。"
    : "测试未运行；先做环境校验和 VB-CABLE 路由测试。";
  if (!running && cloneWasRunning) {
    toast("克隆译音测试已停止，录音已封装");
    recordings().catch(() => {});
  }
  cloneWasRunning = running;
}

async function pollTests() {
  try {
    const [subtitle, clone] = await Promise.all([
      api("/api/test/subtitle/status"),
      api("/api/test/status"),
    ]);
    renderSubtitleStatus(subtitle);
    renderCloneStatus(clone);
  } catch (_) {}
}

function recordingKind(name) {
  if (name.startsWith("cloud_clone")) return "云端克隆";
  if (name.startsWith("virtual_mic")) return "对方听到";
  if (name.startsWith("route_test")) return "路由校准";
  return "测试音频";
}

async function recordings() {
  const result = await api("/api/test/recordings");
  $("#recordings").innerHTML = result.recordings.length ? result.recordings.map((item) => `
    <tr><td><span class="record-kind">${recordingKind(item.name)}</span>${esc(item.name)}</td>
    <td>${Math.ceil(item.size_bytes / 1024)} KB</td>
    <td><audio controls preload="none" src="/api/test/recordings/${encodeURIComponent(item.name)}"></audio></td>
    <td><button class="btn danger del" data-name="${esc(item.name)}">删除</button></td></tr>`).join("") : '<tr><td colspan="4" class="empty">暂无录音</td></tr>';
  $$(".del", $("#recordings")).forEach((button) => button.onclick = async () => {
    try {
      await api(`/api/test/recordings/${encodeURIComponent(button.dataset.name)}`, {method: "DELETE"});
      toast("测试音频已删除");
      await recordings();
    } catch (error) { toast(error.message, true); }
  });
}

async function loadTestConfig() {
  testConfig = await api("/api/config");
  const remote = testConfig.sessions.remote;
  $("#subtitle-direction").textContent = `${remote.source_language === "auto" ? "自动检测" : remote.source_language} → ${remote.target_language}`;
  applySubtitleStyle();
}

document.addEventListener("DOMContentLoaded", () => {
  loadTestConfig().catch((error) => toast(error.message, true));
  pollTests();
  recordings().catch((error) => toast(error.message, true));
  setInterval(pollTests, 700);

  $("#subtitle-probe").onclick = (event) => busy(event.currentTarget, async () => {
    try {
      const selected = testConfig.audio.remote_playback;
      const result = await api("/api/devices/probe-loopback", {
        method: "POST",
        body: JSON.stringify({name: selected.name, use_default: selected.use_default, duration_ms: 2500}),
      });
      $("#subtitle-probe-result").textContent = `${result.audio_detected ? "已检测到媒体声音" : "没有检测到有效声音"} · 峰值 ${result.peak_dbfs} dBFS · ${result.callback_chunks} 个音频块`;
      toast(result.audio_detected ? "媒体回环捕获正常" : "请确认视频正在播放且输出到所选物理设备", !result.audio_detected);
    } catch (error) { toast(error.message, true); }
  });
  $("#subtitle-validate").onclick = (event) => busy(event.currentTarget, async () => {
    try {
      const result = await api("/api/test/subtitle/validate", {method: "POST"});
      toast(`字幕链路可用：${result.remote_loopback} · ${result.source_language} → ${result.target_language}`);
    } catch (error) { toast(error.message, true); }
  });
  $("#subtitle-start").onclick = (event) => busy(event.currentTarget, async () => {
    try {
      await api("/api/test/subtitle/start", {method: "POST", body: JSON.stringify({duration_seconds: Number($("#subtitle-duration").value)})});
      toast("字幕效果测试已启动，请播放有声媒体");
      await pollTests();
    } catch (error) { toast(error.message, true); }
  });
  $("#subtitle-stop").onclick = (event) => busy(event.currentTarget, async () => {
    try { await api("/api/test/subtitle/stop", {method: "POST"}); await pollTests(); }
    catch (error) { toast(error.message, true); }
  });
  $("#test-overlay-open").onclick = async () => {
    try { await api("/api/overlay/open", {method: "POST"}); toast("测试悬浮窗已打开"); }
    catch (error) { toast(error.message, true); }
  };
  $("#test-overlay-close").onclick = async () => {
    try { await api("/api/overlay/close", {method: "POST"}); toast("悬浮窗已关闭"); }
    catch (error) { toast(error.message, true); }
  };

  $("#validate").onclick = (event) => busy(event.currentTarget, async () => {
    try {
      const result = await api("/api/test/validate", {method: "POST"});
      toast(`完整链路可用：${result.microphone} → ${result.virtual_playback} → ${result.virtual_recording}`);
    } catch (error) { toast(error.message, true); }
  });
  $("#route").onclick = (event) => busy(event.currentTarget, async () => {
    try {
      const result = await api("/api/test/route", {method: "POST"});
      toast(result.ok ? `VB-CABLE 路由正常 · ${result.route_latency_ms ?? "—"} ms` : "未从 CABLE Output 回录到校准音", !result.ok);
      await recordings();
    } catch (error) { toast(error.message, true); }
  });
  $("#cloud-start").onclick = (event) => busy(event.currentTarget, async () => {
    try {
      await api("/api/test/cloud/start", {method: "POST", body: JSON.stringify({duration_seconds: Number($("#duration").value)})});
      toast("实时克隆译音测试已启动，请开始说话");
      await pollTests();
    } catch (error) { toast(error.message, true); }
  });
  $("#cloud-stop").onclick = (event) => busy(event.currentTarget, async () => {
    try { const result = await api("/api/test/cloud/stop", {method: "POST"}); renderCloneMetrics(result, false); await pollTests(); await recordings(); }
    catch (error) { toast(error.message, true); }
  });
  $("#recordings-refresh").onclick = () => recordings().catch((error) => toast(error.message, true));
  $("#recordings-delete").onclick = async () => {
    if (!confirm("确定删除正式版测试目录内的全部 WAV 文件？")) return;
    try {
      const result = await api("/api/test/recordings", {method: "DELETE"});
      toast(`已删除 ${result.deleted_count} 个测试音频`);
      await recordings();
    } catch (error) { toast(error.message, true); }
  };
});
