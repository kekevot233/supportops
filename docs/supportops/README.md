# SupportOps：研发知识与技术支持助手

目标：以 HelloAgent 为底层，参考 Onyx 的知识工作台思路，完成一个能演示、可测量、可解释的
AI 应用求职项目。当前完成 P0 产品/资料/离线基线、P1 本机空间与成员权限服务，以及 P2 文档
原件/持久化任务/版本发布与空间隔离检索；P3 已增加私有会话、抽取式引用回答、反馈、运行记录
和 WSGI API；P4 已接通本机工作台，P5a/P5b 已完成回答 smoke 与 SDK 适配，P5c-2 已完成
DeepSeek 三题授权试跑。仍没有生产认证或广泛真实模型质量验证，三题通过不代表完整评测。
2026-10-18 的 P5c-3 已完成相同公开开发集 32 题对照：真实 30/32、Fake 20/32，两个遗漏事实的
失败原样保留；已接累计预算账本。仍不是盲测或生产质量证明。
2026-10-19 新增显式 serve-live 本机网页入口，使用专用公开种子库并共享原累计预算；
当前完成模拟后端与浏览器链路验证，未在该网页发起真实付费请求。

## 从这里阅读

- [产品需求与验收范围](PRD.md)：为谁解决什么问题、第一版做什么、不做什么。
- [页面草图](WIREFRAMES.md)：文档、问答、引用、反馈和运行详情的低保真布局。
- [架构、API 契约和开发顺序](DESIGN.md)：应用层如何复用框架，P1～P6 的完成条件。
- [资料说明](../../data/supportops/README.md)：10 份知识资料的来源、摄取清单和 30 个场景。
- [离线入口](../../supportops/__main__.py)：可执行检索和基线，不调用真实模型。
- [P1 空间、演示身份与权限](P1_WORKSPACES.md)：已实现内容、可信边界及本机演示命令。
- [P2 文档、任务与版本](P2_DOCUMENTS.md)：原件、索引、恢复、删除、版本引用及 CLI。
- [P3 私有会话、回答和 API](P3_CHAT_API.md)：Fake LLM 流程、反馈、运行记录、请求边界及演示。
- [P4 本机工作台](P4_WORKBENCH.md)：网页启动、随机会话、安全边界和浏览器验收。
- [P5a 回答评测](P5_ANSWER_EVALUATION.md)：32 项回答/拒答/污染案例、Fake 失败样本与显式运行时。
- [P5b SDK 适配](P5_SDK_ADAPTERS.md)：具体兼容适配、调用限额、用量、内存 SDK 测试和 v2 稳定基线。
- [P5c-1 离线预检](P5_PREFLIGHT.md)：无密钥配置、1～3 题范围绑定、请求计划；不执行真实试跑。
- [P5c-2 真实试跑](P5_LIVE_PILOT.md)：隔离凭据/SDK、三题真实结果、预算、编码故障与持久化记录。
- [P5c-3 完整对照](P5_FULL_REGRESSION.md)：32 题真实结果、失败分析、累计预算和重复发送保护。
- [P5d-1 完整性与保留集](P5_ANSWER_COMPLETENESS.md)：复核两题标注范围、可选 v2 提示词和冻结保留集；本轮无真实调用。
- [P5e 真实本机工作台](P5_LIVE_WORKBENCH.md)：显式启动、外发确认、隔离 worker、真实回执展示与累计费用边界；当前仅模拟验证。
- [P6a 工单助手](P6_TICKETS.md)：本人私有工单、引用建议、人工确认/接管、处理记录与schema v4迁移；不执行外部操作。

## 立即运行

从仓库根目录运行，不要求安装新的服务或填写密钥：

```powershell
cd D:\code\Hello-agent
D:\conda-envs\helloagent\python.exe -m supportops search "扫描版 PDF 为什么无法检索？"
D:\conda-envs\helloagent\python.exe -m supportops evaluate --output outputs\supportops_baseline_20261015.json
```

