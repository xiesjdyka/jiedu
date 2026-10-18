# Windows 设备访问与恢复

Current workspace decision (2026-09-21): encryption and Owner confirmation are disabled in the visible product. No encrypted objects existed; the old encrypted configuration and backups are retained unchanged but have no restore entry in the ordinary interface. Opening the local Console creates an ordinary same-origin management session automatically. No password, Windows identity check, key migration, or repeated Owner confirmation is part of normal use. The custody mechanisms below apply only if protected storage is deliberately reintroduced in a future product decision.

默认本机管理使用当前 Windows 账户，受保护存储使用 DPAPI CurrentUser 托管。

## 历史实现说明：身份与接口

以下内容仅记录停用前的保护层实现，当前普通模式不会进入这些流程。

页面先取得 CSRF challenge，再向 `device-session` 发起同源 JSON 请求。服务端根据实际 TCP 对端四元组查询进程，读取进程 TokenUser，与服务进程账户比较，并再次检查连接归属；不使用请求头声明的账户。Console 只监听 IPv4 回环地址，禁用代理头处理；设备引导拒绝转发头和客户端 Authorization。OS 核验失败时不回退为允许。

设备浏览器会话独立于数据解密，不设口令倒计时。服务退出或用户手动锁定清除内存会话；刷新/重启后页面自动重新核验。管理写入仍需会话和 CSRF，同源检查不能替代会话。MCP/HTTP 客户端仍走自己的身份、范围策略、撤销及审计。

此设计信任当前 Windows 账户下的本机程序，不声称抵御同账户程序、管理员、恶意本机代理或进程内存访问。远程网页不能读取 CSRF，也不能依靠伪造账户字段获得本机会话。

## 历史实现说明：密钥迁移

“未开启”是当前普通运行状态。普通界面不再提供加密启用或历史配置恢复入口；现有历史配置仍原样保留，只有未来作出新的产品决定并完成复核后才会重新接入。

已有 `keyring.json` 的口令槽、恢复槽及对象密文保持不变。解密后的数据密钥与恢复槽绑定后由 DPAPI 封装，验证回读成功，再原子保存到本机数据目录的 `device-keys/<workspace-id>.json`。已有托管文件更新前保留加密副本。无明文数据密钥、口令或恢复密钥落盘。

旧数据迁移入口当前停用。普通管理不读取旧口令或恢复密钥，也不会触发设备托管。

跨设备、重建 Windows 账户或 DPAPI 材料损坏时，使用原口令/恢复密钥重新托管。没有实际整机重启及不同真实账户的现场验收时，不把隔离进程重启/模拟失败测试描述为这两种验收。

## 历史实现说明：手动锁与回退

设备模式中可设置额外手动锁口令。启用后持久化为手动模式，刷新或重启不会自动解锁；原恢复密钥仍有效。解锁后选择“关闭额外手动锁，恢复设备模式”可重新托管。删除托管文件不是关闭手动锁的必要步骤，也不得删除旧 keyring 或密文。

代码回退到旧版本时保留原密文、keyring 和恢复材料即可通过旧口令/恢复路径读取；旧版本不理解设备托管时会要求手动解锁。正常服务退出不改写密文。

## 验证入口

- `tests.test_device_access`：真实 DPAPI、TCP 账户核验、设备重启、错误托管恢复、手动模式持久化、密文不变及对象读写。
- `tests.device_access_protocol`：隔离工作区真实 HTTP 服务，验证 Origin/代理/管理会话、客户端撤销和进程重启。
- `tests.device_access_real_reload`：正式启动器重载前检查活动任务及索引/候选/Writer 锁，保留加密备份，核验真实工作区与旧密文哈希。
