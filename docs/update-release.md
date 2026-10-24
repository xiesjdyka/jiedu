# 准备更新渠道与发布产物

发布归属为 `xiesjdyka/jiedu`；历史版本已有公开发布，当前 1.1.9 候选尚未发布。固定更新清单为 `https://github.com/xiesjdyka/jiedu/releases/latest/download/update.json`，下载页为 `https://github.com/xiesjdyka/jiedu/releases/latest`。地址已确定不代表 1.1.9 产物或更新清单可用；404、网络失败和校验失败都不能显示为“已是最新版”。发布工具只生成本地材料，不代替 Owner 发布。

## 两种不同产物

| 产物 | 可更新内容 | 必要核对 |
| --- | --- | --- |
| `.jiedu` 程序更新 | 公开白名单中允许的 Python、静态资源、文档和产品版本文件；不含 `desktop/`、原生 EXE、Python 或运行时 | 版本递增；统一产品版本检查；原生构建输入、前后外层 EXE 的 SHA256/大小及运行时清单完全相同；已安装公钥对应的 Ed25519 签名；大小、SHA256、有效期和主机 |
| 完整 Windows 安装器 | 原生外壳、Python 或运行时变动，以及程序本身 | 同一更新入口；已安装公钥签名的渠道清单、文件名、大小、SHA256、PE 产品版本和安装归属均通过；任务安全结束及资料保全验收 |

依赖锁文件相同不表示运行时逐件相同。例如启动器的可移植性修复必须通过完整安装器交付。原生源码、Cargo/Tauri 配置、锁文件、图标或内嵌页面变化也要求完整安装器。程序更新不能更换外层 EXE。程序单独升版使用统一版本工具的 `--scope program`，保留原生构建版本；完整原生构建使用完整版本同步与检查。

Ed25519 是程序包和渠道清单的来源完整性门槛，不能跳过。Windows Authenticode 是另一个可选的发行者信誉签名：没有商业证书不阻止 Owner 已知情的人工下载预览版，但应明确“未签名/未知发布者”提示；SHA256 和版本核对仍必须完成。本页工具不购买、获取、安装或生成 Windows 证书。应用只有在用户点击“更新并重启”、签名渠道与文件核验通过、安装归属明确且任务安全结束后，才由固定外部助手执行完整安装器；不运行任意用户提供的 EXE。

## 1. 生成空配置，等待 Owner 审阅真实地址

在审阅过的源码根目录执行；参数中的目录由发布者自己选择。把全部临时文件、缓存和测试报告放在本轮 D 盘候选目录，私钥单独保管。

```powershell
python -B desktop/update_release.py template --output release-config.json
python -B desktop/update_release.py config-check --config release-config.json
```

模板保留空的 `manifest_url`、`asset_hosts`、`program_asset_url`、`installer_asset_url`。第二条在填入真实地址前应拒绝，而不是显示“已是最新版”。字段用途：

- `manifest_url`：提供签名 `channel.json` 的 HTTPS 地址。
- `asset_hosts`：允许资源和重定向落到的明确主机名列表，不允许通配。
- `program_asset_url`：当前版本 `.jiedu` 文件的地址。
- `installer_asset_url`：完整安装器的发布地址；由签名完整安装器清单绑定相同版本、文件名、大小和 SHA256。
- `channel`：`preview` 或 `stable` 的发布标签。现有客户端依据 `manifest_url` 读取渠道，不会自动按这个标签过滤预览版本；应使用相应渠道地址。

至少填入一种产物地址。工具拒绝空地址、示例域名、`.test/.invalid`、本机地址、IP 字面量、查询参数、用户信息及明显占位符。它不联网核实域名所有权、仓库权限、文件可达性或重定向链；这些由发布审阅者和 Owner 核对。配置地址不放私有令牌；下载时经允许主机返回的重定向仍由客户端单独核验。

