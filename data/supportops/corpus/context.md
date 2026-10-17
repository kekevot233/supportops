# 临时上下文与请求预算

## 文档上下文是否进入历史

DocumentContextProvider 把带引用的检索结果编码为有界 JSON，并标记为不可信参考资料。
检索上下文只用于本轮，不写入长期对话历史。Agent 单次运行只检索一次，工具循环和
多阶段推理复用结果；运行结束后清理临时上下文。

## ContextBuilder 的来源配额

ContextBuilder 汇集、评分、选择、结构化和压缩资料。source_max_tokens 按来源名称限制
所有入选正文的累计 Token 数，0 表示禁用该来源，不是给每条资料分别分配相同额度。
系统规则和当前任务不会被静默截断；强制内容超预算时明确报错。

## 完整请求预算

RequestBudgetManager 复核系统消息、历史、工具 Schema、本轮工具结果和输出预留。
超限时先按完整轮次移除最旧历史副本，再整块移除临时检索资料。
当前任务和本轮 assistant/tool_call_id 工具链保持完整，仍超限则阻止下一次模型调用。

输出预留优先使用本次 max_tokens，其次 LLM.max_tokens，最后预算默认 output_tokens。
同一输出上限传入实际模型调用。服务隐藏聊天模板仍需通过安全余量校准。
