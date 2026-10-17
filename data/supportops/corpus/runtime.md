# 安装与离线 Embedding

## 安装要求

项目要求 Python 3.10 或更高版本。开发安装命令是 `pip install -e .`。
基础依赖是 openai 和 pydantic；本地模型、向量 SDK、图 SDK、文档解析器各有可选依赖。
安装本身不会代替模型配置，也不会自动启动 Qdrant 或 Neo4j。

## HashingEmbedder 是什么

HashingEmbedder 使用 BLAKE2b 确定性特征哈希，把中英文词项映射到固定维度向量。
它无需模型下载和外部 API，适合作为离线开发基线，不是训练过的语义 Embedding 模型。
维度必须与向量存储一致；更换编码方案时应重建索引，不能直接混用不同空间的向量。

## 本地模型

LocalSentenceTransformerEmbedder 可适配已经存在的本地模型路径或注入模型。
真实中文问答质量需要另外评测，本项目离线测试的通过数量不能代表真实模型准确率。
