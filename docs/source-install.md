# 从源码安装与升级

候选产品版本以根目录 `VERSION` 和等价的 Python 元数据为准，不能沿用旧文档中的版本。源码安装、完整安装器、程序更新和原生外壳分别验收；历史干净 Windows 的选定结果不自动覆盖后续候选。当前准确范围见 [版本边界](release-status.md) 及对应候选的验收回执。

## Windows 源码环境

准备 Python 3.12 x64、Git、PowerShell 7，以及依赖所需磁盘空间。把源码放在自己选择的目录。首次安装不连接个人工作区：

```powershell
pwsh -NoProfile -File scripts/setup.ps1 -PythonExecutable python
start.bat
```

脚本创建源码目录下的 .venv312，依赖与临时缓存也默认放在源码目录。已有 config.json 不覆盖，已有知识库和同步进度不读取、不删除；没有自动安装模型、OCR 或 CUDA，也没有全局修改系统 Python。失败后修正网络或环境，再运行同一脚本。

先创建空的演示工作区，导入合成文本，搜索并打开来源。普通文字检索不需要先下载向量模型；需要语义检索再准备对应模型。退出使用 stop.bat。普通浏览器关闭不会停止后台。

## MCP 与认证示例

在 AI 接入页面创建并配对客户端，按页面生成当前机器配置，再重新连接客户端。不要复制作者机器路径或他人的凭据。

HTTP 受控收件箱检索示例位于 examples/authenticated_inbox_search.py。用配对结果设置 JIEDU_CLIENT_ID 和 JIEDU_CLIENT_SECRET 环境变量，并在界面允许收件箱搜索。示例仅连接 127.0.0.1，拒绝代理转发、不修改权限。凭据不写到参数、源码或公开截图中。HTTP 认证示例不等于任意客户端都支持历史自动归档。

## 更新与资料保全

先停止本测试实例，保存自己工作区的备份。源码更新仅替换审查过的程序文件；不覆盖 config.json、data、.jiedu、知识库或旧同步游标。重新执行安装脚本核对依赖。当前已有完整安装器及签名程序更新机制；公开在线渠道仍未上线，不对现有生产工作区试用候选更新。发布者的参数化配置、离线预检和完整安装器检查清单见 [更新发布准备](update-release.md)。

## 桌面构建

可选桌面外壳在 desktop 目录，使用 Tauri 2。安装 Rust stable MSVC 工具链、Visual Studio C++ Build Tools、Windows SDK 与 WebView2，然后从开发者命令行执行 cargo build --release --locked。推荐把 CARGO_HOME、RUSTUP_HOME 和 CARGO_TARGET_DIR 放到空间充足的测试目录。

本仓库原 scripts/build-desktop.ps1 面向已准备的离线 SDK 目录；一般开发者可以用标准开发者命令行构建。桌面程序需要可用的界渡 Python 环境，不能把单个 EXE 当成完整发行包。完整安装器、原生外壳、升级和卸载保全的结论须对应准确产物哈希及验收环境；不以历史源码验收代替新包验收。Authenticode 为可选信誉签名，未签名候选应明确提示，不能省略来源、版本和摘要核对。

## OCR 与模型资源

OCR 的版本、资源地址和哈希在 ocr-resources.lock.json；离线准备器 scripts/prepare_ocr_runtime.py 只使用已下载且验证的资源。此源码候选不分发模型和运行时，首次资源准备仍需按清单核对许可和完整性。不要把安装失败解释成扫描图没有文字。

## 固定依赖的安装方式

清单包含完整依赖版本。两个浏览器读取依赖的上游声明分别使用固定 Git 提交与无提交 URL，直接让 pip 重新解析会报告 URL 冲突。安装脚本按完整清单逐项安装（不重新解析间接依赖），随后执行 pip check，并核对所有版本和两个 Git 提交。任一步失败都会停止。不要只安装部分清单并跳过核对。

源码安装脚本的默认环境为 Python 3.12 x64；完整安装器使用其锁定的自带 Python。不同 Python 版本及系统仍须对应测试，不能把其中一种环境的结果推广到任意配置。