源码内 `knowledge_bridge/update-channel.json` 是客户端配置，只有地址完成审阅后才应在新候选中写入相应 `manifest_url`、`asset_hosts` 和 `channel`。本页工具不会修改它。已经装有空地址配置的版本，不会仅因服务器后来上线而自动发现新渠道；需要经过审阅的完整安装器或明确的配置迁移。

## 2. 构建程序更新

仅在原生外壳和运行时均不变时执行：

```powershell
python -B desktop/release_version.py sync --scope program --source $ReviewedSource
python -B desktop/build_signed_update.py --source $ReviewedSource --previous $PreviousProgram --key $PrivateSigningKey --output $NewEmptyOutput --url $ApprovedProgramAssetUrl --host $ApprovedDownloadHost --runtime-manifest $CurrentRuntimeInventory --previous-runtime-manifest $PreviousRuntimeInventory --native-exe $CurrentNativeExe --previous-native-exe $PreviousNativeExe
```

地址和主机变量必须来自审阅后的配置。私钥必须与上一安装版本的公钥匹配，且位于源码和产物输出目录之外；不能把私钥、工作区、聊天、附件、同步账本或内部验收材料加入公开白名单。不要通过此工具尝试轮换安装公钥。

运行时清单采用 JSON 或 `.json.gz`，记录包内普通文件的相对 `path`、`sha256`、`bytes`；可用文件列表或以路径为键的字典。清单来自对应冻结安装器的逐件核验，不能仅给两个相同的任意文件来声称兼容。必须同时给出前后真实外层 EXE 文件，工具只读比较其字节摘要和大小，不执行它们；不能以同名或相同源码代替二进制一致性。缺清单、缺 EXE、依赖变动、原生输入（含 `desktop/build_native.py`）变动、外层 EXE 或运行时任一项不同时，工具拒绝构建程序更新。

打包器只读取 `public-source-files.txt` 明确列出的文件，再按程序更新安全规则取交集；拒绝越界、链接、缺件和大小写别名。不会因文件碰巧位于允许目录就将其加入包。输出目录不得在源码内，也不能覆盖已有同名产物。

成功输出 `.jiedu` 和签名 `channel.json`。清单默认 30 天后失效，可指定 `--expires-days 1` 至 `365`；本次正式发布清单使用 365 天有效期，维护者须在到期前人工刷新签名清单。公开服务仍需维护版本与签名有效期；本轮不创建定时任务。

## 3. 离线核验渠道和程序包

```powershell
python -B desktop/update_release.py verify-program --config release-config.json --previous $PreviousProgram --package $ProgramPackage --envelope $SignedChannel --report $NewReportFile
```

工具使用上一版本的公钥，强制验证 Ed25519、有效期、配置中的确切资源 URL、允许主机、包大小、SHA256、内部成员清单和版本一致性，并拒绝非递增版本。不会修改旧安装、配置、工作区或缓存，也不会访问地址。

报告中的 `offline_checks_passed=true` 只表示离线核验通过。`public_channel_live=false`、`network_checked=false`、`published=false` 继续保持真实。下一步仍需独立验证 HTTPS 响应、允许的跳转、下载字节和已授权的隔离安装流程；公开地址及发布由 Owner 审阅。

## 4. 完整安装器检查清单

```powershell
python -B desktop/update_release.py installer-checklist --installer $Installer --version $ReviewedVersion --sha256 $ReviewedSha256 --config release-config.json --report $NewInstallerReport
```

SHA256 必须来自独立审阅清单，不能只把被检查文件的摘要再当作批准依据。工具在 Windows 中读取 PE 的 ProductVersion，不执行安装器；并使用 PowerShell 7 查询 Authenticode 状态。系统的证书链服务可能查询系统信任信息。未配置公开地址时，完整产物的内容检查可以完成，但正式配置预检仍不通过。

未签名会写入警告，不强制购买证书，也不豁免来源、摘要或版本核对。报告不会将 Authenticode、Ed25519 和 SHA256 混为同一种保证。完整安装器使用签名 `jiedu-update-channel-v1` 清单的 `kind=complete-installer`，另带 `installer_name`。程序更新使用 `kind=program-only`（旧清单省略 kind 时仍按程序包解释）。两种更新都由同一入口进行来源与摘要核验。

