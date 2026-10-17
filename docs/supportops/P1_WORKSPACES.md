# P1：持久化空间、演示身份与成员权限

日期：2026-10-15；状态：本机 CLI 与应用服务已实现；无 HTTP 服务，无生产认证。

本文记录 P1 的阶段状态；P2 已补充文档与索引流程、空间检索和 v1→v2 迁移，当前行为以
[P2 文档说明](P2_DOCUMENTS.md) 为准，下面“预留表/尚未接入检索”等描述属于 P1 当时的边界。

## 已实现

- 独立 SQLite 应用库，与框架的记忆/正文表分开；记录 application_id 和 schema_version。
  重启复用数据；不识别的数据库或版本直接拒绝，不覆盖、不降级，也没有自动迁移器。
- 可信 IdentityProvider 适配器给出操作者身份，服务再检查本地账号启用状态与实时空间成员关系。
  本次提供公开的 DemoIdentityProvider；另用 Fake 验证可替换契约，没有实现外部身份认证。
- 空间创建、当前成员可见空间查询、成员角色设置/移除、管理员交接和空间内审计查询。
- reader/editor/admin 三种空间角色；editor 的文档写权限在 P2 接入，本阶段不提供上传。
- 成员撤销后下一次调用立即失去访问权限。重复演示初始化不恢复已撤销的成员，不重置角色。
- 权限检查和写操作处于同一短事务；成员变更与审计一起提交，审计失败则回滚变更。
- 最后一名启用中的管理员不能被降级或移除；使用两个独立连接同时降级的测试也保留一名。

当前权限表：

| 操作 | reader | editor | admin |
| --- | --- | --- | --- |
| 查看自己有成员关系的空间 | 允许 | 允许 | 允许 |
| 查看/设置/移除空间成员 | 拒绝 | 拒绝 | 允许 |
| 查看空间成功写入审计 | 拒绝 | 拒绝 | 允许 |
| 访问没有成员关系的其他空间 | 拒绝 | 拒绝 | 拒绝 |

已启用账号可以创建自己的新空间，创建者成为新空间管理员。这不会改变其在旧空间的角色。
admin 不是全局超级用户；“某空间的管理员”不意味着能查看其他空间。

## 身份和安全边界

```text
本机命令选取公开演示档案
→ IdentityProvider 返回 Identity.subject
→ 数据库检查账号启用状态
→ workspace_id + subject 查询成员关系
→ 检查操作所需角色
→ 在同一事务中读取或变更，并记录成功写入审计
```

操作者来自适配器，不接受请求正文的 user_id、owner_id 或角色声明；请求额外字段被拒绝。
成员设置请求中的 member_id 是被操作对象，不是操作者。每次调用重新读数据库角色，不缓存
调用者自报的角色。无成员关系与空间不存在统一返回 NOT_FOUND；空间内角色不足返回 FORBIDDEN。

演示档案 admin/reader 是公开选择器，不是口令，拥有本机 CLI 的人可以主动切换。
因此这只是权限服务逻辑的演示，不能抵御本机操作者伪装身份，也不能作为多用户登录系统。
IdentityProvider、ApplicationDatabase 及其 transaction/bootstrap 方法属于可信应用内部接口；
不能把它们直接暴露成外部工具、请求端点或让 LLM 选择身份。SQL 注入测试通过不等于完整安全审计。

对外部署前必须接入实际验证凭据的身份适配器、服务端会话、速率限制与部署防护；不能简单
把 demo profile 从请求头传入后就当作登录成功。当前没有网络监听器，也不会读取环境配置。

## 数据约束与后续文档功能

现有表：users、workspaces、memberships、audit_events、app_meta，以及预留的 documents、
document_versions、ingestion_jobs。预留表已经建成，但没有文档上传、任务执行或发布接口。

文档使用 (workspace_id, document_id) 标识；版本与任务的复合外键同时约束空间、文档、版本。
current_version_id 不能指向其他空间或其他文档的版本。版本号在文档内唯一，任务幂等键在空间内
唯一，状态和尝试次数有数据库校验；完整任务状态迁移、失败重试和请求摘要核对属于 P2。

短事务采用 SQLite BEGIN IMMEDIATE 与进程内锁；这可保护本阶段权限变更，不是分布式任务
调度或租约。审计只记录本服务成功的写入事件和对象 ID，不保存正文或凭据；失败/拒绝访问审计
与 request_id、HTTP 错误响应将在后续应用层补充。

## 本机演示

从仓库根目录运行，所有 demo 参数放在最后的动作名之前：

```powershell
cd D:\code\Hello-agent
D:\conda-envs\helloagent\python.exe -m supportops demo --enable-demo --profile admin init
D:\conda-envs\helloagent\python.exe -m supportops demo --enable-demo --profile admin workspaces
D:\conda-envs\helloagent\python.exe -m supportops demo --enable-demo --profile reader workspaces
D:\conda-envs\helloagent\python.exe -m supportops demo --enable-demo --profile reader workspace internal-lab
```

前两条初始化并列出 helloagent/internal-lab；reader 只能看到 helloagent，最后一条预期返回
NOT_FOUND 和非零退出码。不是故障，而是对越权请求的拒绝。退出码 0 表示成功，2 表示受控错误。
每次输出都注明“本地演示身份，非企业认证”。默认不启用 demo；省略 --enable-demo 会拒绝且不建库。

默认数据库：outputs/supportops/application.sqlite3。CLI 只允许将 .sqlite3 数据库放到仓库 outputs
内；文件被 Git 忽略，不应加入仓库。库中已经创建两个人工演示空间和两名用户；空间中尚未导入文档。
数据路径不作为外部响应返回，文件也未做加密；真实业务数据还需独立 ACL、备份和数据治理。

如需验证角色变化，可在另一个演示数据库中操作，不影响默认演示状态：

```powershell
D:\conda-envs\helloagent\python.exe -m supportops demo --enable-demo --profile admin --database outputs/supportops/roles.sqlite3 init
D:\conda-envs\helloagent\python.exe -m supportops demo --enable-demo --profile admin --database outputs/supportops/roles.sqlite3 set-member helloagent demo-reader editor
D:\conda-envs\helloagent\python.exe -m supportops demo --enable-demo --profile admin --database outputs/supportops/roles.sqlite3 audit helloagent
```

P0 的 search/evaluate 仍使用固定公开语料，与本数据库分开，尚未接入空间权限；不是支持多用户的
检索接口。P0 的 5 项应用验收继续标记 pending_application，不能因空间单元测试通过而宣称检索、
引用和运行记录的跨空间隔离已完成。

## 验证与下一步

新增 33 项 P1 专项测试，覆盖角色、跨空间拒绝、撤销、伪造字段、存储故障回滚、重启、幂等初始化、
模式识别、复合外键和双连接竞争；与 P0 合计 50 项。测试使用临时本地 SQLite 和 Fake/演示身份，
不调用真实 LLM、Qdrant、Neo4j 或 SMTP，也不依赖密钥。

下一阶段 P2：在上述权限入口之后实现 Markdown/TXT 文件接入、原件存储、持久化任务、版本发布、
失败恢复及删除。检索范围必须绑定已授权空间，新旧版本切换不能仅依赖未经隔离的框架 ingest。
