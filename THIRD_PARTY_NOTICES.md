# 来源、致谢、许可证与改写说明

本项目是学习 Hello-Agents 教程并借助 AI 实现、扩展的求职演示工程，后续发展为 SupportOps。
不声明全部代码由作者独立原创。以下说明不替代逐文件来源核对，也不是法律意见。

## Hello-Agents

- 作者/组织：Datawhale 及 Hello-Agents 贡献者。
- 来源：[datawhalechina/hello-agents](https://github.com/datawhalechina/hello-agents)。
- 参考章节：Agent 框架、记忆与检索、上下文工程等。
- 2026-10-17 核对的上游根 [LICENSE.txt](https://github.com/datawhalechina/hello-agents/blob/main/LICENSE.txt)
  标示 **CC BY-NC-SA 4.0**。根许可证不自动证明所有子目录、示例及依赖的授权范围相同。
- 根据已核对的教程根许可和框架学习分支许可，本项目对相关学习实现和改写保守沿用
  **CC BY-NC-SA 4.0**，不将其重新标为 MIT，不宣称允许任意商用。具体来源范围见下表。

### 2026-10-17 进一步核对

核对的教程树版本：`36f583c233ea5cee4537ef93455702b55053af18`，可见
[固定版本树](https://github.com/datawhalechina/hello-agents/tree/36f583c233ea5cee4537ef93455702b55053af18)。
该版本 `code/chapter4`、`code/chapter6`、`code/chapter7`、`code/chapter8`、
`code/chapter9` 未发现对应目录内独立的 LICENSE/pyproject 许可声明；其他共创项目的 MIT
许可证不能据此应用到这些章节。未把尚未合并 PR 中的 MIT 字样当作有效授权。

教程 README 指向的框架为 [jjyaoao/HelloAgents](https://github.com/jjyaoao/HelloAgents)。
其 [学习分支 LICENSE](https://github.com/jjyaoao/HelloAgents/blob/learn_version/LICENSE)
同样标示 CC BY-NC-SA 4.0。因此目前没有证据将教程相关框架实现统一判定为 MIT。
本项目没有可重建最初逐行复制历史的既有 Git 提交；下列是保守的来源/扩展范围记录，
不是“未列出逐行复制就证明全是原创”的结论。

| 本地范围 | 参考来源与本地改动 |
| --- | --- |
| 根目录 Agent、planner/executor、tools、memory、prompts、llm_client 学习实现 | 教程第 1/4 章的基本循环、ReAct、Plan-and-Solve、Reflection 与工具调用，另有本地改写 |
| `hello_agents/core/`、`agents/`、`tools/` | 第 7 章统一框架/工具思路；本地扩展严格校验、原生调用、预算协调和失败历史隔离 |
| `hello_agents/memory/`、`embeddings/`、`vector_stores/`、`graph_stores/`、`rag/` | 第 8 章记忆与检索主题；本地扩展确定性编码、存储适配、持久化及引用校验 |
| `hello_agents/context/`、笔记与文件探索工具 | 第 9 章上下文工程主题；本地扩展预算、压缩、检查点和路径安全边界 |
| 日报与 LangGraph 案例 | 学习型应用扩展，依赖 LangGraph 等公开接口，不捆绑其依赖源码 |
| `supportops/`、`test_*.py`、评测与公开种子资料 | 在上述框架上通过 AI 辅助实现、测试和整理；Onyx 仅作为产品职责参考 |

署名保留给 Datawhale、Hello-Agents 贡献者及 HelloAgents 框架作者/贡献者；本地改写与扩展
由本项目维护者 kekevot233 负责。2026-10-17，仓库所有者已明确同意：以求职展示为目标，
补齐署名、许可与改写说明后公开 `kekevot233/supportops`，项目内容采用同一 CC BY-NC-SA 4.0。

## Onyx

- 来源：[onyx-dot-app/onyx](https://github.com/onyx-dot-app/onyx)。
- 用途：参考知识工作台、接入/检索/引用/权限的产品职责划分，而非复制其整个实现。
- 目前未引入其客户数据、企业版代码或数据库；公开种子资料是本项目人工整理的项目说明。
- 上游 [LICENSE](https://github.com/onyx-dot-app/onyx/blob/main/LICENSE) 对非企业版与企业版目录区分授权。
  不能把非企业版许可证扩展到企业版。如后续实际复用代码，需另行记录路径、版本与授权。

## 依赖与资产

依赖仅通过 Python 包声明安装，未将第三方依赖源码、预训练权重或 Docker 镜像打入候选包。
依赖各自的许可证仍适用；这不是完整 SBOM 或自动合规认证。
`docs/assets/supportops-documents.png` 是本项目本机公开离线夹具的工作台截图，已单独目视检查，
未复制 Onyx 图片。课程全文、真实配置、客户资料和浏览器个人资料不属于发布内容。

## 本项目许可与修改标记

Release license decision: CC-BY-NC-SA-4.0 (owner confirmed 2026-10-17).

- 本地新增与修改部分：Copyright (c) 2026 kekevot233；上游相关部分版权归原作者及贡献者。
- 除单独标示的第三方材料外，本仓库发布的源码、文档、测试、人工整理种子资料及自制截图，
  按根目录 [LICENSE](LICENSE) 的 CC BY-NC-SA 4.0 提供。该许可不授予维护者未拥有的权利。
- 与教程相比，本地增加/改写了严格 Schema、工具调用边界、失败历史隔离、存储补偿、版本引用、
  上下文预算、工作台、离线/真实评测适配和发布工具；上表标记具体目录，不宣称为上游原版。
- 署名不意味着 Datawhale、HelloAgents 或 Onyx 为本项目背书。未引入教程全文或第三方企业资料。
- 未捆绑的依赖及其商标、专利等权利不由本项目重新授权；未来商业使用需按具体来源另外核对。

这是来源核对与所有者许可决定记录，不是法律意见、逐行原创性鉴定或完整依赖合规审计。
