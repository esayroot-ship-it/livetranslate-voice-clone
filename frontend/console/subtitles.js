let subtitleConfig = null;

function selectedSourceMode() {
  return $("input[name='source-mode']:checked")?.value || "remote";
}

function syncSourceMode(value) {
  const mode = value || "remote";
  $("#source-mode-value").value = mode;
  const radio = $(`input[name='source-mode'][value='${mode}']`);
  if (radio) radio.checked = true;
}

function luminance(hex) {
  const values = hex.match(/[0-9a-f]{2}/gi).map((value) => parseInt(value, 16) / 255);
  const linear = values.map((value) => value <= 0.03928
    ? value / 12.92
    : ((value + 0.055) / 1.055) ** 2.4);
  return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2];
}

function contrastRatio(foreground, background) {
  const a = luminance(foreground);
  const b = luminance(background);
  return (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05);
}

function showContrast(id, foreground, background) {
  const ratio = contrastRatio(foreground, background);
  const node = $(`#${id}`);
  node.textContent = `${ratio.toFixed(2)} : 1`;
  node.className = ratio >= 4.5 ? "good" : "warn";
}

function preview() {
  const box = $("#preview");
  const sourceMode = selectedSourceMode();
  $("#source-mode-value").value = sourceMode;
  box.style.background = $("#bg-color").value;
  box.style.opacity = $("#opacity").value;
  box.style.color = $("#font-color").value;
  box.style.width = `${$("#window-width").value}%`;
  box.style.padding = `${$("#vertical-padding").value}px ${$("#horizontal-padding").value}px`;
  box.classList.toggle(
    "compact",
    $('[data-path="subtitle.compact_background"]').checked,
  );
  $("#opacity-value").textContent = `${Math.round(Number($("#opacity").value) * 100)}%`;
  $("#width-value").textContent = `${$("#window-width").value}%`;
  const showSource = $('[data-path="subtitle.show_source"]').checked;
  const showTranslation = $('[data-path="subtitle.show_translation"]').checked;
  const showLabels = $('[data-path="subtitle.show_channel_labels"]').checked;
  const showRemote = sourceMode === "remote" || sourceMode === "both";
  const showLocal = sourceMode === "local" || sourceMode === "both";
  $("#preview-remote").style.display = showRemote ? "block" : "none";
  $("#preview-local").style.display = showLocal ? "block" : "none";
  $$(".preview-channel .source").forEach((node) => {
    node.style.display = showSource ? "block" : "none";
    node.style.fontSize = `${$("#source-font-size").value}px`;
    node.style.color = $("#source-font-color").value;
  });
  $$(".preview-channel .translation").forEach((node) => {
    node.style.display = showTranslation ? "block" : "none";
    node.style.fontSize = `${$("#font-size").value}px`;
    node.style.color = $("#font-color").value;
  });
  $$(".preview-label").forEach((node) => {
    node.style.display = showLabels ? "block" : "none";
    node.style.color = $("#label-color").value;
  });
  showContrast("translation-contrast", $("#font-color").value, $("#bg-color").value);
  showContrast("source-contrast", $("#source-font-color").value, $("#bg-color").value);
  showContrast("label-contrast", $("#label-color").value, $("#bg-color").value);
  const localTranslation = Boolean(subtitleConfig?.microphone?.translate_enabled);
  if (!localTranslation) $("#preview-local .translation").style.display = "none";
  const explanation = {
    remote: "当前只显示系统媒体或会议对方的字幕。",
    local: localTranslation
      ? "当前只显示本地麦克风原文和翻译，适合演讲、录屏和视频录制。"
      : "当前只显示本地麦克风识别原文；麦克风翻译已关闭。",
    both: localTranslation
      ? "当前同时显示远端和麦克风的原文/译文。"
      : "当前显示远端字幕和麦克风原文；麦克风翻译已关闭。",
  };
  $("#source-explanation").textContent = explanation[sourceMode];
}

async function load() {
  subtitleConfig = await api("/api/config");
  fillForm(subtitleConfig, $("#form"));
  syncSourceMode(subtitleConfig.subtitle.source_mode);
  preview();
}

async function saveSubtitleSettings() {
  $("#source-mode-value").value = selectedSourceMode();
  const value = collectForm($("#form"));
  value.allow_pending_restart = true;
  subtitleConfig = await api("/api/config", {
    method: "PUT",
    body: JSON.stringify(value),
  });
  preview();
  toast("字幕来源和显示设置已保存");
}

document.addEventListener("DOMContentLoaded", () => {
  load().catch((error) => toast(error.message, true));
  $$("#form input,#form select").forEach((element) => {
    element.oninput = preview;
    element.onchange = preview;
  });
  $("#save").onclick = (event) => busy(event.currentTarget, async () => {
    try { await saveSubtitleSettings(); }
    catch (error) { toast(error.message, true); }
  });
  $("#open").onclick = async () => {
    try {
      await saveSubtitleSettings();
      await api("/api/overlay/close", { method: "POST" });
      await api("/api/overlay/open", { method: "POST" });
      toast("字幕设置已保存，悬浮窗已按新来源打开");
    } catch (error) { toast(error.message, true); }
  };
  $("#close").onclick = async () => {
    try { await api("/api/overlay/close", { method: "POST" }); toast("悬浮窗已关闭"); }
    catch (error) { toast(error.message, true); }
  };
});
