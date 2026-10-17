# P3：私有会话、引用回答、反馈与 JSON API

日期：2026-10-16；状态：持久化应用服务、CLI 和 WSGI 适配器已实现；未启动网络服务，未接真实模型或生产认证。

以上及下文是 P3 阶段快照。2026-10-17 P4 已增加本机工作台、受控启动器和随机 local_session
凭据，API 默认仍拒绝该模式，只有本机外壳显式开启。当前网页/API 扩展与验收见 [P4 说明](P4_WORKBENCH.md)。

## 本阶段完成内容

- 私有会话与整轮历史持久化：空间成员关系和会话所有者同时校验，空间管理员也不能查看其他成员的私有对话。
- 引用回答：临时检索证据经过 ContextBuilder 和完整请求预算进入注入的 invoke() 客户端，引用严格绑定资料版本。
- 成功提交：答案、引用、user/assistant 两条消息、会话 revision、运行成功状态和审计在同一事务提交。
- 失败隔离：无效模型输出、编造引用、索引故障、预算不足、权限撤销、资料变化都不提交半轮历史。
- 反馈更新：只允许会话所有者对真实存在的回答提交/更新自己的反馈，不接受客户端指定 user_id。
- 脱敏运行记录：request_id、模式、调用尝试、估算 Token、耗时及稳定错误码；不保存完整提示词或底层异常。
- JSON API：基于标准 WSGI 的薄适配器，可注入身份、数据库和模型；无新增依赖、无自动网络监听。

数据库版本升级到 v3；已知 v1/v2 以附加 DDL 迁移，不重建旧表、不重置成员权限，失败回滚 DDL 与版本号。
新增 conversations、answer_runs、answers、answer_citations、chat_messages、answer_feedback。

## 问答流程与一致性

```text
验证空间和会话所有者 → 幂等键/完整问题摘要 → 记录 running 执行
→ 获取最近完整历史与空间发布版本快照 → 当前版本检索
→ ContextBuilder 选取完整证据（不压缩 JSON）→ RequestBudgetManager 裁剪历史副本
→ 再查权限/资料快照并记录调用开始 → invoke()
→ 严格 JSON / 原文片段 / 引用标签校验
→ 再查权限、版本、执行期限与会话 revision → 原子提交整轮
```

快照保守地覆盖整个空间的当前发布版本。生成过程中即使另一份资料发布，也会返回 SOURCE_CHANGED，
要求新请求重试；本阶段不做精细的依赖级失效计算。已经发往外部模型的内容也不能在权限撤销后撤回。

同会话同时只允许一个 running 执行；相同幂等键与同问题返回已有答案，不重复调用客户端。
同键不同问题或尚在进行返回 CONFLICT。失败请求的同键不自动重放，要重试时使用新键。
执行期限为 300 秒，无心跳；崩溃执行在该会话下一次请求时标记 RUN_EXPIRED，旧执行者无法晚到发布。
这不是跨模型服务的 exactly-once 保证，也不自动退还已经发生的调用费用。

每个空间每用户最多 100 个会话，每会话最多 100 个成功轮次/200 个执行记录；模型默认只加载最近
12 个完整轮次，预算管理仍可裁剪副本。数据库完整历史不会因本轮裁剪而被改写。

## 当前“回答”到底是什么

这是严格抽取式回答协议，不是自由生成的故障诊断。模型客户端只能返回：

```json
{"status":"answered","passages":[{"label":"D1","quote":"资料中的连续原文"}]}
```

服务端验证标签确实来自本轮送入客户端的证据、quote 逐字存在于对应片段，然后组装带引用的回答。
不支持的引用、空 answered、重复键、NaN、额外字段等会失败。协议也允许：

```json
{"status":"insufficient_evidence","passages":[]}
```

没有召回资料时不调用客户端，直接返回资料不足；有相近资料不意味着有足够证据，客户端仍应拒答。
上述检查只证明“摘录确实出自当前资料”，不证明“摘录足以回答问题”，不替代语义支持性或准确率评测。

CLI 明确使用 DemoQuoteLLM（fake_extractive），它只是确定性选择首个片段的程序，不具备语义判断或
模型生成能力；即使命中了不相关的资料，也可能摘出它。因此 CLI 的 answered 不能当成真实回答正确。
所有 Fake 测试验证协议和工程边界，不验证真实模型的拒答能力或提示注入鲁棒性。

## 引用更新与隐私

引用记录只保存空间、文档、版本、块与标签，不存一份可绕过删除的引用正文快照。
读原文时重新经过 DocumentService 权限与删除检查。

- 文档更新：旧回答可显示，标记 historical；引用仍指原版本，不悄悄换成新文本。
- 送入新一轮模型：旧版本的历史回答替换为“资料已变更”，不让旧正文作为当前事实重入提示词。
- 文档删除：答案接口、会话历史和幂等重放隐藏依赖该资料的整个回答；引用返回 SOURCE_GONE。
- 成员撤销：会话、回答、反馈、运行记录和引用都无法再访问；管理员身份不绕过私有会话所有权。

隐藏不等于物理擦除。数据库中历史答案、用户自己输入的问题、反馈和审计仍按保留策略保存，
需要进一步的数据删除/备份治理；不能宣称“删除文档已经取证级清除了全部关联数据”。

