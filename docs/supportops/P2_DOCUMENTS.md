# P2：文档接入、持久化任务与版本发布

日期：2026-10-15；状态：应用服务和本机 CLI 已实现；没有 HTTP 上传、网页或真实模型调用。

本文记录 P2 阶段状态。2026-10-16 的 P3 已补充私有问答/反馈/WSGI JSON 接口与 v3 迁移，
当前能力见 [P3 说明](P3_CHAT_API.md)；P2 的“未接 API”等表述属于当时边界。

## 现在可以做什么

接收 UTF-8 的 Markdown/TXT 字节，持久化原件、创建文档版本与索引任务；显式运行 worker
后，检索空间内已发布的资料，读取版本绑定的原文引用。支持更新、失败重试、崩溃后的任务
重领和删除。reader 可读，editor/admin 可接入资料、执行任务、重试和删除；权限沿用 P1。

这是本机应用功能，不是生产认证。admin/reader 仍为公开演示档案，不得对外暴露。

## 数据流与设计取舍

```text
验证身份、空间成员、写入角色
→ 校验文件名、UTF-8、大小和幂等键
→ 同事务保存原始字节 + 待处理版本 + pending 任务 + 审计
→ worker 领取任务，保存 token 和超时时间
→ 读取原件并核对 SHA-256
→ 写事务外切块、Embedding、校验片段与向量
→ 再次校验权限、领取 token、期限和当前版本
→ 同事务写入索引 + 切换当前版本 + 标记成功 + 审计
```

本版选择 SQLite BLOB 表保存原始文件字节，与 indexed_chunks 索引表分开，不把向量当作唯一
事实源。它让原件、版本、任务与删除都处于同一数据库事务中，避免首版就引入文件/数据库的
双写不一致。OriginalStore 接口可注入；替换成 S3/MinIO 等外部存储时必须重新设计提交和清理
补偿，不能继续声称所有后端原子提交。文件名只作元数据，不作为本机路径打开。

索引复用 HelloAgent 的 DocumentChunker、HashingEmbedder、InMemoryVectorStore 和
HybridReranker。块和向量序列化后按空间/文档/版本保存到 SQLite，查询时仅装载当前发布的块。
块 ID 额外包含空间/版本命名空间；索引 profile 固定编码器版本、维度和切块参数，不匹配时报错，
不混用向量空间。仍是离线特征哈希基线，不是训练过的语义模型，也不是 Qdrant 在线索引。

## 状态与故障行为

| 情况 | 结果 |
| --- | --- |
| 重复幂等键、相同完整请求 | 返回原任务，不新建文档/版本；成功任务也可重放 |
| 同一空间内幂等键绑定不同内容/标题/文件名/目标文档 | CONFLICT，不覆盖旧请求 |
| 首次索引尚未完成 | 文档可列出，但不进入检索 |
| 新版本失败 | 任务 failed，旧发布版本和标题不变 |
| 新版本成功 | 索引、当前版本、标题与任务状态同时提交 |
| 老任务晚于已发布新版本完成 | cancelled / SUPERSEDED，不回退当前版本 |
| 索引中崩溃 | 保持 running；下次 work 在租约过期后重领 |
| 旧执行者拿着失效 token 发布 | CONFLICT，不覆盖新执行者的结果 |
| 发布时权限已撤销 | 拒绝发布；有权限的执行者可在租约过期后恢复 |
| 索引损坏、编码 profile 不匹配或召回后端异常 | INDEX_UNAVAILABLE，不伪装成“没有相关资料” |
| 删除 | 原件与所有版本索引在同事务删除，撤销未完成任务并留下墓碑；重复删除幂等 |

默认租约 60 秒，没有续租心跳。超时任务要在下一次 work 调用时才恢复；失败任务需显式 retry。
每个任务最多实际领取 3 次，超限后失败，不能无限重试。这是带令牌校验的本机持久化队列，
不是常驻异步服务、分布式调度器或端到端 exactly-once；新的外部副作用仍需单独幂等设计。

