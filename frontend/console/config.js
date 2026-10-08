const languageNames = {
  auto: "自动检测", zh: "中文", en: "English", de: "Deutsch", it: "Italiano",
  pt: "Português", es: "Español", ja: "日本語", ko: "한국어", fr: "Français",
  ru: "Русский", th: "ไทย", id: "Indonesia", ar: "العربية", cs: "Čeština",
  da: "Dansk", nl: "Nederlands", fi: "Suomi", he: "עברית", hi: "हिन्दी",
  is: "Íslenska", ms: "Melayu", no: "Norsk", fa: "فارسی", pl: "Polski",
  sv: "Svenska", tl: "Filipino", tr: "Türkçe", ur: "اردو", vi: "Tiếng Việt",
};

let dirty = false;
let runtimeRunning = false;

function languageOptions() {
  $$('[data-languages]').forEach((element) => {
    const allowAuto = element.dataset.auto === "true";
    element.innerHTML = Object.entries(languageNames)
      .filter(([key]) => allowAuto || key !== "auto")
      .map(([key, name]) => `<option value="${key}">${name} · ${key}</option>`)
      .join("");
  });
}

function injectSameLanguageHelp() {
  const languageGrid = $$(".grid.two")[0];
  if (!languageGrid || $("#same-language-help")) return;
  languageGrid.insertAdjacentHTML("afterend", `
    <article id="same-language-help" class="card" style="margin-top:18px">
      <h3>“同语言跳过”如何工作？</h3>
      <p class="hint">它不是语言过滤器。服务端先识别本句实际源语言；只有当本句源语言与目标语言相同，并且目标语言是中文或英文时，才根据下面两个开关抑制输出。</p>
      <div class="grid three">
        <div class="metric"><span>skip_text</span><b style="font-size:14px">跳过翻译文本</b><small>本应用仍请求原文 ASR；这里只控制同语言时的翻译文本输出。</small></div>
        <div class="metric"><span>skip_audio</span><b style="font-size:14px">跳过合成译音</b><small>同语言时不生成克隆音频，可避免把原语言再次送入虚拟麦克风。</small></div>
        <div class="metric"><span>生效范围</span><b style="font-size:14px">目标语言仅 zh / en</b><small>目标语言为其他语种时，阿里云官方说明该配置不生效。</small></div>
      </div>
      <div class="table-wrap" style="margin-top:14px"><table><thead><tr><th>skip_text</th><th>skip_audio</th><th>源语言恰好等于目标语言时</th></tr></thead><tbody>
        <tr><td>false</td><td>false</td><td>保留同语言文本和音频输出</td></tr>
        <tr><td>true</td><td>false</td><td>不输出翻译文本，但仍可输出同语言音频</td></tr>
        <tr><td>false</td><td>true</td><td>保留文本，不生成同语言音频；会议同传推荐</td></tr>
        <tr><td>true</td><td>true</td><td>翻译文本和音频都跳过</td></tr>
      </tbody></table></div>
      <div id="same-language-current" class="notice info" style="margin-top:14px"></div>
    </article>`);
}

function describeChannel(channel, label, textOnly) {
  const target = $(`[data-path="sessions.${channel}.target_language"]`).value;
  const skipText = $(`[data-path="sessions.${channel}.same_language_skip_text"]`).checked;
  const skipAudio = $(`[data-path="sessions.${channel}.same_language_skip_audio"]`).checked;
  if (!["zh", "en"].includes(target)) {
    return `${label}：目标语言为 ${target}，同语言跳过配置不会生效。`;
  }
  const text = skipText ? "跳过翻译文本" : "保留翻译文本";
  if (textOnly) {
    return `${label}：若检测到的本句语言也是 ${target}，将${text}；该通道本来就是 text-only，因此 skip_audio 不改变结果。`;
  }
  const audio = skipAudio ? "跳过克隆译音" : "保留克隆译音";
  return `${label}：若检测到的本句语言也是 ${target}，将${text}，并${audio}。`;
}

function updateSameLanguageHelp() {
  const node = $("#same-language-current");
  if (!node) return;
  node.innerHTML = `${esc(describeChannel("remote", "远端字幕通道", true))}<br>${esc(describeChannel("local", "本地同传通道", false))}`;
}

function setSaveState(message, type = "info") {
  const node = $("#config-save-state");
  node.textContent = message;
  node.className = `notice ${type === "bad" ? "" : "info"} config-save-state`;
  node.dataset.state = type;
}

function markDirty() {
  dirty = true;
  setSaveState("有未保存修改。切换页面前请点击“保存配置”或“保存并校验”。", "warn");
}

function updateKeyState(config) {
  $("#key-state").textContent = config.aliyun.api_key_configured
    ? `已配置（来源：${config.aliyun.api_key_source}），留空不会覆盖`
    : "尚未配置";
}

async function load() {
  languageOptions();
  injectSameLanguageHelp();
  const [config, runtime] = await Promise.all([
    api("/api/config"),
    api("/api/runtime/status"),
  ]);
  fillForm(config, $("#form"));
  runtimeRunning = Boolean(runtime.running);
  dirty = false;
  updateKeyState(config);
  updateSameLanguageHelp();
  setSaveState(
    runtimeRunning
      ? "当前翻译程序正在运行。修改可以保存，但当前会话不会热更新；停止并重新启动后生效。"
      : "当前页面显示的是已保存配置。修改后必须点击下方保存按钮。",
  );
}

async function submitConfig({ validate = false } = {}) {
  const value = collectForm($("#form"));
  value.allow_pending_restart = true;
  if ($("#clear-key").checked) value.clear_api_key = true;
  const config = await api("/api/config", { method: "PUT", body: JSON.stringify(value) });
  fillForm(config, $("#form"));
  updateKeyState(config);
  $("#clear-key").checked = false;
  dirty = false;
  updateSameLanguageHelp();

  if (validate) await api("/api/config/validate", { method: "POST" });
  const restartRequired = Boolean(config.save_status?.restart_required);
  runtimeRunning = restartRequired || runtimeRunning;
  if (restartRequired) {
    const active = config.save_status.active_components.join("、");
    setSaveState(`配置已写入文件；${active}仍使用旧配置。停止并重新启动后生效。`, "warn");
    toast(validate ? "配置已保存并校验；重启后生效" : "配置已保存；重启后生效");
  } else {
    setSaveState(validate ? "配置已保存并校验通过。" : "配置已保存，切换页面不会丢失。", "good");
    toast(validate ? "配置已保存并校验通过" : "接口、翻译和热词配置已保存");
  }
}

document.addEventListener("DOMContentLoaded", () => {
  load().catch((error) => {
    setSaveState(error.message, "bad");
    toast(error.message, true);
  });

  $("#form").addEventListener("input", markDirty);
  $("#form").addEventListener("change", (event) => {
    markDirty();
    if (event.target.matches('[data-path*="same_language"], [data-path$="target_language"]')) {
      updateSameLanguageHelp();
    }
  });

  $("#save").onclick = (event) => busy(event.currentTarget, async () => {
    try { await submitConfig(); }
    catch (error) { setSaveState(error.message, "bad"); toast(error.message, true); }
  });

  $("#check").onclick = (event) => busy(event.currentTarget, async () => {
    try { await submitConfig({ validate: true }); }
    catch (error) { setSaveState(error.message, "bad"); toast(error.message, true); }
  });

  window.addEventListener("beforeunload", (event) => {
    if (!dirty) return;
    event.preventDefault();
    event.returnValue = "";
  });
});
