"""显式 SDK 工厂组合；仅供评测库调用，不把外部运行接到本机演示页面。"""

from threading import Lock

from .document_index import EmbeddedDocumentIndex, LocalDocumentIndex
from .provider_adapters import ChatSDKConfig, CompatibleChatClient, CompatibleEmbedder, EmbeddingSDKConfig, ProviderCallLedger
from .qa_runtime import EvaluationRuntime, RuntimeSpec


class _SDKDocumentIndex(EmbeddedDocumentIndex):
    def close(self):
        self.embedder.close()


def compatible_runtime(spec: RuntimeSpec, chat: ChatSDKConfig, *, chat_sdk_factory,
                       embedding: EmbeddingSDKConfig | None = None, embedding_sdk_factory=None, limits=None):
    """工厂必须每次返回新的、由本运行时独占的 SDK root；不得共享客户端连接所有权。"""
    spec = RuntimeSpec.model_validate(spec.model_dump())
    chat = ChatSDKConfig.model_validate(chat.model_dump())
    if spec.execution != "external" or chat.model != spec.model_id or not callable(chat_sdk_factory):
        raise ValueError("在线 SDK 运行时必须显式标为 external 并匹配模型。")
    if embedding is not None:
        embedding = EmbeddingSDKConfig.model_validate(embedding.model_dump())
        if embedding.model != spec.embedding_id or not callable(embedding_sdk_factory):
            raise ValueError("Embedding 工厂与身份不匹配。")
    elif embedding_sdk_factory is not None or (spec.embedding_id, spec.embedding_revision) != (LocalDocumentIndex.embedding_id, LocalDocumentIndex.embedding_revision):
        raise ValueError("未提供在线编码器时，只允许原 Hashing 基线。")
    ledger = ProviderCallLedger(limits)
    opening_lock, opened = Lock(), False

    def index_factory():
        nonlocal opened
        # 每次评测需新建组合，防止复用后把两轮用量/限额混入同一份报告。
        with opening_lock:
            if opened:
                raise ValueError("SDK 评测运行时只能打开一次。")
            opened = True
        if embedding is None:
            return LocalDocumentIndex()
        embedder = CompatibleEmbedder(embedding_sdk_factory(), embedding, ledger=ledger, owns_client=True)
        try:
            return _SDKDocumentIndex(embedder, embedding_id=spec.embedding_id, embedding_revision=spec.embedding_revision)
        except Exception:
            embedder.close()
            raise

    def client_factory():
        return CompatibleChatClient(chat_sdk_factory(), chat, ledger=ledger, owns_client=True)

    def telemetry():
        return {**ledger.snapshot(), "settings": {"implementation": "openai-compatible-sdk-v1",
            "chat": chat.model_dump(), "embedding": embedding.model_dump() if embedding else None,
            "automatic_retries": 0}}

    return EvaluationRuntime(spec, index_factory, client_factory, telemetry=telemetry)


__all__ = ["compatible_runtime"]
