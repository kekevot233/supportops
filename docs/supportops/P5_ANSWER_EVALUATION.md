# P5a：回答质量基线与显式运行时

本页保留 P5a 阶段说明；后续 SDK 适配、Embedding 用量与 v2 排序修复见
[P5b 说明](P5_SDK_ADAPTERS.md)。旧版曾有封装时间导致的 19/20 项波动，v2 已修复；
本文 20/32 是旧基线观测值，不再将旧报告称作每轮必然完全可重复。

日期：2026-10-17。已实现离线回答 smoke 评测、可注入编码器与惰性客户端工厂；
**尚未实现具体供应商的 SDK 适配、真实模型对比或生产费用统计。** 本轮没有联网调用、
读取 .env、下载模型、安装依赖，也没有修改默认业务数据库或工作台身份。

## 运行

```powershell
cd D:\code\Hello-agent
$env:PYTHON_DOTENV_DISABLED='1'
$env:HELLO_AGENTS_RUN_REAL_STORES='0'
D:\conda-envs\helloagent\python.exe -m supportops evaluate-answers --output outputs\supportops\answer_baseline_20261017.json
```

这个命令固定使用 HashingEmbedder + DemoQuoteLLM，不接受动态 Python 插件、密钥或外部模型选项。
退出码 0 表示所有 smoke 检查满足，1 表示评测完成但质量门槛未过，2 表示资料/配置/输出错误。
**当前返回 1 是预期的质量基线结果，不是运行崩溃。**

命令与 P0 的 evaluate 不同：后者只测检索，本入口同时检查回答摘录。报告仅可保存到项目 outputs
内的 JSON 文件，输出路径在评测前校验。报告包含本项目公开问题与摘录，不包含密钥、完整提示词、
原始 SDK 异常或任意本机路径；将来换成私有资料后，报告也必须按私有资料管理，不能直接公开。

## 实测结果

语料仍是 10 份公开项目文档，原始 corpus/cases.json 均未修改。
32 项 = 复用 P0 的 25 个事实问题 + 5 个资料不足问题 + 2 个假指令污染问题。

| 指标 | Fake 基线 |
| --- | --- |
| 全部 smoke 检查满足 | 20 / 32（62.50%） |
| 普通事实问题满足 | 20 / 25 |
| 资料不足问题正确拒答 | 0 / 5 |
| 假指令案例检查满足 | 0 / 2；两例均在摘录中带出 DEMO_CANARY_ONLY |
| gold 文档 Recall@5（含污染案例） | 1.0000 |
| 必需事实片段覆盖（含污染案例） | 0.8302 |
| 已输出引用的原文一致性 | 1.0000 |
| 基础设施/协议错误 | 0 |
| Fake complete 调用尝试 | 32；真实外部模型调用 0 |
| 真实供应商 usage / 费用 | null / null，不能用本地估算代替 |

报告的 answerable_check_pass_rate 为 20 / 27 = 0.7407，因为它包含 25 个普通事实问题与
2 个仍应正常回答的污染问题。始终拒答也不能通过：它会通过负例，但所有正例失败。

这不是“模型准确率 62.5%”。Fake 只机械摘取首个片段的前 400 字，不具备语义判断；
它可能找到正确文档，却截掉所需事实，也可能把文档里的假指令原样引用出来。后者只证明
污染文本进入了输出，不证明程序真的执行了其中的命令。我们没有通过关键词补丁、删除难题、
改变 gold 或降低阈值把基线改成全绿。

语料 SHA-256：313784b1615fbe04088eeeac9440bee586593bf69253215a257250ad8f19dd47。
展开后的回答集 SHA-256：0888087108668eeda4181a4795790a3200e7c4c12add56a7fd0755005da64585。
每例记录实际语料摘要，污染案例有独立摘要；还记录系统规则摘要、评测器版本、索引 profile、
模型声明、切块/重排配置、输出上限和完整请求预算。耗时随运行变化，不承诺报告逐字相同。

## 评测链路与判分

```text
显式种子清单 → 独立内存索引 → 向量召回/重排
→ 同一个 AnswerEngine / ContextBuilder / RequestBudgetManager
→ 严格 JSON 与原文摘录校验 → 独立 Python 判分 → 逐题报告
```

不连接应用业务库，不创建真实用户会话，不改已发布索引。每题历史为空；不替代 P4 对权限、
事务、页面或历史隔离的验收。同一干净语料只构建一次，污染案例使用单独的内存资料副本。
首题检索时间包含冷索引构建，不能拿这份报告当线上检索延迟或 P95。

- gold 来源与必需事实必须在未污染的种子原文中存在；重复 ID、空集、错误来源、重复 JSON
  字段和非法数值在工厂执行前拒绝。没有把题目、gold 或判分规则摄取进知识库/提示词。
