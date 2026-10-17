# GitHub 发布准备

发布目标：[kekevot233/supportops](https://github.com/kekevot233/supportops)。
产品名 SupportOps，Python 导入名仍为 `hello_agents`。2026-10-17，所有者已明确同意补齐
署名、许可与改写说明后公开发布，项目内容采用 CC BY-NC-SA 4.0。不会使用或改动 `fitpilot`。
公开代码不等于部署服务，也不等于允许任意商用。

## 发布内容

- `supportops/` 应用和已有静态页面；`hello_agents/` 框架。
- 根目录 Python 学习案例/测试、`scripts/` 中公开源码、公开基准。
- `data/supportops/` 的人工项目知识与开发评测，`configs/supportops/*.example.json`。
- README、架构/阶段文档、来源说明、安全/贡献说明、离线 CI 和占位符配置。
- 仅一张经目视审核的公开夹具截图：`docs/assets/supportops-documents.png`。

排除真实 `.env*`、IDE 配置、`HANDOFF.md`、`outputs/`、数据库、日志、模型权重、浏览器资料、
原始真实运行报告及未列入允许清单的文件。`PROJECT_SUMMARY.md` 保留以支持语料的 source_refs；
课程全文不打包。根目录早期日报案例不会自动执行，但真实运行可能付费或发邮件。

## 本机候选检查

```powershell
python -m unittest test_release_preparation -v
python scripts/check_release.py --export
```

检查器只遍历明确允许的源码目录，不打开真实配置，不遍历 outputs。
它也读取允许范围内的**已暂存 blob**以发现“工作区已修好、索引仍有问题”的情况；被排除的
暂存路径只报路径、不读内容。没有 Git 历史扫描，也不替代专业密钥扫描/人工检查。
报告仅返回路径、规则、行号，不返回匹配内容。截图不做 OCR，需单独目视检查。
三个已核对的负例/MockTransport 测试值仅按“确切路径 + 值摘要”豁免，不跳过整个测试目录。

输出 ZIP 位于 `outputs/release/Supportops-review-<时间>.zip`，含精确文件 SHA-256 清单
`RELEASE_REVIEW.json`，旁边另存同名 `.json` 便于直接审阅；报告标记
`local_review_only`。`public_release_ready` 需要候选扫描与 Git 索引通过，以及已记录的所有者
许可决定和确切官方 LICENSE 摘要相符；仅出现一个名为 LICENSE 的文件不足以通过。
该标志不代表自动获准发布，不是法律或安全认证。
检查和导出不修改索引；工作区候选通过也不代表当前索引可直接提交。导出会拒绝敏感规则命中、
路径问题或待封包快照与清单摘要不符；仅打包已审计字节，不重新读取后来变更的源码。
现有索引的排除项/旧版本会单独报告。
请人工复核清单，确认没有真实资料、启动链接或未经许可内容。

## 发布决策与检查点

1. **来源与许可证范围**：已核对教程根许可和独立框架学习分支，保守沿用 CC BY-NC-SA 4.0，
   不套 MIT。已保留原作者署名、范围表和本地改写说明，见 [第三方说明](../THIRD_PARTY_NOTICES.md)。
   第三方依赖仍按各自许可，本次不是逐行原创性鉴定或完整依赖合规审计。
2. **现有索引**：已按用户授权整理候选路径（添加 LICENSE 后共 219 项），移除 5 个 IDE 配置及一个已删除的
   错拼文件的暂存记录，本地文件保留；原索引在被忽略的 outputs/release 中备份。
   发布检查区分 Git 的 CRLF→LF 规范化与真实源码变更，不把 Windows 换行符转换误报为旧版本。
3. **所有者最终审阅**：已确认 `kekevot233/supportops`、公开可见、CC BY-NC-SA 4.0。
   本地提交使用 GitHub noreply 邮箱，上传使用 Git 命令行；不覆盖已有仓库，不强制推送。

CI 默认跑 SupportOps、发布专项和
检索基线；完整回归需 Actions 手动勾选。依赖文件固定已验证的顶层版本，不是完整传递依赖锁。
不在 CI 存放任何模型密钥，也不将 localhost 演示服务部署到 GitHub Pages。

## 发布前复现建议

在独立临时目录解开候选包，从 `Supportops/` 运行 README 的无密钥命令，检查引用、种子导入和
测试；不复用旧数据库或 `.env`。公开开发集不是盲测，README 的 25/25、20/32 和真实三题要分开
陈述。GitHub 运行通过、模型质量达标和生产就绪是三个不同结论。

## 2026-10-17 本机验证

最初候选 218 个文件，规则扫描无未处理命中；当时原索引含 5 个 IDE 路径与 20 个旧版本路径。
后续已整理，新增 LICENSE 后为 219 项。初次发布专项 15 项中 14 项通过、1 项因 Windows
符号链接权限跳过；后续补充换行符与许可决定测试（另有无需权限的模拟防护测试）。
SupportOps 264 项全部通过。候选包解压到独立目录，确认从候选源码导入，检索 25/25；
首次 init → import-seed → 10 次 work 后可列出 10 份文档。此验证复用本机已装依赖，
不是全新环境依赖安装、Linux/GitHub CI 或完整回归的验证；真实模型请求为 0。
