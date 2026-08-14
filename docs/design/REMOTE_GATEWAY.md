# Remote Gateway 设计

## 角色

Remote Gateway 是现有 RPC v2 Host 外侧的传输与信任边界。它不拥有第二个 Agent Loop，
不直接执行 Tool，不自行回答 Confirmation，也不持久化第二份对话。一个 Gateway 进程只持有
一个 `CodingHarness`、一个 Workspace、一个 Confirmation Broker 和一个 Event Stream。

```text
TLS / trusted proxy
  -> device authentication
  -> scope authorization
  -> control lease
  -> RPC v2
  -> CodingHarness
  -> Policy / Confirmation / Session / Trace
```

## 信任模型

本机初始化的 Host 拥有受保护的管理密钥和稳定 Host ID。设备使用 P-256 身份。公网配对只会
创建 pending request；批准、Scope 修改和撤销只能通过 challenge-HMAC 认证的本地 IPC 完成。
获批设备仍以当前用户权限操作，Remote 不是 OS 沙箱。

Scope 固定为：

- `observe`：读取 Runtime 状态和可能敏感的 Event。
- `control`：包含 observe；持有唯一控制租约时才能请求 Run 操作。
- `confirm`：对既有 Confirmation 作 revision-bound 响应，与 control 独立。

Policy 始终是唯一执行裁决链。设备不能直接调用 Tool、创建 Confirmation、覆盖 `block`、
批准工件或修改 Host 配置。

Remote Frame、管理 IPC、Host/Device Store 和配对安全状态使用严格版本化 Codec。未知字段、
重复 JSON key、非有限数值和布尔值伪装的整数 schema version 均 fail closed。Challenge、
Lease 与持久安全状态时间必须带显式 UTC offset。Pairing State 恢复会重新验证精确字段、
规范 ID/Scope、正 revision、时间顺序、pending 身份唯一性、approved Request 与 Device 的
一一对应，以及 P-256 JWK 与 fingerprint 绑定，不信任落盘派生值。

## 网络边界

非 loopback 直连必须使用 TLS 1.2 或以上。生产环境推荐由反向代理、WAF 或 Tunnel 终止 TLS
并承担体量型防护，EvoPi 仅监听 loopback。Forwarded 地址只信任显式 CIDR；Host 总是通过
allowlist 校验，浏览器 Origin 必须精确匹配。

实现固定为单进程、单 worker。进程内限流与有界队列缓解应用层滥用，但不宣称抵御体量型
DDoS。

## 生命周期与恢复

30 秒控制租约避免多个远程控制端同时修改一个 Run。控制端断线不终止 Run；同一设备可以
接管未过期租约，其他设备需等待过期。观察连接可按有界 Cursor 重连回放。网络结果未知时，
客户端不得自动重放带副作用请求。

Gateway 重启会创建新 Event Stream。Session v4 与 Trace v2 仍是持久事实；Remote 不增加
持久 Event Store。

## Audit

安全操作写入脱敏、append-only 的 SHA-256 摘要链，并按天或 50 MiB 分段。Prompt、消息正文、
Tool 参数、凭据、签名和 Provider State 禁止写入。原始客户端 IP 位于独立受保护 sidecar，
30 天后删除；永久链只保留 IP 摘要。Audit 失效时 Gateway 进入 not-ready、断开远程连接并拒绝
后续安全操作。敏感字段检查递归覆盖 JSON 数组与对象；追加操作在文件锁内检测其他实例更新
的链头，避免虽然串行写入却使用过期 `previous_hash`。

## 客户端与控制台

Python Client 组合 `EvoPiRpcClient`；TypeScript package 使用浏览器 Web Crypto 实现同一
Remote/RPC v2 协议。Remote 关闭只关闭认证传输，不发送被公网边界禁止的 RPC `shutdown`；
关闭时所有已发送但未决的 Remote 请求以 outcome-unknown 结束。可选控制台使用 IndexedDB
不可导出密钥、纯文本渲染和严格 CSP。

完整契约见 [Remote Protocol v1](../REMOTE_PROTOCOL_V1.md)，部署样例见
[`docs/deployment/remote`](../deployment/remote/README.md)。
