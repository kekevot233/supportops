# 工具调用协议与事务式历史

## 文本和原生工具调用

SimpleAgent 提供 text 和 native 两种工具模式。文本协议使用
[TOOL_CALL:工具名:严格JSON对象]；原生模式使用 assistant.tool_calls 和 tool_call_id。
原生工具响应可以 content=None，只包含工具调用。工具参数必须经 JSON 和 Schema 校验。
ToolRegistry 只执行已注册的工具，工具失败或非法 JSON 不应当被当成成功最终回答。

## 并行调用与结果

模型一次返回多个工具请求时，先检查本批是否超过调用次数上限，再执行工具。
每个结果必须与对应调用 ID 关联，不能在裁剪时留下孤立的 tool 消息。
工具可以产生实际副作用，模型请求失败并不会自动撤销已经执行的工具。

## 失败后的对话历史

Simple、ReAct、Reflection 和 Plan-and-Solve 只在本轮成功结束后提交原始 user 和最终
assistant 消息。中间工具结果、反思稿和失败轮次不会污染长期历史。
流式调用需要正常结束后才提交完整回答；中途关闭或抛出错误不提交半轮历史。
