# 1.2.4 更新说明

界渡程序版本为 1.2.4。通过界渡生成的 Claude Desktop 扩展版本为 1.1.9。

- 下载前核验扩展清单、实际入口文件和启动参数。重新准备可用旧身份时，界渡保留现有连接及权限，刷新旧扩展包。
- Node 中转入口只透传 MCP 标准输入输出，启动参数错误、找不到程序或工作目录、子进程异常退出时将原因写入标准错误。
- 重新生成 Claude 凭据会替换之前的扩展包。确认后请在 Claude Desktop 安装新包，再实际读取一次。

本次通过程序更新包交付。新用户可先安装 [1.2.3 完整安装包](https://github.com/xiesjdyka/jiedu/releases/download/1.2.3/Jiedu-1.2.3-windows-x64-setup.exe)，再在界渡内检查更新。实际下载与签名清单以 [1.2.4 发布页](https://github.com/xiesjdyka/jiedu/releases/tag/1.2.4) 为准。
