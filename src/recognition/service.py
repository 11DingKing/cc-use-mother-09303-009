"""领域服务：案件生命周期全部业务规则。

写操作一律使用 ``BEGIN IMMEDIATE`` 事务；防重复消费除应用层预检外，
最终由数据库部分唯一索引兜底，保证批准动作原子且并发安全。
"""
from __future__ import annotations

import hashlib
import sqlite3
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable

from . import repo
from .enums import CaseStatus, EntitlementStatus, ItemVerdict, Role, VerificationStatus
from .errors import (
    CredentialExpired,
    DuplicateCredential,
    DuplicateEntitlement,
    MappingChanged,
    NoEligibleBenefit,
    NotFound,
    PermissionDenied,
    StateConflict,
    SupplementDeadlinePassed,
    ValidationError,
    VerificationInvalid,
)

TERMINAL_STATUSES = {
    CaseStatus.REJECTED.value,
    CaseStatus.REVOKED.value,
}
EVIDENCE_OPEN_STATUSES = {
    CaseStatus.SUBMITTED.value,
    CaseStatus.SUPPLEMENTING.value,
    CaseStatus.VERIFIED.value,
    CaseStatus.UNDER_EVALUATION.value,
}
ACTIVE_USAGE = (EntitlementStatus.GRANTED.value, EntitlementStatus.CONSUMED.value)
MAX_EVIDENCE_BYTES = 1 * 1024 * 1024


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12].upper()}"


