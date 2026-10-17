# P5b：SDK 适配与离线兼容性验证

日期：2026-10-17。已实现 OpenAI-compatible Chat Completions / Embeddings 同步 SDK 适配，
并用 Fake SDK 和已安装的 OpenAI SDK 3.6.0 + 内存 MockTransport 验证；没有发送真实服务请求。
**这不是 DeepSeek、百炼或 OpenAI 真实端点的联调结果，也不代表回答质量已经改善。**

后续更新：P5c-1 的非敏感配置和离线预检已实现，见 [预检说明](P5_PREFLIGHT.md)；
P5c-2 的隔离工厂/专用凭据和三题真实结果见 [真实试跑](P5_LIVE_PILOT.md)，下文为本阶段历史快照。

## 本轮变化

- CompatibleChatClient：将 SDK 的响应正文和独立 usage 映射到 P5a 的 Completion 契约。
- CompatibleEmbedder：实现 BaseEmbedder，分批编码、按返回 index 恢复顺序，并校验数量/维度/有限数值。
- ProviderCallLedger：统一记录 chat/embedding 的 SDK create 尝试、失败、完整或部分用量与本地限额。
- compatible_runtime：将非敏感配置和两个惰性 SDK 工厂组成显式 external 评测运行时。
- 回答协议可携带“响应不可用但已收到用量”的错误回执，避免把截断后的账单信息丢掉。
- 修复证据封装时间影响同分片段顺序的问题；默认按检索分数排序，同分保持检索顺序。

没有修改工作台页面、默认业务库、种子资料、题目/标准答案或 .env，没有安装新依赖。
`serve` 和 `evaluate-answers` 仍默认使用 Fake，未新增可偷偷启用外部模型的 CLI/HTTP 参数。

## 官方契约与实现选择

本轮使用 OpenAI Docs 核对 SDK 和 API 契约，随后对照本机安装版本的签名与实际序列化结果。

