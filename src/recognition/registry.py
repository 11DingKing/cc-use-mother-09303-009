"""已发布映射注册表：成绩等级表与"成绩区间 -> 学分权益"规则。

映射一经发布即冻结：同一标准版本只能发布一次，规则区间不得重叠。
案件提交时以当时已发布版本计算权益，结果记入案件，不受后续改版影响。
"""
from __future__ import annotations

import sqlite3

from .errors import (
    InvalidCertificateError,
    MappingConflictError,
    NoPublishedMappingError,
    ValidationError,
)


class MappingRegistry:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    # ----------------------------------------------------------- grade scales

    def register_grade_scale(self, standard_code: str, version: str,
                             ordered_grades: list[str]) -> None:
        """登记成绩等级表（从低到高）。发布后冻结，不可再改。"""
        if not standard_code or not version:
            raise ValidationError("标准代码与版本不能为空")
        if len(ordered_grades) < 2 or len(ordered_grades) != len(set(ordered_grades)):
            raise ValidationError("成绩等级表至少包含两个且不得重复的等级")
        if self._has_published(standard_code, version):
            raise MappingConflictError("该标准版本已发布映射，等级表不可修改")
        self.conn.executemany(
            """INSERT INTO mapping_grades (standard_code, standard_version, grade, ordinal)
               VALUES (?,?,?,?)
               ON CONFLICT(standard_code, standard_version, grade)
                 DO UPDATE SET ordinal=excluded.ordinal""",
            [
                (standard_code, version, grade, ordinal)
                for ordinal, grade in enumerate(ordered_grades)
            ],
        )

    # ------------------------------------------------------------- publishing

    def publish(self, standard_code: str, version: str,
                rules: list[dict], published_by: str) -> None:
        """发布一个标准版本的完整映射（原子、一次性）。"""
        if self._has_published(standard_code, version):
            raise MappingConflictError("该标准版本的映射已发布，不能重复发布或修改")
        if not rules:
            raise ValidationError("发布映射至少需要一条规则")

        prepared: list[tuple[str, str, int, int, int]] = []
        for rule in rules:
            grade_min = str(rule["grade_min"])
            grade_max = str(rule["grade_max"])
            credits = int(rule["credits"])
            if credits <= 0:
                raise ValidationError("权益学分必须为正整数")
            o_min = self._ordinal(standard_code, version, grade_min)
            o_max = self._ordinal(standard_code, version, grade_max)
            if o_min is None or o_max is None:
                raise ValidationError(
                    f"等级 {grade_min}~{grade_max} 未在成绩等级表中登记"
                )
            if o_min > o_max:
                raise ValidationError(f"区间 {grade_min}~{grade_max} 下沿高于上沿")
            prepared.append((standard_code, version, grade_min, grade_max, credits))

        # 区间不得重叠（按序数排序后相邻检查）
        ordinals = sorted(
            ((self._ordinal(standard_code, version, g_min),
              self._ordinal(standard_code, version, g_max))
             for _, _, g_min, g_max, _ in prepared),
        )
        for (_, prev_max), (nxt_min, _) in zip(ordinals, ordinals[1:]):
            if nxt_min <= prev_max:  # type: ignore[operator]
                raise MappingConflictError("已发布映射的成绩区间不允许重叠")

        from .store import _now
        self.conn.executemany(
            """INSERT INTO mapping_rules (standard_code, standard_version,
                   grade_min, grade_max, credits, published, published_at, published_by)
               VALUES (?,?,?,?,?,1,?,?)""",
            [
                (sc, ver, gmin, gmax, credits, _now(), published_by)
                for sc, ver, gmin, gmax, credits in prepared
            ],
        )

    # -------------------------------------------------------------- evaluation

    def credits_for(self, standard_code: str, version: str, grade: str) -> int:
        """依据已发布映射计算成绩对应的可申请学分。"""
        published = self._has_published(standard_code, version)
        ordinal = self._ordinal(standard_code, version, grade)
        if ordinal is None:
            if not published:
                raise NoPublishedMappingError(
                    f"标准 {standard_code}@{version} 尚未发布任何映射"
                )
            raise InvalidCertificateError(
                f"成绩 {grade} 不在标准 {standard_code}@{version} 的等级表内"
            )
        row = self.conn.execute(
            """SELECT r.credits FROM mapping_rules r
                 JOIN mapping_grades g1
                   ON g1.standard_code=r.standard_code
                  AND g1.standard_version=r.standard_version
                  AND g1.grade=r.grade_min
                 JOIN mapping_grades g2
                   ON g2.standard_code=r.standard_code
                  AND g2.standard_version=r.standard_version
                  AND g2.grade=r.grade_max
                WHERE r.standard_code=? AND r.standard_version=?
                  AND r.published=1
                  AND g1.ordinal <= ? AND g2.ordinal >= ?
                ORDER BY (g2.ordinal - g1.ordinal) ASC, r.credits DESC
                LIMIT 1""",
            (standard_code, version, ordinal, ordinal),
        ).fetchone()
        if row is None:
            raise NoPublishedMappingError(
                f"标准 {standard_code}@{version} 没有覆盖成绩 {grade} 的已发布映射"
            )
        return int(row["credits"])

    # ---------------------------------------------------------------- helpers

    def _ordinal(self, standard_code: str, version: str, grade: str) -> int | None:
        row = self.conn.execute(
            """SELECT ordinal FROM mapping_grades
                WHERE standard_code=? AND standard_version=? AND grade=?""",
            (standard_code, version, grade),
        ).fetchone()
        return int(row["ordinal"]) if row else None

    def _has_published(self, standard_code: str, version: str) -> bool:
        row = self.conn.execute(
            """SELECT 1 FROM mapping_rules
                WHERE standard_code=? AND standard_version=? AND published=1
                LIMIT 1""",
            (standard_code, version),
        ).fetchone()
        return row is not None
