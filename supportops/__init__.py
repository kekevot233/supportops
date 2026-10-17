"""SupportOps 应用层：离线检索、持久化空间与演示权限服务。"""

from .database import ApplicationDatabase
from .errors import SupportOpsError
from .documents import DocumentService
from .answering import AnswerEngine, DemoQuoteLLM
from .chat import ChatService
from .document_index import LocalDocumentIndex
from .identity import DemoIdentityProvider, IdentityProvider
from .seed import SeedCorpus, load_seed_corpus, open_seed_index
from .workspaces import WorkspaceService

__all__ = ["SeedCorpus", "load_seed_corpus", "open_seed_index", "ApplicationDatabase",
           "SupportOpsError", "IdentityProvider", "DemoIdentityProvider", "WorkspaceService",
           "DocumentService", "LocalDocumentIndex", "AnswerEngine", "DemoQuoteLLM", "ChatService"]
