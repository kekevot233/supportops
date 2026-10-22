# P5c-3：完整 32 题真实对照与累计预算账本

日期：2026-10-18。用户授权继续项目并调用真实模型，沿用累计 5 元预算；只发送公开项目资料，
不调用在线 Embedding，不切换网页、不写默认业务数据库。固定授权 ID：
`supportops-20261018-full32-total-cny5`。本次授权已消费，不能通过重复命令补发。

## 实测结果

报告保存在本机被忽略的 `outputs/supportops/deepseek_full32_20261018.json`；不上传真实运行报告。
使用相同 10 份人工整理资料、32 道公开开发 smoke 和 v2 评测器，未修改题目、gold、阈值或语料。

| 范围 | Hashing + Fake | Hashing + deepseek-flash |
| --- | --- | --- |
| 全部案例 | 20/32 | 30/32 |
| 普通事实 | 20/25 | 23/25 |
| 证据不足拒答 | 0/5 | 5/5 |
| 污染资料 | 0/2 | 2/2 |
| 基础设施错误 | 0 | 0 |

32 次 SDK Chat 请求均收到 HTTP 200、完整独立 usage；63749 输入 + 1524 输出 = 65273 Token。
真实引用区间一致性 1.0，标注事实覆盖 0.9623，gold 文档 Recall@5=1.0。没有在线 Embedding。
30/32 为这组开发检查的通过比例，不是生产回答准确率，也不是全面安全或抗注入证明。

### 两个保留的失败

- **RET-08**：正确摘录 `replace=True`，但未覆盖同内容重复摄取返回 `unchanged` 的标注事实。
- **RET-19**：正确摘录 SQLite 重启恢复正文，但未覆盖“内存向量库需要重建”的标注事实。

这两题引用原文有效、协议正常，但覆盖不完整，仍判失败。`quality_gate_passed=false`，CLI
退出码 1 是质量门槛未全过，不是进程崩溃。没有为全绿修改标注、重跑挑选结果或追加付费调用。
后续应先复核标注与用户问题的对应关系、分析摘录完整性，必要时以独立保留集检验改进。

## 持久化累计预算

`supportops/spend_ledger.py` 使用 SQLite、整数微元和 `BEGIN IMMEDIATE`，对并发预留原子计数。
账本位于 `outputs/supportops/cumulative_chat_budget.sqlite3`，不是业务库、不保存密钥/提示词。

| 占用项 | 保守预留（元） | 说明 |
| --- | --- | --- |
| 旧首轮 | 1.000000 | 请求/账单无法核实，保留原整轮占用，不记成零 |
| 旧 round2 | 0.228808 | 保留原完整计划预留，不因已知 Token 估算较低而释放 |
| 本轮 32 题 | 2.487352 | 在启动 worker 前一次性原子预留全部计划 |
| 累计占用 | 3.716160 | 未超累计授权 5 元 |
| 尚可预留 | 1.283840 | 不是账户余额，也不表示获准自动追加实验 |

账本不自动退款。中断、失败、usage 未知及未发送的计划余量仍占用预留；旧 grant 与 started
均保留。独占领取与单独的 started 标记阻止重复命令/直接 worker 复用同一授权。
每次 SDK create 前持久化 attempted，收到后只记录经过校验的数字回执；未知或缺失回执时停止。
historical 记录只承接原活动的保守金额，不虚构首轮真实用量；账本新 recorded_attempts 不包含旧轮。

本轮按照 Flash 高峰、未缓存输入 2 元/百万 Token、输出 8 元/百万 Token 保守估算为
**0.139690 元**；实际供应商账单仍未知。2026-10-18 核对
[官方价格页](https://api-docs.deepseek.com/zh-cn/quick_start/pricing/)，不依赖空闲/缓存优惠。
请求预留沿用 `4 × UTF-8 请求字节 + 4096` 输入估算；这不是供应商 tokenizer 或财务硬限额。
若用量超出预留假设则停止后续发送；本机账本不是云端支付控制，也不防本机恶意篡改。

## 隔离调用与结果恢复

- 新入口 `run-deepseek-regression` 必须显式 `--authorize-live`；父进程先做无凭据 Fake 干跑。
- 只允许预期 corpus/dataset SHA-256 和完整 32 题；逐次发送复核与干跑相同的消息摘要和顺序。
- 固定 `python -I` worker、最小环境，不继承模型凭据/代理/自定义 headers，不读原 `.env`/SMTP。
- 只有 worker 程序解析 `.env.supportops`，不输出或写入密钥。复用官方 host/path 白名单、TLS、
  禁代理/重定向、SDK 与 transport 重试均为 0、JSON/non-thinking、每题最多 1024 输出 Token。
- 独立 worker 报告 fsync 后才通知父进程，stdout 用 ASCII JSON，避免先前 GBK/UTF-8 丢失问题。
- 共享 CallLimits 要求正数，但运行时没有在线 Embedding 工厂或路径，外部 Embedding 实际为 0。
- 本机环境隔离不是恶意代码的 OS 沙箱；账本/schema/报告仍需可信本地文件系统。

SDK 重试与超时边界通过 OpenAI Docs 技能核对
[官方 Python SDK 文档](https://developers.openai.com/api/reference/python)，并用已有 Fake SDK /
MockTransport 测试验证，不调用 OpenAI 模型服务。

## 测试与下一步

```powershell
$env:PYTHON_DOTENV_DISABLED='1'
$env:HELLO_AGENTS_RUN_REAL_STORES='0'
python -m unittest test_supportops_live_regression test_supportops_live_pilot -v
```

覆盖预算承接/精度、并发防超用、重启、重复领取、发送前事件、未知用量、部分完成、原三题上限、
完整 Fake SDK 流程及密钥脱敏。默认测试不读取真实专用配置，不进行真实联网。
网页继续为 Fake；下一步是完善失败诊断与独立质量验证，而非根据这 32 题直接切换生产问答。
