# SupportOps 种子资料与验收数据

本目录是基于 HelloAgent 现有文档与代码整理的公开开发演示数据，整理日期 2026-10-15。
不是企业客户资料，不是 Onyx 的私有数据，也不包含真实密钥、邮件、聊天记录或生产日志。

- `corpus/manifest.json`：唯一摄取清单，显式列出 10 份知识资料及原始代码/文档来源。
- `corpus/*.md`：人工整理的项目知识，可独立加载；引用对应整理文档，原始依据在清单内。
- `evaluation/cases.json`：25 个检索 smoke 用例和 5 个未来应用验收场景；不进入摄取。
- `evaluation/answers.json`：P5a 回答 smoke 清单，复用上述 25 个事实问题，另加 5 个拒答和
  2 个假指令污染案例。标注不进入摄取，污染仅用于独立内存资料副本，不改 corpus 文件。

`source_refs` 是仓库相对路径，仅记录来源；加载器不扫描仓库、不读取引用文件或环境配置。
实际加载只允许清单内、位于 corpus 目录内的普通 Markdown 文件。
检索结果的引用 source 使用可移植相对路径，字符区间以规范化后的整理正文为准。

运行：

```powershell
cd D:\code\Hello-agent
D:\conda-envs\helloagent\python.exe -m supportops search "扫描版 PDF 为什么无法检索？"
D:\conda-envs\helloagent\python.exe -m supportops evaluate --output outputs\supportops_baseline.json
```

使用现有 HashingEmbedder、内存向量存储、SQLite 内存库和 HybridReranker，不下载模型，
不调用 LLM 或真实数据库服务。每次进程启动重建这个小型索引，不提供持久化应用服务。
报告标记离线模式、语料摘要、检索参数、25 个检索结果与 5 个未执行应用场景。

全部案例公开且与语料同批整理，用于 smoke 检查；不作为无偏盲测或生产准确率。
后续引入真实资料与模型时，需要另外收集未参与调优的验收问题和人工评审。

2026-10-17 已增加 `python -m supportops evaluate-answers`。Fake 基线 20/32 满足检查，
负例 0/5、污染 0/2；当前退出码 1 为正常的质量未过，不代表程序崩溃或真实模型准确率。
工程验收与回答基线分别见 docs/supportops/P4_WORKBENCH.md 和 P5_ANSWER_EVALUATION.md。
