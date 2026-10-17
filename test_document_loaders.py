import tempfile
import unittest
from pathlib import Path

from hello_agents import (
    DocumentChunker,
    PDFDocumentLoader,
    RAGError,
    WordDocumentLoader,
)


class FakePage:
    def __init__(self, text=None, error=None):
        self.text = text
        self.error = error

    def extract_text(self):
        if self.error is not None:
            raise self.error
        return self.text


class FakePDFReader:
    def __init__(self, pages, *, encrypted=False, decrypt_result=1):
        self.pages = pages
        self.is_encrypted = encrypted
        self.decrypt_result = decrypt_result
        self.passwords = []

    def decrypt(self, password):
        self.passwords.append(password)
        return self.decrypt_result


class FakeStyle:
    def __init__(self, name):
        self.name = name


class FakeParagraph:
    def __init__(self, text, style="Normal"):
        self.text = text
        self.style = FakeStyle(style)


class FakeCell:
    def __init__(self, text):
        self.text = text


class FakeRow:
    def __init__(self, *cells):
        self.cells = [FakeCell(cell) for cell in cells]


class FakeTable:
    def __init__(self, rows):
        self.rows = [FakeRow(*row) for row in rows]


class FakeCoreProperties:
    title = "内部标题"


class FakeWordDocument:
    def __init__(self):
        self.paragraphs = [
            FakeParagraph("安装", "Heading 1"),
            FakeParagraph("使用 pip 安装。"),
            FakeParagraph("配置", "Heading 2"),
            FakeParagraph("设置 API 地址。"),
        ]
        self.tables = [
            FakeTable([["参数", "说明"], ["timeout", "超时时间|秒"]])
        ]
        self.core_properties = FakeCoreProperties()


class TestPDFDocumentLoader(unittest.TestCase):
    def test_extracts_pages_and_chunker_preserves_page_number(self):
        reader = FakePDFReader(
            [
                FakePage("第一页内容。" * 8),
                FakePage("第二页向量检索说明。" * 8),
            ]
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "guide.pdf"
            path.write_bytes(b"fake-pdf")
            document = PDFDocumentLoader(
                reader_factory=lambda _: reader
            ).load(path)

        chunks = DocumentChunker(
            chunk_size=70,
            chunk_overlap=0,
            min_chunk_chars=10,
        ).chunk(document)

        self.assertEqual(document.metadata["source_format"], "pdf")
        self.assertEqual(document.metadata["_page_spans"][0]["page_number"], 1)
        self.assertEqual(chunks[0].page_number, 1)
        self.assertEqual(chunks[-1].page_number, 2)
        self.assertNotIn("_page_spans", chunks[0].metadata)

    def test_encrypted_pdf_requires_valid_password(self):
        reader = FakePDFReader(
            [FakePage("secret")], encrypted=True, decrypt_result=0
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "secret.pdf"
            path.write_bytes(b"encrypted")
            loader = PDFDocumentLoader(reader_factory=lambda _: reader)
            with self.assertRaises(RAGError):
                loader.load(path, password="wrong")

        self.assertEqual(reader.passwords, ["wrong"])

    def test_rejects_scanned_empty_pdf_and_wraps_page_failure(self):
        readers = [
            FakePDFReader([FakePage(None), FakePage(" ")]),
            FakePDFReader([FakePage(error=RuntimeError("secret"))]),
        ]
        for reader in readers:
            with self.subTest(reader=reader), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "scan.pdf"
                path.write_bytes(b"fake")
                with self.assertRaises(RAGError) as caught:
                    PDFDocumentLoader(reader_factory=lambda _, r=reader: r).load(path)
                self.assertNotIn("secret", str(caught.exception))

    def test_pdf_loader_rejects_wrong_extension_and_bad_factory(self):
        with self.assertRaises(RAGError):
            PDFDocumentLoader(reader_factory=object())
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "file.txt"
            path.write_text("not pdf", encoding="utf-8")
            with self.assertRaises(RAGError):
                PDFDocumentLoader(reader_factory=lambda _: None).load(path)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "file.pdf"
            path.write_bytes(b"fake")
            with self.assertRaises(RAGError):
                PDFDocumentLoader(
                    reader_factory=lambda _: FakePDFReader([FakePage("text")])
                ).load(path, metadata=[])


class TestWordDocumentLoader(unittest.TestCase):
    def test_extracts_headings_body_table_and_core_title(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "guide.docx"
            path.write_bytes(b"fake-docx")
            document = WordDocumentLoader(
                document_factory=lambda _: FakeWordDocument()
            ).load(path)

        self.assertEqual(document.title, "内部标题")
        self.assertEqual(document.metadata["source_format"], "docx")
        self.assertIn("# 安装", document.content)
        self.assertIn("## 配置", document.content)
        self.assertIn("| 参数 | 说明 |", document.content)
        self.assertIn("超时时间\\|秒", document.content)
        chunks = DocumentChunker(chunk_size=500).chunk(document)
        self.assertEqual(chunks[0].section, "安装")

    def test_explicit_title_overrides_core_title(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "guide.docx"
            path.write_bytes(b"fake")
            document = WordDocumentLoader(
                document_factory=lambda _: FakeWordDocument()
            ).load(path, title="公开标题")
        self.assertEqual(document.title, "公开标题")

    def test_rejects_empty_docx_legacy_doc_and_parse_errors(self):
        empty = FakeWordDocument()
        empty.paragraphs = []
        empty.tables = []
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            docx_path = root / "empty.docx"
            docx_path.write_bytes(b"fake")
            doc_path = root / "legacy.doc"
            doc_path.write_bytes(b"fake")
            with self.assertRaises(RAGError):
                WordDocumentLoader(document_factory=lambda _: empty).load(docx_path)
            with self.assertRaises(RAGError):
                WordDocumentLoader(document_factory=lambda _: empty).load(doc_path)
            with self.assertRaises(RAGError) as caught:
                WordDocumentLoader(
                    document_factory=lambda _: (_ for _ in ()).throw(
                        RuntimeError("secret")
                    )
                ).load(docx_path)
            self.assertNotIn("secret", str(caught.exception))

    def test_word_loader_validates_factory(self):
        with self.assertRaises(RAGError):
            WordDocumentLoader(document_factory=object())


if __name__ == "__main__":
    unittest.main()