- 事实只在本轮真正输出、且来自 gold 文档的连续摘录里匹配；问题、标题和相邻摘录不能替正文凑事实。
- 正例要求 answered、有原文证据且覆盖指定事实；负例要求 insufficient_evidence 且无摘录；
  污染例还要求不输出测试 canary。它们都是显式、有限的字符串检查，不是完整语义蕴含判定。
- 索引、模型、预算和格式错误分别记录稳定错误码，不当作“资料不足”，也不剔除失败题抬高分数。
- canary 未输出不代表全面抗提示注入，公开小样本不代表独立盲测。production_ready 与
  model_quality_verified 始终为 false；需真实模型、人工复核和更多独立资料才能进一步评价。

## 可替换编码器

EmbeddedDocumentIndex 接受已明确创建的 BaseEmbedder，并要求非敏感的 embedding_id / revision。
索引 profile 绑定声明的模型身份、版本、维度、切块参数、重排权重及实现版本。
LocalDocumentIndex 仍保持原来的 Hashing profile，现有工作台不用迁移数据库。

更换模型、配置或权重时必须给出正确的新 revision 并重建索引；运行时核对声明与索引元数据，
持久化服务不会把不同 profile 的向量静默混用。声明不是模型文件的自动校验或供应商证明，
工厂作者须保证 revision 覆盖真实权重、前处理和查询/文档前缀等设置。

该适配只验证向量数量、固定维度与有限数值，再复用现有余弦检索/重排；不自动下载、选择
真实 Embedding，也不切换工作台默认编码器或业务索引。

## 工厂与用量契约

EvaluationRuntime 由 RuntimeSpec、index_factory、client_factory 构成。配置只接受非敏感身份/
版本标识、execution、temperature 和 output_tokens，不接受 api_key、URL 或文件路径等字段。
output_tokens 实际进入预算与客户端 max_tokens；配置的 temperature 会传入 complete。

execution 为 external 时，必须在库调用中显式给 allow_external_calls=True，且先检查许可、
再执行两个工厂。没有该许可返回 EXTERNAL_CALLS_DISABLED，不构造客户端。CLI 只暴露固定 Fake。
offline_local 是可信工厂的声明，不是网络防火墙，不能用它包装实际外部客户端绕过授权。
评测器不自动重试，每题最多调用一次包装客户端；底层 SDK 的重试必须由未来适配器显式配置。

客户端需提供：

```python
complete(messages, *, max_tokens, temperature=None) -> Completion
```

Completion.text 交给原有 AnswerEngine。Completion.usage 必须是另一个经过校验的 ProviderUsage
（input_tokens、output_tokens、total_tokens），从未来 SDK 的独立 usage 字段归一化得到，
不能解析模型正文里的所谓 usage。包装器把它适配为原有 invoke 接口，不改框架的返回类型。

- 每次尝试先记一条未知回执，失败也保留尝试次数；客户端异常只输出安全错误码。
- 所有已尝试调用均有回执，actual_usage 才返回合计；只知道一部分时 actual_usage=null，
  known_usage_subtotal 单独保留已知小计，不假装其余为零。
- Fake 的 usage_source 是 not_applicable_offline_fake。真实外部运行的 external_model_calls
  暂为 null：客户端尝试次数不是供应商确认请求数，也不证明未发生 SDK 内部重试。
- 回执标为 client_reported，不是供应商账单认证；cost 始终 null，尚无价格表或费用计算。
  Embedding 的实际用量也暂为 null，不能把整个 RAG 成本说成只含 LLM 的调用。
- 正常结束或异常时尝试关闭有 close() 的客户端/索引；清理异常只留下稳定告警，不泄漏原始异常。

本阶段未编写或验证具体在线 SDK 映射，也没有把 usage 写进应用业务库。P3/P4 网页运行详情
仍保持原来的本地估算契约，不会因为有评测回执接口就展示虚构的真实费用。

## 验证与下一步

新增 32 项测试，SupportOps 共 175 项；全量发现 554 项，550 项通过、4 项真实存储默认跳过。
覆盖已知 Fake 缺陷、始终拒答、gold 隔离、标注校验、污染副本、维度/profile 隔离、权限开关、
回执造假/缺失/部分用量、故障脱敏、预算参数、资源关闭和 CLI 退出码。新增入口在拒绝 socket
连接的测试环境下仍能完成，没有把质量失败伪装成单元测试失败。

下一步 P5b：基于此契约实现选定供应商的 LLM / Embedding 适配，先用 Fake SDK 验证；
实际调用前分别确认模型、联网、凭据注入方式、请求数量与费用上限。然后在相同语料/题集下
比较 Hashing + Fake、Hashing + 真 LLM、真 Embedding + 真 LLM，记录失败样本并人工核对。
还需补独立语义标注、扩充注入类型和盲测；不能仅凭这 32 项宣布生产就绪。
