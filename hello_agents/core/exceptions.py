"""HelloAgents 框架统一使用的异常类型。"""


class HelloAgentsError(Exception):
    """所有 HelloAgents 框架异常的共同父类。"""


class ConfigurationError(HelloAgentsError):
    """配置缺失、格式错误或取值无效。"""


class LLMError(HelloAgentsError):
    """大语言模型客户端初始化或调用失败。"""


class AgentError(HelloAgentsError):
    """Agent 初始化、推理循环或输出解析失败。"""


class ToolError(HelloAgentsError):
    """工具注册、参数校验或执行失败。"""


class MemorySystemError(HelloAgentsError):
    """记忆配置、存储或检索失败。"""


class EmbeddingError(HelloAgentsError):
    """文本向量化配置、模型加载或编码失败。"""


class VectorStoreError(HelloAgentsError):
    """向量存储配置、写入、删除或检索失败。"""


class GraphStoreError(HelloAgentsError):
    """图存储配置、写入、删除或查询失败。"""


class RAGError(HelloAgentsError):
    """文档加载、切块、摄取、检索或引用生成失败。"""


class ContextEngineeringError(HelloAgentsError):
    """上下文汇集、筛选、结构化或压缩失败。"""
