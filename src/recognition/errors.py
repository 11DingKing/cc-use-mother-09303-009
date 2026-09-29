"""领域错误类型。

所有业务规则违反都抛出 ``DomainError`` 子类，HTTP 层据此映射状态码。
"""
from __future__ import annotations


class DomainError(Exception):
    """所有可预期业务错误的基类。"""

    code = "domain_error"
    http_status = 400

    def __init__(self, message: str, *, details: dict | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}

    def to_dict(self) -> dict:
        return {"error": self.code, "message": self.message, "details": self.details}


class ValidationError(DomainError):
    """输入数据不满足格式或取值约束。"""

    code = "validation_error"
    http_status = 400


class NotFound(DomainError):
    """资源不存在（对无权查看者同样返回 404，避免存在性泄露）。"""

    code = "not_found"
    http_status = 404


class PermissionDenied(DomainError):
    """身份认证或资源授权失败。"""

    code = "permission_denied"
    http_status = 403


class StateConflict(DomainError):
    """案件当前状态不允许该动作。"""

    code = "state_conflict"
    http_status = 409


class SupplementDeadlinePassed(StateConflict):
    """超过补交截止时间，补交通道关闭。"""

    code = "supplement_deadline_passed"


class VerificationInvalid(StateConflict):
    """颁发机构核验未通过，案件不能进入评估。"""

    code = "verification_invalid"


class CredentialExpired(StateConflict):
    """批准时证书已超出有效期。"""

    code = "credential_expired"


class MappingChanged(StateConflict):
    """批准时已发布映射发生停用或变更，需重新生成案件。"""

    code = "mapping_changed"


class DuplicateCredential(StateConflict):
    """同一证书摘要指纹已注册。"""

    code = "duplicate_credential"


class DuplicateEntitlement(StateConflict):
    """同一证书的同一课程权益已经在另一个案件中写入，防重复消费触发。"""

    code = "duplicate_entitlement"


class NoEligibleBenefit(ValidationError):
    """当前没有任何符合条件的已发布映射权益。"""

    code = "no_eligible_benefit"
