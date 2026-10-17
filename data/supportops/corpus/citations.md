# 检索重排与原文引用

## HybridReranker 的实际含义

文档检索先向量召回，再由 HybridReranker 融合向量相似度和中英文词项覆盖率重新排序。
当前默认向量权重是 0.75，词项覆盖权重是 0.25。
这不是 BM25 与向量双路独立召回，也不是已经训练好的模型式重排器。

## 引用包含哪些信息

每个结果生成 D1、D2 等引用标签；回答展示为 [D1]、[D2]。
Citation 包含 document_id、chunk_id、source、title、chunk_index、section、page_number、
start_char 和 end_char。原文字符区间对应被索引正文中的位置。
能够定位引用，不等于已经证明生成答案正确；仍要核查原文是否支持对应结论。

## 版本与来源

本演示的 source 使用整理文档的仓库相对路径；原始代码和文档依据记在 manifest 的
source_refs。以后业务应用应保留引用时的文档版本，避免更新后指向其他文本。