## 运行记录

model_attempts 表示开始尝试 invoke()，不是供应商确认的计费次数。开始前先持久化标记，即使进程中断
也不把已开始的调用写成“未调用”。usage_source 为 local_estimate 或 not_called；actual_usage/cost
保持 null，因为当前 invoke() 契约没有接入供应商 usage。估算也不等于服务真实 Token 数。

API request_id 由服务端生成并关联新运行；幂等重放返回已有运行，原始运行保留第一次请求的 request_id。
执行失败的安全错误包含 run_id，所有者可以查询脱敏运行记录。权限检查之前拒绝的请求没有运行记录。
耗时使用本地单调时钟，失败阶段也记录耗时；未进行线上 P95、成本、吞吐或供应商故障验证。

## 直接用 CLI 验证

项目已有 P2 的 10 份公开种子资料；新副本先按 P2 导入并执行索引任务。PowerShell：

```powershell
cd D:\code\Hello-agent
$supportOpsPython = "D:\conda-envs\helloagent\python.exe"
$supportOpsChat = (& $supportOpsPython -m supportops demo --enable-demo --profile reader new-chat helloagent --title "研发支持演示" | ConvertFrom-Json).result.id
& $supportOpsPython -m supportops demo --enable-demo --profile reader ask helloagent $supportOpsChat "扫描版 PDF 为什么无法检索？" --key question-1
& $supportOpsPython -m supportops demo --enable-demo --profile reader chat helloagent $supportOpsChat
```

相同 question-1 和相同问题会重放已有答案；换问题应换键。返回 answer_id（id）、run_id 和引用标签后：

```text
demo ... answer helloagent <answer_id>
demo ... answer-citation helloagent <answer_id> <label>
demo ... feedback helloagent <answer_id> useful
demo ... feedback helloagent <answer_id> not_useful --reason irrelevant
demo ... run helloagent <run_id>
demo ... chats helloagent
```

仍然没有网页；CLI 每次都输出公开演示身份警告。记录中的 fake_extractive 是演示模式，不需要 API Key、
Docker 或联网，不发送邮件。请勿在终端参数中放敏感问题或资料。

## API 适配器的边界

SupportOpsAPI(database, identity_provider, engine) 是 WSGI callable，构造它不会绑定端口。
已用直接请求和标准库 WSGI validator 验证基本响应，未部署 HTTP 服务或完成生产网络安全验证。
公开 DemoIdentityProvider 在构造时被拒绝，返回 local_demo 的身份也被拒绝；业务接口只接受
Authorization: Bearer ...，交给注入适配器实际验证。external 标记本身不是凭据验证，不能伪造
一个永远返回管理员的适配器当作生产认证。测试仅使用 FakeIdentities，没有可部署的真实登录实现。

已支持的接口：

| 路由 | 行为 |
| --- | --- |
| GET /healthz、/readyz | 进程响应与本地数据库探测；不是 LLM/索引供应商健康承诺 |
| GET /api/workspaces | 当前成员可见空间 |
| GET/POST /api/workspaces/{ws}/documents | 列表 / JSON 文本接入，POST 返回 202 任务，不是 multipart 文件上传 |
| GET/DELETE /api/workspaces/{ws}/documents/{doc} | 文档状态 / 确认删除（正文必须为 {"confirm":true}） |
| POST /api/workspaces/{ws}/documents/{doc}/versions | 新版本 JSON 文本接入 |
| GET /api/workspaces/{ws}/jobs/{job}、POST .../{job}/retry | 任务查询与显式重试；无公开 worker 执行端点 |
| GET/POST /api/workspaces/{ws}/conversations | 本人会话列表 / 建立私有会话 |
| GET /api/workspaces/{ws}/conversations/{chat}、POST .../{chat}/messages | 本人历史 / 提交问题 |
| GET /api/workspaces/{ws}/answers/{answer}、GET .../{answer}/citations/{label} | 本人回答 / 经重新鉴权的原文 |
| GET/PUT /api/workspaces/{ws}/answers/{answer}/feedback | 本人反馈查询与更新 |
| GET /api/workspaces/{ws}/runs/{run} | 本人脱敏运行记录 |

正文必须为 JSON 对象，最大 300000 字节，拒绝重复键/NaN；文档原件仍限 256000 字节。
列表仅提供有界 limit，尚无分页游标。响应带 request_id、no-store 和 nosniff，错误不会返回 traceback。
尚无登录、Cookie 会话、速率限制、TLS、CORS、网页、生产服务器启动器或外部存储/模型的就绪探测。

## 验证与下一阶段

新增 36 项测试，SupportOps 四阶段共 124 项；全量 503 项中 499 项通过、4 项真实存储默认跳过。
实际多个 CLI 进程验证了建会话、带引用回答、同键重放、整轮历史、原文读取和运行记录；管理员读取
reader 私有回答得到 NOT_FOUND；真实外部模型调用为 0。没有修改环境凭据或部署网络服务。

下一阶段 P4：工作台、可控本机演示启动方式、API/页面衔接与端到端应用验收。真实认证与部署安全
必须单独完成，不能直接把 CLI 的公开 profile 映射到网络请求；真实模型质量另走 P5 授权评测。
