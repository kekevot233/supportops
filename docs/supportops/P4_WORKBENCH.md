# P4：本机可视化工作台

日期：2026-10-17。工作台已实现并通过 Chrome 无界面浏览器验收；这是本机离线演示，
不是生产部署。没有读取真实 .env、安装新依赖、调用真实模型或发布外网网站。

## 启动与演示

现有项目数据库已包含 10 份公开项目资料。PowerShell：

```powershell
cd D:\code\Hello-agent
$env:PYTHON_DOTENV_DISABLED='1'
$env:HELLO_AGENTS_RUN_REAL_STORES='0'
D:\conda-envs\helloagent\python.exe -m supportops serve --enable-demo --profile admin --port 8765 --open-browser
```

保持终端运行，Ctrl+C 停止。浏览器未自动打开时，粘贴启动器输出的完整临时链接
（含 `#access=…`）。只打开端口地址不会登录，刷新后需要重新打开完整链接。
不要截图、分享或提交带凭据的链接。链接最多 8 小时有效，重启服务后失效。

admin 可管理文档并使用自己的问答；reader 只能读取其空间资料和使用自己的问答。
若要演示两种身份，另开一个终端使用不同端口：

```powershell
D:\conda-envs\helloagent\python.exe -m supportops serve --enable-demo --profile reader --port 8766 --open-browser
```

两个进程可用相同 SQLite 库，但随机凭据互不通用。身份在启动参数中固定，页面没有
公开 profile 登录或身份切换器。服务器不会自动重建、清空或导入默认业务数据库。

新副本没有资料时，先初始化并创建种子任务：

```powershell
D:\conda-envs\helloagent\python.exe -m supportops demo --enable-demo --profile admin init
D:\conda-envs\helloagent\python.exe -m supportops demo --enable-demo --profile admin import-seed helloagent
```

然后在管理员工作台逐次点击“处理下一个任务”，直至 10 个任务成功。初始化是幂等的，
不会恢复已经被撤销的成员权限。

## 页面流程

1. 文档与任务：选择 UTF-8 TXT/Markdown，或粘贴正文，确认标题和文件名后提交。
   原件最多 256000 字节，JSON 请求另限 300000 字节。上传名不会被当作服务器本机路径。
2. 显式处理任务：每次只领取一个。失败可显式重试，成功才切换当前版本；没有常驻 worker。
3. 知识问答：建立本人私有会话，提问后点 D1 等引用，重新鉴权读取版本、章节与字符区间。
   更新后旧回答标为历史版本，引用不换源；删除后隐藏正文、引用失效。
4. 反馈与运行：提交有用/无用、原因和说明；查看 request_id、执行状态、本地 Token 估算与
   耗时。没有真实供应商 usage、费用或语义质量分。
5. 刷新与断开：刷新重新查空间/资料/会话状态，清除旧证据面板；无实时推送。
   已呈现在浏览器中的内容无法被服务器追溯收回。断开仅清除本页面的凭据和显示，
   要废止启动链接需停止服务进程。

上传和提问在网络结果不明时保留当前页面内的同一幂等键；刷新/关闭页面会丢失该状态，
不承诺跨浏览器重启的 exactly-once 提交。反馈为本人回答上的幂等更新。

## 安全与实现边界

- 标准库 WSGI 单线程演示服务器，只监听字面地址 `127.0.0.1`，无 host 参数，连接超时 15 秒。
  不适合高并发或生产部署。
- 每次启动创建 32 随机字节的 URL-safe capability，常量时间比较，固定 8 小时有效期。
  返回独立的 `local_session` 身份，不冒充 external 企业身份。
- API 默认仍拒绝 DemoIdentityProvider、local_demo 和 local_session；只有受控本机外壳显式
  开启 local_session。开关和适配器属于可信应用代码，不是请求参数。
- fragment 不随 HTTP 请求发送，页面读取后立即移除地址栏 fragment；凭据只保留在 JS 内存，
  不写 Cookie、localStorage 或 sessionStorage，每次 API 请求使用 Bearer 凭据。
