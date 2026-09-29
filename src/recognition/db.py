"""SQLite 持久化：schema 初始化与演示种子数据。

防重复消费不依赖应用层判断，而由数据库约束保证：

* ``ux_cases_active_credential``：同一证书在同一项目只允许一个未结案申请；
* ``ux_credential_active_use``：一张证书在全校范围内只允许一条有效权益占用
  （跨项目重复领取在写入时直接被数据库拒绝）；
* ``ux_entitlement_active``：同一证书对同一门课程的可消费权益唯一；
* ``case_events`` 仅追加，序号在案件内唯一，支撑完整历史追溯。
"""
from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS persons (
    person_id   TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    role        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS api_keys (
    key         TEXT PRIMARY KEY,
    person_id   TEXT NOT NULL REFERENCES persons(person_id)
);

CREATE TABLE IF NOT EXISTS programs (
    program_id  TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    active      INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS target_courses (
    course_id   TEXT PRIMARY KEY,
    program_id  TEXT NOT NULL REFERENCES programs(program_id),
    course_code TEXT NOT NULL,
    course_name TEXT NOT NULL,
    credits     REAL NOT NULL CHECK (credits > 0),
    UNIQUE (program_id, course_code)
);

CREATE TABLE IF NOT EXISTS authorities (
    authority_id TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    active       INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS standard_versions (
    version_id   TEXT PRIMARY KEY,
    authority_id TEXT NOT NULL REFERENCES authorities(authority_id),
    label        TEXT NOT NULL,
    published_at TEXT NOT NULL,
    active       INTEGER NOT NULL DEFAULT 1,
    UNIQUE (authority_id, label)
);

CREATE TABLE IF NOT EXISTS mappings (
    mapping_id        TEXT PRIMARY KEY,
    version_id        TEXT NOT NULL REFERENCES standard_versions(version_id),
    credential_title  TEXT NOT NULL,
    course_id         TEXT NOT NULL REFERENCES target_courses(course_id),
    min_score         REAL NOT NULL,
    credits_granted   REAL NOT NULL CHECK (credits_granted > 0),
    published_at      TEXT NOT NULL,
    active            INTEGER NOT NULL DEFAULT 1,
    UNIQUE (version_id, credential_title, course_id)
);

CREATE TABLE IF NOT EXISTS credentials (
    credential_id TEXT PRIMARY KEY,
    fingerprint   TEXT NOT NULL UNIQUE,
    holder_id     TEXT NOT NULL REFERENCES persons(person_id),
    holder_name   TEXT NOT NULL,
    serial_number TEXT NOT NULL,
    title         TEXT NOT NULL,
    issued_at     TEXT NOT NULL,
    valid_from    TEXT NOT NULL,
    valid_until   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cases (
    case_id            TEXT PRIMARY KEY,
    program_id         TEXT NOT NULL REFERENCES programs(program_id),
    credential_id      TEXT NOT NULL REFERENCES credentials(credential_id),
    applicant_id       TEXT NOT NULL REFERENCES persons(person_id),
    reviewer_id        TEXT REFERENCES persons(person_id),
    status             TEXT NOT NULL,
    verification_status TEXT NOT NULL,
    verification_ref   TEXT,
    verification_method TEXT,
    score_achieved     REAL NOT NULL,
    score_scale_min    REAL NOT NULL,
    score_scale_max    REAL NOT NULL,
    version_id         TEXT NOT NULL REFERENCES standard_versions(version_id),
    supplement_due     TEXT,
    decision_at        TEXT,
    decision_by        TEXT,
    decision_comment   TEXT,
    rejected_reason    TEXT,
    revoked_reason     TEXT,
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL
);

-- 同一项目内，同一证书只能有一个未结案（未驳回/未撤销）案件。
CREATE UNIQUE INDEX IF NOT EXISTS ux_cases_active_credential
    ON cases(program_id, credential_id)
    WHERE status NOT IN ('REJECTED', 'REVOKED');

CREATE TABLE IF NOT EXISTS case_items (
    item_id          TEXT PRIMARY KEY,
    case_id          TEXT NOT NULL REFERENCES cases(case_id),
    mapping_id       TEXT NOT NULL REFERENCES mappings(mapping_id),
    course_id        TEXT NOT NULL REFERENCES target_courses(course_id),
    course_code      TEXT NOT NULL,
    course_name      TEXT NOT NULL,
    min_score        REAL NOT NULL,
    offered_credits  REAL NOT NULL,
    verdict          TEXT,
    decided_credits  REAL NOT NULL DEFAULT 0,
    UNIQUE (case_id, course_id)
);

CREATE TABLE IF NOT EXISTS entitlements (
    entitlement_id TEXT PRIMARY KEY,
    case_id        TEXT NOT NULL REFERENCES cases(case_id),
    credential_id  TEXT NOT NULL REFERENCES credentials(credential_id),
    holder_id      TEXT NOT NULL REFERENCES persons(person_id),
    course_id      TEXT NOT NULL REFERENCES target_courses(course_id),
    credits        REAL NOT NULL CHECK (credits > 0),
    status         TEXT NOT NULL,
    granted_at     TEXT NOT NULL,
    consumed_at    TEXT,
    revoked_at     TEXT
);

-- 同一证书对同一门课程，只允许一条有效/已消费权益（撤销后留墓碑，放行重新申请）。
CREATE UNIQUE INDEX IF NOT EXISTS ux_entitlement_active
    ON entitlements(credential_id, course_id)
    WHERE status IN ('GRANTED', 'CONSUMED');

CREATE TABLE IF NOT EXISTS credential_usage (
    usage_id      TEXT PRIMARY KEY,
    credential_id TEXT NOT NULL REFERENCES credentials(credential_id),
    case_id       TEXT NOT NULL REFERENCES cases(case_id),
    program_id    TEXT NOT NULL REFERENCES programs(program_id),
    total_credits REAL NOT NULL,
    status        TEXT NOT NULL
);

-- 一张证书全校范围内只允许一条占用：这是跨项目防重复领取的最终闸门。
CREATE UNIQUE INDEX IF NOT EXISTS ux_credential_active_use
    ON credential_usage(credential_id)
    WHERE status IN ('GRANTED', 'CONSUMED');

CREATE TABLE IF NOT EXISTS evidence (
    evidence_id TEXT PRIMARY KEY,
    case_id     TEXT NOT NULL REFERENCES cases(case_id),
    kind        TEXT NOT NULL,
    label       TEXT NOT NULL,
    content     TEXT NOT NULL,
    uploaded_by TEXT NOT NULL REFERENCES persons(person_id),
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS case_events (
    event_id   TEXT PRIMARY KEY,
    case_id    TEXT NOT NULL REFERENCES cases(case_id),
    seq        INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    actor_id   TEXT,
    actor_role TEXT,
    payload    TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    UNIQUE (case_id, seq)
);

CREATE TABLE IF NOT EXISTS audit_log (
    audit_id   TEXT PRIMARY KEY,
    actor_id   TEXT,
    actor_role TEXT,
    action     TEXT NOT NULL,
    case_id    TEXT,
    details    TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_cases_applicant ON cases(applicant_id);
CREATE INDEX IF NOT EXISTS ix_cases_reviewer ON cases(reviewer_id);
CREATE INDEX IF NOT EXISTS ix_cases_status ON cases(status);
CREATE INDEX IF NOT EXISTS ix_events_case ON case_events(case_id, seq);
"""


def connect(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def seed(conn: sqlite3.Connection, *, today: date | None = None) -> None:
    """写入演示基础数据与身份密钥（幂等）。"""
    today = today or date.today()
    rows = [
        ("P-LI", "李娜", "APPLICANT"),
        ("P-WANG", "王磊", "APPLICANT"),
        ("P-CHEN", "陈审核", "REVIEWER"),
        ("P-ZHAO", "赵记账", "REGISTRAR"),
        ("P-ADMIN", "教务处管理员", "ADMIN"),
    ]
    conn.executemany("INSERT OR IGNORE INTO persons VALUES (?,?,?)", rows)
    keys = [
        ("sk-applicant-li", "P-LI"),
        ("sk-applicant-wang", "P-WANG"),
        ("sk-reviewer-chen", "P-CHEN"),
        ("sk-registrar-zhao", "P-ZHAO"),
        ("sk-admin", "P-ADMIN"),
    ]
    conn.executemany("INSERT OR IGNORE INTO api_keys VALUES (?,?)", keys)

    conn.executemany(
        "INSERT OR IGNORE INTO programs VALUES (?,?,1)",
        [("PRG-SE", "软件工程硕士",), ("PRG-DS", "数据科学硕士",)],
    )
    courses = [
        ("C-DS", "PRG-SE", "DATA501", "数据结构与算法", 3.0),
        ("C-DB", "PRG-SE", "DATA502", "数据库系统", 3.0),
        ("C-ST", "PRG-DS", "STAT510", "统计推断", 3.0),
        ("C-ML", "PRG-DS", "DATA620", "机器学习基础", 4.0),
    ]
    conn.executemany(
        "INSERT OR IGNORE INTO target_courses VALUES (?,?,?,?,?)", courses
    )
    conn.execute(
        "INSERT OR IGNORE INTO authorities VALUES (?,?,1)",
        ("AUTH-IDSB", "国际数字技能认证局"),
    )
    conn.execute(
        "INSERT OR IGNORE INTO standard_versions VALUES (?,?,?,?,1)",
        ("VER-IDSB-2024", "AUTH-IDSB", "IDSB 标准 2024 版", today.isoformat()),
    )
    # 境外技能证书标题 -> 课程减免映射，只有成绩达到下限才可申请。
    mappings = [
        ("MAP-SWE-DS", "VER-IDSB-2024", "国际软件工程师认证", "C-DS", 70.0, 3.0),
        ("MAP-SWE-DB", "VER-IDSB-2024", "国际软件工程师认证", "C-DB", 70.0, 3.0),
        ("MAP-SWE-ML", "VER-IDSB-2024", "国际软件工程师认证", "C-ML", 80.0, 4.0),
        ("MAP-DAS-ST", "VER-IDSB-2024", "国际数据分析师认证", "C-ST", 75.0, 3.0),
        ("MAP-DAS-ML", "VER-IDSB-2024", "国际数据分析师认证", "C-ML", 85.0, 4.0),
    ]
    conn.executemany(
        "INSERT OR IGNORE INTO mappings VALUES (?,?,?,?,?,?,?,1)",
        [(m[0], m[1], m[2], m[3], m[4], m[5], today.isoformat()) for m in mappings],
    )
    conn.commit()


def is_seeded(conn: sqlite3.Connection) -> bool:
    return conn.execute("SELECT 1 FROM api_keys LIMIT 1").fetchone() is not None