更新后，历史引用仍指向原版本，返回 is_current=false，不偷偷换成新版正文。删除后历史引用
返回 SOURCE_GONE；无空间权限则先返回 NOT_FOUND，不泄漏资源存在。SHA-256 核对和索引验证
用于一致性检查，不是身份认证或抗本机管理员篡改保证。

删除保留无正文的版本/任务/审计元数据和墓碑，清除文档标题；不等于备份、SQLite 空闲页、
操作系统缓存或命令历史的取证级擦除。应用数据库仍需独立访问控制和备份治理。

## 本机操作

从仓库根目录运行，未指定 --database 时复用 outputs/supportops/application.sqlite3。
P1 数据库会执行已知的 v1→v2 附加迁移，保留用户、空间、角色和审计；迁移失败回滚 DDL 与
版本标记，未知版本仍拒绝。以下命令不读取 .env、不下载模型、不发送邮件。

```powershell
cd D:\code\Hello-agent
D:\conda-envs\helloagent\python.exe -m supportops demo --enable-demo --profile admin init
D:\conda-envs\helloagent\python.exe -m supportops demo --enable-demo --profile admin upload helloagent --filename guide.md --title "部署手册" --content "服务端口为 8080。" --key demo-guide-v1
D:\conda-envs\helloagent\python.exe -m supportops demo --enable-demo --profile admin work helloagent
D:\conda-envs\helloagent\python.exe -m supportops demo --enable-demo --profile reader search helloagent "服务端口是多少？"
```

upload 返回 document_id、version_id 和 job_id。work 每次只处理一个任务，任务多时需多次执行。
--content 是演示文本，不是文件路径；不要把敏感文档放进终端命令历史。未来 HTTP 上传接收
文件字节，目前没有 --file 任意路径读取功能。

也可以显式导入已有的 10 份公开项目种子资料：

```powershell
D:\conda-envs\helloagent\python.exe -m supportops demo --enable-demo --profile admin import-seed helloagent
D:\conda-envs\helloagent\python.exe -m supportops demo --enable-demo --profile admin jobs helloagent
D:\conda-envs\helloagent\python.exe -m supportops demo --enable-demo --profile admin documents helloagent
```

只按 corpus/manifest.json 摄取，不扫描工作区或验收目录。同一语料摘要重复导入重放原任务；
新摘要视为新资料组，不是持续同步连接器。导入后仍需执行 work。P0 的顶层 search/evaluate
保持原来的独立公开基线；只有 demo search 才使用本阶段的空间/版本权限链路。

其他操作（用实际返回的 ID 替换占位符）：

```text
demo ... update <workspace> <document_id> --filename guide.md --title 新版 --content 新内容 --key new-key
demo ... job <workspace> <job_id>
demo ... retry <workspace> <job_id>
demo ... document <workspace> <document_id>
demo ... citation <workspace> <document_id> <version_id> <chunk_id>
demo ... delete <workspace> <document_id> --confirm
```

delete 不带 --confirm 会拒绝。search 只返回检索证据，不生成答案，也尚未实现无充分证据时
的应用级拒答判定。引用字符范围基于去 BOM、统一换行并去首尾空白后的文本；原始字节另行保留。

## 容量与验证边界

本地演示限制：单文件 256000 字节；每空间最多累计创建 100 个文档（含墓碑），每文档最多
20 个版本；单次检索最多装载 2000 个当前版本块，最多返回 20 条，问题最长 2000 字符。
超过限制明确报错；不是针对大规模知识库优化的索引服务。

新增 38 项 P2 测试；与 P0/P1 共 88 项专项测试。覆盖双连接幂等提交/领取、任务租约与重启、
发布顺序、权限撤销、索引/原件故障、版本引用、删除、迁移回滚及独立 CLI 调用链路。
使用本地 SQLite、确定性索引与 Fake 故障，不依赖真实 LLM 或数据库服务。

P0 的 5 项应用验收仍标记 pending_application；其中部分文档与权限场景已在服务层测试，
但不能当作 HTTP、生成答案、生产认证和完整工作台验收通过。

下一阶段 P3：持久化会话、回答、引用、反馈和运行记录，并在统一权限边界上提供可测试的
应用 API；先用 Fake LLM 验证成功提交、失败不污染历史、证据不足和引用校验，再接真实模型。
