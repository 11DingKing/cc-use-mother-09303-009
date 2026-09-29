"""领域模型与枚举。"""
from __future__ import annotations

import dataclasses
import enum
import re
from dataclasses import dataclass, field
from datetime import date, datetime


class Role(str, enum.Enum):
    GRADUATE = "graduate"          # 毕业生（证书本人）
    REVIEWER = "reviewer"          # 教务审核员
    ISSUER = "issuer"              # 认证机构
    REGISTRY_ADMIN = "registry_admin"  # 映射发布管理员


class CaseStatus(str, enum.Enum):
    SUBMITTED = "submitted"        # 申请
    PENDING_SUPPLEMENT = "pending_supplement"  # 待补交
    UNDER_REVIEW = "under_review"  # 核验/评估
    APPROVED = "approved"          # 批准
    REJECTED = "rejected"          # 拒绝（全额不认可）
    PARTIALLY_APPROVED = "partially_approved"  # 部分认可
    REVOKED = "revoked"            # 撤销
    CLOSED = "closed"              # 结案（撤销后权益结清）


class DecisionMode(str, enum.Enum):
    FULL = "full"
    PARTIAL = "partial"
    REJECT = "reject"


class EntitlementStatus(str, enum.Enum):
    GRANTED = "granted"            # 已写入可消费权益
    CONSUMED = "consumed"          # 已被项目核销（记账）
    REVOKED = "revoked"            # 撤销，不可再用
    EXPIRED = "expired"


class VerificationStatus(str, enum.Enum):
    PENDING = "pending"
    VERIFIED = "verified"
    FAILED = "failed"


# 证书摘要：算法:十六进制，例如 sha256:64 位 hex
CERT_DIGEST_RE = re.compile(r"^(sha256|sha384|sha512):[0-9a-f]{32,128}$")
CASE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{2,63}$")
PROJECT_CODE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{1,40}$")


@dataclass(frozen=True)
class Principal:
    """调用方身份。``user_id`` 即毕业生学号；授权审核员携带授权案件范围。"""

    user_id: str
    role: Role
    name: str = ""
    # reviewer 被授权查看/处理的案件编号；None 表示不限制（教务主管）
    authorized_case_ids: frozenset[str] | None = None

    def can_access_case(self, case: "Case") -> bool:
        match self.role:
            case Role.GRADUATE:
                return case.student_id == self.user_id
            case Role.REVIEWER:
                return (
                    self.authorized_case_ids is None
                    or case.case_id in self.authorized_case_ids
                )
            case Role.ISSUER:
                return False
            case Role.REGISTRY_ADMIN:
                return False
        return False


@dataclass
class Certificate:
    """境外技能证书（仅保存摘要，不保存原件）。"""

    digest: str                       # 证书摘要
    issuer_code: str                  # 颁发机构代码
    issuer_verification_ref: str      # 颁发机构验证回执编号
    standard_code: str                # 标准代码（如行业标准族）
    standard_version: str             # 标准版本
    grade: str                        # 成绩/等级
    valid_from: date
    valid_until: date                 # 有效期
    holder_student_id: str

    def validate(self, today: date | None = None) -> None:
        if not CERT_DIGEST_RE.match(self.digest):
            raise ValueError("证书摘要格式应为 alg:hex（如 sha256:...）")
        if not self.issuer_code or not self.issuer_verification_ref:
            raise ValueError("颁发机构代码与验证回执不能为空")
        if not self.standard_code or not self.standard_version:
            raise ValueError("标准代码与版本不能为空")
        if not self.grade:
            raise ValueError("成绩范围不能为空")
        if self.valid_from > self.valid_until:
            raise ValueError("证书有效期起始日不得晚于截止日")
        if today is not None and today > self.valid_until:
            raise ValueError("证书已超出有效期")


@dataclass
class MappingRule:
    """已发布映射的单行：标准版本 + 成绩区间 -> 可申请学分权益。"""

    standard_code: str
    standard_version: str
    grade_min: str          # 等级区间下沿（含），按等级表排序比较
    grade_max: str          # 等级区间上沿（含）
    credits: int            # 可减免学分
    published: bool = False


@dataclass
class Case:
    case_id: str
    student_id: str
    project_code: str
    certificate_digest: str
    issuer_code: str
    issuer_verification_ref: str
    standard_code: str
    standard_version: str
    grade: str
    cert_valid_from: date
    cert_valid_until: date
    status: CaseStatus
    requested_credits: int
    mapped_credits: int = 0          # 提交时依据已发布映射计算的可申请上限
    granted_credits: int = 0
    decision_mode: DecisionMode | None = None
    verification: VerificationStatus = VerificationStatus.PENDING
    verification_evidence_ref: str | None = None
    decided_by: str | None = None
    decided_at: datetime | None = None
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime = field(default_factory=datetime.utcnow)
    version: int = 1  # 乐观锁

    def is_open(self) -> bool:
        return self.status in (
            CaseStatus.SUBMITTED,
            CaseStatus.PENDING_SUPPLEMENT,
            CaseStatus.UNDER_REVIEW,
        )


@dataclass
class Supplement:
    id: int | None
    case_id: str
    material_type: str          # 材料类别
    evidence_ref: str           # 加密证据存储引用（只存引用，不存明文）
    submitted_by: str
    submitted_at: datetime
    note: str = ""


@dataclass
class Entitlement:
    """可消费权益（学分）。同一证书的权益被全项目共享，余额原子扣减。"""

    id: int | None
    certificate_digest: str
    holder_student_id: str
    total_credits: int          # 映射计算的总权益
    available_credits: int      # 剩余可申请
    status: EntitlementStatus
    source_case_id: str         # 首次生成权益的案件
    created_at: datetime = field(default_factory=datetime.utcnow)


@dataclass
class ConsumptionRecord:
    """权益消费明细（记账），一行对应一个项目对证书权益的核销。"""

    id: int | None
    entitlement_id: int
    certificate_digest: str
    case_id: str
    project_code: str
    student_id: str
    credits: int
    consumed_at: datetime


@dataclass
class AuditEvent:
    id: int | None
    aggregate_type: str        # case / entitlement / registry
    aggregate_id: str
    event_type: str
    actor_id: str
    actor_role: str
    payload: dict
    occurred_at: datetime
