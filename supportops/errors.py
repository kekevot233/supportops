"""应用层的稳定错误，不向调用者返回 SQLite 或身份后端详情。"""

_ERRORS = {
    "PILOT_CONFIG_INVALID": (400, "专用试跑配置无效；请检查三项配置、官方地址和 deepseek-flash 模型名。"),
    "PILOT_ALREADY_RESERVED": (409, "本轮试跑授权已预留或使用；不会自动重复发起请求。"),
    "PILOT_PROCESS_FAILED": (503, "隔离试跑未正常结束；不自动重试，已保留授权预留记录。"),
    "PREFLIGHT_INVALID": (400, "试跑预检失败，请检查非敏感配置、样本、资料摘要和输出路径。"),
    "EXTERNAL_CALLS_DISABLED": (403, "评测未授权外部调用。"),
    "EVALUATION_SETUP_FAILED": (503, "评测运行时初始化失败。"),
    "EVALUATION_LIMIT_EXCEEDED": (429, "评测请求或预留用量已达本地限额。"),
    "INVALID_REQUEST": (400, "请求参数无效。"),
    "UNAUTHENTICATED": (401, "请提供有效身份。"),
    "FORBIDDEN": (403, "当前角色无权执行此操作。"),
    "NOT_FOUND": (404, "资源不存在或无权访问。"),
    "CONFLICT": (409, "资源状态冲突。"),
    "SOURCE_GONE": (410, "文档已删除，原文与引用已失效。"),
    "UPLOAD_TOO_LARGE": (413, "文档超过 256000 字节的大小限制。"),
    "REQUEST_TOO_LARGE": (413, "请求正文超过大小限制。"),
    "INDEX_UNAVAILABLE": (503, "文档索引暂时不可用。"),
    "MODEL_UNAVAILABLE": (503, "回答服务暂时不可用。"),
    "MODEL_RESPONSE_INVALID": (502, "回答格式或原文引用校验未通过。"),
    "CONTEXT_BUDGET_EXCEEDED": (422, "当前问题或证据超出上下文预算。"),
    "SOURCE_CHANGED": (409, "回答期间资料已更新或删除，请重新提问。"),
    "RUN_EXPIRED": (409, "上一次回答执行已过期，请使用新的请求标识重试。"),
    "API_UNAVAILABLE": (503, "应用接口暂时不可用。"),
    "CAPACITY_EXCEEDED": (503, "本地演示容量已达上限。"),
    "DEMO_DISABLED": (403, "必须显式启用本地演示模式。"),
    "STORAGE_UNAVAILABLE": (503, "应用数据暂时不可用。"),
    "STORAGE_VERSION_UNSUPPORTED": (503, "数据库格式不受支持，请检查应用版本。"),
}


class SupportOpsError(Exception):
    def __init__(self, code: str, *, run_id: str | None = None):
        self.code = code
        self.run_id = run_id
        self.status, self.message = _ERRORS[code]
        super().__init__(self.message)

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message, **({"run_id": self.run_id} if self.run_id else {})}


__all__ = ["SupportOpsError"]