- 精确校验 Host、REMOTE_ADDR、Origin 和 Fetch Metadata，无 CORS。静态服务只允许三个固定路径，
  不提供磁盘目录浏览。响应设置 no-store、nosniff、no-referrer、禁止内联脚本/外部资源/嵌入的 CSP。
  访问日志关闭，不输出问题或请求凭据。
- 不可信标题、正文、回答、反馈、错误和引用通过 textContent 显示。Markdown 原样显示，
  不执行 HTML、不自动打开文档链接。写按钮隐藏只是交互优化，权限仍由服务端实时检查。
- 可信范围是本机使用者、终端和浏览器；不防恶意本机进程、浏览器扩展、截屏泄露或系统入侵。
  **禁止公网暴露、反向代理、隧道或端口转发。** 没有生产 SSO、TLS、限流或会话撤销管理。

前端按 Sites 本地 buildless 工作流实现于 supportops/web/dist，没有注册或发布托管 Site。
静态源文件在 .gitignore 中显式放行；报告、截图和运行数据仍在被忽略的 outputs 中。

新增接口：GET /api/workspaces/{ws}/jobs；本机外壳专用 GET /api/session 和
POST /api/workspaces/{ws}/jobs/process-next（空 JSON 对象，editor/admin）。会话历史增加
answer_id，供 UI 重新查答案/引用/反馈；此 ID 不进入模型历史。数据库 schema 仍为 v3。

## 验收结果

P4 新增 19 项 Python 测试，SupportOps 共 143 项；全量 522 项中 518 项通过、4 项真实存储跳过。
浏览器使用隔离临时数据库和 Chrome 配置、
两份随机会话及显式 Fake 故障注入，不访问默认业务资料或用户真实浏览器资料。

13 组浏览器检查通过：锁定/启动链接、上传/发布、只读/空间拒绝、原文与安全渲染、反馈/运行、
390px 窄屏、模型故障历史隔离、会话所有权、版本失败/重试、旧引用/新查询、删除失效、
空库资料不足和断开清理。已检查 1440px 桌面与窄屏截图，无页面横向溢出或未捕获 JS 异常。

| P0 场景 | 实际状态 |
| --- | --- |
| APP-01 无公司资料时拒答 VPN 地址 | Fake 无语义判断，不能据此通过；待真实模型验证 |
| APP-02 空间/身份伪造 | 浏览器 API 路径通过；B 有独立正文、答案与运行，reader 访问均拒绝且不泄漏 |
| APP-03 更新失败、发布、删除 | 浏览器 Fake 流程通过；失败保留 v1、重试发布 v2、删除隐藏旧答案并使引用失效 |
| APP-04 文档假指令 | 仅纯文本安全渲染工程检查通过；真实模型抗提示注入未评测 |
| APP-05 依赖故障 | 浏览器 Fake 索引/模型故障通过；显示服务错误，失败轮次不入历史，运行脱敏 |

没有把原始 cases.json 全部改成 passed，以免改变既有基线摘要并混淆工程与语义评测。
这些是开发 smoke 测试，不是生产安全审计、性能或生成准确率评测。

复现：

```powershell
$env:PYTHON_DOTENV_DISABLED='1'
$env:HELLO_AGENTS_RUN_REAL_STORES='0'
D:\conda-envs\helloagent\python.exe -m unittest test_supportops_workbench -v
node --check supportops/web/dist/app.js
node scripts/supportops_browser_smoke.mjs
D:\conda-envs\helloagent\python.exe -m unittest discover -p 'test_*.py' -q
```

浏览器脚本要求本机 Chrome/Edge 和支持内置 WebSocket 的 Node（实测 24.15.0），不安装新包；
可用 SUPPORTOPS_TEST_PYTHON / SUPPORTOPS_TEST_BROWSER 指定可执行文件。沙箱不允许启动本机
浏览器时需授权，不能用 JS 语法检查冒充浏览器验收。

报告/截图：outputs/supportops/browser/report.json、documents-desktop.png、chat-desktop.png、
chat-mobile.png。脚本结束停止本轮服务器和浏览器，隔离 Chrome QA profile 留在同目录用于诊断，
不属于用户日常浏览器配置。

下一阶段 P5：先准备结构化抽取、语义拒答与注入评测，再分别确认真实 Embedding/LLM 的联网、
凭据及费用。当前不能宣布真实回答准确率或生产就绪。
