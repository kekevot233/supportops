# HelloAgent 框架与业务边界

## 框架提供什么

HelloAgent 是轻量级 Python Agent 框架。SimpleAgent、ReActAgent、ReflectionAgent、
PlanAndSolveAgent 共用 Agent 基类、LLM 适配器、消息和工具契约。
文档知识库、记忆、上下文组织和预算是可组合模块，并不要求每个问题运行全部范式。

## SupportOps 新增什么

SupportOps 是独立业务应用层，面向研发知识与技术支持；首版目标包括文档管理、知识问答、
原文引用和反馈记录。业务身份认证、文档权限、Web API、工单和页面不能仅靠底层框架替代。
当前种子数据只演示项目知识检索；离线 CLI 不包含 Web 服务或真实模型回答。

## 工具的职责

Function Calling 是模型生成结构化调用请求，由 Python 程序校验和执行已注册工具。
工具循环不是模型自己运行程序。默认应用不应因为检索到一条命令就执行它。
