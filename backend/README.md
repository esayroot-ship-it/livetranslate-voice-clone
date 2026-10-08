# 后端模块：实时音频、翻译与音色管理

本目录是 Qwen LiveTranslate Voice Clone 的 Python 后端。正式版、独立测试工具和声音复刻 CLI 共用音频 / 云端能力；前端源码不在这里，实际配置与运行数据也分别位于项目根目录的 `config/` 和 `data/`。

## 模块职责

| 模块 | 关键文件 / 子模块 | 功能 |
|---|---|---|
| `formal_app/` | `app.py`、`server.py` | 正式版启动 / 关闭、FastAPI 服务、页面与 API、本机状态推送 |
| 正式版配置 | `formal_app/config.py` | 合并默认配置、实际参数和密钥；校验模式、模型、语言、设备与字幕参数；Web 返回值隐藏密钥 |
| 正式版运行 | `formal_app/runtime.py` | 根据模式选择远端字幕或麦克风 / 双路控制器；编排悬浮窗、会话状态和延迟 |
| 正式版测试 | `formal_app/testing.py` | 媒体字幕测试与虚拟麦克风测试实验室 |
| 正式版音色管理 | `formal_app/voice_manager.py` | 调用声音复刻客户端，创建 / 查询 / 删除 / 选择音色 |
| `src/ai_interpreter/` | `audio.py`、`buffers.py` | 设备枚举与解析、WASAPI 回环、物理麦克风、PCM 转换 / 重采样、有界输入队列与译音 RingBuffer |
| 实时协议 | `protocol.py`、`session.py` | 阿里云会话配置、原译文增量关联、音频增量、错误分类、会话结束与断线重连 |
| 核心控制器 | `controller.py` | 管理采集 / 输出工作线程和云端链路，按住说话时控制麦克风传输 |
| 悬浮字幕 | `overlay.py` | Tkinter 置顶字幕窗，来源过滤和原文 / 译文样式 |
| `virtual_mic_test/` | `engine.py` 及配套模块 | 校准音、实时克隆译音、CABLE 回录、信号与延迟检测 |
| `voice_clone/` | `src/bailian_voice_clone/` | 独立音色管理 CLI 和文件译音验证 |

## 正式版调用顺序

1. 根目录启动脚本建立目录映射、准备缺失配置并设置 Python 模块路径。
2. `formal_app.app` 启动本机 Web 服务；此时可以配置，不会仅因打开网页就开始正式翻译。
3. 总览“启动程序”调用运行层，先校验密钥、语言、音色和设备，再启动当前模式的链路。
4. 音频核心把输入整理为 16 kHz 单声道 PCM16；实时协议发送到 LiveTranslate，并将文本 / 音频事件转为字幕、状态和译音输出。
5. 停止时按云端结束流程收尾，再关闭工作线程与设备；根目录 `stop.bat` 还会关闭整个 Web 后台。

双路同传中，远端只请求文本，本地按配置请求文本和音频。正常断线按配置的 1 / 2 / 4 / 8 / 15 秒延迟尝试恢复；认证或配置类错误不会靠无限重试解决。重连清空旧输入，避免重新播放断线期间积压的话音。

## 运行与验证

从项目根目录执行：

```powershell
.\install.bat
.\start.bat
.\run-tests.bat
.\stop.bat
```

不要单独复制本目录运行，也不要为了匹配新仓库名改动 `formal_app` / `ai_interpreter` 模块名。统一入口已处理前端映射、配置路径和辅助模块依赖。

实际设备、云权限与会议可听性需要[测试实验室](../docs/测试实验室使用.md)验证。模块自动测试分别位于根目录 `tests/` 和各模块 `tests/`。

进一步阅读：[根 README](../README.md)、[音频路由](../docs/音频路由使用说明.md)、[声音复刻](../docs/声音复刻使用说明.md)。
