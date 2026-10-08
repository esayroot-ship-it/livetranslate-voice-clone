# 声音复刻 CLI 与文件译音验证

此模块是 Qwen LiveTranslate Voice Clone 的独立音色管理客户端，正式版 Web“音色管理”也复用其中的百炼客户端实现。

## 功能与接口

| 功能 | 实现入口 | 外部依赖 |
|---|---|---|
| 创建、分页查询、删除音色 | `bailian_voice_clone.cli` / `client.py` | 阿里云 `qwen-voice-enrollment` HTTP API |
| 显示录音文案、检查配置和样本 | CLI `prompt` / `check` | 本地文件；不发送创建或翻译请求 |
| 本地文件翻译成克隆译音 | `bailian_voice_clone.translate_test` | `qwen3.5-livetranslate-flash-realtime`、有效 voice_id、FFmpeg 解码 |

固定音色的 `target_model` 必须与实时翻译模型相同，不应混用 Omni、CosyVoice 或其他模型的 ID。该 CLI 不自建本地语音克隆模型。

## 配置与样本

- 独立配置：项目根目录 `config/voice-clone/voice_clone.local.yaml`。
- 声音样本：`data/voice-clone/recordings/`，由使用者自己准备。
- 文件译音输出：`data/voice-clone/outputs/`。
- 正式版 Web 与 CLI 的配置独立；在 Web 保存密钥或选择音色，不会自动写入 CLI 配置。

## 使用

先在根目录运行 `install.bat`，填写独立配置，再从根目录执行：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\manage.ps1 -Action voice prompt
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\manage.ps1 -Action voice check
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\manage.ps1 -Action voice list
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\manage.ps1 -Action voice create
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\manage.ps1 -Action voice delete YOUR_VOICE_ID
```

`YOUR_VOICE_ID` 是占位符。创建有费用确认，删除有不可逆操作确认；不要为了批量方便而盲目加 `--yes`。

文件译音不属于 CLI 的 create/list/delete 子命令，必须按专门模块入口运行。完整配置、分页 / JSON 参数、录音规范与文件译音步骤见[声音复刻使用说明](../../docs/声音复刻使用说明.md)。