class Service:
    def __init__(self, clock: Callable[[], datetime] | None = None) -> None:
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    # ---------- 基础工具 ----------

    def now(self) -> datetime:
        return self.clock()

    def _today(self) -> date:
        return self.clock().date()

    def _ts(self) -> str:
        return self.clock().replace(microsecond=0).isoformat().replace("+00:00", "Z")

    def _event(
        self,
        conn: sqlite3.Connection,
        case_id: str,
        event_type: str,
        actor,
        payload: dict,
    ) -> None:
        """追加案件事件（仅追加，序号案件内递增）。"""
        seq = conn.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 FROM case_events WHERE case_id = ?",
            (case_id,),
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO case_events VALUES (?,?,?,?,?,?,?,?)",
            (
                _new_id("EVT"),
                case_id,
                seq,
                event_type,
                getattr(actor, "actor_id", None),
                getattr(actor, "role", None),
                _json(payload),
                self._ts(),
            ),
        )

    def _audit(
        self, conn: sqlite3.Connection, actor, action: str, case_id: str | None, details: dict
    ) -> None:
        # 审计日志不记录证据正文等敏感字段（调用方负责只放元数据）。
        conn.execute(
            "INSERT INTO audit_log VALUES (?,?,?,?,?,?,?)",
            (
                _new_id("AUD"),
                getattr(actor, "actor_id", None),
                getattr(actor, "role", None),
                action,
                case_id,
                _json(details),
                self._ts(),
            ),
        )

    # ---------- 身份 ----------

    def authenticate(self, conn: sqlite3.Connection, key: str):
        from .models import Actor

        row = conn.execute(
            "SELECT p.person_id, p.role FROM api_keys k "
            "JOIN persons p ON p.person_id = k.person_id WHERE k.key = ?",
            (key,),
        ).fetchone()
        if not row:
            raise PermissionDenied("无效的 API 密钥")
        return Actor(row["person_id"], row["role"])

    def me(self, conn: sqlite3.Connection, actor) -> dict:
        row = conn.execute(
            "SELECT person_id, name, role FROM persons WHERE person_id = ?",
            (actor.actor_id,),
        ).fetchone()
        return dict(row)

    # ---------- 指纹与映射 ----------

    @staticmethod
    def fingerprint(holder_id: str, serial_number: str, title: str, issued_at: str | date) -> str:
        material = "|".join(
            [holder_id, str(serial_number).strip().upper(),
             str(title).strip(), str(issued_at).strip()]
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    def _eligible_mappings(
        self,
        conn: sqlite3.Connection,
        *,
        program_id: str,
        version_id: str,
        title: str,
        achieved: float,
    ) -> list[sqlite3.Row]:
        return conn.execute(
            "SELECT m.*, c.course_code, c.course_name FROM mappings m "
            "JOIN target_courses c ON c.course_id = m.course_id "
            "WHERE m.version_id = ? AND m.credential_title = ? AND c.program_id = ? "
            "AND m.active = 1 AND m.min_score <= ? "
            "ORDER BY c.course_code",
            (version_id, title.strip(), program_id, achieved),
        ).fetchall()

    def _all_program_mappings(
        self, conn: sqlite3.Connection, *, program_id: str, version_id: str, title: str
    ) -> list[sqlite3.Row]:
        return conn.execute(
            "SELECT m.*, c.course_code, c.course_name FROM mappings m "
            "JOIN target_courses c ON c.course_id = m.course_id "
            "WHERE m.version_id = ? AND m.credential_title = ? AND c.program_id = ? "
            "AND m.active = 1 ORDER BY c.course_code",
            (version_id, title.strip(), program_id),
        ).fetchall()

    def _active_usage(self, conn: sqlite3.Connection, credential_id: str) -> sqlite3.Row | None:
        return conn.execute(
            "SELECT * FROM credential_usage WHERE credential_id = ? AND status IN (?, ?)",
            (credential_id, *ACTIVE_USAGE),
        ).fetchone()

    # ---------- 权益试算 ----------

    def eligibility(self, conn: sqlite3.Connection, actor, body: dict) -> dict:
        """依据已发布映射计算可申请权益（不落库），并提示跨项目占用。"""
        self._require_role(actor, Role.APPLICANT)
        program_id = body.get("program_id")
        version_id = body.get("version_id")
        title = str(body.get("credential_title", "")).strip()
        achieved = _number(body, "score_achieved")
        serial = str(body.get("serial_number", "")).strip()
        issued_at = str(body.get("issued_at", "")).strip()
        self._check_program_version(conn, program_id, version_id)
        if not title or not serial or not issued_at:
            raise ValidationError("证书标题、编号与签发日期必填")

        rows = self._all_program_mappings(
            conn, program_id=program_id, version_id=version_id, title=title
        )
        eligible, ineligible = [], []
        for r in rows:
            item = {
                "mapping_id": r["mapping_id"],
                "course_id": r["course_id"],
                "course_code": r["course_code"],
                "course_name": r["course_name"],
                "min_score": r["min_score"],
                "credits_granted": r["credits_granted"],
            }
            (eligible if achieved >= r["min_score"] else ineligible).append(item)

        fp = self.fingerprint(actor.actor_id, serial, title, issued_at)
        cred = repo.find_credential_by_fingerprint(conn, fp)
        cross_use = None
        if cred:
            usage = self._active_usage(conn, cred["credential_id"])
            if usage:
                cross_use = {
                    "case_id": usage["case_id"],
                    "program_id": usage["program_id"],
                    "status": usage["status"],
                    "total_credits": usage["total_credits"],
                }
        return {
            "program_id": program_id,
            "version_id": version_id,
            "credential_title": title,
            "score_achieved": achieved,
            "eligible": eligible,
            "ineligible_below_min_score": ineligible,
            "cross_project_active_use": cross_use,
            "approvable": bool(eligible) and cross_use is None,
        }

    # ---------- 申请 ----------

    def create_case(self, conn: sqlite3.Connection, actor, body: dict) -> dict:
        self._require_role(actor, Role.APPLICANT)
        program_id = body.get("program_id")
        version_id = body.get("version_id")
        cert = body.get("certificate") or {}
        serial = str(cert.get("serial_number", "")).strip()
        title = str(cert.get("title", "")).strip()
        issued_at = _date_str(cert, "issued_at")
        valid_from = _date_str(cert, "valid_from")
        valid_until = _date_str(cert, "valid_until")
        if not serial or not title:
            raise ValidationError("证书编号与名称必填")
        if not (valid_from <= valid_until):
            raise ValidationError("有效期起始日不得晚于截止日")
        if valid_until < self._today():
            raise CredentialExpired("证书已超出有效期，不能提交申请")

        verification = body.get("verification") or {}
        v_status = str(verification.get("status", "")).strip()
        if v_status not in {s.value for s in VerificationStatus}:
            raise ValidationError("颁发机构验证状态非法（VERIFIED/PENDING/FAILED）")
        v_ref = str(verification.get("reference", "")).strip()
        v_method = str(verification.get("method", "API")).strip() or "API"
        v_date = verification.get("verified_at")
        if v_status == VerificationStatus.VERIFIED.value and not v_ref:
            raise ValidationError("核验通过必须提供机构核验回执编号")
        if v_date:
            _parse_date(v_date)

        score = body.get("score") or {}
        achieved = _number(score, "achieved")
        scale_min = float(score.get("scale_min", 0.0))
        scale_max = float(score.get("scale_max", 100.0))
        if not (scale_min < scale_max):
            raise ValidationError("成绩范围上下限非法")
        if not (scale_min <= achieved <= scale_max):
            raise ValidationError("持证成绩必须落在成绩范围内")

        self._check_program_version(conn, program_id, version_id)

        person = conn.execute(
            "SELECT name FROM persons WHERE person_id = ?", (actor.actor_id,)
        ).fetchone()
        holder_name = str(cert.get("holder_name", "")).strip()
        if holder_name and holder_name != person["name"]:
            raise ValidationError("证书持有人姓名与学籍登记姓名不一致")
        fp = self.fingerprint(actor.actor_id, serial, title, issued_at)

        conn.execute("BEGIN IMMEDIATE")
        try:
            mappings = self._eligible_mappings(
                conn,
                program_id=program_id,
                version_id=version_id,
                title=title,
                achieved=achieved,
            )
            if not mappings:
                raise NoEligibleBenefit("当前没有成绩达标的已发布映射权益可申请")

            cred = repo.find_credential_by_fingerprint(conn, fp)
            if cred:
                credential_id = cred["credential_id"]
                existing = conn.execute(
                    "SELECT case_id FROM cases WHERE program_id = ? AND credential_id = ? "
                    "AND status NOT IN ('REJECTED','REVOKED')",
                    (program_id, credential_id),
                ).fetchone()
                if existing:
                    raise StateConflict(
                        "该证书在本项目已有未结案案件",
                        details={"case_id": existing["case_id"]},
                    )
                usage = self._active_usage(conn, credential_id)
                if usage:
                    raise DuplicateEntitlement(
                        "该证书的学分权益已在其他项目使用，不能跨项目重复领取",
                        details={
                            "case_id": usage["case_id"],
                            "program_id": usage["program_id"],
                        },
                    )
            else:
                credential_id = _new_id("CRED")
                try:
                    conn.execute(
                        "INSERT INTO credentials VALUES (?,?,?,?,?,?,?,?,?)",
                        (
                            credential_id,
                            fp,
                            actor.actor_id,
                            person["name"],
                            serial,
                            title,
                            issued_at,
                            valid_from.isoformat(),
                            valid_until.isoformat(),
                        ),
                    )
                except sqlite3.IntegrityError:
                    raise DuplicateCredential("证书指纹冲突，请核对编号与签发日期")

            case_id = _new_id("CASE")
            ts = self._ts()
            if v_status == VerificationStatus.VERIFIED.value:
                status = CaseStatus.VERIFIED.value
            else:
                status = CaseStatus.SUBMITTED.value
            conn.execute(
                "INSERT INTO cases ("
                "case_id, program_id, credential_id, applicant_id, reviewer_id, status, "
                "verification_status, verification_ref, verification_method, "
                "score_achieved, score_scale_min, score_scale_max, version_id, "
                "supplement_due, decision_at, decision_by, decision_comment, "
                "rejected_reason, revoked_reason, created_at, updated_at"
                ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    case_id,
                    program_id,
                    credential_id,
                    actor.actor_id,
                    None,
                    status,
                    v_status,
                    v_ref or None,
                    v_method,
                    achieved,
                    scale_min,
                    scale_max,
                    version_id,
                    None,  # supplement_due
                    None,  # decision_at
                    None,  # decision_by
                    None,  # decision_comment
                    None,  # rejected_reason
                    None,  # revoked_reason
                    ts,
                    ts,
                ),
            )
            for r in mappings:
                conn.execute(
                    "INSERT INTO case_items VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (
                        _new_id("ITEM"),
                        case_id,
                        r["mapping_id"],
                        r["course_id"],
                        r["course_code"],
                        r["course_name"],
                        r["min_score"],
                        r["credits_granted"],
                        None,
                        0.0,
                    ),
                )

            self._event(
                conn,
                case_id,
                "CASE_CREATED",
                actor,
                {
                    "program_id": program_id,
                    "version_id": version_id,
                    "credential_id": credential_id,
                    "fingerprint": fp[:16],
                    "score_achieved": achieved,
                    "item_count": len(mappings),
                },
            )
            self._event(
                conn,
                case_id,
                "VERIFICATION_RECORDED",
                actor,
                {"status": v_status, "method": v_method, "reference": v_ref or None,
                 "verified_at": v_date},
            )
            self._audit(conn, actor, "CASE_CREATE", case_id, {"program_id": program_id})
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return self.get_case(conn, case_id, actor, include_events=False)

    # ---------- 读取与授权 ----------

    def _load_case_row(self, conn: sqlite3.Connection, case_id: str) -> sqlite3.Row:
        row = repo.get_case_row(conn, case_id)
        if not row:
            raise NotFound("案件不存在")
        return row

    def _can_view_case(self, row: sqlite3.Row, actor) -> bool:
        if row["applicant_id"] == actor.actor_id:
            return True
        return actor.role in {Role.REVIEWER.value, Role.REGISTRAR.value, Role.ADMIN.value}

    def get_case(self, conn: sqlite3.Connection, case_id: str, actor, *, include_events: bool = True) -> dict:
        row = self._load_case_row(conn, case_id)
        if not self._can_view_case(row, actor):
            # 对无权者与不存在统一 404，避免存在性泄露。
            raise NotFound("案件不存在")
        data = repo._row_to_case(row)
        data["items"] = repo.list_items(conn, case_id)
        data["entitlements"] = repo.list_entitlements_for_case(conn, case_id)
        data["evidence"] = repo.list_evidence(conn, case_id)
        cred = repo.get_credential(conn, row["credential_id"])
        data["certificate"] = {
            "credential_id": cred["credential_id"],
            "title": cred["title"],
            "serial_number": cred["serial_number"],
            "issued_at": cred["issued_at"],
            "valid_from": cred["valid_from"],
            "valid_until": cred["valid_until"],
            "holder_name": cred["holder_name"]
            if actor.actor_id == row["applicant_id"] or actor.role == Role.REVIEWER.value
            else None,
        }
        prog = conn.execute(
            "SELECT program_id, name FROM programs WHERE program_id = ?",
            (row["program_id"],),
        ).fetchone()
        data["program"] = dict(prog) if prog else {"program_id": row["program_id"]}
        if include_events:
            data["events"] = repo.list_events(conn, case_id)
        return data

    def list_cases(self, conn: sqlite3.Connection, actor) -> list[dict]:
        if actor.role == Role.APPLICANT.value:
            return repo.list_cases(conn, person_id=actor.actor_id, role=actor.role)
        if actor.role in {Role.REVIEWER.value, Role.REGISTRAR.value, Role.ADMIN.value}:
            return repo.list_cases(conn, person_id=actor.actor_id, role=actor.role)
        raise PermissionDenied("无权查看案件")

    # ---------- 颁发机构核验 ----------

    def record_verification(self, conn: sqlite3.Connection, case_id: str, actor, body: dict) -> dict:
        self._require_role(actor, Role.REVIEWER)
        status = str(body.get("status", "")).strip()
        if status not in {s.value for s in VerificationStatus}:
            raise ValidationError("核验状态非法")
        reference = str(body.get("reference", "")).strip()
        if status == VerificationStatus.VERIFIED.value and not reference:
            raise ValidationError("核验通过必须提供回执编号")
        method = str(body.get("method", "API")).strip() or "API"
        verified_at = body.get("verified_at") or self._today().isoformat()

        conn.execute("BEGIN IMMEDIATE")
        try:
            row = self._load_case_row(conn, case_id)
            if row["status"] in TERMINAL_STATUSES or row["status"] == CaseStatus.POSTED.value:
                raise StateConflict("案件已结案，不能再登记核验结果")
            new_case_status = row["status"]
            if status == VerificationStatus.VERIFIED.value and row["status"] == CaseStatus.SUBMITTED.value:
                new_case_status = CaseStatus.VERIFIED.value
            if status != VerificationStatus.VERIFIED.value and row["status"] == CaseStatus.VERIFIED.value:
                # 机构撤回确认：案件退回待核验。
                new_case_status = CaseStatus.SUBMITTED.value
            conn.execute(
                "UPDATE cases SET verification_status = ?, verification_ref = ?, "
                "verification_method = ?, status = ?, updated_at = ? WHERE case_id = ?",
                (status, reference or None, method, new_case_status, self._ts(), case_id),
            )
            self._event(
                conn, case_id, "VERIFICATION_RECORDED", actor,
                {"status": status, "method": method, "reference": reference or None,
                 "verified_at": verified_at},
            )
            self._audit(conn, actor, "VERIFICATION_RECORD", case_id, {"status": status})
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return self.get_case(conn, case_id, actor, include_events=False)

    # ---------- 补交材料 ----------

    def request_supplement(self, conn: sqlite3.Connection, case_id: str, actor, body: dict) -> dict:
        self._require_role(actor, Role.REVIEWER)
        reason = str(body.get("reason", "")).strip()
        if not reason:
            raise ValidationError("必须说明补交原因与材料清单")
        due_days = int(body.get("due_days", 7))
        if due_days < 1 or due_days > 30:
            raise ValidationError("补交期限需在 1-30 天之间")
        due = self._today() + timedelta(days=due_days)

        conn.execute("BEGIN IMMEDIATE")
        try:
            row = self._load_case_row(conn, case_id)
            if row["status"] not in {
                CaseStatus.SUBMITTED.value,
                CaseStatus.VERIFIED.value,
                CaseStatus.UNDER_EVALUATION.value,
            }:
                raise StateConflict("当前状态不能要求补交材料")
            conn.execute(
                "UPDATE cases SET status = ?, supplement_due = ?, reviewer_id = ?, "
                "updated_at = ? WHERE case_id = ?",
                (CaseStatus.SUPPLEMENTING.value, due.isoformat(), actor.actor_id,
                 self._ts(), case_id),
            )
            self._event(
                conn, case_id, "SUPPLEMENT_REQUESTED", actor,
                {"reason": reason, "due_days": due_days, "supplement_due": due.isoformat()},
            )
            self._audit(conn, actor, "SUPPLEMENT_REQUEST", case_id,
                        {"supplement_due": due.isoformat()})
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return self.get_case(conn, case_id, actor, include_events=False)

    def submit_supplement(self, conn: sqlite3.Connection, case_id: str, actor, body: dict) -> dict:
        row0 = self._load_case_row(conn, case_id)
        if row0["applicant_id"] != actor.actor_id:
            if actor.role != Role.REVIEWER.value:
                raise NotFound("案件不存在")
            raise PermissionDenied("只有申请人本人可以补交材料")
        if row0["status"] != CaseStatus.SUPPLEMENTING.value:
            raise StateConflict("案件不处于待补交状态")
        if self._today() > _parse_date(row0["supplement_due"]):
            raise SupplementDeadlinePassed(
                "已超过补交截止时间，补交通道关闭",
                details={"supplement_due": row0["supplement_due"]},
            )
        note = str(body.get("note", "")).strip()
        evidence_ids = body.get("evidence_ids") or []
        if not note and not evidence_ids:
            raise ValidationError("补交说明或证据材料至少提供一项")

        conn.execute("BEGIN IMMEDIATE")
        try:
            row = self._load_case_row(conn, case_id)
            if evidence_ids:
                placeholders = ",".join("?" * len(evidence_ids))
                found = conn.execute(
                    f"SELECT evidence_id FROM evidence WHERE case_id = ? "
                    f"AND evidence_id IN ({placeholders})",
                    (case_id, *evidence_ids),
                ).fetchall()
                if len(found) != len(set(evidence_ids)):
                    raise ValidationError("存在不属于本案件的证据编号")
            new_status = (
                CaseStatus.VERIFIED.value
                if row["verification_status"] == VerificationStatus.VERIFIED.value
                else CaseStatus.SUBMITTED.value
            )
            conn.execute(
                "UPDATE cases SET status = ?, updated_at = ? WHERE case_id = ?",
                (new_status, self._ts(), case_id),
            )
            self._event(
                conn, case_id, "SUPPLEMENT_SUBMITTED", actor,
                {"note": note, "evidence_ids": list(evidence_ids)},
            )
            self._audit(conn, actor, "SUPPLEMENT_SUBMIT", case_id,
                        {"evidence_count": len(evidence_ids)})
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return self.get_case(conn, case_id, actor, include_events=False)

    # ---------- 评估与决定 ----------

    def start_evaluation(self, conn: sqlite3.Connection, case_id: str, actor, body: dict | None = None) -> dict:
        self._require_role(actor, Role.REVIEWER)
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = self._load_case_row(conn, case_id)
            if row["verification_status"] != VerificationStatus.VERIFIED.value:
                raise VerificationInvalid(
                    "颁发机构核验未通过或仍在核验中，不能进入评估",
                    details={"verification_status": row["verification_status"]},
                )
            if row["status"] != CaseStatus.VERIFIED.value:
                raise StateConflict("只有核验通过的案件才能进入评估")
            conn.execute(
                "UPDATE cases SET status = ?, reviewer_id = ?, updated_at = ? WHERE case_id = ?",
                (CaseStatus.UNDER_EVALUATION.value, actor.actor_id, self._ts(), case_id),
            )
            self._event(conn, case_id, "EVALUATION_STARTED", actor, {})
            self._audit(conn, actor, "EVALUATION_START", case_id, {})
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return self.get_case(conn, case_id, actor, include_events=False)

    def reject_case(self, conn: sqlite3.Connection, case_id: str, actor, body: dict) -> dict:
        """审核驳回：用于核验失败、补交超期或材料不属实等情形，结案且不产生权益。"""
        self._require_role(actor, Role.REVIEWER)
        reason = str(body.get("reason", "")).strip()
        if not reason:
            raise ValidationError("驳回必须填写原因")

        conn.execute("BEGIN IMMEDIATE")
        try:
            row = self._load_case_row(conn, case_id)
            if row["status"] in {
                CaseStatus.APPROVED.value,
                CaseStatus.PARTIALLY_APPROVED.value,
                CaseStatus.POSTED.value,
            }:
                raise StateConflict("已批准的决定只能走撤销流程")
            if row["status"] in TERMINAL_STATUSES:
                raise StateConflict("案件已结案")
            conn.execute(
                "UPDATE cases SET status = ?, rejected_reason = ?, decision_at = ?, "
                "decision_by = ?, decision_comment = ?, updated_at = ? WHERE case_id = ?",
                (CaseStatus.REJECTED.value, reason, self._ts(), actor.actor_id,
                 reason, self._ts(), case_id),
            )
            self._event(conn, case_id, "CASE_REJECTED", actor, {"reason": reason})
            self._audit(conn, actor, "CASE_REJECT", case_id, {})
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return self.get_case(conn, case_id, actor)

    def decide(self, conn: sqlite3.Connection, case_id: str, actor, body: dict) -> dict:
        """登记逐项审核结论；全部认可=批准，部分认可=部分批准，全部驳回=驳回。

        批准时在同一事务内写入可消费权益与证书占用，由唯一索引原子阻止重复领取。
        """
        self._require_role(actor, Role.REVIEWER)
        comment = str(body.get("comment", "")).strip()
        raw_items = body.get("items")
        if not isinstance(raw_items, list) or not raw_items:
            raise ValidationError("必须给出逐项审核结论 items")

        return self._decide_tx(conn, case_id, actor, comment, raw_items)

    def _decide_tx(self, conn, case_id, actor, comment, raw_items) -> dict:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = self._load_case_row(conn, case_id)
            if row["status"] != CaseStatus.UNDER_EVALUATION.value:
                raise StateConflict("只有评估中的案件可以登记决定")
            if row["verification_status"] != VerificationStatus.VERIFIED.value:
                raise VerificationInvalid("核验结论已失效，不能批准")

            cred = repo.get_credential(conn, row["credential_id"])
            if _parse_date(cred["valid_until"]) < self._today():
                raise CredentialExpired(
                    "批准时证书已超出有效期",
                    details={"valid_until": cred["valid_until"]},
                )

            items = repo.list_items(conn, case_id)
            by_course = {it["course_id"]: it for it in items}
            decisions: list[dict] = []
            seen = set()
            for raw in raw_items:
                course_id = raw.get("course_id")
                verdict = str(raw.get("verdict", "")).strip()
                if course_id not in by_course:
                    raise ValidationError(f"结论包含案件外课程：{course_id}")
                if verdict not in {v.value for v in ItemVerdict}:
                    raise ValidationError("结论只能是 APPROVED 或 REJECTED")
                if course_id in seen:
                    raise ValidationError(f"课程结论重复：{course_id}")
                seen.add(course_id)
                it = by_course[course_id]

                # 映射时效检查：停用或更新均视为变更，旧案件需重新生成。
                mp = conn.execute(
                    "SELECT * FROM mappings WHERE mapping_id = ?", (it["mapping_id"],)
                ).fetchone()
                if (
                    mp is None
                    or not mp["active"]
                    or mp["min_score"] != it["min_score"]
                    or mp["credits_granted"] != it["offered_credits"]
                ):
                    raise MappingChanged(
                        "已发布映射已停用或更新，本案件需按新版本重新生成",
                        details={"mapping_id": it["mapping_id"], "course_id": course_id},
                    )

                decided = 0.0
                if verdict == ItemVerdict.APPROVED.value:
                    decided = float(raw.get("decided_credits", it["offered_credits"]))
                    if not (0 < decided <= it["offered_credits"]):
                        raise ValidationError(
                            f"认可学分需在 0 与映射学分之间：{course_id}"
                        )
                decisions.append(
                    {
                        "item_id": it["item_id"],
                        "course_id": course_id,
                        "verdict": verdict,
                        "decided_credits": decided,
                    }
                )
            missing = set(by_course) - seen
            if missing:
                raise ValidationError("仍有课程未给出结论", details={"missing": sorted(missing)})

            approved = [d for d in decisions if d["verdict"] == ItemVerdict.APPROVED.value]
            full = all(
                d["verdict"] == ItemVerdict.REJECTED.value
                or d["decided_credits"] == by_course[d["course_id"]]["offered_credits"]
                for d in decisions
            )
            if not approved:
                new_status = CaseStatus.REJECTED.value
                if not comment:
                    raise ValidationError("全部驳回必须填写驳回原因")
            elif full:
                new_status = CaseStatus.APPROVED.value
            else:
                new_status = CaseStatus.PARTIALLY_APPROVED.value

            for d in decisions:
                conn.execute(
                    "UPDATE case_items SET verdict = ?, decided_credits = ? WHERE item_id = ?",
                    (d["verdict"], d["decided_credits"], d["item_id"]),
                )

            granted = []
            if approved:
                # 跨项目重复领取的最终闸门：唯一索引冲突 -> 业务错误。
                usage = self._active_usage(conn, row["credential_id"])
                if usage:
                    raise DuplicateEntitlement(
                        "该证书的学分权益已在其他项目使用，不能重复领取",
                        details={"case_id": usage["case_id"], "program_id": usage["program_id"]},
                    )
                for d in approved:
                    ent_id = _new_id("ENT")
                    try:
                        conn.execute(
                            "INSERT INTO entitlements VALUES (?,?,?,?,?,?,?,?,?,?)",
                            (
                                ent_id,
                                case_id,
                                row["credential_id"],
                                row["applicant_id"],
                                d["course_id"],
                                d["decided_credits"],
                                EntitlementStatus.GRANTED.value,
                                self._ts(),
                                None,
                                None,
                            ),
                        )
                    except sqlite3.IntegrityError:
                        # 此处唯一约束冲突只可能来自同证书同课程的有效权益索引。
                        raise DuplicateEntitlement(
                            "该证书对本课程已有有效权益，不能重复领取",
                            details={"course_id": d["course_id"]},
                        )
                    granted.append(
                        {
                            "entitlement_id": ent_id,
                            "course_id": d["course_id"],
                            "credits": d["decided_credits"],
                        }
                    )
                try:
                    conn.execute(
                        "INSERT INTO credential_usage VALUES (?,?,?,?,?,?)",
                        (
                            _new_id("USE"),
                            row["credential_id"],
                            case_id,
                            row["program_id"],
                            sum(g["credits"] for g in granted),
                            EntitlementStatus.GRANTED.value,
                        ),
                    )
                except sqlite3.IntegrityError:
                    # 唯一约束冲突只可能来自全校证书占用索引：跨项目重复领取。
                    raise DuplicateEntitlement(
                        "该证书已在其他项目产生有效权益占用，跨项目重复领取被阻止",
                        details={"credential_id": row["credential_id"]},
                    )

            conn.execute(
                "UPDATE cases SET status = ?, decision_at = ?, decision_by = ?, "
                "decision_comment = ?, rejected_reason = ?, updated_at = ? WHERE case_id = ?",
                (
                    new_status,
                    self._ts(),
                    actor.actor_id,
                    comment or None,
                    comment if new_status == CaseStatus.REJECTED.value else None,
                    self._ts(),
                    case_id,
                ),
            )

            self._event(
                conn, case_id, "EVALUATION_DECIDED", actor,
                {"decisions": decisions, "result": new_status, "comment": comment or None},
            )
            for g in granted:
                self._event(conn, case_id, "ENTITLEMENT_GRANTED", actor, g)
            self._event(
                conn, case_id, "STATUS_CHANGED", actor,
                {"from": CaseStatus.UNDER_EVALUATION.value, "to": new_status},
            )
            self._audit(
                conn, actor, "CASE_DECIDE", case_id,
                {"result": new_status, "granted_count": len(granted)},
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return self.get_case(conn, case_id, actor)

    # ---------- 记账领取 ----------

    def post_credits(self, conn: sqlite3.Connection, case_id: str, actor, body: dict | None = None) -> dict:
        """学籍记账：把已批准案件的可消费权益原子置为已消费。"""
        self._require_role(actor, Role.REGISTRAR)
        ledger_ref = str((body or {}).get("ledger_reference", "")).strip()
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = self._load_case_row(conn, case_id)
            if row["status"] == CaseStatus.POSTED.value:
                raise StateConflict("该案件已完成记账")
            if row["status"] not in {CaseStatus.APPROVED.value, CaseStatus.PARTIALLY_APPROVED.value}:
                raise StateConflict("只有已批准（含部分认可）的案件可以记账")
            ents = conn.execute(
                "SELECT * FROM entitlements WHERE case_id = ? AND status = ?",
                (case_id, EntitlementStatus.GRANTED.value),
            ).fetchall()
            if not ents:
                raise StateConflict("案件没有可消费的权益")
            for e in ents:
                conn.execute(
                    "UPDATE entitlements SET status = ?, consumed_at = ? WHERE entitlement_id = ?",
                    (EntitlementStatus.CONSUMED.value, self._ts(), e["entitlement_id"]),
                )
            conn.execute(
                "UPDATE credential_usage SET status = ? WHERE case_id = ? AND status = ?",
                (EntitlementStatus.CONSUMED.value, case_id, EntitlementStatus.GRANTED.value),
            )
            conn.execute(
                "UPDATE cases SET status = ?, updated_at = ? WHERE case_id = ?",
                (CaseStatus.POSTED.value, self._ts(), case_id),
            )
            self._event(
                conn, case_id, "CREDITS_POSTED", actor,
                {"ledger_reference": ledger_ref or None,
                 "entitlements": [e["entitlement_id"] for e in ents],
                 "total_credits": sum(e["credits"] for e in ents)},
            )
            self._audit(conn, actor, "CREDITS_POST", case_id,
                        {"ledger_reference": ledger_ref or None})
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return self.get_case(conn, case_id, actor)

    # ---------- 撤销决定 ----------

    def revoke(self, conn: sqlite3.Connection, case_id: str, actor, body: dict) -> dict:
        """撤销批准/部分认可/已记账决定：权益墓碑化并释放证书占用，允许重新申请。

        已记账权益撤销后，事件中列明已消费学分，供学籍岗人工冲账。
        """
        if actor.role not in {Role.REVIEWER.value, Role.ADMIN.value}:
            raise PermissionDenied("只有教务审核员可以撤销决定")
        reason = str(body.get("reason", "")).strip()
        if not reason:
            raise ValidationError("撤销必须填写原因")

        conn.execute("BEGIN IMMEDIATE")
        try:
            row = self._load_case_row(conn, case_id)
            if row["status"] not in {
                CaseStatus.APPROVED.value,
                CaseStatus.PARTIALLY_APPROVED.value,
                CaseStatus.POSTED.value,
            }:
                raise StateConflict("只有已生效的批准决定可以撤销")
            ents = conn.execute(
                "SELECT * FROM entitlements WHERE case_id = ? AND status IN (?, ?)",
                (case_id, EntitlementStatus.GRANTED.value, EntitlementStatus.CONSUMED.value),
            ).fetchall()
            consumed = [
                {"entitlement_id": e["entitlement_id"], "course_id": e["course_id"],
                 "credits": e["credits"]}
                for e in ents if e["status"] == EntitlementStatus.CONSUMED.value
            ]
            for e in ents:
                conn.execute(
                    "UPDATE entitlements SET status = ?, revoked_at = ? WHERE entitlement_id = ?",
                    (EntitlementStatus.REVOKED.value, self._ts(), e["entitlement_id"]),
                )
            conn.execute(
                "UPDATE credential_usage SET status = ? WHERE case_id = ? AND status IN (?, ?)",
                (EntitlementStatus.REVOKED.value, case_id, *ACTIVE_USAGE),
            )
            conn.execute(
                "UPDATE cases SET status = ?, revoked_reason = ?, updated_at = ? WHERE case_id = ?",
                (CaseStatus.REVOKED.value, reason, self._ts(), case_id),
            )
            self._event(
                conn, case_id, "DECISION_REVOKED", actor,
                {"reason": reason, "revoked_entitlements": len(ents),
                 "consumed_credits_to_reverse_in_ledger": consumed},
            )
            self._audit(conn, actor, "DECISION_REVOKE", case_id,
                        {"entitlements": len(ents), "consumed": len(consumed)})
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return self.get_case(conn, case_id, actor)

    # ---------- 敏感证据 ----------

    def upload_evidence(self, conn: sqlite3.Connection, case_id: str, actor, body: dict) -> dict:
        kind = str(body.get("kind", "")).strip()
        label = str(body.get("label", "")).strip()
        content = body.get("content")
        if not kind or not label:
            raise ValidationError("证据类型与名称必填")
        if not isinstance(content, str) or not content:
            raise ValidationError("证据正文必填（文本或 Base64）")
        if len(content.encode("utf-8")) > MAX_EVIDENCE_BYTES:
            raise ValidationError("证据大小超过 1 MiB 限制")

        conn.execute("BEGIN IMMEDIATE")
        try:
            row = self._load_case_row(conn, case_id)
            is_owner = row["applicant_id"] == actor.actor_id
            is_assigned_reviewer = (
                actor.role == Role.REVIEWER.value and row["reviewer_id"] == actor.actor_id
            )
            if not is_owner and not is_assigned_reviewer:
                if actor.role == Role.APPLICANT.value:
                    raise NotFound("案件不存在")
                raise PermissionDenied("只有本人和被授权的审核员可以上传证据")
            if row["status"] not in EVIDENCE_OPEN_STATUSES:
                raise StateConflict("案件已结案，不能再上传证据")
            evidence_id = _new_id("EVD")
            conn.execute(
                "INSERT INTO evidence VALUES (?,?,?,?,?,?,?)",
                (evidence_id, case_id, kind, label, content, actor.actor_id, self._ts()),
            )
            # 事件与审计只记录元数据，绝不写入正文。
            self._event(
                conn, case_id, "EVIDENCE_UPLOADED", actor,
                {"evidence_id": evidence_id, "kind": kind, "label": label},
            )
            self._audit(conn, actor, "EVIDENCE_UPLOAD", case_id,
                        {"evidence_id": evidence_id, "kind": kind})
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return {
            "evidence_id": evidence_id,
            "case_id": case_id,
            "kind": kind,
            "label": label,
            "uploaded_by": actor.actor_id,
            "created_at": self._ts(),
        }

    def download_evidence(self, conn: sqlite3.Connection, evidence_id: str, actor) -> dict:
        record = repo.get_evidence_content(conn, evidence_id)
        if record is None:
            raise NotFound("证据不存在")
        row = self._load_case_row(conn, record["case_id"])
        is_owner = row["applicant_id"] == actor.actor_id
        is_assigned_reviewer = (
            actor.role == Role.REVIEWER.value and row["reviewer_id"] == actor.actor_id
        )
        if not is_owner and not is_assigned_reviewer:
            # 无权与不存在统一 404。
            raise NotFound("证据不存在")
        return dict(record)

    # ---------- 权益查询 ----------

    def my_entitlements(self, conn: sqlite3.Connection, actor) -> list[dict]:
        self._require_role(actor, Role.APPLICANT)
        return repo.list_entitlements_for_holder(conn, actor.actor_id)

    # ---------- 辅助 ----------

    def _require_role(self, actor, role: Role) -> None:
        if actor.role != role.value:
            raise PermissionDenied(f"该操作仅面向 {role.value} 角色")

    def _check_program_version(self, conn, program_id, version_id) -> None:
        prog = conn.execute(
            "SELECT * FROM programs WHERE program_id = ?", (program_id,)
        ).fetchone()
        if not prog or not prog["active"]:
            raise ValidationError("培养项目不存在或已停招")
        ver = conn.execute(
            "SELECT * FROM standard_versions WHERE version_id = ?", (version_id,)
        ).fetchone()
        if not ver or not ver["active"]:
            raise ValidationError("标准版本不存在或已停用")
        auth = conn.execute(
            "SELECT * FROM authorities WHERE authority_id = ?", (ver["authority_id"],)
        ).fetchone()
        if not auth or not auth["active"]:
            raise ValidationError("颁发机构不存在或已停用")


def _json(value: Any) -> str:
    import json

    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _number(container: dict, key: str) -> float:
    try:
        return float(container[key])
    except (KeyError, TypeError, ValueError):
        raise ValidationError(f"字段 {key} 必须是数字")


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        raise ValidationError(f"日期格式应为 YYYY-MM-DD：{value!r}")


def _date_str(container: dict, key: str) -> date:
    value = container.get(key)
    if not value:
        raise ValidationError(f"字段 {key} 必填（YYYY-MM-DD）")
    return _parse_date(str(value))
