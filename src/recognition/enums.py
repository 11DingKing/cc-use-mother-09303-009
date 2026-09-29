"""领域枚举。"""
from __future__ import annotations

from enum import StrEnum


class Role(StrEnum):
    APPLICANT = "APPLICANT"  # 毕业生本人
    REVIEWER = "REVIEWER"  # 教务审核员
    REGISTRAR = "REGISTRAR"  # 学籍记账岗位
    ADMIN = "ADMIN"  # 基础数据管理员
    AUTHORITY = "AUTHORITY"  # 认证机构（留作外部核验对接）


class CaseStatus(StrEnum):
    SUBMITTED = "SUBMITTED"  # 申请
    SUPPLEMENTING = "SUPPLEMENTING"  # 待补交材料
    VERIFIED = "VERIFIED"  # 核验通过
    UNDER_EVALUATION = "UNDER_EVALUATION"  # 评估中
    APPROVED = "APPROVED"  # 全部认可并批准
    PARTIALLY_APPROVED = "PARTIALLY_APPROVED"  # 部分认可
    REJECTED = "REJECTED"  # 全部驳回
    POSTED = "POSTED"  # 已记账（权益已消费）
    REVOKED = "REVOKED"  # 决定被撤销


class VerificationStatus(StrEnum):
    VERIFIED = "VERIFIED"  # 颁发机构已确认有效
    PENDING = "PENDING"  # 核验中，阻塞审核流程
    FAILED = "FAILED"  # 核验失败


class ItemVerdict(StrEnum):
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class EntitlementStatus(StrEnum):
    GRANTED = "GRANTED"  # 已写入、可消费
    CONSUMED = "CONSUMED"  # 已记账领取
    REVOKED = "REVOKED"  # 决定撤销（保留行作为墓碑，防止再次领取）
