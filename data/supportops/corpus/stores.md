# SQLite Qdrant Neo4j 与集成验证

## 正文和向量分工

SQLiteMemoryStore 保存完整记忆或文档块正文及元数据，Qdrant 保存向量索引。
正文与向量通过业务 ID 关联；Qdrant 适配器把非 UUID 业务 ID 确定性映射为 UUID，
同时保留原始业务 ID，使上层不必把自己的 ID 格式绑定到 Qdrant。

## Neo4j 与共享实体

Neo4jGraphStore 保存实体、关系和来源记忆归属。共享实体可能被多条记忆引用，
删除一条记忆不能误删仍被其他来源引用的共享实体。
查询和删除具有 user_id 过滤，但这些过滤不能替代应用身份认证和空间访问控制。

## 真实存储测试

test_real_stores.py 默认跳过真实存储测试。只有显式设置 HELLO_AGENTS_RUN_REAL_STORES=1
并提供本机服务地址和测试凭据，才会运行真实兼容性验证。
普通离线回归应使用 Fake 或内存后端，不能因为执行测试而隐式访问真实外部服务。
本机单节点兼容性通过不等于云托管、TLS、集群或压力测试已通过。
