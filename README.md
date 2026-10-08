# Qwen LiveTranslate Voice Clone｜实时翻译、同声传译与音色克隆

基于阿里云百炼 **Qwen3.5 LiveTranslate** 的 Windows 实时语音翻译工具：把会议同声传译、系统音频实时翻译、麦克风双语字幕、声音复刻和克隆译音虚拟麦克风推流，集中到一个本地 Web 控制台中。

适用于中英文会议沟通、外语视频字幕、独立演讲、OBS 录屏和跨语言内容制作。既可以只看本地字幕，也可以让会议对方听到保留指定音色的翻译语音。

**Qwen LiveTranslate Voice Clone** is a Windows real-time speech translation and simultaneous interpretation client powered by Alibaba Cloud Model Studio. It provides WASAPI system-audio capture, live bilingual subtitles, microphone translation, voice cloning, and translated-speech routing through a VB-CABLE virtual microphone, with a local Web UI and audio test lab.

> 这是云模型的本地客户端，不是离线翻译引擎。真实识别、翻译和音色管理需要阿里云百炼 API Key、Workspace ID、对应模型权限及可用额度。把译音送进会议软件需要另外安装虚拟音频驱动；项目不会自动安装驱动，也不附带密钥、私人音色或录音。

## 目录导航

- [三种运行模式](#三种运行模式)
- [环境与外部依赖](#环境与外部依赖)
- [快速开始](#快速开始)
- [Web 控制台功能](#web-控制台功能)
- [会议软件与 OBS 的音频路由](#会议软件与-obs-的音频路由)
- [翻译、字幕与麦克风参数](#翻译字幕与麦克风参数)
- [声音复刻与音色管理](#声音复刻与音色管理)
- [功能模块与项目结构](#功能模块与项目结构)
- [启动、停止与命令行](#启动停止与命令行)
- [测试与延迟评估](#测试与延迟评估)
- [隐私与数据保存](#隐私与数据保存)
- [常见问题](#常见问题)

## 三种运行模式

| 模式 | 输入与处理 | 输出 | 适用场景 |
|---|---|---|---|
| 远端字幕 | 捕获选定物理播放设备上的系统媒体音，识别并翻译 | 本地页面字幕和悬浮字幕，不使用本地麦克风 | 看外语视频、听会议、只需要翻译字幕 |
| 麦克风字幕 | 仅采集物理麦克风；可开关翻译，也可同时启用克隆译音 | 识别原文 / 翻译字幕；启用译音后可写入虚拟麦克风 | 独立演讲、视频录制、直播字幕 |
| 会议同传 | 两条独立 WebSocket：对方音频生成字幕；自己的麦克风翻译并生成克隆译音 | 对方 / 自己 / 双方的字幕，以及本地克隆译音推流 | 中英文双向会议、腾讯会议、Teams 等设备路由场景 |

会议同传默认方向是：对方声音 → 中文字幕；自己中文 → 英文字幕和克隆英文译音。可在会话开始前调整双方的源语言和目标语言。

字幕来源与音频链路是两件事：在会议同传中选择“仅显示对方”或“仅显示自己”，只过滤字幕显示，不会停止另一方的采集或译音输出。想只处理自己的声音，应改用“麦克风字幕”模式。

关闭麦克风翻译后，需要明确设置麦克风源语言，并启用原文显示；识别仍调用云端服务，并非本地离线 ASR。启用克隆声音会要求同时启用翻译。

## 环境与外部依赖

### Windows 与 Python

| 项目 | 要求 / 用途 |
|---|---|
| 操作系统 | Windows 11 x64；目标使用环境为 24H2 / 25H2，不承诺 Linux、macOS 或 ARM 的音频链路兼容性 |
| Python | Python 3.11–3.13 x64，建议使用 3.11；安装时确保 `python` 可从终端调用 |
| 浏览器 | 本机控制台建议用 Chrome 或 Edge；无需单独部署前端服务或安装 Node.js |
| 音频硬件 | 物理麦克风和物理耳机；系统媒体捕获使用 WASAPI 播放端点回环 |
| Python 依赖 | FastAPI、Uvicorn、`websockets`、PyAudioWPatch、NumPy、SoXR、PyYAML、Requests；由 `install.bat` 安装 |
| 悬浮字幕 | 使用 Python Tkinter；如果定制 Python 未包含 Tk，需要补齐 Tcl/Tk |
| 文件译音测试 | `voice` 可选依赖包含 `imageio-ffmpeg`，统一安装脚本会一并安装 |

这是源码运行项目，不是内置 Python、驱动和云服务的独立安装包。下载仓库后仍需完成依赖安装与个人配置。

### 阿里云百炼 / DashScope 模型依赖

| 能力 | 当前使用的模型或接口 | 说明 |
|---|---|---|
| 实时翻译与译音 | `qwen3.5-livetranslate-flash-realtime` | WebSocket 流式会话；当前代码按该模型协议实现 |
| 输入原文识别 | `qwen3-asr-flash-realtime` | 作为 LiveTranslate 会话的 `input_audio_transcription.model`，不是另外启动第三条 ASR 连接 |
| 创建、查询、删除音色 | `qwen-voice-enrollment` | HTTP 音色管理接口；创建音色的 `target_model` 固定为本项目的 LiveTranslate 模型 |

需要准备同一区域、同一业务空间可用的 API Key、Workspace ID、模型访问权限和测试额度。程序配置支持北京 `cn-beijing` 与新加坡 `ap-southeast-1`，实际权限以账号控制台为准；不要混用不同区域或不同目标模型创建的音色。

模型名不是可以随意替换的标签：`qwen3.5-omni-plus-realtime` 以及其他版本的 LiveTranslate，不保证与本项目的参数和事件适配兼容。官方文档可能同时介绍多个版本，配置时应查看 Qwen3.5 对应章节。[LiveTranslate 官方说明](https://help.aliyun.com/en/model-studio/qwen3-5-livetranslate-flash-realtime)

真实翻译和音色创建可能产生云端费用。并发连接数、速率限制、价格与额度由阿里云账号和服务规则决定，本项目不提供免费额度或固定成本承诺。

### 虚拟音频设备驱动依赖

仅看远端字幕，或只看麦克风字幕，不需要 VB-CABLE。需要把克隆译音作为会议麦克风、OBS 音频输入，或执行虚拟麦克风回录测试时，应先安装 [VB-Audio VB-CABLE](https://vb-audio.com/Cable/)。

按官方说明解压安装包，以管理员身份运行安装程序，并在安装后重启 Windows。驱动不属于 Python 依赖，运行 `install.bat` 不会安装它。[VB-CABLE 安装与端点说明](https://vb-audio.com/Cable/)

| 端点 | Windows 设备类型 | 应由谁使用 |
|---|---|---|
| `CABLE Input` | 播放设备 | 本项目把克隆译音写入这里 |
| `CABLE Output` | 录音设备 / 虚拟麦克风 | 会议软件、OBS 或回录测试从这里读取声音 |

本应用不会替换物理麦克风驱动，也不会自动改变所有软件的默认麦克风。请在目标软件中主动选择 `CABLE Output`。完整步骤见[音频路由使用说明](docs/音频路由使用说明.md)。

## 快速开始

### 1. 下载并安装依赖

在 PowerShell 中执行：

```powershell
git clone https://github.com/esayroot-ship-it/qwen-livetranslate-voice-clone.git
cd qwen-livetranslate-voice-clone
.\install.bat
.\start.bat
```

如果使用 CMD，克隆后在项目根目录执行：

```bat
install.bat
start.bat
```

也可以下载整个仓库 ZIP，解压后双击 `install.bat`，再双击 `start.bat`。不要只下载某个 Python 文件或 `frontend/`：正式版还需要音频核心、音色管理和测试引擎。

安装脚本在项目 `.venv` 中安装依赖。首次启动会从公开模板生成本地配置和示例术语表，不覆盖已经填写的实际配置。控制台默认地址为 `http://127.0.0.1:8788/`。

### 2. 配置云接口

1. 打开“接口与翻译”，填写 API Key、Workspace ID 和对应区域。
2. 设置远端与麦克风两条链路的源语言、目标语言；中文用 `zh`，英文用 `en`。
3. 保存配置。页面不会回显已保存的 API Key；若设置了环境变量 `DASHSCOPE_API_KEY`，它会优先于文件中的密钥。
4. 初次使用建议先选择“远端字幕”，验证媒体声音、接口权限和字幕，不必立即配置虚拟麦克风。

可以先启动 Web 控制台、再填写密钥；点击页面中的“启动程序”才会开始正式的采集和翻译会话。

### 3. 选择设备并测试

1. 在“音频设备”刷新物理设备列表。麦克风选择实际说话的设备；媒体捕获选择浏览器 / 会议声音实际播放的物理耳机端点。
2. 先用“平衡模式”预设，执行电平检测或校准。需要推流时，再确认 `CABLE Input` / `CABLE Output`。
3. 在“测试实验室”先验证字幕；使用克隆译音前创建 / 选择音色，再做本地路由与实时译音测试。
4. 回到总览，选择模式，执行启动前检查，开始翻译。

Windows 需要允许桌面应用访问麦克风。设备被拔出、重命名或系统播放设备改变后，应停止会话、重新刷新并选择设备。

## Web 控制台功能

前端是多页面结构，由同一个本机后端提供，无需单独运行前端开发服务器。

| 页面 | 路径 | 功能与常用操作 |
|---|---|---|
| 总览 | `/` | 三种模式；字幕显示哪一方；麦克风翻译 / 克隆输出；启动前检查、启动 / 停止会话、开关悬浮窗、按住说话；查看链路状态与延迟 |
| 接口与翻译 | `/config.html` | 云端区域、业务空间、密钥；双方语言方向；同语言跳过；超时、服务端 VAD、音色策略、两路热词 |
| 音频设备 | `/audio.html` | 物理麦克风、物理播放设备及虚拟端点；电平检测 / 校准；三种预设；增益、噪声门、分块和缓冲 |
| 字幕 | `/subtitles.html` | 仅对方 / 仅自己 / 双方；原文、译文、标签；字体、颜色、透明度、紧凑背景、宽度、内边距及位置 |
| 音色管理 | `/voices.html` | 上传本人或授权样本；创建、查询、选择、删除云端音色；设置复刻策略 |
| 测试实验室 | `/test.html` | 媒体字幕测试、VB-CABLE 校准音回录、实时克隆译音、延迟指标、测试音频试听与删除 |
| 后台状态 | `/status.html` | 检查音频捕获、云端推送、播放和恢复状态，定位认证、设备、断线或输出异常 |

保存参数和把参数应用到正在运行的云会话并不是同一步。修改语言、设备、VAD、音色等会话参数后，按页面提示停止并重新启动会话；运行期间不应切换模式。配置归属见[配置目录说明](config/README.md)。

## 会议软件与 OBS 的音频路由

以“自己中文 → 克隆英文译音，对方英文 → 中文字幕”为例：

| 位置 | 应选择的设备 / 参数 |
|---|---|
| 本项目麦克风 | 物理耳麦 / USB 麦克风，不是 `CABLE Output` |
| 本项目媒体捕获 | 会议扬声器实际使用的物理耳机播放端点 |
| 本项目译音输出 | `CABLE Input` |
| 会议软件麦克风 | `CABLE Output` |
| 会议软件扬声器 | 物理耳机，不是 `CABLE Input` |
| 本地链路语言 | 源语言 `zh`，目标语言 `en` |
| 远端链路语言 | 源语言 `en` 或自动，目标语言 `zh` |

音频链路是“物理麦克风 → 百炼克隆译音 → CABLE Input → CABLE Output → 会议软件”。这是设备级路由，不是腾讯会议、Teams 或 Zoom 的专用 SDK 集成。Google Meet 等浏览器会议也需在自身设备设置中选择虚拟麦克风，并按实际环境验证权限和可听性。

媒体链路采集指定播放端点上的全部系统声音，不是某个标签页或会议进程。使用耳机、关闭无关声音，避免把克隆译音播放回同一个捕获端点，否则可能形成回声和二次识别。

OBS 如需录制译音，添加 `CABLE Output` 音频输入；如需字幕，捕获悬浮窗或字幕区域。避免同时录入原声轨和译音轨造成重复。详见[会议、录屏和防回授设置](docs/音频路由使用说明.md)。

## 翻译、字幕与麦克风参数

### 语言与热词

两条链路分别配置源语言、目标语言和术语映射。例如本地链路配置 `人工智能 → artificial intelligence`，远端链路配置反向映射。热词用于帮助识别和翻译，不是强制字符串替换，也不保证所有场景都准确。

当前主要使用与测试场景是中文和英文。云模型支持的语言范围不能等同于本应用所有语言、音色和设备组合都已验收。

### 同语言跳过机制

“同语言时跳过文本 / 音频”由 LiveTranslate 服务端判断输入语言与目标语言是否相同，并决定是否产生相应的翻译输出：

- 跳过文本：可能不再返回该段翻译文本；输入原文识别和界面的原文显示是另外的设置。
- 跳过音频：该段可能没有译音，不代表 CABLE 路由损坏。
- 它不是“关闭整条连接”，也不是“跳过另一方字幕”。字幕来源筛选在“字幕”页面或总览设置。

只做麦克风原文字幕时，关闭麦克风翻译、明确源语言，并启用原文显示。排查“有原文、没有译文 / 译音”时，应同时检查语言方向、这两个开关、音色和路由状态。[同语言输出字段说明](https://help.aliyun.com/en/model-studio/live-translator-client-events)

### 麦克风推荐预设

| 预设 | 输入分块 | 服务端 VAD 静音阈值 | 译音缓冲最大容量 | 适合 |
|---|---|---|---|---|
| 清晰优先 | 80 ms | 550 ms | 5000 ms | 优先保证连续表达和尾字完整 |
| 平衡模式 | 60 ms | 500 ms | 5000 ms | 日常会议的起始配置 |
| 低延迟 | 50 ms | 400 ms | 4000 ms | 网络和近讲信号稳定后尝试，需检查吞字和断句 |

三种预设默认关闭本地噪声门并启用目标语言保护。RingBuffer 数值是最大容错容量，不是固定等这么久才播放；过度缩小缓冲会在云端突发返回音频时丢字。

先把麦克风靠近嘴边、校准电平，再调增益和 VAD。普通噪声门不能可靠区分自己的轻声与远处人声。嘈杂环境优先用“按住说话保护”；自定义快捷键仅在总览页面具有焦点时生效，不是系统全局热键。松开后发送静音，让已提交的语音完成翻译和播放，不应立即退出程序。

## 声音复刻与音色管理

推荐用 Web“音色管理”完成：上传 WAV / MP3 / M4A 样本 → 创建音色 → 查询列表 → 选择音色 → 实时译音测试。音色存在阿里云服务中，`voice_id` 保存到本地配置；删除云端音色不可逆，正在使用该 ID 的会话需要换用有效音色。

优先使用预先创建的固定音色：`enable_voice_clone=true`、`clone_frequency=never` 和选定的 `voice_id`。这不表示关闭克隆译音，而是“不在每次实时响应中重新复刻”。

页面也提供 `once` / `always`。按官方规范，这两种实时复刻策略需使用 `voice=default`，不能只切换频率后仍沿用固定克隆 ID；请在“接口与翻译”的音色 ID 字段按规范配置，并做真实云端验证。本项目不会仅凭切换选项宣称该策略已经验收。[音色策略官方说明](https://help.aliyun.com/en/model-studio/qwen3-5-livetranslate-flash-realtime)

仅使用本人或明确获授权的声音。建议准备 10–20 秒单人、清晰、无背景音乐的样本。独立 CLI 提供创建、分页查询、删除、录音文案与离线样本检查，并可另行执行文件译音验证。完整步骤见[声音复刻使用说明](docs/声音复刻使用说明.md)。

## 功能模块与项目结构

```text
qwen-livetranslate-voice-clone/
├─ backend/
│  ├─ formal_app/            正式版服务、业务编排、配置、字幕与音色管理
│  ├─ src/ai_interpreter/    音频捕获、重采样、实时协议、队列、播放和悬浮窗
│  ├─ virtual_mic_test/      虚拟麦克风检测、译音推流与回录测试引擎
│  └─ voice_clone/           百炼音色管理 CLI 与文件译音测试
├─ frontend/
│  ├─ console/              正式版七个 Web 页面及共享脚本 / 样式
│  ├─ virtual-mic/          独立虚拟麦克风测试页面
│  └─ legacy/               保留的原型控制台
├─ config/                  公开模板和被 Git 忽略的本地实际配置
├─ data/                    私人样本、测试音频、日志与运行数据，不上传
├─ docs/                    音频路由、测试实验室、声音复刻使用说明
├─ tests/                   音频核心、协议等自动化测试
├─ scripts/manage.ps1       统一安装、启动、停止、检查和 CLI 入口
├─ install.bat
├─ start.bat
├─ stop.bat
├─ run-tests.bat
└─ pyproject.toml
```

| 模块 | 主要职责 | 使用入口 / 进一步说明 |
|---|---|---|
| 正式版业务层 | 本机 HTTP / WebSocket 服务；根据模式启动链路；管理配置、字幕、音色、测试和状态 | `start.bat`；[后端说明](backend/README.md) |
| 音频与翻译核心 | WASAPI 与麦克风采集；PCM16 转换与重采样；LiveTranslate 事件适配；有界队列、重连和播放 | 正式版和测试引擎复用，不需单独启动 |
| Web 控制台 | 七个页面展示并调用后端；真实音频设备由 Python 后端访问 | 本机 8788；[前端说明](frontend/README.md) |
| 音色管理 CLI | 用 `qwen-voice-enrollment` 管理固定音色；独立样本与文件译音验证 | `manage.ps1 -Action voice`；[CLI 模块](backend/voice_clone/README.md) |
| 虚拟麦克风测试 | 校准音、实时推流、CABLE 回录、信号电平和延迟 | 正式版“测试实验室”；独立工具端口 8776 |
| 配置与数据 | 公共模板与个人配置分离；正式版、CLI、原型各自配置 | [配置归属](config/README.md)、[本地数据](data/README.md) |

前后端分开存放，由一个后端统一提供服务。启动脚本用 Windows Junction 建立兼容目录映射；映射不进入 Git，不要在映射路径再复制第二份前端或配置。

仓库名称不改变 Python 模块 / 安装包名称：安装包仍为 `ai-meeting-interpreter`，核心模块为 `ai_interpreter`，正式版入口为 `formal_app`。

## 启动、停止与命令行

以下 PowerShell 命令均从项目根目录执行；统一加上执行策略参数，避免个人脚本策略阻止启动：

```powershell
# 准备目录和缺失配置，不连接云服务
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\manage.ps1 -Action prepare

# 检查配置、前端路径和密钥配置状态，不等于云权限和硬件验收
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\manage.ps1 -Action check

# 不自动打开浏览器
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\manage.ps1 -Action start -NoBrowser

# 关闭整个后台并释放设备
.\stop.bat

# 自动化回归
.\run-tests.bat

# 查看录音文案、查询音色
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\manage.ps1 -Action voice prompt
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\manage.ps1 -Action voice list
```

页面“停止程序”停止翻译会话，Web 控制台仍可配置；`stop.bat` 关闭整个后台。关闭浏览器标签页并不等于停止后台采集。

独立虚拟麦克风工具：`-Action virtual-mic -NoBrowser`，默认端口 8776；原型控制台：`-Action legacy`，默认端口 8765。它们使用独立配置，通常无需同时运行；使用相同设备时应先停止另一套采集 / 推流。

## 测试与延迟评估

先运行 `run-tests.bat` 做本地回归，再在“测试实验室”依次执行：

1. 媒体声音检测和字幕测试，确认正确播放端点。
2. VB-CABLE 校准音路由测试，确认 `CABLE Output` 回录有声。
3. 实时麦克风克隆译音测试，确认边讲话边返回字幕和译音。
4. 停止后对比云端译音与虚拟麦克风回录，最后让真实会议另一端试听。

总览显示捕获、服务端 VAD、首个字幕、首块云端音频和写入 CABLE 等链路指标。测试实验室另外显示三段译音延迟、回录延迟和电平。没有有效语音 / 音频事件时显示“—”，不是测得 0 ms。

写入 `CABLE Input`、`CABLE Output` 回录有声、会议对方实际听见，是三个不同验收点。本机指标不包含会议编码、网络传输和对方播放延迟；厂商宣传延迟也不是本项目的全链路承诺。

自动测试覆盖配置、音频处理、队列、协议事件、页面契约、音色管理和测试引擎，不证明实际账号、麦克风、驱动和会议软件已验收。详见[测试步骤、延迟口径与故障判断](docs/测试实验室使用.md)。

## 隐私与数据保存

- `config/settings.yaml` 保存正式版参数；`config/settings.local.yaml` 保存 API Key；环境变量 `DASHSCOPE_API_KEY` 优先。
- 独立 CLI、独立测试工具和原型配置各自独立，不会因修改正式版页面就自动同步。
- Git 只保留白名单示例与通用术语；实际配置、音色 ID、样本、音视频、测试输出和日志不上传。
- Git 忽略不等于加密。不要公开实际配置、完整终端输出或含业务空间 / 音色信息的截图。
- 云端识别与翻译会向阿里云发送音频，创建音色会上传样本；使用前应取得相应声音和会议内容的使用授权。
- 正常会话与测试保存行为不同：实验室保存可试听 / 删除的音频；文件译音测试保存 WAV 和可能含原文 / 译文的 JSON 摘要，都应按私人数据处理。
- 默认只监听 `127.0.0.1`，不要把控制台作为公网网站暴露。

## 常见问题

| 问题 | 检查方向 |
|---|---|
| 视频有声，但没有字幕 | 确认实际播放端点与媒体捕获一致；先“检测媒体声音”；停止后重新选择设备 |
| 页面打开了，但没开始翻译 | `start.bat` 先启动控制台；仍需完成配置和设备检查，再点击总览“启动程序” |
| 有原文，没译文或译音 | 检查语言、同语言跳过、字幕显示开关、有效音色和云端事件 |
| 云端译音有声，CABLE 回录无声 | 检查端点方向、输出开关、驱动重启和设备电平 |
| 回录有声，会议对方听不到 | 会议麦克风选 `CABLE Output`；检查静音、音量、权限与降噪，并做另一端试听 |
| 轻声被远处人声干扰 | 近讲耳麦和按住说话；不要只提高噪声门阈值，否则轻声也会被截断 |
| 延迟或吞字明显 | 先用平衡预设；检查网络、VAD 和溢出；不要把缓冲容量当固定延迟而盲目缩小 |
| 端口占用 / WinError 10048 | 正式版默认 8788；确认是否已启动，先用 `stop.bat`，不要随意结束其他程序 |
| 保存配置后还是旧效果 | 按提示重新启动翻译会话；保存不等于已重新配置当前云会话 |
| 找不到声音样本 | 仓库不提供私人录音；准备自己的样本并设置路径 |

## 官方资料与使用文档

- [Qwen LiveTranslate 实时翻译模型](https://help.aliyun.com/en/model-studio/qwen3-5-livetranslate-flash-realtime)
- [LiveTranslate 客户端事件](https://help.aliyun.com/en/model-studio/live-translator-client-events)、[服务端事件](https://help.aliyun.com/en/model-studio/live-translator-server-events)
- [声音复刻 HTTP API](https://help.aliyun.com/en/model-studio/voice-clone-design-http-api)、[声音样本与录制要求](https://help.aliyun.com/en/model-studio/voice-cloning-user-guide)
- [VB-CABLE 官方驱动、安装与设备方向](https://vb-audio.com/Cable/)
- [音频路由使用说明](docs/音频路由使用说明.md)
- [测试实验室使用](docs/测试实验室使用.md)
- [声音复刻使用说明](docs/声音复刻使用说明.md)

这是第三方客户端，不是阿里云或 VB-Audio 官方软件；云服务、模型和驱动的授权与可用性以供应商说明为准。
