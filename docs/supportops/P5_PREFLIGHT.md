# P5c-1：有限样本配置与离线预检

后续更新：P5c-2 已在单独授权下实现真实试跑，见 [真实试跑说明](P5_LIVE_PILOT.md)；
本文描述离线预检入口，不能用它的成功返回值代替真实执行授权。端点现已兼容 HTTPS 根路径。

日期：2026-10-17。完成 P5c 的第一部分，不代表整个 P5c 或真实模型评测完成。
新增 `preflight-pilot`，只检查非敏感配置、资料摘要、样本和请求预算；**不运行 SDK、不联网、
不获取密钥、不读取环境或 .env**。网页与 `evaluate-answers` 仍使用 Fake。

## 现在可以做什么

```powershell
cd D:\code\Hello-agent
$env:PYTHON_DOTENV_DISABLED='1'
$env:HELLO_AGENTS_RUN_REAL_STORES='0'
D:\conda-envs\helloagent\python.exe -m supportops preflight-pilot --config configs/supportops/pilot.example.json --output outputs/supportops/pilot_preflight_20261017.json
```

示例是待审阅模板：`choose-*` 不是选定模型/地域，`.invalid` 不是可用的服务域名。
它能通过离线配置检查，但不会被执行。无需填写任何真实凭据。

退出码：0 表示离线检查满足；1 表示配置合法但请求/字符/输出预留或输入边界检查不满足；
2 表示配置、样本、资料摘要或输出路径错误。0 **不是**网络许可或真实端点验证成功。

报告明确包含：

```json
{
  "offline_checks_passed": true,
  "status": "awaiting_review",
  "execution_authorized": false,
  "live_execution_supported": false,
  "external_model_calls": 0,
  "credentials_accessed": false
}
```

仍未提供 `--api-key`、`--allow-external` 或真实试跑命令。配置里也不能写密钥、密钥环境变量名、
任意请求头、代理或插件。CLI 错误不会回显配置校验原值，参数解析错误也使用固定提示，避免
误传的敏感值进入错误日志；这不能清除 shell 历史，**不要在命令行或 JSON 填密钥**。

## 配置和数据绑定

`configs/supportops/pilot.example.json` 仅声明模型、版本、地域、端点、参数方言、限额与样本。
严格模型拒绝未知字段、模型身份不一致、非 external 声明、缺少在线 Embedding 端点等情况。

- 配置只能从项目 `configs/supportops` 下显式 `.json` 加载，最大 64 KiB；解析重复字段和非有限
  JSON 数值时拒绝。路径解析后必须仍在配置目录内，不能借路径打开既有 .env 或其他任意文件。
- 端点限制为规范 ASCII 域名、精确 host 白名单和 HTTPS `/v1` 或 `/compatible-mode/v1` 等路径。
  拒绝 IP、本地域名、用户信息、端口、查询、片段、转义及控制字符，不自动补全/纠正供应商地址。
  **这里只做语法检查，不解析 DNS**。公共外观域名仍可能指向私网，不是 SSRF 防护或地域证明。
- 首轮显式选择 1～3 个不重复样本，Chat 尝试上限不超过 3。模板选择 RET-02、REF-01、INJ-01，
  分别覆盖普通事实、证据不足和污染资料；这不是挑选通过题的质量报告。
- 固定完整语料和完整题集 SHA-256，非试跑题目的标注变动也需要重新审阅。返回的样本顺序与配置
  一致，深拷贝避免修改原题集。CLI 不接受任意企业资料或另一份题集路径。
- 报告记录配置、语料、完整/选中题集、系统规则、评测器版本和固定切块参数，生成 review_sha256。
  摘要用于检查改动，**不是签名、授权令牌、代码完整性证明或人工批准**。

## 请求预算如何计算

预检只运行真实链路共用的切块和分批规则，不生成向量、不预测语义召回。

| 部分 | 预检处理 |
| --- | --- |
| Chat | 每个样本至多一次；最多 3 次，不自动重试 |
| Chat 输入字符 | 按适配器每次允许的 200000 字符上界保守预留，不称为实际输入大小 |
| 输出预留 | 样本数 × runtime.output_tokens；不是实际输出或账单 Token |
| Hashing | 外部 Embedding 请求与输入字符为 0 |
| 在线 Embedding | 逐文档切块/分批，加每题一次查询编码；字符数计入去空白后的块及问题 |
| 污染语料 | 与评测器相同的有效语料摘要缓存；新污染版本触发整库重建，不只编码被污染的文档 |

Embedding 适配器和预检共享纯函数 `plan_embedding_batches`，避免两套批次规则逐渐分叉。
若块/问题不满足输入长度边界，预检拒绝并把该类计划量标记为 null，不能把漏算当零。
计划按各阶段全部成功计算；实际故障可能使某个有效语料构建被后续样本重新尝试，
因此它**不是失败重建情形的请求硬上界**。真正运行仍须使用 ProviderCallLedger 的硬限额。

模板实际结果：3 次 Chat 上界，0 次外部 Embedding，2 个有效语料版本；累计 Chat 字符保守
预留 600000，输出预留 3072 Token。报告没有输出问题、gold、原始资料正文或污染 canary。

`actual_usage` 和 `cost` 仍为 null，`monetary_cap_enforced=false`。
次数/字符/预留 Token 不是金额上限。价格、缓存、计费规则和供应商财务限额仍需确认。

## 为什么暂不直接构造真实 SDK

本轮使用 OpenAI Docs 核对[官方 Python SDK 配置](https://developers.openai.com/api/reference/python)：
SDK 的重试/超时需要显式控制。此前对已安装 SDK 3.6.0 的代码和内存传输验证还发现，构造和
with_options 可能带入环境中的配置/附加请求头。显式传 api_key、base_url 不等于完全隔离环境。
本轮据此保留“无凭据预检”和“真实执行”两条边界，没有用生产代码修改全局环境来绕过问题。

真正的下一步是设计受控 SDK 工厂/隔离启动和凭据注入：禁用环境代理、重定向、transport 重试和
未知请求头，核对 DNS/TLS/端点归属，并在工厂构造前绑定明确授权。不能把当前预检报告直接
传成 `allow_external_calls=True`；仍需确认供应商、模型、地域、发送的数据、请求次数及费用上限。

## 验证与未完成事项

新增 32 项测试，包含端点/凭据字段拒绝、摘要与样本隔离、污染整库重建、字符/输出/请求检查、
无环境访问、错误脱敏和 CLI。用 P5b Fake SDK 走实际适配器/评测链路，对比 Embedding 请求数与
字符数；测试禁用 socket 连接和 DNS，不调用实际服务。

全量 618 项中 614 项通过，4 项真实存储默认跳过；SupportOps 共 239 项。
输出 `outputs/supportops/pilot_preflight_20261017.json` 可复现；没有安装依赖、修改网页/默认业务库、
题目/gold、语料或日报任务。Fake 20/32 基线不变，未测出任何真实模型质量提升。
本轮另实际重跑完整 Fake 评测，报告位于 `outputs/supportops/answer_baseline_preflight_20261017.json`，
仍为 20/32、基础设施错误 0、质量门槛未过，预期退出码 1；先前报告保留。

尚未完成：隔离 SDK 工厂、受控凭据获取/注入、授权后的 1～3 个真实样本、三组真实对照、
真实费用统计、APP-01 语义拒答和 APP-04 模型抗注入验收。
