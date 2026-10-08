let config;
let suggestedGateDbfs = null;

const presets = {
  clear: {
    label: "清晰优先",
    values: {
      "audio.chunk_ms": 80,
      "audio.microphone_gain_db": 0,
      "audio.microphone_noise_gate_enabled": false,
      "audio.microphone_noise_gate_dbfs": -52,
      "audio.microphone_noise_gate_hold_ms": 300,
      "audio.target_language_guard_enabled": true,
      "audio.silence_dbfs": -48,
      "audio.silence_report_after_ms": 2500,
      "vad.threshold": 0.2,
      "vad.silence_duration_ms": 550,
      "queues.input_chunks": 30,
      "queues.output_buffer_ms": 5000,
    },
  },
  balanced: {
    label: "平衡模式",
    values: {
      "audio.chunk_ms": 60,
      "audio.microphone_gain_db": 0,
      "audio.microphone_noise_gate_enabled": false,
      "audio.microphone_noise_gate_dbfs": -52,
      "audio.microphone_noise_gate_hold_ms": 300,
      "audio.target_language_guard_enabled": true,
      "audio.silence_dbfs": -48,
      "audio.silence_report_after_ms": 2200,
      "vad.threshold": 0.2,
      "vad.silence_duration_ms": 500,
      "queues.input_chunks": 35,
      "queues.output_buffer_ms": 5000,
    },
  },
  low_latency: {
    label: "低延迟",
    values: {
      "audio.chunk_ms": 50,
      "audio.microphone_gain_db": 0,
      "audio.microphone_noise_gate_enabled": false,
      "audio.microphone_noise_gate_dbfs": -52,
      "audio.microphone_noise_gate_hold_ms": 300,
      "audio.target_language_guard_enabled": true,
      "audio.silence_dbfs": -48,
      "audio.silence_report_after_ms": 2000,
      "vad.threshold": 0.2,
      "vad.silence_duration_ms": 400,
      "queues.input_chunks": 36,
      "queues.output_buffer_ms": 4000,
    },
  },
};

function addOptions(select, items, current, filter) {
  const names = [...new Set(items.filter(filter).map((item) => item.name))];
  if (current && !names.includes(current)) names.unshift(current);
  select.innerHTML = '<option value="">请选择设备</option>' +
    names.map((name) => `<option value="${esc(name)}">${esc(name)}</option>`).join("");
  select.value = current || "";
}

async function loadDevices({ showToast = true } = {}) {
  config = await api("/api/config");
  const result = await api("/api/devices");
  const devices = result.devices;
  addOptions($("#remote-device"), devices, config.audio.remote_playback.name,
    (item) => item.loopback || item.outputs > 0);
  addOptions($("#mic-device"), devices, config.audio.microphone.name,
    (item) => item.inputs > 0 && !item.loopback);
  addOptions($("#cable-input"), devices, config.audio.virtual_output.name,
    (item) => item.outputs > 0);
  addOptions($("#cable-output"), devices, config.audio.virtual_recording.name,
    (item) => item.inputs > 0);
  fillForm(config, $("#form"));
  updateLanguageRouteCheck();
  updateParameterSummary();
  if (showToast) toast(`已枚举 ${devices.length} 个音频端点`);
}

function updateLanguageRouteCheck() {
  const local = config.sessions.local;
  const routeOk = local.source_language === "zh" && local.target_language === "en";
  const route = $("#local-language-route");
  route.textContent = `${local.source_language} → ${local.target_language}${routeOk ? " · 正确" : " · 请修正"}`;
  route.classList.toggle("bad", !routeOk);
  const guardEnabled = field("audio.target_language_guard_enabled").checked;
  const guard = $("#language-guard-state");
  guard.textContent = guardEnabled ? "已启用" : "未启用";
  guard.classList.toggle("bad", !guardEnabled);
}

function field(path) {
  return $(`[data-path="${path}"]`, $("#form"));
}

function fieldValue(path) {
  const element = field(path);
  return element.type === "checkbox" ? element.checked : Number(element.value);
}

function setFieldValue(path, value) {
  const element = field(path);
  if (!element) return;
  if (element.type === "checkbox") element.checked = Boolean(value);
  else element.value = String(value);
}

function matchesPreset(profile) {
  return Object.entries(profile.values).every(([path, expected]) => {
    const actual = fieldValue(path);
    return typeof expected === "boolean" ? actual === expected : Number(actual) === expected;
  });
}

function updateProfileState() {
  const active = Object.entries(presets).find(([, profile]) => matchesPreset(profile));
  $("#active-profile").textContent = active ? active[1].label : "自定义配置";
  $$("[data-profile-card]").forEach((card) => {
    const selected = Boolean(active) && card.dataset.profileCard === active[0];
    card.classList.toggle("active", selected);
    const button = $(".preset-apply", card);
    button.classList.toggle("selected", selected);
    button.disabled = selected;
    button.textContent = selected ? "✓ 当前配置" : "应用并保存";
  });
}