- 使用 with_options(max_retries=0, timeout=...)，默认 30 秒；适配器不自行重试。
  官方 SDK 提供显式重试和超时配置：[Python SDK 文档](https://developers.openai.com/api/reference/python)。
- ChatSDKConfig 默认发送 max_completion_tokens；如选定服务需要旧 max_tokens，必须显式选择。
  不在 400/500 后自动换参数再发一遍。JSON object 模式同样为显式 opt-in，不假设所有服务支持。
  参数区别见 [Chat Completions](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create)。
- Embeddings 显式使用 encoding_format=float。dimensions 仅在 request_dimensions=True 时发送，
  但返回维度始终校验；按每项 index 恢复输入顺序。字段见
  [Embeddings](https://developers.openai.com/api/reference/resources/embeddings/methods/create)。

兼容协议不等于各供应商参数、模型能力、地域端点或计价完全一致。本阶段没有选择最新模型、
核验账号可用性、测试区域接入，也没有声称支持所有模型/SDK 版本。Responses、流式、多模态与
工具调用不在此适配器范围内；没有把函数调用响应当成答案或自动执行工具。

## Chat 响应与用量

请求仅接受已有问答链路的 system/user/assistant 文本消息；固定 n=1、stream=False，
输出预留与 P5a 的预算一致。收到响应后：

1. 先从 SDK 独立 usage 字段校验 prompt_tokens / completion_tokens / total_tokens。
2. 再检查恰好一个 index=0 的 assistant 文本回答、finish_reason=stop，且无工具调用或安全拒绝字段。
3. 正常正文交给既有 AnswerEngine，继续执行严格 JSON、引用标签与原文校验。

length 截断、content_filter、工具调用、安全拒绝、非法正文等返回 MODEL_RESPONSE_INVALID，
不是 insufficient_evidence；已获得的有效 usage 仍保留。服务连接/超时异常返回 MODEL_UNAVAILABLE。
缺少 usage 可以保留为未知；若提供了自相矛盾或非法的 usage，则响应不被接受。
原始 SDK 异常、提示词、密钥和响应正文不进入调用账本。

## Embedding 与调用限额

所有输入在第一个请求前完成非空和字符长度校验；空批次不发请求。默认每批最多 16 条、
每条最多 8000 字符、每批最多 32000 字符。这些是本地字符限制，**不是供应商 Token 限制**，
更不能保证每种模型都接受相同长度。服务拒绝后不隐式重切、重试或降维。

返回索引必须是从 0 开始、不重复且完整覆盖的整数；拒绝布尔索引、遗漏、错误维度、NaN、
无穷、零向量和 base64 字符串。后续批次失败时不返回半组向量，但保留已完成请求的用量小计。
Embedding usage 归一化为 input_tokens=prompt_tokens、output_tokens=0；实际总量不一致时拒绝。

CallLimits 默认值：chat 32 次、embedding 128 次、两类请求累计输入 2000000 字符、累计预留
输出 32768 Token。用锁原子检查，超限时在 SDK create 前拒绝；未发送的请求不会记为 SDK 尝试。
失败请求已经占用的额度不退回，避免无限失败重试。限额拒绝数独立记录。

**这不是人民币/美元费用上限。** 价格表、缓存 Token 差异、供应商账单确认均未实现。
actual_usage 只在该类全部已尝试请求有回执时合计；否则为 null，known_usage_subtotal 单列。
cost 仍为 null，SDK 尝试也不等于供应商确认或计费次数。

## 工厂、授权与所有权

`compatible_runtime(spec, chat, chat_sdk_factory=..., embedding=..., embedding_sdk_factory=..., limits=...)`
只组合配置与函数，不执行 SDK 工厂。spec 必须为 external，模型声明与配置必须匹配；
不提供在线 Embedding 时只能搭配原 Hashing 基线。

`evaluate_answers(..., runtime=runtime, allow_external_calls=True)` 通过许可后才执行工厂；
未授权仍返回 EXTERNAL_CALLS_DISABLED。每个组合只允许打开一次，每轮评测重新创建组合，
防止将多轮调用和用量混入同一个报告。

工厂需每次返回新的、由本轮独占的 SDK root。组合拥有客户端并在完成/失败后关闭；
直接构造适配器时 owns_client 默认 false，调用方保留所有权。with_options 可能共享连接，
因此只关闭拥有的 root 一次，不把借来的连接关闭。SDK 初始化中途失败也会尝试清理。

适配器**不负责创建真实 SDK、查找密钥或选择服务地址**。应用方工厂必须在获得授权后，
显式注入已确认的 URL/凭据，检查代理、重定向、HTTP transport 重试和 ambient headers。
SDK 本身可能读取环境配置，不能把“适配器没有读取 .env”理解为“任意 SDK 工厂已自动隔离环境”。
尤其不要直接复制默认 OpenAI() 构造器、共享其他业务客户端或继承不明的 Authorization headers。
本轮 SDK 测试只替换 SDK 模块的环境引用并注入测试凭据，不遍历或修改真实 os.environ。

可供未来应用层接线的接口示意（不是可直接执行的真实服务命令）：

```python
runtime = compatible_runtime(
    RuntimeSpec(
        runtime_id="approved-run", execution="external",
        model_id="approved-chat", model_revision="pinned-revision",
        embedding_id="hashing-blake2b", embedding_revision="v1",
        temperature=0.0, output_tokens=1024,
    ),
    ChatSDKConfig(model="approved-chat", token_parameter="max_completion_tokens"),
    chat_sdk_factory=build_authorized_isolated_sdk,
    limits=CallLimits(max_chat_requests=3),
)
```

build_authorized_isolated_sdk 是未来应用方提供的惰性函数，不是当前内置函数。实际调用前还需
确认供应商、模型/地域、密钥注入方式、数据范围、请求数和费用上限。不存在内置真实密钥加载器。

## 可重复性修复与报告

全量回归曾发现旧 Fake 基线在 19/32 与 20/32 间波动。重复运行定位到 ContextPacket 的
逐条封装时间：同分证据被误当成有新旧差异，影响首段摘录。现在对没有发布时间的资料使用统一
未知时间，默认关闭新近性/优先级加权，仅使用检索分数；同分时保持输入顺序。

评测器版本升为 supportops-answer-smoke-v2，报告新增 context_config。没有修改 gold、题目、
阈值或做针对题目的关键词修补。修复后连续 12 轮均为 20/32；拒答仍 0/5、污染仍 0/2，
引用一致性仍 1.0。这只是让基线稳定，不是语义质量改善。

默认 Fake 报告不产生 provider_calls；使用 SDK 组合时，报告增加两类 SDK 尝试/用量和设置，
pipeline.embedding_actual_usage 在完整回执时填入。业务数据库和网页仍保留原估算契约。
SDK MockTransport 的数字是测试回执，不是实际线上用量，本轮没有生成真实模型的质量/费用报告。

## 复现与后续

```powershell
cd D:\code\Hello-agent
$env:PYTHON_DOTENV_DISABLED='1'
$env:HELLO_AGENTS_RUN_REAL_STORES='0'
D:\conda-envs\helloagent\python.exe -m unittest test_supportops_provider_adapters -v
D:\conda-envs\helloagent\python.exe -m supportops evaluate-answers --output outputs\supportops\answer_baseline_v2_20261017.json
```

后一个命令仍为 Fake，预期退出码 1。旧 P5a 报告保留，不与 v2 配置混作完全相同的运行。
新增 32 项测试；SupportOps 共 207 项，全量 586 项中 582 项通过、4 项真实存储默认跳过。
包括真实 SDK 的内存请求序列化、500/超时不重试、乱序向量、部分回执、截断、限额并发、
资源所有权、授权前不构造 SDK，以及同分证据排序。整个新增测试环境禁止 socket 连接。

下一步 P5c-2：实现隔离 SDK 工厂与受控凭据注入，确认授权后先跑 1～3 个真实样本，再决定
是否扩大到相同 32 题的三组对照。SDK 合约测试不替代真实服务兼容性、模型语义、TLS/网络故障
或生产安全验收。APP-01/APP-04 的真实模型部分依旧待验证。
