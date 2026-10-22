# P5e：真实模型接入本机工作台

日期：2026-10-19。接入代码、费用边界与模拟浏览器流程已验证；本阶段没有读取真实密钥、
发起付费请求或启动真正的 live 服务，尚未完成网页到供应商的真实端到端验收。

## 启动方式

原 `serve` 命令保持 Fake。新增 `serve-live` 是独立、显式、会产生费用的入口：

```powershell
cd D:\code\Hello-agent
$env:PYTHON_DOTENV_DISABLED='1'
D:\conda-envs\helloagent\python.exe -m supportops serve-live --enable-live --ack-public-data --session-id web-local-20261019 --session-budget-cny 1 --request-budget-cny 0.20 --max-chat-requests 10 --port 8766 --open-browser
```

以上命令授权该启动会话最多预留1元、最多10次 Chat 尝试，单次预留不超过0.20元，并允许
隔离 worker 读取项目根目录的 `.env.supportops`。不会因为打开页面就请求模型；每次点击发送
还需在页面确认问题、相关历史与公开证据可以发给 DeepSeek。不要输入密钥、个人或客户资料。
预算、请求或完整载荷大小限制可能让实际可发送次数少于10次。

配置文件仍只接受既有三项 `LLM_MODEL_ID`、`LLM_BASE_URL`、`LLM_API_KEY`；不要把值粘贴到
聊天、命令行或网页。仅允许 `deepseek-flash` 与官方 HTTPS 地址，实际凭据只在子进程内使用。
本轮没有查看或确认现有配置是否仍有效。SDK/网络/配置错误不返回内部详情，也不自动重试。

服务只绑定 `127.0.0.1`；用终端输出的完整临时链接进入。链接含访问凭据，不要分享。
关停用 Ctrl+C。重启沿用相同 session-id 和参数，已发生预留不会恢复；改变同一 ID 的参数
会冲突。新 ID 也不能清除原累计账本占用，不要为绕过停止状态或重复请求而换 ID。

## 数据与调用链

```text
本机随机访问凭据 → 只读成员身份与会话所有权 → 公开种子资料检索
→ ContextBuilder / RequestBudgetManager → 当前权限与版本复核
→ 原累计账本内原子预留并记录 attempted
→ -I / 最小环境隔离 worker → 官方 DeepSeek Chat
→ 独立 Token 回执落盘 → 严格 JSON / 连续原文引用校验
→ 成功才提交问答历史 → 原文、运行详情与反馈
```

- 专用业务库固定为 `outputs/supportops/live-workbench.sqlite3`，首次自动导入10份公开种子资料。
  后续 [P6a](P6_TICKETS.md) 在同库新增本人私有工单；生成建议沿用本节模型/费用限制，资料仍只读。
  不打开或复制默认 `application.sqlite3` 的对话。只读身份固定为 reader；HTTP 层拒绝资料
  上传、更新、删除与索引处理。发送前还检查召回正文属于本轮公开种子文本。
- 不加载保留集或评测 gold，不启用在线 Embedding，继续使用 Hashing 检索。
- 默认提示词保持 `extractive-v1`，不把完整性候选 v2 偷换成已通过真实验证的版本。
- 一次业务请求最多一次 Chat；关闭 SDK 重试、代理继承和重定向，开启 TLS 校验，限制官方
  host / path / method。非思考 JSON 模式，输出最多1024 Token，完整请求最多20000 UTF-8字节。
  子进程最多45秒；结果只能通过有界 JSON 返回，错误不输出 SDK traceback 或 headers。
- 请求幂等键复用由原 ChatService 处理；同一已完成请求不再次占用模型额度。未完成/失败
  不自动重发。程序中断后 attempted 仍在，后续该会话停止，不能把未知结果当成免费调用。

## 费用与用量

`web_budget.py` 在既有 `outputs/supportops/cumulative_chat_budget.sqlite3` 增加网页会话与
业务 run 关联表，不删除或修改历史行。原 SpendLedger 累计5元上限不变；每次请求的预留、
started 与 attempted 在同一 BEGIN IMMEDIATE 事务中持久化，再启动 worker。
同时检查单次、启动会话累计、启动会话次数与全局累计限额；同一启动会话下新建聊天不清零。
启动会话1元不是额外赠予1元，也不提前占满1元；实际按每次请求的保守预留占用全局余额。

缺失原累计账本时拒绝运行，不通过隐式重建遗忘既有 full32 和未知用量记录。新克隆仓库
没有账本时应先另行制定预算初始化与授权方案，不能复制虚构账本冒充已核对历史。
本轮没有修改本机真实账本；历史最近记录为累计保守预留3.716160/5元，不是账户余额或账单。

预留沿用完整请求UTF-8字节×4+4096的保守输入启发式，输出预留按上限计算；不是供应商
tokenizer 或财务硬限额。按2026-10-19核对的 Flash 高峰未缓存输入2元/百万、输出8元/百万
估算，不依赖缓存或空闲折扣；见[DeepSeek官方价格](https://api-docs.deepseek.com/zh-cn/quick_start/pricing/)。
价格可能变化，后续启动前需要重新核对，不保证应用能阻止供应商超启发式计费。

页面展示模型模式、外发提示、会话费用预留/次数、累计可继续预留金额和供应商 Token 回执。
回执按高峰价估算与预留分开，真实 `cost` 仍为 null；未知用量不填零，不自动退回预留。
SDK失败、用量缺失/超界或原文协议失败后停止该启动会话，保留已收到的独立用量与失败运行。

密钥仅在服务端使用的边界参考了 OpenAI Docs 的
[认证说明](https://developers.openai.com/api/reference/overview#authentication)；这不代表使用
OpenAI 模型或验证了 DeepSeek 的数据保留/地域政策。本机 capability 仍不是生产认证。

## 本轮验证与剩余边界

- 只运行本轮7项费用/隔离/问答链路定点检查，使用临时库与模拟客户端；未做整套回归。
- 本机浏览器通过模拟流程：真实模式提示、发送确认、回答、原文、Token回执、只读资料和
  次数限额拦截。截图中10/20/30 Token来自模拟回执，不能作为真实供应商结果。
- JS语法与差异空白检查通过；未打开保留题集、读取真实配置、改变真实预算或推送 GitHub。
- 真正的网页付费请求、超时与供应商回执仍需启动后受控验收。仍是同步本机 WSGI、单操作者
  与单写者 SQLite；没有公网认证、流式输出、常驻worker、分布式账务或生产可用性保证。

下一步：按上方明确限额启动，只输入公开问题完成一次真实回答，再核对引用、运行与费用。
不要重跑已消费的 full32 实验，也不要为演示打开冻结保留题集。
