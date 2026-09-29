"""数据访问：行 -> 字典的序列化与常用查询。"""
from __future__ import annotations

import json
import sqlite3
from typing import Any


def _row_to_case(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "case_id": row["case_id"],
        "program_id": row["program_id"],
        "credential_id": row["credential_id"],
        "applicant_id": row["applicant_id"],
        "reviewer_id": row["reviewer_id"],
        "status": row["status"],
        "verification": {
            "status": row["verification_status"],
            "reference": row["verification_ref"],
            "method": row["verification_method"],
        },
        "score": {
            "achieved": row["score_achieved"],
            "scale_min": row["score_scale_min"],
            "scale_max": row["score_scale_max"],
        },
        "version_id": row["version_id"],
        "supplement_due": row["supplement_due"],
        "decision_at": row["decision_at"],
        "decision_by": row["decision_by"],
        "decision_comment": row["decision_comment"],
        "rejected_reason": row["rejected_reason"],
        "revoked_reason": row["revoked_reason"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def get_case(conn: sqlite3.Connection, case_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM cases WHERE case_id = ?", (case_id,)).fetchone()
    return _row_to_case(row) if row else None


def get_case_row(conn: sqlite3.Connection, case_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM cases WHERE case_id = ?", (case_id,)).fetchone()


def list_items(conn: sqlite3.Connection, case_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM case_items WHERE case_id = ? ORDER BY course_code", (case_id,)
    ).fetchall()
    return [dict(r) for r in rows]


def get_credential(conn: sqlite3.Connection, credential_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM credentials WHERE credential_id = ?", (credential_id,)
    ).fetchone()


def find_credential_by_fingerprint(conn: sqlite3.Connection, fingerprint: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM credentials WHERE fingerprint = ?", (fingerprint,)
    ).fetchone()


def list_entitlements_for_case(conn: sqlite3.Connection, case_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM entitlements WHERE case_id = ? ORDER BY course_id", (case_id,)
    ).fetchall()
    return [dict(r) for r in rows]


def list_entitlements_for_holder(conn: sqlite3.Connection, holder_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM entitlements WHERE holder_id = ? ORDER BY granted_at", (holder_id,)
    ).fetchall()
    return [dict(r) for r in rows]


def list_evidence(conn: sqlite3.Connection, case_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT evidence_id, kind, label, uploaded_by, created_at "
        "FROM evidence WHERE case_id = ? ORDER BY created_at",
        (case_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_evidence_content(conn: sqlite3.Connection, evidence_id: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM evidence WHERE evidence_id = ?", (evidence_id,)
    ).fetchone()
    return dict(row) if row else None


def list_events(conn: sqlite3.Connection, case_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT event_id, seq, event_type, actor_id, actor_role, payload, created_at "
        "FROM case_events WHERE case_id = ? ORDER BY seq",
        (case_id,),
    ).fetchall()
    result = []
    for r in rows:
        item = dict(r)
        item["payload"] = json.loads(item["payload"] or "{}")
        result.append(item)
    return result


def list_cases(conn: sqlite3.Connection, *, person_id: str, role: str) -> list[dict]:
    """案件列表：本人看自己的案件；授权审核员/记账员看全部。"""
    if role == "REVIEWER":
        rows = conn.execute(
            "SELECT * FROM cases ORDER BY created_at DESC"
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM cases WHERE applicant_id = ? ORDER BY created_at DESC",
            (person_id,),
        ).fetchall()
    return [_row_to_case(r) for r in rows]