search 返回召回片段、重排分数和原文引用，不生成答案；即使问题没有答案，也可能返回相近片段。
当前没有应用级拒答判定，不能把“有召回结果”当作“已有充分证据”。
evaluate 退出码：0 为检索门槛通过，1 为门槛未通过，2 为资料、参数或输出路径错误。
报告只能写入项目 outputs 目录下的 JSON 文件；outputs 被 Git 忽略，可按命令重建。
supportops 目前是仓库应用目录，不包含在只分发 hello_agents 的框架 wheel 中。

## 2026-10-15 实测基线

环境：Python 3.12.14；HashingEmbedder 384 维；SQLite 内存正文库、内存向量库；
切块大小 700、重叠 80；向量召回后 HybridReranker 重排（向量权重 0.75）；top_k=5。

| 检查 | 结果 |
| --- | --- |
| 摄取资料 | 10 份，仅显式清单；验收问题不入库 |
| 检索用例 | 25 / 25 通过 |
| gold 文档 Recall@5 | 1.0000 |
| MRR@5 | 0.9400 |
| 标注支持片段命中率 | 1.0000 |
| 引用与原文字符区间一致性 | 1.0000 |
| 应用验收 | 5 项待实施，不计入通过率 |
| 模型调用 | 0 |
| 专项与全量测试 | 新增 17 项；全量 396 项中 392 项通过、4 项跳过 |

Recall 按返回块所属文档是否覆盖 gold 文档计算；MRR 按第一个 gold 文档块的返回名次计算，
不是按去重后的文档名次计算。引用一致性检查块身份、标题、章节、页码和规范化正文的字符区间，
不是生成答案的引用支持性评分。

语料 SHA-256：`313784b1615fbe04088eeeac9440bee586593bf69253215a257250ad8f19dd47`。
评测集 SHA-256：`5f0a21bd64af1608e841510aaf4c060e8bcc64c8cf5fa03ca25c51b52ce1d484`。
两者随报告保存；结果来自同批整理的公开开发 smoke 集，规模很小，不能外推为生产准确率。
没有通过修改召回阈值、对照答案入库或使用 LLM 自评来宣称应用已通过。

## P4 本机工作台已实现，下一步真实模型评测准备

已实现独立 SQLite 应用库、显式演示身份、空间/成员/审计权限，以及 P2 文档原件、任务重领、
失败重试、原子版本发布与删除，以及 P3 私有会话/回答/引用/反馈/运行记录。
P3 新增 36 项测试，SupportOps 共 124 项；2026-10-16 全量 503 项中 499 项通过、4 项跳过。
默认数据库位于 outputs/supportops/application.sqlite3，已迁移至 v3，保留原有角色与文档。

```powershell
D:\conda-envs\helloagent\python.exe -m supportops demo --enable-demo --profile admin init
D:\conda-envs\helloagent\python.exe -m supportops demo --enable-demo --profile reader search helloagent "扫描版 PDF 为什么无法检索？"
```

已实际导入 10 份公开种子文档并完成 10 个索引任务；reader 检索上述问题首条命中“文档加载和
格式限制”，访问 internal-lab 则返回 NOT_FOUND。新副本需按 P2 说明先 import-seed，再逐次 work。
demo search 只返回原文证据；P3 的 demo ask 使用 Fake 客户端选择原文、经校验后组装回答，
不是自由生成或语义判断。原件存 SQLite BLOB，索引条目分表存储，未接在线向量服务。

演示档案是公开选择器，不是登录凭据；请勿对外提供此身份适配器。P0 顶层 search/evaluate
仍独立；demo search/ask 使用授权空间/当前版本，私有会话额外检查所有者。
5 项应用场景不能用 Fake 全部验收：P4 浏览器已验证 APP-02 空间拒绝、APP-03 生命周期和 APP-05 依赖故障；
APP-01 语义拒答与 APP-04 模型抗注入仍待真实模型阶段。公开 demo profile 没有暴露为登录。

