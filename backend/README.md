# 后端

- `formal_app/`：正式版 HTTP/WebSocket 服务和业务编排。
- `src/ai_interpreter/`：音频与实时翻译核心。
- `virtual_mic_test/`：正式版测试实验室所需的虚拟麦克风模块。
- `voice_clone/`：正式版音色管理所需的百炼客户端及独立 CLI。

Python 文件内容与整理前一致。统一使用根目录启动脚本；前端实际文件位于 `frontend/`，配置在 `config/`，运行文件在 `data/`。后端目录中的对应映射由 `scripts/manage.ps1` 自动生成。
