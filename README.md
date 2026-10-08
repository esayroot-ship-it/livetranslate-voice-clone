# AI 会议同声传译

Windows 本机会议翻译程序：Web 控制台、悬浮字幕、麦克风翻译、克隆译音、VB-CABLE 输出，以及独立测试实验室。

## 启动与停止

- 双击根目录 `start.bat`：启动正式版，并打开 `http://127.0.0.1:8788/`。
- 双击 `stop.bat`：停止程序并释放音频设备；已停止时可以再次执行。
- 首次使用或缺少依赖时，运行 `install.bat`，在项目 `.venv` 中安装依赖。
- 运行 `run-tests.bat` 执行现有自动化测试。

运行环境：Windows 11 x64、Python 3.11–3.13 x64。克隆声音送入会议软件时需安装 VB-CABLE。

## 目录结构

```text
translate/
├─ backend/                 Python 后端及必需的辅助模块
│  ├─ formal_app/            正式版 API、配置管理、字幕和音色管理
│  ├─ src/ai_interpreter/    音频捕获、重采样、云端协议和播放
│  ├─ virtual_mic_test/      虚拟麦克风测试引擎及其测试
│  └─ voice_clone/           音色管理 CLI 及其测试
├─ frontend/                HTML、JavaScript、CSS 的唯一存放位置
│  ├─ console/              正式版控制台
│  ├─ virtual-mic/          独立虚拟麦克风测试页面
│  └─ legacy/               原型界面，保留原有兼容功能
├─ config/                  正式版配置与无密钥示例
│  ├─ legacy/               原型配置和术语表
│  ├─ virtual-mic/          独立虚拟麦克风工具配置
│  └─ voice-clone/          声音复刻 CLI 配置
├─ data/                    本地声音样本、测试音频、日志和运行文件
├─ docs/                    使用文档
├─ tests/                   核心模块测试
├─ scripts/manage.ps1       统一启动、停止、安装和测试入口
├─ start.bat
├─ stop.bat
├─ install.bat
├─ run-tests.bat
└─ pyproject.toml
```

应用的 Python、HTML、JavaScript 和 CSS 内容保持原样。启动脚本自动建立 Windows 目录映射，让原有代码继续按原路径读取独立的前端、配置和运行数据；这些映射不进入 Git。不要在映射目录中再存放第二份源码。

## 配置

正式版使用 `config/settings.yaml` 和 `config/settings.local.yaml`。这两份实际配置只保存在使用者本机，包含语言、音色、设备、字幕和密钥。

通过网页“接口与翻译”“音频设备”“字幕”页面修改配置。参数保存后按页面提示重新启动翻译会话。网页不会回显 API Key。

Git 只记录明确列入白名单的 `*.example.yaml` 和示例术语表。所有实际配置（包括深层目录的 JSON、TOML 等格式）、声音样本、录音、视频、日志和运行文件均被忽略。首次从 Git 获取项目时，启动脚本会从示例创建配置文件和术语表，再通过网页填写个人配置。

## 使用文档

- [音频路由和会议设置](docs/音频路由使用说明.md)
- [测试实验室使用](docs/测试实验室使用.md)
- [声音复刻使用](docs/声音复刻使用说明.md)

## 命令行

```powershell
.\scripts\manage.ps1 -Action check
.\scripts\manage.ps1 -Action start -NoBrowser
.\scripts\manage.ps1 -Action stop
.\scripts\manage.ps1 -Action test
.\scripts\manage.ps1 -Action voice prompt
.\scripts\manage.ps1 -Action voice list
.\scripts\manage.ps1 -Action virtual-mic -NoBrowser
```

独立虚拟麦克风页面使用端口 8776，通常直接使用正式版中的“测试实验室”即可。原型页面可用 `-Action legacy` 启动，使用独立配置和端口 8765。

## 从仓库开始使用

下载或克隆完整仓库后，运行 `install.bat`，再运行 `start.bat`。启动脚本会建立目录映射并生成本地配置，随后在控制台填写 API Key、Workspace ID、设备和授权音色。

创建声音复刻音色时使用自己的录音样本。样本和测试输出保存在 `data/`，不会包含在公共仓库中。
