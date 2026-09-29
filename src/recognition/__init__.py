"""技能证书互认申请后端。

对外提供：

- :class:`~recognition.service.RecognitionService`：领域用例编排；
- :class:`~recognition.store.SqliteStore`：SQLite 事务与持久化；
- :class:`~recognition.models.Principal`：调用方身份与角色；
- :func:`~recognition.service.build_service`：组装默认服务。
"""
from __future__ import annotations

from .errors import (
    CertificateExpiredError,
    DomainError,
    IllegalStateError,
    InvalidCertificateError,
    NotFoundError,
    PermissionDeniedError,
    CreditAlreadyConsumedError,
    DuplicateApplicationError,
)
from .models import CaseStatus, DecisionMode, EntitlementStatus, Principal, Role
from .service import RecognitionService, build_service

__all__ = [
    "RecognitionService",
    "build_service",
    "Principal",
    "Role",
    "CaseStatus",
    "DecisionMode",
    "EntitlementStatus",
    "DomainError",
    "InvalidCertificateError",
    "PermissionDeniedError",
    "NotFoundError",
    "IllegalStateError",
    "DuplicateApplicationError",
    "CreditAlreadyConsumedError",
]
