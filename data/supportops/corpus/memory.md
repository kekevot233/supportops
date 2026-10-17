# 工作记忆情景记忆与语义记忆

## WorkingMemory 与 EpisodicMemory

WorkingMemory 是进程内工作记忆，具有容量、TTL 过期、重要性过滤和相关性排序。
EpisodicMemory 是情景记忆，组合 SQLite 正文和可替换向量索引，支持重启后恢复正文。
正文持久化不意味着所有索引都自动持久化；内存向量库需要重建，Qdrant 后端由服务保存。

## SemanticMemory

SemanticMemory 组合正文、向量与知识图谱，描述实体、关系和来源记忆。
默认从结构化 metadata 读取实体与关系；可选 LLMKnowledgeExtractor 需要应用显式注入模型。
现有混合检索不等于可靠的多跳根因推理，未经验证不能宣称自动找到真实故障原因。

## 生命周期

MemoryManager 支持遗忘和跨后端整合；整合采用先写目标再删来源、失败补偿的方式。
可选 SQLiteLifecycleStore 保存幂等日志，帮助重启后对账，但不是跨存储原子事务。
智能体记住案例不会改变 LLM 的模型权重；记忆通过后续检索进入上下文。
