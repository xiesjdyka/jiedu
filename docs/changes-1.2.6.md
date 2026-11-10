# 1.2.6 更新说明

界渡 1.2.6 修复 Claude Desktop 中 Cowork/Code 的 MCP 协议回退失败。扩展继续使用 1.1.10。

1. 在界渡中检查更新，确认程序已升级到 1.2.6。
2. 已安装 1.1.10 扩展且仍连接到这个界渡安装时，完全退出 Claude Desktop，再重新打开。新启动的扩展进程会加载已更新的界渡程序，可以继续使用现有扩展。
3. 在 Cowork 或 Code 中使用一项已获准的只读界渡工具验证连接。

若仍使用更早的扩展，或扩展指向另一处界渡安装，请从当前界渡的 Claude 接入卡重新准备并下载扩展，再到 Claude Desktop 安装并启用。

界渡让 MCP SDK 单独处理连接开始时的 server/discover，由第一条非探测请求确定该连接使用哪种协议。现代请求和旧版 initialize 都沿用 SDK 的版本及能力协商。标准错误记录连接标识和协商结果，便于区分各条连接。

本机宿主通道重放已验证探测超时后的回退、现代请求、普通会话和并发连接。真实 Cowork 界面的连接仍需按上述步骤确认。

本版通过程序更新包交付。新用户可先安装 [1.2.3 完整安装包](https://github.com/xiesjdyka/jiedu/releases/download/1.2.3/Jiedu-1.2.3-windows-x64-setup.exe)，再在界渡内检查更新。下载文件与签名清单见 [1.2.6 发布页](https://github.com/xiesjdyka/jiedu/releases/tag/1.2.6)。
