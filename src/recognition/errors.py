"""领域错误：携带稳定错误码，供 API 层映射 HTTP 状态。"""
from __future__ import annotations


class DomainError(Exception):
    """所有领域规则违反的基类。"""

    code = "domain_error"
    http_status = 409

    def __init__(self, message: str, *, details: dict | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}


class ValidationError(DomainError):
    code = "validation_error"
    http_status = 400


class InvalidCertificateError(DomainError):
    """证书摘要格式、有效期或成绩不合法。"""

    code = "invalid_certificate"
    http_status = 400


class PermissionDeniedError(DomainError):
    code = "permission_denied"
    http_status = 403


class NotFoundError(DomainError):
    code = "not_found"
    http_status = 404


class IllegalStateError(DomainError):
    """案件当前状态不允许该动作（含补交、撤销窗口等约束）。"""

    code = "illegal_state"
    http_status = 409


class DuplicateApplicationError(DomainError):
    """同一证书在同一项目存在未结案申请。"""

    code = "duplicate_application"
    http_status = 409


class MappingConflictError(DomainError):
    """映射区间重叠或映射已发布不可修改。"""

    code = "mapping_conflict"
    http_status = 409


class NoPublishedMappingError(DomainError):
    code = "no_published_mapping"
    http_status = 422


class CertificateExpiredError(DomainError):
    code = "certificate_expired"
    http_status = 422


class CreditAlreadyConsumedError(DomainError):
    """证书权益余额不足，批准会造成跨项目重复领取。"""

    code = "credit_already_consumed"
    http_status = 409


class AlreadyDecidedError(DomainError):
    """案件已经生成过决定/消费记录，防止重复记账。"""

    code = "already_decided"
    http_status = 409


class EntitlementClosedError(DomainError):
    code = "entitlement_closed"
    http_status = 409
