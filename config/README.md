# 配置

- `settings.yaml`：正式版模式、语言、设备、音色和字幕参数。
- `settings.local.yaml`：正式版 API Key。
- `*.example.yaml`：不含真实账号数据的 Git 配置模板。
- `legacy/terminology/*.example.json`：通用示例术语，首次启动复制为本地实际术语表。
- `legacy/`：原型配置及术语表。
- `virtual-mic/`：独立虚拟麦克风测试工具配置。
- `voice-clone/`：声音复刻 CLI 配置。

实际本地配置不进入 Git。`config/` 采用明确的文件白名单，其他格式和深层目录配置也会被忽略。启动脚本仅在配置文件不存在时从示例创建，不覆盖已经填写的文件。
