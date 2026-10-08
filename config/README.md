# 配置文件：模板、个人参数与密钥分离

公开仓库只保存无真实账号信息的示例。启动脚本在文件缺失时从示例复制本地配置，不覆盖已有配置。所有实际配置均被 Git 忽略，包括深层目录中的 YAML、JSON、TOML、INI 等格式。

## 配置归属

| 位置 | 使用者 | 内容 / 注意事项 |
|---|---|---|
| `settings.yaml` | 正式版 Web 控制台 | 模式、语言、Workspace ID、音色 ID、设备、字幕、队列和测试参数 |
| `settings.local.yaml` | 正式版 | API Key；网页写入此文件，不回显已保存的密钥 |
| `voice-clone/voice_clone.local.yaml` | 独立声音复刻 CLI / 文件译音测试 | 独立百炼配置、样本、分页及文件译音参数；不会自动读取正式版页面配置 |
| `virtual-mic/settings.yaml` 或 `settings.local.yaml` | 独立虚拟麦克风工具 | 独立设备与测试配置，不是正式版测试实验室的配置入口 |
| `legacy/settings.yaml`、`settings.local.yaml` | 原型控制台 | 原型自己的接口、设备和会话参数 |
| `legacy/terminology/*.json` | 原型 | 本地实际术语表；启动脚本从通用示例创建 |

每套配置的文件名相近，但服务归属不同。正式版配置应优先通过 Web 保存；使用独立 CLI 时，应另外填写 CLI 的业务空间和音色测试 ID。

## 正式版参数分组

| 分组 | 主要参数 | 作用 |
|---|---|---|
| `app` | `mode`、`auto_open_overlay` | 三种模式与自动打开悬浮窗 |
| `microphone` | `translate_enabled`、`clone_audio_enabled`、`push_to_talk_*` | 麦克风原文 / 翻译、克隆输出和按住说话保护 |
| `aliyun` | `region`、`workspace_id`、`model`、超时 | 连接对应业务空间的云服务 |
| `sessions.remote/local` | 源 / 目标语言、`same_language_skip_*` | 分别设置对方和自己链路 |
| `voice` | `voice_id`、`enable_voice_clone`、`clone_frequency` | 音色与复刻策略；管理模型固定为 `qwen-voice-enrollment` |
| `audio` | 四类设备、增益、噪声门、采样格式、分块、目标语言保护 | 实际采集和推流 |
| `vad` | `threshold`、`silence_duration_ms` | 服务端语音检测和断句 |
| `queues` | `input_chunks`、`output_buffer_ms` | 最大容错容量，不是固定播放等待 |
| `reconnect` | `delays_seconds` | 恢复尝试的退避间隔 |
| `hotwords.remote/local` | 原词 → 目标词映射 | 两条链路各自的术语提示 |
| `subtitle` | 来源、原译文、字体、颜色、背景、位置 | 只控制字幕显示，不切断音频链路 |
| `test` | 信号阈值、校准音、时长、最大录音数 | 正式版测试实验室 |
| `web` / `logging` | 本机端口、日志路径与轮转 | 默认仅本机访问，运行数据位于 `data/` |

输入流固定为 16 kHz 单声道 PCM16；云端译音格式为 24 kHz 单声道 PCM16。不是所有协议字段都能任意改值，程序会校验固定模型、格式和参数范围。

## 密钥优先级与示例

正式版密钥优先级：环境变量 `DASHSCOPE_API_KEY` → 合并后的本地配置。正式版参数按“默认值 → settings.yaml → settings.local.yaml”顺序覆盖。

本地密钥文件的结构示例：

```yaml
aliyun:
  api_key: YOUR_DASHSCOPE_API_KEY
```

这里是占位符，不是可直接使用的密钥。本地 YAML 是明文，Git 忽略不等于加密。不要提交实际文件，也不要把含设备名、业务空间、音色 ID 或字幕正文的截图作为公开问题附件。

## 修改与校验

1. 停止正在运行的会话 / 测试。
2. 在对应 Web 页面修改并保存，或在服务归属正确的本地文件中修改。
3. 回到总览做启动前检查，再开始会话。修改文件不会自动重新配置已经建立的 WebSocket。
4. 若只是验证配置结构，可从根目录运行：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\manage.ps1 -Action check
```

此检查不发送真实翻译请求，不证明云账号权限、音色存在或设备路由可用。

公开文件使用精确白名单，新增文件即使叫 `*.example.yaml` 也不一定会被记录。需要新增模板时，先去掉所有真实账号 / 设备 / 术语信息，再审核 `.gitignore` 白名单。

关联说明：[根 README](../README.md)、[音频路由](../docs/音频路由使用说明.md)、[音色配置与 CLI](../docs/声音复刻使用说明.md)。
