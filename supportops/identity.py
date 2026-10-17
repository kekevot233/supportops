"""本地演示身份适配器，不提供密码登录、令牌验证或企业认证。"""

from __future__ import annotations

from typing import Protocol

from .errors import SupportOpsError
from .models import Identity


class IdentityProvider(Protocol):
    """适配器位于可信应用边界；未来认证实现必须验证凭据后才返回 Identity。"""

    def authenticate(self, credential: str | None) -> Identity: ...


class DemoIdentityProvider:
    # 这些是公开演示档案，不是口令；不可将此适配器挂到对外 HTTP 服务。
    _PROFILES = {"admin": "demo-admin", "reader": "demo-reader"}

    def __init__(self, *, enabled: bool = False):
        if enabled is not True:
            raise SupportOpsError("DEMO_DISABLED")

    def authenticate(self, credential: str | None) -> Identity:
        if not isinstance(credential, str) or credential not in self._PROFILES:
            raise SupportOpsError("UNAUTHENTICATED")
        return Identity(subject=self._PROFILES[credential], auth_mode="local_demo")


__all__ = ["IdentityProvider", "DemoIdentityProvider"]
