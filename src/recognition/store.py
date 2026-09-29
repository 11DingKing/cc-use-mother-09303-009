"""SQLite 持久化：建表脚本、行映射与事务边界。

写事务一律 ``BEGIN IMMEDIATE``，配合进程内互斥锁，保证批准动作的
"读余额—扣减—写案件"在全局串行执行，杜绝跨项目并发重复领取。
"""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Iterator

from .models import (
    Case,
    CaseStatus,
    ConsumptionRecord,
    DecisionMode,
    Entitlement,
    EntitlementStatus,
    MappingRule,
    Supplement,
    VerificationStatus,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS mapping_grades (
    standard_code TEXT NOT NULL,
    standard_version TEXT NOT NULL,
    grade TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    PRIMARY KEY (standard_code, standard_version, grade)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS mapping_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    standard_code TEXT NOT NULL,
    standard_version TEXT NOT NULL,
    grade_min TEXT NOT NULL,
    grade_max TEXT NOT NULL,
    credits INTEGER NOT NULL CHECK (credits > 0),
    published INTEGER NOT NULL DEFAULT 0,
    published_at TEXT,
    published_by TEXT,
    UNIQUE (standard_code, standard_version, grade_min, grade_max)
);

CREATE TABLE IF NOT EXISTS cases (
    case_id TEXT PRIMARY KEY,
    student_id TEXT NOT NULL,
    project_code TEXT NOT NULL,
    certificate_digest TEXT NOT NULL,
    issuer_code TEXT NOT NULL,
    issuer_verification_ref TEXT NOT NULL,
    standard_code TEXT NOT NULL,
    standard_version TEXT NOT NULL,
    grade TEXT NOT NULL,
    cert_valid_from TEXT NOT NULL,
    cert_valid_until TEXT NOT NULL,
    status TEXT NOT NULL,
    requested_credits INTEGER NOT NULL CHECK (requested_credits > 0),
    mapped_credits INTEGER NOT NULL DEFAULT 0,
    granted_credits INTEGER NOT NULL DEFAULT 0,
    decision_mode TEXT,
    verification TEXT NOT NULL DEFAULT 'pending',
    verification_evidence_ref TEXT,
    decided_by TEXT,
    decided_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1
);

-- 同一证书在同一项目只允许一条未结案申请（跨项目允许）
CREATE UNIQUE INDEX IF NOT EXISTS ux_cases_open
    ON cases (certificate_digest, project_code)
    WHERE status IN ('submitted', 'pending_supplement', 'under_review');

CREATE INDEX IF NOT EXISTS ix_cases_student ON cases (student_id);
CREATE INDEX IF NOT EXISTS ix_cases_digest ON cases (certificate_digest);

CREATE TABLE IF NOT EXISTS supplements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id TEXT NOT NULL REFERENCES cases(case_id),
    material_type TEXT NOT NULL,
    evidence_ref TEXT NOT NULL,
    submitted_by TEXT NOT NULL,
    submitted_at TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS entitlements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    certificate_digest TEXT NOT NULL UNIQUE,
    holder_student_id TEXT NOT NULL,
    total_credits INTEGER NOT NULL CHECK (total_credits > 0),
    available_credits INTEGER NOT NULL CHECK (available_credits >= 0),
    status TEXT NOT NULL,
    source_case_id TEXT NOT NULL REFERENCES cases(case_id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS consumption_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entitlement_id INTEGER NOT NULL REFERENCES entitlements(id),
    certificate_digest TEXT NOT NULL,
    case_id TEXT NOT NULL UNIQUE,
    project_code TEXT NOT NULL,
    student_id TEXT NOT NULL,
    credits INTEGER NOT NULL CHECK (credits > 0),
    consumed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reviewer_case_access (
    reviewer_id TEXT NOT NULL,
    case_id TEXT NOT NULL REFERENCES cases(case_id),
    granted_by TEXT NOT NULL,
    granted_at TEXT NOT NULL,
    PRIMARY KEY (reviewer_id, case_id)
) WITHOUT ROWID;

-- 追加式审计日志，只允许 INSERT
CREATE TABLE IF NOT EXISTS audit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    aggregate_type TEXT NOT NULL,
    aggregate_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    actor_role TEXT NOT NULL,
    payload TEXT NOT NULL,
    occurred_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_audit_aggregate
    ON audit_events (aggregate_type, aggregate_id, id);

CREATE TRIGGER IF NOT EXISTS trg_audit_no_update
BEFORE UPDATE ON audit_events
BEGIN
    SELECT RAISE(ABORT, 'audit_events 为追加式日志，禁止修改');
END;
CREATE TRIGGER IF NOT EXISTS trg_audit_no_delete
BEFORE DELETE ON audit_events
BEGIN
    SELECT RAISE(ABORT, 'audit_events 为追加式日志，禁止删除');
END;
"""


def _d(value: date) -> str:
    return value.isoformat()


def _date(value: str) -> date:
    return date.fromisoformat(value)


def _dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _now() -> str:
    return datetime.utcnow().isoformat(timespec="seconds")


class SqliteStore:
    """封装 SQLite 连接与行映射。"""

    def __init__(self, db_path: str | Path = ":memory:") -> None:
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            str(db_path), check_same_thread=False, isolation_level=None
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.executescript(SCHEMA)

    @property
    def lock(self) -> threading.RLock:
        return self._lock

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """写事务：加锁后立即 ``BEGIN IMMEDIATE``，提交或回滚。"""
        self._lock.acquire()
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")
        finally:
            self._lock.release()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------------ cases

    def insert_case(self, conn: sqlite3.Connection, case: Case) -> None:
        conn.execute(
            """INSERT INTO cases (case_id, student_id, project_code,
                   certificate_digest, issuer_code, issuer_verification_ref,
                   standard_code, standard_version, grade,
                   cert_valid_from, cert_valid_until, status,
                   requested_credits, mapped_credits, granted_credits, decision_mode,
                   verification, verification_evidence_ref,
                   decided_by, decided_at, created_at, updated_at, version)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                case.case_id, case.student_id, case.project_code,
                case.certificate_digest, case.issuer_code,
                case.issuer_verification_ref, case.standard_code,
                case.standard_version, case.grade,
                _d(case.cert_valid_from), _d(case.cert_valid_until),
                case.status.value, case.requested_credits,
                case.mapped_credits, case.granted_credits,
                case.decision_mode.value if case.decision_mode else None,
                case.verification.value, case.verification_evidence_ref,
                case.decided_by,
                case.decided_at.isoformat(timespec="seconds") if case.decided_at else None,
                _now(), _now(), case.version,
            ),
        )

    def update_case(self, conn: sqlite3.Connection, case: Case) -> None:
        cur = conn.execute(
            """UPDATE cases SET status=?, requested_credits=?, mapped_credits=?,
                   granted_credits=?,
                   decision_mode=?, verification=?, verification_evidence_ref=?,
                   decided_by=?, decided_at=?, updated_at=?, version=version+1
               WHERE case_id=? AND version=?""",
            (
                case.status.value, case.requested_credits, case.mapped_credits,
                case.granted_credits,
                case.decision_mode.value if case.decision_mode else None,
                case.verification.value, case.verification_evidence_ref,
                case.decided_by,
                case.decided_at.isoformat(timespec="seconds") if case.decided_at else None,
                _now(), case.case_id, case.version,
            ),
        )
        if cur.rowcount != 1:
            raise RuntimeError("案件版本冲突或已丢失")  # 由事务回滚保护

    def get_case_row(self, conn: sqlite3.Connection, case_id: str) -> sqlite3.Row | None:
        return conn.execute("SELECT * FROM cases WHERE case_id=?", (case_id,)).fetchone()

    def list_cases_for_student(self, conn: sqlite3.Connection, student_id: str) -> list[sqlite3.Row]:
        return list(conn.execute(
            "SELECT * FROM cases WHERE student_id=? ORDER BY created_at", (student_id,)
        ))

    @staticmethod
    def row_to_case(row: sqlite3.Row) -> Case:
        return Case(
            case_id=row["case_id"],
            student_id=row["student_id"],
            project_code=row["project_code"],
            certificate_digest=row["certificate_digest"],
            issuer_code=row["issuer_code"],
            issuer_verification_ref=row["issuer_verification_ref"],
            standard_code=row["standard_code"],
            standard_version=row["standard_version"],
            grade=row["grade"],
            cert_valid_from=_date(row["cert_valid_from"]),
            cert_valid_until=_date(row["cert_valid_until"]),
            status=CaseStatus(row["status"]),
            requested_credits=row["requested_credits"],
            mapped_credits=row["mapped_credits"],
            granted_credits=row["granted_credits"],
            decision_mode=DecisionMode(row["decision_mode"]) if row["decision_mode"] else None,
            verification=VerificationStatus(row["verification"]),
            verification_evidence_ref=row["verification_evidence_ref"],
            decided_by=row["decided_by"],
            decided_at=_dt(row["decided_at"]),
            created_at=_dt(row["created_at"]) or datetime.utcnow(),
            updated_at=_dt(row["updated_at"]) or datetime.utcnow(),
            version=row["version"],
        )

    # -------------------------------------------------------------- supplements

    def insert_supplement(self, conn: sqlite3.Connection, supp: Supplement) -> int:
        cur = conn.execute(
            """INSERT INTO supplements (case_id, material_type, evidence_ref,
                   submitted_by, submitted_at, note)
               VALUES (?,?,?,?,?,?)""",
            (
                supp.case_id, supp.material_type, supp.evidence_ref,
                supp.submitted_by, _now(), supp.note,
            ),
        )
        return int(cur.lastrowid)

    def list_supplements(self, conn: sqlite3.Connection, case_id: str) -> list[Supplement]:
        rows = conn.execute(
            "SELECT * FROM supplements WHERE case_id=? ORDER BY id", (case_id,)
        )
        return [
            Supplement(
                id=r["id"], case_id=r["case_id"], material_type=r["material_type"],
                evidence_ref=r["evidence_ref"], submitted_by=r["submitted_by"],
                submitted_at=_dt(r["submitted_at"]) or datetime.utcnow(),
                note=r["note"],
            )
            for r in rows
        ]

    # -------------------------------------------------------------- entitlements

    def ensure_entitlement(self, conn: sqlite3.Connection, ent: Entitlement) -> Entitlement:
        """首次批准时按证书建档；已存在则直接返回（UNIQUE 防重）。"""
        conn.execute(
            """INSERT OR IGNORE INTO entitlements (certificate_digest,
                   holder_student_id, total_credits, available_credits,
                   status, source_case_id, created_at)
               VALUES (?,?,?,?,?,?,?)""",
            (
                ent.certificate_digest, ent.holder_student_id,
                ent.total_credits, ent.total_credits, ent.status.value,
                ent.source_case_id, _now(),
            ),
        )
        return self.get_entitlement(conn, ent.certificate_digest)  # type: ignore[return-value]

    def get_entitlement(self, conn: sqlite3.Connection, digest: str) -> Entitlement | None:
        row = conn.execute(
            "SELECT * FROM entitlements WHERE certificate_digest=?", (digest,)
        ).fetchone()
        return self._row_to_entitlement(row) if row else None

    @staticmethod
    def _row_to_entitlement(row: sqlite3.Row) -> Entitlement:
        return Entitlement(
            id=row["id"],
            certificate_digest=row["certificate_digest"],
            holder_student_id=row["holder_student_id"],
            total_credits=row["total_credits"],
            available_credits=row["available_credits"],
            status=EntitlementStatus(row["status"]),
            source_case_id=row["source_case_id"],
            created_at=_dt(row["created_at"]) or datetime.utcnow(),
        )

    def debit_entitlement(self, conn: sqlite3.Connection, ent_id: int, credits: int) -> int:
        """条件扣减：余额足够才生效，返回影响行数（0 即余额不足）。"""
        cur = conn.execute(
            """UPDATE entitlements
                  SET available_credits = available_credits - ?
                WHERE id = ? AND available_credits >= ? AND status = 'granted'""",
            (credits, ent_id, credits),
        )
        return cur.rowcount

    def refund_entitlement(self, conn: sqlite3.Connection, ent_id: int, credits: int) -> None:
        conn.execute(
            """UPDATE entitlements
                  SET available_credits = available_credits + ?,
                      status = 'granted'
                WHERE id = ?""",
            (credits, ent_id),
        )

    def mark_entitlement_status(self, conn: sqlite3.Connection, ent_id: int, status: EntitlementStatus) -> None:
        conn.execute("UPDATE entitlements SET status=? WHERE id=?", (status.value, ent_id))

    def insert_consumption(self, conn: sqlite3.Connection, rec: ConsumptionRecord) -> int:
        cur = conn.execute(
            """INSERT INTO consumption_records (entitlement_id, certificate_digest,
                   case_id, project_code, student_id, credits, consumed_at)
               VALUES (?,?,?,?,?,?,?)""",
            (
                rec.entitlement_id, rec.certificate_digest, rec.case_id,
                rec.project_code, rec.student_id, rec.credits, _now(),
            ),
        )
        return int(cur.lastrowid)

    def list_consumptions(self, conn: sqlite3.Connection, digest: str) -> list[ConsumptionRecord]:
        rows = conn.execute(
            "SELECT * FROM consumption_records WHERE certificate_digest=? ORDER BY id",
            (digest,),
        )
        return [
            ConsumptionRecord(
                id=r["id"], entitlement_id=r["entitlement_id"],
                certificate_digest=r["certificate_digest"], case_id=r["case_id"],
                project_code=r["project_code"], student_id=r["student_id"],
                credits=r["credits"],
                consumed_at=_dt(r["consumed_at"]) or datetime.utcnow(),
            )
            for r in rows
        ]

    def get_consumption_by_case(self, conn: sqlite3.Connection, case_id: str) -> ConsumptionRecord | None:
        row = conn.execute(
            "SELECT * FROM consumption_records WHERE case_id=?", (case_id,)
        ).fetchone()
        if not row:
            return None
        return ConsumptionRecord(
            id=row["id"], entitlement_id=row["entitlement_id"],
            certificate_digest=row["certificate_digest"], case_id=row["case_id"],
            project_code=row["project_code"], student_id=row["student_id"],
            credits=row["credits"],
            consumed_at=_dt(row["consumed_at"]) or datetime.utcnow(),
        )

    # ------------------------------------------------------------------ registry

    def list_published_mappings(self, conn: sqlite3.Connection) -> list[MappingRule]:
        rows = conn.execute(
            """SELECT * FROM mapping_rules WHERE published=1
                ORDER BY standard_code, standard_version, grade_min"""
        )
        return [self._row_to_rule(r) for r in rows]

    @staticmethod
    def _row_to_rule(row: sqlite3.Row) -> MappingRule:
        return MappingRule(
            standard_code=row["standard_code"],
            standard_version=row["standard_version"],
            grade_min=row["grade_min"],
            grade_max=row["grade_max"],
            credits=row["credits"],
            published=bool(row["published"]),
        )

    # ------------------------------------------------------------- access / audit

    def grant_case_access(self, conn: sqlite3.Connection,
                          reviewer_id: str, case_id: str, granted_by: str) -> None:
        conn.execute(
            """INSERT OR IGNORE INTO reviewer_case_access
                   (reviewer_id, case_id, granted_by, granted_at)
               VALUES (?,?,?,?)""",
            (reviewer_id, case_id, granted_by, _now()),
        )

    def reviewer_authorized_case_ids(self, reviewer_id: str) -> frozenset[str] | None:
        """返回该审核员被授权的案件集合；空集合表示无任何授权。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT case_id FROM reviewer_case_access WHERE reviewer_id=?",
                (reviewer_id,),
            ).fetchall()
        return frozenset(r["case_id"] for r in rows)

    def append_audit(self, conn: sqlite3.Connection, *, aggregate_type: str,
                     aggregate_id: str, event_type: str, actor_id: str,
                     actor_role: str, payload: dict) -> None:
        import json
        conn.execute(
            """INSERT INTO audit_events (aggregate_type, aggregate_id, event_type,
                   actor_id, actor_role, payload, occurred_at)
               VALUES (?,?,?,?,?,?,?)""",
            (
                aggregate_type, aggregate_id, event_type, actor_id,
                actor_role, json.dumps(payload, ensure_ascii=False, sort_keys=True),
                _now(),
            ),
        )

    def list_audit(self, conn: sqlite3.Connection,
                   aggregate_type: str, aggregate_id: str) -> list[dict]:
        import json
        rows = conn.execute(
            """SELECT * FROM audit_events
                WHERE aggregate_type=? AND aggregate_id=?
                ORDER BY id""",
            (aggregate_type, aggregate_id),
        )
        return [
            {
                "id": r["id"],
                "aggregate_type": r["aggregate_type"],
                "aggregate_id": r["aggregate_id"],
                "event_type": r["event_type"],
                "actor_id": r["actor_id"],
                "actor_role": r["actor_role"],
                "payload": json.loads(r["payload"]),
                "occurred_at": r["occurred_at"],
            }
            for r in rows
        ]
