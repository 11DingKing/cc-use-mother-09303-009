"""领域用例编排：申请、补交、核验、评估、批准记账、撤销与追溯。

所有写操作在单个 ``BEGIN IMMEDIATE`` 事务内完成。批准动作的关键序列为：

    读取/建立证书权益（UNIQUE: certificate_digest）
    -> 条件扣减 available_credits（余额不足则 0 行，回滚）
    -> 写消费明细（UNIQUE: case_id）
    -> 案件置为已批准

任一步失败整体回滚，因此跨项目并发批准时，同一张证书的学分权益
不可能被两个项目重复领取。
"""
from __future__ import annotations

import secrets
from datetime import date, datetime, timedelta
from typing import Any

from .errors import (
    CertificateExpiredError,
    CreditAlreadyConsumedError,
    DuplicateApplicationError,
    IllegalStateError,
    InvalidCertificateError,
    NotFoundError,
    PermissionDeniedError,
    ValidationError,
)
from .models import (
    CASE_ID_RE,
    PROJECT_CODE_RE,
    Case,
    CaseStatus,
    Certificate,
    ConsumptionRecord,
    DecisionMode,
    Entitlement,
    EntitlementStatus,
    Principal,
    Role,
    Supplement,
    VerificationStatus,
)
from .registry import MappingRegistry
from .store import SqliteStore


