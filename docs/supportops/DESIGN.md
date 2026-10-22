# SupportOps 架构、API 与实施顺序

日期：2026-10-19；P5c-3 完成真实开发集对照，P5d-1 冻结保留集，P5e 显式真实网页入口已完成模拟验证；没有生产认证或外网部署。

## 应用边界

```text
工作台：文档 / 问答 / 原文引用 / 反馈与运行记录
    ↓ 已认证的请求与服务端空间成员校验
应用服务：文档版本、索引任务、会话、回答、反馈、审计
    ↓ 显式注入依赖
HelloAgent：DocumentKnowledgeBase、Agent、ContextBuilder、RequestBudgetManager
    ↓
业务数据库 + 文档原件 / Qdrant 向量索引 / 应用注入的 Embedding 和 LLM
```

HelloAgent 继续保持可单独使用的库；业务账号、上传接口和 HTTP 错误映射放在独立应用层。
`supportops/` 已承载 P0 独立基线、P1 空间/成员权限，以及 P2 有界文本接入、版本任务与权限检索。
具体见 [P1 说明](P1_WORKSPACES.md) 和 [P2 说明](P2_DOCUMENTS.md)；这两阶段没有 Web 服务或生产认证，
演示档案不能用于对外登录。P3 新增私有会话、回答/引用、反馈、运行记录及 JSON API，详见
[P3 说明](P3_CHAT_API.md)；API 接受注入的身份验证器并拒绝演示身份，没有附带可部署的生产登录。
Neo4j 在有真实服务依赖或故障关系需求后接入，不作为首版问答的强制依赖。

本地演示可使用单写者 SQLite；多人部署的业务数据库应使用支持并发事务的持久化服务。
文档原件独立于索引保存，向量索引可重建，不能作为唯一事实源。当前本机版本用 SQLite BLOB
保存原始字节，块和向量另表存储；未来外部对象存储需重新设计跨存储提交与清理补偿。

## 数据实体

| 实体 | 核心字段/约束 |
| --- | --- |
| Workspace / Membership | 空间、已验证用户、角色；由服务端控制身份 |
| Document / DocumentVersion | 稳定 ID、空间、版本、原件位置、摘要、当前索引版本 |
| IngestionJob | 任务 ID、幂等键、文档版本、pending/running/succeeded/failed/cancelled |
| Conversation / Message | 空间、所有者、完整轮次、成功提交时间 |
| Answer / AnswerCitation | 回答状态、引用的版本/块/字符范围、运行 ID |
| Feedback | 回答 ID、用户、有用/无用、原因；唯一键允许更新 |
| RunRecord / AuditEvent | request_id、权限结果、模型/检索版本、耗时、usage 来源、脱敏错误码 |

引用记录保存回答时的版本与片段；文档更新后不能把旧引用悄悄指向新内容。
删除遵循空间策略清理原件、正文块、向量和必要关联；已有回答标记引用失效，不能绕过权限
返回删除后的敏感快照。外部 API 不返回本机绝对文件路径。

## 拟定 API

| 方法与路径 | 行为 |
| --- | --- |
| GET /healthz | 进程健康，不暴露密钥或底层异常 |
| GET /readyz | 必需依赖就绪状态；探测失败返回受控错误 |
| GET /api/workspaces | 当前身份有权访问的空间 |
| GET /api/workspaces/{id}/documents | 有权限的文档及处理状态 |
| POST /api/workspaces/{id}/documents | 有界上传，返回 202 和 job_id；支持幂等键 |
| POST /api/workspaces/{id}/documents/{doc}/versions | 创建新版本和索引任务，成功后切换 |
| DELETE /api/workspaces/{id}/documents/{doc} | 当前经授权与确认后同步删除原件/索引，重复请求幂等 |
| GET /api/workspaces/{id}/jobs/{job} | 查询状态，不暴露上传内容 |
| POST /api/workspaces/{id}/jobs/{job}/retry | 只重试可重试的失败任务 |
| POST /api/workspaces/{id}/conversations | 建立空间内的会话 |
| POST /api/workspaces/{id}/conversations/{chat}/messages | 服务端校验所有权，返回 answered/insufficient_evidence |
| GET /api/workspaces/{id}/answers/{answer}/citations/{label} | 校验当前权限后返回对应版本原文片段 |
| PUT /api/workspaces/{id}/answers/{answer}/feedback | 关联回答提交或更新反馈 |
| GET /api/workspaces/{id}/runs/{run} | 有权限的运行诊断；默认不返回原始提示词 |

统一错误包含 code、message、request_id。400 为格式/限额错误，401 为未认证，403/404 为
访问拒绝，409 为幂等或版本冲突，413 为文件超限，429 为限流，503 为必需依赖不可用。
命中向量分数不能直接作为答案置信度返回给用户。

## 两条验证路径

