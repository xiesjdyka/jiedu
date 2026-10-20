# 统一发布版本

根目录 `VERSION` 是程序发布版本的来源。当前候选程序、原生外壳和安装器均为 `1.1.5`。

## 1.1.5 的字段对应

| 位置 | 当前候选的值 |
| --- | --- |
| VERSION、关于与托盘的程序版本、设置页 | `1.1.5` |
| pyproject.toml、Python 包版本 | `1.1.5` |
| 安装器文件名、ProductVersion 文本 | `Jiedu-1.1.5-windows-x64-setup.exe`、`1.1.5` |
| 安装器数字 FileVersion / ProductVersion | `1.1.5.0` |
| Cargo、Tauri、关于另列的外壳构建版本 | `1.1.5` |
| 重新编译原生 EXE 的数字 FileVersion / ProductVersion | `1.1.5.0` |

本候选同步程序与原生构建版本，并重新编译原生外壳。联网安装器自动下载经固定 SHA256 校验的 Python 3.13.16、向量模型和 OCR 资源；其实际字节、解释器启动与功能验收以安装器及资源回执为准。

Windows 数字字段使用四段数字，第四段对应候选序号；稳定版的第四段为零。[Inno Setup 数字产品版本](https://jrsoftware.org/ishelp/topic_setup_versioninfoproductversion.htm)与[文本产品版本](https://jrsoftware.org/ishelp/topic_setup_versioninfoproducttextversion.htm)是不同字段。安装器从 payload 的 VERSION 派生文件名和版本资源，不另写硬编码候选号。

## 仅更新程序元数据时

实际原生编译输入、外层 EXE 和运行时文件清单都未变化时，可保留已有外壳构建元数据。修改 VERSION 后，在源码根目录执行：

```powershell
python desktop/release_version.py sync --scope program
python desktop/release_version.py check --scope program
```

此模式更新 Python 版本字段，保留 Cargo、Tauri 与本包 Cargo.lock 的外壳构建版本；它们仍须互相一致。同步工具不构建、上传或发布。发布工具还必须核验原生指纹、前后外壳 EXE 的字节与 SHA256、前后运行时清单，不能靠选择参数放行变化。

关于和托盘读取已安装的 VERSION 与 pyproject.toml 并校验程序版本。关于另列外壳构建版本；Windows 文件属性记录该二进制自身的构建版本。原生窗口保持打开时会在版本文件变化后刷新托盘；元数据不一致时明确显示未确认。rc6 安装器可包含 rc4 外壳，上表分别说明两者身份；其实际显示以对应安装器回执为准。

## 原生或运行时发生变化时

原生源、构建输入、外壳二进制、Python 或依赖发生变化，必须使用新完整安装器；仅覆盖 program 的 `.jiedu` 更新包不能替换外层 EXE 或运行时。需要重建原生程序时，先修改 VERSION，再同步并检查全部版本元数据：

```powershell
python desktop/release_version.py sync
python desktop/release_version.py check
```

原生构建工具会校正和核验 Windows 版本资源，同时保留图标与应用清单。旧 rc4 原生重建和旧安装器的验收属于历史记录，不能替代新安装器的版本、WebView、托盘、安装升级及资料保全检查。签名和渠道边界见[更新发布准备](update-release.md)。