## 测试和交接边界

隔离合成链路可显式添加 `--test-only`。对应配置必须同时设 `test_only=true`，只允许 `.example/.test/.invalid` 等保留主机或 loopback HTTPS；构建器要求前后 fixture 使用同一临时公钥，且不得等于发行公钥。包和渠道清单均带签名覆盖的 `test_only=true` 标记。`verify-program` 也必须显式加此开关，否则拒绝测试配置。测试模式不允许看似真实的下载主机，不修改或启用正式渠道。

测试模式成功仍会返回 `formal_preflight_passed=false`，并保持 `published=false` 和 `public_channel_live=false`；它仅用于隔离合成验收，不能作为正式发布配置或产物。不要把测试私钥或测试配置放进公开包。

合成测试使用临时 Ed25519 密钥和不联网的虚构地址，可信 Authenticode 分支只模拟返回值，不创建自签名证书冒充发行签名。测试重点覆盖白名单遗漏、占位地址、错误签名、过期、版本/摘要/URL 不符、运行时或原生变化，以及未签名人工预览版的警告。

交接保留公开产物的大小和 SHA256、源文件白名单、前后版本、组件兼容清单、实际测试命令和限制。对外只选择审阅过的产物；不要整体公开本地候选、私钥或内部回执目录。


## 应用内自动检查和一次开始更新

启动后后台检查，常驻托盘时低频复查；前端刷新本地状态不会触发每次联网。新版本通过不抢焦点的提示展示，可选择“稍后提醒”，手动检查入口继续保留。点击一次“更新并重启”后，依次下载、复核、等待当前任务、退出、安装和重新启动；开始更新不等于允许中断问答、导入或索引写入。

准备安装时关闭新任务入口，等待已经接受的请求、管家任务、导入、模型下载、索引及后台归档写入自然完成。停机标记绑定启动 ID、随机事务 token、程序与控制目录及有效期；服务和原生壳协作退出。确认回执不能替代核对真实进程退出，超时或仍有占用时不强杀、不替换文件。旧外壳不支持握手时应安全失败。

自动完整升级面向由本安装器注册的 `jiedu-application-v1` 普通用户安装，不能把自包含知识工作区当成可整体替换的程序目录。现有 `jiedu-self-contained-v1` 保持布局与资料原位；需要完整升级时先单独安装新版程序，再连接原工作区。工作区位于安装目录内或目录归属无法确认时拒绝自动替换，并给出迁移指引。保全验收还需确认原资料路径和工作区绑定仍然有效。

### 老用户如何更新

优先打开原来的界渡，在设置中检查更新。受支持的完整升级会先等待任务结束、保留上一份安装，再安装新版并核对启动状态。手动下载的安装器只接受新的空目录；即使目录含合法安装标记，也不会直接覆盖旧程序。内置升级助手已经将旧目录移到回退位置后，安装器仍可正常安装到原位置。

如果旧版不能使用内置升级，请选新的空文件夹安装，再连接原工作区，保留旧安装目录。原知识和归档进度留在工作区；连接密钥、本机设置可能保存在旧程序控制目录，外部 AI 的配置也可能仍指向旧路径。连接原工作区不等于这些设置已自动迁移，需要按接入指引核对身份、原有权限和采集状态，再开始使用。不要把“需要核对连接”当成清空进度或重新采集的理由。

安装失败保留上一版及事务记录；新实例未健康或仍有进程时不强杀回滚，展示恢复入口。恢复同样要等任务和占用安全结束。健康成功事务的回退材料有界保留，失败、待恢复或无法核验归属的材料不能混入自动清理。

1.1.9 候选须让安装器、源码、原生版本和签名清单一致，再核对旧版升级的资料、权限与同步进度保全。`releases/latest` 不应被当作预发布 rc 的固定入口；本地候选和离线核验不代表 1.1.9 已公开。
