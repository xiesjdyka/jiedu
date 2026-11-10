# 1.2.5 更新说明

界渡 1.2.5 修复 Claude Desktop 扩展初始化超时。界渡生成的新扩展版本为 1.1.10。

1. 在界渡中检查更新，升级至 1.2.5。
2. 在「连接 AI」打开 Claude 接入卡，选择「准备并下载扩展包」。已有有效配对沿用原身份和权限。
3. 在 Claude Desktop 的 Settings → Extensions → Advanced settings 中选择 Install Extension，安装并启用刚下载的扩展包，再用一项已获准的只读工具验证连接。

更新界渡后，Claude Desktop 内的旧扩展仍需按以上步骤重新安装。扩展包含本机配对信息，请仅在本机使用。

新版通过 Claude 内置 Node 宿主的消息通道转发 MCP 输入和输出，由 MCP SDK 协商协议版本和能力。客户端鉴权、控制台连接和向量检索在工具调用阶段执行。依赖控制台的工具在连接失败或超时后返回明确错误，混合检索沿用有界关键词回退并说明原因。启动与连接诊断写入标准错误。

使用同系列 Node 24 重放 Claude 的宿主消息通道，并运行随包 Python，冷启动握手实测约 1.3 至 1.6 秒，仍高于 1 秒目标。

本版通过程序更新包交付。新用户可先安装 [1.2.3 完整安装包](https://github.com/xiesjdyka/jiedu/releases/download/1.2.3/Jiedu-1.2.3-windows-x64-setup.exe)，再在界渡内检查更新。下载文件与签名清单见 [1.2.5 发布页](https://github.com/xiesjdyka/jiedu/releases/tag/1.2.5)。