function updateParameterSummary() {
  const chunk = fieldValue("audio.chunk_ms");
  const inputChunks = fieldValue("queues.input_chunks");
  const vadSilence = fieldValue("vad.silence_duration_ms");
  const outputBuffer = fieldValue("queues.output_buffer_ms");
  $("#summary-chunk").textContent = `${chunk} ms`;
  $("#summary-input-queue").textContent = `${chunk * inputChunks} ms`;
  $("#summary-vad").textContent = `${vadSilence} ms`;
  $("#summary-output-buffer").textContent = `${outputBuffer} ms`;

  const warnings = [];
  if (chunk * inputChunks > 3000) warnings.push("输入容错队列超过 3 秒");
  if (outputBuffer > 5000) warnings.push("输出积压容量较大");
  if (fieldValue("audio.microphone_gain_db") > 6) warnings.push("数字增益较高，可能削波");
  if (fieldValue("vad.threshold") > 0.5) warnings.push("VAD 阈值较高，可能漏掉轻声");
  const warning = $("#parameter-warning");
  warning.textContent = warnings.length ? warnings.join("；") : "参数处于常用范围；最终效果请用测试实验室实测。";
  warning.classList.toggle("warn", warnings.length > 0);
  updateProfileState();
  if (config) updateLanguageRouteCheck();
}

async function saveCurrent(message = "音频传输参数已保存") {
  config = await saveForm($("#form"));
  updateParameterSummary();
  toast(message);
  return config;
}

async function restoreSavedValues() {
  config = await api("/api/config");
  fillForm(config, $("#form"));
  updateParameterSummary();
}

async function applyPreset(name) {
  const profile = presets[name];
  Object.entries(profile.values).forEach(([path, value]) => setFieldValue(path, value));
  updateParameterSummary();
  await saveCurrent(`已应用并保存“${profile.label}”`);
}

function showMicCalibration(result) {
  $("#mic-noise").textContent = `${result.noise_floor_dbfs} dBFS`;
  $("#mic-peak").textContent = `${result.speech_peak_dbfs} dBFS`;
  $("#mic-snr").textContent = `${result.estimated_snr_db} dB`;
  const hasSuggestion = typeof result.recommended_gate_dbfs === "number";
  $("#mic-gate-suggest").textContent = hasSuggestion
    ? `${result.recommended_gate_dbfs} dBFS`
    : "不建议启用";
  $("#mic-quality").textContent = ({ good: "良好", marginal: "一般", poor: "不足" })[
    result.calibration_quality
  ] || "未知";
  suggestedGateDbfs = hasSuggestion ? result.recommended_gate_dbfs : null;
  $("#use-gate-suggest").disabled = !hasSuggestion;
}

document.addEventListener("DOMContentLoaded", () => {
  loadDevices().catch((error) => toast(error.message, true));

  $("#form").addEventListener("input", updateParameterSummary);

  $("#refresh").onclick = (event) => busy(event.currentTarget, async () => {
    try { await loadDevices(); } catch (error) { toast(error.message, true); }
  });

  $("#reload").onclick = (event) => busy(event.currentTarget, async () => {
    try { await restoreSavedValues(); toast("已恢复当前保存值"); }
    catch (error) { toast(error.message, true); }
  });

  $("#save").onclick = (event) => busy(event.currentTarget, async () => {
    try { await saveCurrent(); } catch (error) { toast(error.message, true); }
  });

  $("#validate").onclick = (event) => busy(event.currentTarget, async () => {
    try {
      await saveCurrent("配置已保存，正在解析设备");
      const result = await api("/api/devices/validate", { method: "POST" });
      toast(`设备解析通过：${Object.values(result.resolved).join(" / ")}`);
    } catch (error) { toast(error.message, true); }
  });

  $$(".preset-apply").forEach((button) => {
    button.onclick = async (event) => {
      await busy(event.currentTarget, async () => {
        try {
          await applyPreset(event.currentTarget.dataset.preset);
        } catch (error) {
          toast(error.message, true);
          await restoreSavedValues();
        }
      });
      updateProfileState();
    };
  });

  $("#probe").onclick = (event) => busy(event.currentTarget, async () => {
    try {
      const result = await api("/api/devices/probe-loopback", {
        method: "POST",
        body: JSON.stringify({
          name: $("#remote-device").value,
          use_default: field("audio.remote_playback.use_default").checked,
          duration_ms: 2500,
        }),
      });
      $("#probe-result").textContent =
        `${result.audio_detected ? "检测到声音" : "未检测到有效声音"} · 峰值 ${result.peak_dbfs} dBFS · ${result.callback_chunks} 个回调块`;
      toast(result.audio_detected ? "系统媒体捕获正常" : "请播放视频并检查播放设备", !result.audio_detected);
    } catch (error) { toast(error.message, true); }
  });

  $("#probe-mic").onclick = (event) => busy(event.currentTarget, async () => {
    try {
      $("#mic-probe-result").classList.add("measuring");
      const result = await api("/api/devices/probe-microphone", {
        method: "POST",
        body: JSON.stringify({
          name: $("#mic-device").value,
          use_default: field("audio.microphone.use_default").checked,
          duration_ms: 4000,
        }),
      });
      showMicCalibration(result);
      toast(
        result.recommendation || (result.audio_detected
          ? "校准完成"
          : "未检测到清晰讲话，请检查麦克风或输入音量"),
        !result.audio_detected || result.calibration_quality === "poor",
      );
    } catch (error) {
      toast(error.message, true);
    } finally {
      $("#mic-probe-result").classList.remove("measuring");
    }
  });

  $("#use-gate-suggest").onclick = () => {
    if (suggestedGateDbfs === null) return;
    setFieldValue("audio.microphone_noise_gate_enabled", true);
    setFieldValue("audio.microphone_noise_gate_dbfs", suggestedGateDbfs);
    updateParameterSummary();
    toast("已填入建议阈值；点击保存后生效");
  };
});
