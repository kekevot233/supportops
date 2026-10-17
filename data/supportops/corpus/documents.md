# 文档加载和格式限制

## 支持的文档格式

LocalTextDocumentLoader 读取 UTF-8 的 TXT、MD 和 Markdown 文档。
PDFDocumentLoader 使用可选 pypdf；WordDocumentLoader 使用可选 python-docx 读取 DOCX。
文档必须是可读取的普通文件，扩展名、大小和非空正文需要校验。

## 扫描版 PDF

扫描版 PDF 如果没有文字层，就没有可提取文本，当前需要先做 OCR。
当前框架没有实现 OCR；仅把扫描图片装进 PDF 不会自动变成可检索文字。
加密 PDF 需要显式提供有效密码，解析器错误必须转为安全的公开错误。

## 旧版 Word 与版面

旧版 .doc 不受支持；需要转换为 DOCX 或其他受支持格式。
DOCX 会保留标题层级并把表格转为 Markdown；PDF 按页提取正文并保留页码映射。
复杂 PDF 版面还原、图片内容理解和扫描件 OCR 不属于当前文档解析能力。