class RecognitionService:
    def __init__(self, store: SqliteStore, *,
                 revoke_window_days: int = 30,
                 clock: Any = date,
                 now_provider: Any = datetime.utcnow) -> None:
        self.store = store
        self.revoke_window = timedelta(days=revoke_window_days)
        # clock/now_provider 可注入固定时间，便于测试有效期与撤销窗口
        self._date = clock if callable(clock) else date.today
        self._now = now_provider if callable(now_provider) else datetime.utcnow

    # ============================================================ 毕业生：申请

    def submit_application(self, principal: Principal, payload: dict) -> dict:
        """接收证书摘要、颁发机构验证、标准版本、成绩范围和有效期，生成案件。"""
        if principal.role is not Role.GRADUATE:
            raise PermissionDeniedError("仅毕业生本人可以提交申请")
        data = dict(payload)
        student_id = str(data.get("student_id") or "").strip()
        if student_id != principal.user_id:
            raise PermissionDeniedError("不得为他人提交证书互认申请")

        case_id = str(data.get("case_id") or "").strip() or self._new_case_id()
        if not CASE_ID_RE.match(case_id):
            raise ValidationError("案件编号需为 3~64 位字母、数字、下划线或短横线")
        project_code = str(data.get("project_code") or "").strip()
        if not PROJECT_CODE_RE.match(project_code):
            raise ValidationError("项目代码格式不合法")

        cert = self._build_certificate(student_id, data)

        with self.store.transaction() as conn:
            if self.store.get_case_row(conn, case_id):
                raise DuplicateApplicationError(f"案件编号 {case_id} 已存在")
            mapped_credits = MappingRegistry(conn).credits_for(
                cert.standard_code, cert.standard_version, cert.grade
            )

            case = Case(
                case_id=case_id,
                student_id=student_id,
                project_code=project_code,
                certificate_digest=cert.digest,
                issuer_code=cert.issuer_code,
                issuer_verification_ref=cert.issuer_verification_ref,
                standard_code=cert.standard_code,
                standard_version=cert.standard_version,
                grade=cert.grade,
                cert_valid_from=cert.valid_from,
                cert_valid_until=cert.valid_until,
                status=CaseStatus.SUBMITTED,
                requested_credits=int(data.get("requested_credits") or mapped_credits),
                mapped_credits=mapped_credits,
            )
            if case.requested_credits <= 0:
                raise ValidationError("申请学分数必须为正整数")
            try:
                self.store.insert_case(conn, case)
            except Exception as exc:  # 唯一索引：同项目存在未结案申请
                if "ux_cases_open" in str(exc) or "UNIQUE" in str(exc).upper():
                    raise DuplicateApplicationError(
                        "该证书在本项目已有未结案申请，同一项目不得重复申请",
                        details={"project_code": project_code,
                                 "certificate_digest": cert.digest},
                    )
                raise
            self.store.append_audit(
                conn, aggregate_type="case", aggregate_id=case_id,
                event_type="application_submitted",
                actor_id=principal.user_id, actor_role=principal.role.value,
                payload={"project_code": project_code,
                         "standard": f"{cert.standard_code}@{cert.standard_version}",
                         "grade": cert.grade,
                         "mapped_credits": mapped_credits,
                         "requested_credits": case.requested_credits},
            )
            return self._case_view(self.store.row_to_case(
                self.store.get_case_row(conn, case_id)  # type: ignore[arg-type]
            ), principal)

    # ====================================================== 毕业生：补交材料

    def submit_supplement(self, principal: Principal, case_id: str,
                          material_type: str, evidence_ref: str,
                          note: str = "") -> dict:
        """补交材料仅允许在"待补交"状态由本人提交，提交后回到核验评估。"""
        if principal.role is not Role.GRADUATE:
            raise PermissionDeniedError("仅毕业生本人可以补交材料")
        if not material_type or not evidence_ref:
            raise ValidationError("材料类别与加密证据引用不能为空")

        with self.store.transaction() as conn:
            case = self._load_case(conn, case_id)
            if case.student_id != principal.user_id:
                raise PermissionDeniedError("不得为他人案件补交材料")
            if case.status is not CaseStatus.PENDING_SUPPLEMENT:
                raise IllegalStateError(
                    "仅处于待补交状态的案件可以补交材料，"
                    f"当前状态：{case.status.value}"
                )
            supp = Supplement(
                id=None, case_id=case_id, material_type=material_type,
                evidence_ref=evidence_ref, submitted_by=principal.user_id,
                submitted_at=self._now(), note=note,
            )
            supp_id = self.store.insert_supplement(conn, supp)
            case.status = CaseStatus.UNDER_REVIEW
            self.store.update_case(conn, case)
            self.store.append_audit(
                conn, aggregate_type="case", aggregate_id=case_id,
                event_type="supplement_submitted",
                actor_id=principal.user_id, actor_role=principal.role.value,
                payload={"supplement_id": supp_id,
                         "material_type": material_type,
                         "evidence_ref": evidence_ref, "note": note},
            )
            return {"supplement_id": supp_id, "status": case.status.value}

    def request_supplement(self, principal: Principal, case_id: str,
                           material_type: str, reason: str) -> dict:
        """审核员要求补交；结案后不得再要求。"""
        self._require_reviewer(principal)
        with self.store.transaction() as conn:
            case = self._load_authorized_case(conn, case_id, principal)
            if not case.is_open():
                raise IllegalStateError("案件已结案，不能再要求补交材料")
            case.status = CaseStatus.PENDING_SUPPLEMENT
            self.store.update_case(conn, case)
            self.store.append_audit(
                conn, aggregate_type="case", aggregate_id=case_id,
                event_type="supplement_requested",
                actor_id=principal.user_id, actor_role=principal.role.value,
                payload={"material_type": material_type, "reason": reason},
            )
            return {"status": case.status.value}

    # ======================================================== 认证机构：核验

    def verify_certificate(self, principal: Principal, case_id: str,
                           passed: bool, evidence_ref: str,
                           verification_ref: str) -> dict:
        """颁发机构依据验证回执确认证书真伪；通过后案件进入评估。"""
        if principal.role is not Role.ISSUER:
            raise PermissionDeniedError("仅认证机构可以提交核验结果")
        with self.store.transaction() as conn:
            case = self._load_case(conn, case_id)
            if case.issuer_code != principal.user_id:
                raise PermissionDeniedError("该证书并非由本机构颁发")
            if case.issuer_verification_ref != verification_ref:
                raise PermissionDeniedError("验证回执编号与申请不一致")
            if not case.is_open():
                raise IllegalStateError("案件已结案，不能再提交核验结果")
            case.verification = (
                VerificationStatus.VERIFIED if passed else VerificationStatus.FAILED
            )
            case.verification_evidence_ref = evidence_ref or None
            if passed:
                case.status = CaseStatus.UNDER_REVIEW
            self.store.update_case(conn, case)
            self.store.append_audit(
                conn, aggregate_type="case", aggregate_id=case_id,
                event_type="certificate_verified" if passed else "verification_failed",
                actor_id=principal.user_id, actor_role=principal.role.value,
                payload={"passed": passed, "evidence_ref": evidence_ref or None},
            )
            return {"status": case.status.value, "verification": case.verification.value}

    # ========================================================== 审核员：决定

    def decide(self, principal: Principal, case_id: str, mode: str,
               credits: int | None = None, reason: str = "") -> dict:
        """评估并作出决定；full/partial 原子写入权益并核销，reject 不产生权益。"""
        self._require_reviewer(principal)
        try:
            decision_mode = DecisionMode(mode)
        except ValueError:
            raise ValidationError("决定方式必须是 full / partial / reject")

        with self.store.transaction() as conn:
            case = self._load_authorized_case(conn, case_id, principal)
            if not case.is_open():
                raise IllegalStateError(
                    f"案件已结案（{case.status.value}），不能再次作出决定"
                )
            if case.status is CaseStatus.PENDING_SUPPLEMENT:
                raise IllegalStateError("案件等待毕业生补交材料，暂不能作出决定")
            if self.store.get_consumption_by_case(conn, case_id) is not None:
                # 消费明细 UNIQUE(case_id) 是最后防线，这里给出明确领域错误
                raise IllegalStateError("该案件已生成权益消费记录，不能重复记账")

            now = self._now()
            case.decided_by = principal.user_id
            case.decided_at = now
            case.decision_mode = decision_mode

            if decision_mode is DecisionMode.REJECT:
                case.status = CaseStatus.REJECTED
                case.granted_credits = 0
                self.store.update_case(conn, case)
                self._audit_decision(conn, case, principal, decision_mode, 0, reason)
                return self._decided_view(case)

            # 全额/部分认可都必须通过机构核验
            if case.verification is not VerificationStatus.VERIFIED:
                raise IllegalStateError("证书尚未通过颁发机构核验，不能批准")

            granted = min(case.requested_credits, case.mapped_credits) \
                if decision_mode is DecisionMode.FULL \
                else self._partial_credits(case, credits)

            # —— 原子记账开始 ——
            entitlement = self.store.ensure_entitlement(conn, Entitlement(
                id=None,
                certificate_digest=case.certificate_digest,
                holder_student_id=case.student_id,
                total_credits=case.mapped_credits,
                available_credits=case.mapped_credits,
                status=EntitlementStatus.GRANTED,
                source_case_id=case.case_id,
                created_at=now,
            ))
            if entitlement.status is EntitlementStatus.REVOKED:
                raise IllegalStateError("该证书权益已被撤销，不能再批准")
            if entitlement.available_credits < granted:
                raise CreditAlreadyConsumedError(
                    "证书学分权益已在其他项目使用，余额不足以在本项目重复领取",
                    details={
                        "certificate_digest": case.certificate_digest,
                        "total_credits": entitlement.total_credits,
                        "available_credits": entitlement.available_credits,
                        "requested_grant": granted,
                    },
                )
            rows = self.store.debit_entitlement(conn, entitlement.id, granted)
            if rows != 1:
                # 条件 UPDATE 未命中：并发下余额已被其他项目抢先核销
                raise CreditAlreadyConsumedError(
                    "证书学分权益已被并发批准的其他项目核销"
                )
            self.store.insert_consumption(conn, ConsumptionRecord(
                id=None, entitlement_id=entitlement.id,
                certificate_digest=case.certificate_digest, case_id=case.case_id,
                project_code=case.project_code, student_id=case.student_id,
                credits=granted, consumed_at=now,
            ))
            if entitlement.available_credits - granted == 0:
                self.store.mark_entitlement_status(
                    conn, entitlement.id, EntitlementStatus.CONSUMED
                )
            # —— 原子记账结束（其后仅案件状态与审计，同事务提交）——

            case.granted_credits = granted
            case.status = (
                CaseStatus.APPROVED if decision_mode is DecisionMode.FULL
                else CaseStatus.PARTIALLY_APPROVED
            )
            self.store.update_case(conn, case)
            self._audit_decision(conn, case, principal, decision_mode, granted, reason)
            return self._decided_view(case)

    # ========================================================== 审核员：撤销

    def revoke(self, principal: Principal, case_id: str, reason: str) -> dict:
        """撤销约束：仅已批准案件、仅授权审核员、在撤销窗口内、不可重复撤销。

        撤销原子回退已核销权益（其他项目可继续使用退回的学分），
        原始消费明细保留并在审计日志中记录冲销。
        """
        self._require_reviewer(principal)
        if not reason:
            raise ValidationError("撤销必须填写理由")
        with self.store.transaction() as conn:
            case = self._load_authorized_case(conn, case_id, principal)
            if case.status not in (CaseStatus.APPROVED,
                                   CaseStatus.PARTIALLY_APPROVED):
                raise IllegalStateError(
                    f"仅已批准案件可以撤销，当前状态：{case.status.value}"
                )
            if case.decided_at and self._now() - case.decided_at > self.revoke_window:
                raise IllegalStateError(
                    f"已超过决定后 {self.revoke_window.days} 天的撤销窗口"
                )
            consumption = self.store.get_consumption_by_case(conn, case_id)
            if consumption is None:
                raise IllegalStateError("缺少消费明细，无法执行撤销冲账")
            entitlement = self.store.get_entitlement(
                conn, case.certificate_digest
            )
            if entitlement is None:
                raise IllegalStateError("权益账户缺失，撤销中止")

            self.store.refund_entitlement(
                conn, entitlement.id, consumption.credits
            )
            case.status = CaseStatus.REVOKED
            case.granted_credits = 0
            self.store.update_case(conn, case)
            self.store.append_audit(
                conn, aggregate_type="case", aggregate_id=case_id,
                event_type="decision_revoked",
                actor_id=principal.user_id, actor_role=principal.role.value,
                payload={
                    "reason": reason,
                    "refunded_credits": consumption.credits,
                    "project_code": case.project_code,
                },
            )
            self.store.append_audit(
                conn, aggregate_type="entitlement",
                aggregate_id=case.certificate_digest,
                event_type="credits_refunded",
                actor_id=principal.user_id, actor_role=principal.role.value,
                payload={"case_id": case_id,
                         "credits": consumption.credits},
            )
            return {"status": case.status.value,
                    "refunded_credits": consumption.credits}

    # ================================================================ 查询

    def get_case(self, principal: Principal, case_id: str) -> dict:
        """案件详情。敏感证据引用仅向本人与授权审核员开放。"""
        with self.store.transaction() as conn:
            case = self._load_case(conn, case_id)
            self._require_case_access(principal, case)
            view = self._case_view(case, principal)
            view["supplements"] = [
                {
                    "id": s.id,
                    "material_type": s.material_type,
                    "evidence_ref": s.evidence_ref,
                    "submitted_by": s.submitted_by,
                    "submitted_at": s.submitted_at.isoformat(timespec="seconds"),
                    "note": s.note,
                }
                for s in self.store.list_supplements(conn, case_id)
            ]
            consumption = self.store.get_consumption_by_case(conn, case_id)
            view["consumption"] = (
                {"credits": consumption.credits,
                 "project_code": consumption.project_code,
                 "consumed_at": consumption.consumed_at.isoformat(timespec="seconds")}
                if consumption else None
            )
            return view

    def list_cases(self, principal: Principal) -> list[dict]:
        with self.store.transaction() as conn:
            if principal.role is Role.GRADUATE:
                rows = self.store.list_cases_for_student(conn, principal.user_id)
                cases = [self.store.row_to_case(r) for r in rows]
            elif principal.role is Role.REVIEWER:
                all_rows = conn.execute(
                    "SELECT * FROM cases ORDER BY created_at"
                ).fetchall()
                cases = [self.store.row_to_case(r) for r in all_rows]
                if principal.authorized_case_ids is not None:
                    allowed = principal.authorized_case_ids
                    cases = [c for c in cases if c.case_id in allowed]
            else:
                raise PermissionDeniedError("该角色不能列举案件")
            return [self._case_view(c, principal, include_sensitive=False)
                    for c in cases]

    def get_entitlement(self, principal: Principal, digest: str) -> dict:
        """权益账户与跨项目使用明细：仅证书本人可查。"""
        if principal.role is not Role.GRADUATE:
            # 先鉴权后查询，避免通过 404/200 差异泄露证书是否存在
            raise PermissionDeniedError("权益信息仅向证书本人开放")
        with self.store.transaction() as conn:
            ent = self.store.get_entitlement(conn, digest)
            if ent is None:
                raise NotFoundError("未找到该证书的权益账户")
            if principal.user_id != ent.holder_student_id:
                raise PermissionDeniedError("权益信息仅向证书本人开放")
            records = self.store.list_consumptions(conn, digest)
            return {
                "certificate_digest": digest,
                "total_credits": ent.total_credits,
                "available_credits": ent.available_credits,
                "status": ent.status.value,
                "source_case_id": ent.source_case_id,
                "usages": [
                    {"case_id": r.case_id, "project_code": r.project_code,
                     "credits": r.credits,
                     "consumed_at": r.consumed_at.isoformat(timespec="seconds")}
                    for r in records
                ],
            }

    def history(self, principal: Principal, case_id: str) -> dict:
        """完整追溯：案件的全部领域事件，按时间顺序返回。"""
        with self.store.transaction() as conn:
            case = self._load_case(conn, case_id)
            self._require_case_access(principal, case)
            events = self.store.list_audit(conn, "case", case_id)
            ent_events = self.store.list_audit(
                conn, "entitlement", case.certificate_digest
            )
            all_events = sorted(events + ent_events,
                                key=lambda e: (e["occurred_at"], e["id"]))
            return {"case_id": case_id, "events": all_events}

    def grant_reviewer_access(self, principal: Principal,
                              reviewer_id: str, case_id: str) -> dict:
        """教务主管（不限案件范围的审核员）向审核员逐案授权。"""
        if principal.role is not Role.REVIEWER \
                or principal.authorized_case_ids is not None:
            raise PermissionDeniedError("仅教务主管可以授予案件审核权限")
        with self.store.transaction() as conn:
            self._load_case(conn, case_id)
            self.store.grant_case_access(
                conn, reviewer_id, case_id, granted_by=principal.user_id
            )
            self.store.append_audit(
                conn, aggregate_type="case", aggregate_id=case_id,
                event_type="reviewer_access_granted",
                actor_id=principal.user_id, actor_role=principal.role.value,
                payload={"reviewer_id": reviewer_id},
            )
            return {"reviewer_id": reviewer_id, "case_id": case_id}

    # ====================================================== 映射发布管理员

    def register_grade_scale(self, principal: Principal, standard_code: str,
                             version: str, ordered_grades: list[str]) -> dict:
        """登记某标准版本的成绩等级表（低 -> 高）。"""
        self._require_registry_admin(principal)
        with self.store.transaction() as conn:
            MappingRegistry(conn).register_grade_scale(
                standard_code, version, list(ordered_grades)
            )
            self.store.append_audit(
                conn, aggregate_type="registry",
                aggregate_id=f"{standard_code}@{version}",
                event_type="grade_scale_registered",
                actor_id=principal.user_id, actor_role=principal.role.value,
                payload={"grades": list(ordered_grades)},
            )
            return {"standard_code": standard_code,
                    "standard_version": version,
                    "grades": list(ordered_grades)}

    def publish_mapping(self, principal: Principal, standard_code: str,
                        version: str, rules: list[dict]) -> dict:
        """发布"成绩区间 -> 学分"映射；发布后冻结。"""
        self._require_registry_admin(principal)
        with self.store.transaction() as conn:
            MappingRegistry(conn).publish(
                standard_code, version, rules,
                published_by=principal.user_id,
            )
            self.store.append_audit(
                conn, aggregate_type="registry",
                aggregate_id=f"{standard_code}@{version}",
                event_type="mapping_published",
                actor_id=principal.user_id, actor_role=principal.role.value,
                payload={"rules": rules},
            )
            return {"standard_code": standard_code,
                    "standard_version": version,
                    "rule_count": len(rules)}

    def list_published_mappings(self, principal: Principal) -> list[dict]:
        """已发布映射属于公开规则，任意已认证角色可查。"""
        with self.store.transaction() as conn:
            return [
                {
                    "standard_code": r.standard_code,
                    "standard_version": r.standard_version,
                    "grade_min": r.grade_min,
                    "grade_max": r.grade_max,
                    "credits": r.credits,
                }
                for r in self.store.list_published_mappings(conn)
            ]

    # ============================================================ 内部工具

    @staticmethod
    def _require_registry_admin(principal: Principal) -> None:
        if principal.role is not Role.REGISTRY_ADMIN:
            raise PermissionDeniedError("仅映射发布管理员可以执行该动作")

    def _build_certificate(self, student_id: str, data: dict) -> Certificate:
        try:
            valid_from = date.fromisoformat(str(data["valid_from"]))
            valid_until = date.fromisoformat(str(data["valid_until"]))
        except (KeyError, TypeError, ValueError):
            raise InvalidCertificateError("证书有效期需为 ISO 日期 (YYYY-MM-DD)")
        cert = Certificate(
            digest=str(data.get("certificate_digest") or "").lower(),
            issuer_code=str(data.get("issuer_code") or "").strip(),
            issuer_verification_ref=str(data.get("issuer_verification_ref") or "").strip(),
            standard_code=str(data.get("standard_code") or "").strip(),
            standard_version=str(data.get("standard_version") or "").strip(),
            grade=str(data.get("grade") or "").strip(),
            valid_from=valid_from,
            valid_until=valid_until,
            holder_student_id=student_id,
        )
        today = self._date()
        try:
            cert.validate()  # 结构性校验
        except ValueError as exc:
            raise InvalidCertificateError(str(exc))
        if today > cert.valid_until:
            raise CertificateExpiredError(
                "证书已超出有效期",
                details={"valid_until": cert.valid_until.isoformat(),
                         "today": today.isoformat()},
            )
        return cert

    def _partial_credits(self, case: Case, credits: int | None) -> int:
        if credits is None or not isinstance(credits, int):
            raise ValidationError("部分认可必须给出认可学分数")
        if credits <= 0:
            raise ValidationError("部分认可学分必须为正整数")
        if credits >= case.mapped_credits:
            raise ValidationError(
                f"部分认可学分必须小于映射上限 {case.mapped_credits}，"
                "全额认可请使用 full"
            )
        if credits > case.requested_credits:
            raise ValidationError("认可学分不得超过毕业生申请学分")
        return credits

    @staticmethod
    def _require_reviewer(principal: Principal) -> None:
        if principal.role is not Role.REVIEWER:
            raise PermissionDeniedError("仅教务审核员可以执行该动作")

    def _load_case(self, conn, case_id: str) -> Case:
        row = self.store.get_case_row(conn, case_id)
        if row is None:
            raise NotFoundError(f"案件 {case_id} 不存在")
        return self.store.row_to_case(row)

    def _load_authorized_case(self, conn, case_id: str,
                              principal: Principal) -> Case:
        case = self._load_case(conn, case_id)
        if principal.authorized_case_ids is not None \
                and case_id not in principal.authorized_case_ids:
            raise PermissionDeniedError("未获得该案件的审核授权")
        return case

    @staticmethod
    def _require_case_access(principal: Principal, case: Case) -> None:
        if not principal.can_access_case(case):
            raise PermissionDeniedError(
                "敏感证据仅向证书本人与授权审核员开放"
            )

    @staticmethod
    def _new_case_id() -> str:
        return "CASE-" + secrets.token_hex(6).upper()

    def _audit_decision(self, conn, case: Case, principal: Principal,
                        mode: DecisionMode, credits: int, reason: str) -> None:
        self.store.append_audit(
            conn, aggregate_type="case", aggregate_id=case.case_id,
            event_type={
                DecisionMode.FULL: "approved_full",
                DecisionMode.PARTIAL: "approved_partial",
                DecisionMode.REJECT: "rejected",
            }[mode],
            actor_id=principal.user_id, actor_role=principal.role.value,
            payload={"granted_credits": credits,
                     "mapped_credits": case.mapped_credits,
                     "reason": reason},
        )

    @staticmethod
    def _case_view(case: Case, principal: Principal, *,
                   include_sensitive: bool = True) -> dict:
        """组装案件视图；无权访问敏感证据时剔除相关字段。"""
        sensitive = include_sensitive and principal.can_access_case(case)
        view = {
            "case_id": case.case_id,
            "student_id": case.student_id,
            "project_code": case.project_code,
            "certificate_digest": case.certificate_digest,
            "issuer_code": case.issuer_code,
            "standard_code": case.standard_code,
            "standard_version": case.standard_version,
            "grade": case.grade,
            "cert_valid_from": case.cert_valid_from.isoformat(),
            "cert_valid_until": case.cert_valid_until.isoformat(),
            "status": case.status.value,
            "requested_credits": case.requested_credits,
            "mapped_credits": case.mapped_credits,
            "granted_credits": case.granted_credits,
            "decision_mode": case.decision_mode.value if case.decision_mode else None,
            "verification": case.verification.value,
            "decided_by": case.decided_by,
            "decided_at": case.decided_at.isoformat(timespec="seconds")
            if case.decided_at else None,
            "version": case.version,
        }
        if sensitive:
            view["issuer_verification_ref"] = case.issuer_verification_ref
            view["verification_evidence_ref"] = case.verification_evidence_ref
        return view

    @staticmethod
    def _decided_view(case: Case) -> dict:
        return {
            "case_id": case.case_id,
            "status": case.status.value,
            "decision_mode": case.decision_mode.value if case.decision_mode else None,
            "granted_credits": case.granted_credits,
        }


def build_service(db_path: str = ":memory:", *,
                  revoke_window_days: int = 30,
                  clock: Any = None,
                  now_provider: Any = None) -> RecognitionService:
    """组装默认服务。"""
    store = SqliteStore(db_path)
    return RecognitionService(
        store,
        revoke_window_days=revoke_window_days,
        clock=clock or date.today,
        now_provider=now_provider or datetime.utcnow,
    )