现阶段不需要你提供额外企业资料或密钥；真实模型效果对比、实际企业数据或对外部署时再分别
确认数据授权、服务配置和费用。WSGI API 已接本机启动器，支持有界 JSON 文本接入、文件选择后
的 UTF-8 解码和显式处理任务；没有 multipart、生产认证、常驻 worker、工单或自动诊断。

```powershell
D:\conda-envs\helloagent\python.exe -m supportops serve --enable-demo --profile admin --port 8765 --open-browser
```

仅允许本机演示，禁止代理/端口转发。启动凭据只放在完整链接 fragment 和当前页面内存，
8 小时失效、重启即失效，不是企业登录。详见 P4。2026-10-17 新增 19 项 Python 测试，
SupportOps 专项共 143 项；13 组真实浏览器检查通过，无真实外部模型调用。
该 P4 阶段全量离线回归：522 项中 518 项通过、4 项真实存储默认跳过。

## P5a 回答质量基线已完成，真实模型尚未接入

```powershell
D:\conda-envs\helloagent\python.exe -m supportops evaluate-answers --output outputs\supportops\answer_baseline_20261017.json
```

当前预期退出码 1：32 项 smoke 中仅 20 项满足，5 个负例均不拒答、2 个污染案例均带出测试
canary；原文引用一致性仍为 1.0。这不是实际模型准确率，而是 Fake 无语义判断的可复现证据。
默认仍离线；不连接业务库，不将标注入库，不新增真实 SDK、外部请求或费用。
新增 32 项专项测试；SupportOps 共 175 项，全量 554 项中 550 项通过、4 项跳过。
后续 P5b 接具体供应商 SDK，先用 Fake 验证，再单独确认实际模型/联网/凭据/费用。

## P5b SDK 适配已离线验证，真实联调仍未执行

已实现 Chat Completions/Embeddings 兼容 SDK 适配及惰性工厂、两类请求限额与回执统计。
新增 32 项测试通过（含已安装 SDK 3.6.0 的内存 MockTransport），不连接真实服务。
修复封装时间引起的同分证据排序波动，v2 连续 12 轮均为 20/32；不代表模型质量提高。
当前全量 586 项中 582 项通过、4 项跳过，SupportOps 共 207 项。
工作台与 evaluate-answers 仍为 Fake；该阶段没有真实样本联调。

## P5c-1 离线配置预检已完成，尚未开放真实调用

```powershell
D:\conda-envs\helloagent\python.exe -m supportops preflight-pilot --config configs/supportops/pilot.example.json --output outputs/supportops/pilot_preflight_20261017.json
```

示例可直接运行，无需密钥；只检查配置、端点语法、1～3 题选择、资料/题集摘要和请求预算。
成功退出也明确 execution_authorized=false、live_execution_supported=false，不是服务可用或联网授权。
模板 `.invalid` 域名与 choose-* 均为占位，不要填密钥；报告不展开问题/gold/资料正文。
新增 32 项测试；全量 618 项中 614 项通过、4 项跳过，SupportOps 共 239 项。
该阶段没有真实模型调用；后续接线见下方。

## P5c-2 三题真实模型试跑完成

用户明确授权读取专用 .env.supportops，后续将累计预算由 1 元扩充为 5 元；不调用在线 Embedding。
独立进程使用官方 deepseek-flash，正常问答/拒答/污染各 1 题全部通过，同批 Fake 为 1/3。
有效报告 outputs/supportops/deepseek_live_pilot_20261017_round2.json：3 次 HTTP 200、5853 输入 / 97 输出
Token，按高峰未缓存价格保守估算 0.012482 元，不是供应商账单。首轮结果通道失败、用量未知，
单独保留，按原整轮 1 元占用；没有自动重发或记为免费。
独占授权、ASCII JSON、独立 UTF-8 报告与逐次事件已接通；当前 grant 已消费，不要直接重跑。
新增 25 项；全量 643 项中 639 项通过、4 项跳过，SupportOps 共 264 项。
工作台/evaluate-answers 仍为 Fake；三题通过不代表完整 32 题或生产语义验收通过，见 P5c-2 说明。