离线路径：项目整理资料 → 确定性 HashingEmbedder → 内存向量库 → 重排 → 原文引用。
用于检查摄取、召回和引用，不调用模型，也不提供生成式答案或有效权限服务。

P3 应用路径：身份与会话所有权 → 当前版本检索 → ContextBuilder → RequestBudgetManager →
注入的 invoke() 客户端 → 严格抽取式原文/引用校验 → 再查权限与资料 → 整轮提交 → 反馈/审计。
SimpleAgent/工具将在有明确工具场景时再接入，不在每个问答中强制运行多范式或多 Agent。
模型错误、预算不足、索引失败与“没有证据”是不同状态，不能全部返回“没有相关资料”。

## 可直接实施的任务清单

| 顺序 | 任务 | 完成条件 |
| --- | --- | --- |
| P0（已完成） | 需求、页面草图、10 份资料、30 个场景、离线检索基线 | 清单能加载；来源可核对；基线可重复；待办不算通过 |
| P1（本机服务已完成） | 应用数据库、演示身份、空间与成员校验 | 空间/成员/审计入口以空间和可信适配器身份过滤；跨空间测试通过 |
| P2（服务与 CLI 已完成） | 文本字节接入、持久化任务、原件与版本 | 重启、重试、幂等提交、更新失败和删除已验证；空间检索/版本引用已接权限，无 HTTP 上传 |
| P3（服务/CLI/WSGI 已完成） | 私有会话、引用回答、反馈、运行记录 API | Fake LLM 验证协议/失败不提交历史；引用绑定版本，语义拒答/真实模型效果未评测 |
| P4（本机工作台已实现） | 工作台、受控本机启动与工程验收 | 浏览器文档到反馈流程通过；APP-02/03/05 通过，APP-01/04 真实模型语义部分待 P5 |
| P5a（已完成） | 独立回答 smoke、可注入索引和显式运行时 | 32 项基线与失败样本；引用/事实/拒答/污染分开；无真实外部调用 |
| P5b（SDK 适配已离线验证） | Chat/Embedding SDK、用量、限额与工厂 | Fake SDK / 实际 SDK 内存传输通过；未调用真实服务 |
| P5c-1（离线预检已完成） | 无密钥配置、1～3 题与摘要绑定、请求预算 | 无环境/SDK/网络；报告明确未授权执行，不冒充金额上限 |
| P5c-2（三题真实试跑完成） | 隔离 SDK、专用凭据、发送前预算与持久化记录 | deepseek-flash 三题 3/3，同批 Fake 1/3；首轮结果丢失如实保留，金额为保守估算非账单 |
| P5c-3（完整对照已完成） | 完整题集对照、持久化总预算 | deepseek-flash 30/32、Fake 20/32；保留两题失败，累计预留 3.716160/5 元，不复用旧 grant，无在线 Embedding |
| P5d-1（离线实现） | 失败复核、完整性候选与冻结保留集 | 原 gold 与 30/32 基线不改；v2 显式可选；新语料12题未生成答案，新版真实效果待验证 |
| P5e（模拟链路已验证） | 显式真实模型本机网页、公开数据和费用边界 | 默认 Fake 不变；专用公开种子库、外发确认、隔离 worker、共享累计账本；尚未从网页实际请求供应商 |
| P6a（最小工单闭环已实现） | 本人私有工单、引用建议、人工采纳/接管与解决记录 | 模拟服务/浏览器闭环已验证；schema v4附加迁移；不自动执行或关闭外部工单 |
| P6b（待实施） | 只读诊断、案例沉淀与团队流程 | 工具权限明确；幂等；人工确认后生成待审核知识，不由模型直接发布 |

P0～P4 的离线与本机演示不替代上线验收。WSGI 路由与本机工作台见 [P4 说明](P4_WORKBENCH.md)，
仅允许 127.0.0.1 随机会话，无生产认证或常驻 worker；真实模型效果仍需分别授权和评测。
当前不安装第三方服务、不修改原有日报计划任务。
P5a 使用方法与基线见 [回答评测](P5_ANSWER_EVALUATION.md)；不替代 P4 业务事务/页面验收。
P5b 适配范围与 v2 排序修复见 [SDK 说明](P5_SDK_ADAPTERS.md)，不能把离线 SDK 测试当真实兼容性结果。
P5c-1 见 [离线预检](P5_PREFLIGHT.md)。成功路径的分批计划不包含失败后的重建尝试，
实际运行仍需硬限额；预检摘要不能充当运行授权或 DNS/TLS/地域保证。
P5c-2 的官方真实端点、三题结果、初轮编码故障、回执和费用边界见 [真实试跑说明](P5_LIVE_PILOT.md)。
P5c-3 的完整对照、两个失败、累计账本和恢复约束见 [完整真实评测](P5_FULL_REGRESSION.md)。
