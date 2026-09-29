"""领域用例测试：映射、申请、补交、核验、批准原子记账、撤销、隐私、追溯。"""
from __future__ import annotations

import sys
import threading
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from recognition.errors import (
    CreditAlreadyConsumedError,
    DomainError,
    IllegalStateError,
    InvalidCertificateError,
    MappingConflictError,
    NoPublishedMappingError,
    PermissionDeniedError,
    ValidationError,
)
from recognition.models import Principal, Role
from recognition.service import build_service

DIGEST = "sha256:" + "a" * 64
TODAY = date(2026, 9, 29)


class MutableClock:
    def __init__(self, start: datetime) -> None:
        self.value = start

    def __call__(self) -> datetime:
        return self.value


class RecognitionTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = MutableClock(datetime(2026, 9, 29, 10, 0, 0))
        self.svc = build_service(":memory:", clock=lambda: TODAY,
                                 now_provider=self.clock)
        self.admin = Principal("REG-ADMIN", Role.REGISTRY_ADMIN)
        self.student = Principal("2026001", Role.GRADUATE, "毕业生甲")
        self.student2 = Principal("2026002", Role.GRADUATE, "毕业生乙")
        self.supervisor = Principal("REV-ADMIN", Role.REVIEWER, "教务主管")
        self.reviewer = Principal("REV-01", Role.REVIEWER, "审核员甲",
                                  authorized_case_ids=frozenset())
        self.issuer = Principal("ISSUER-GLOBALIT", Role.ISSUER, "认证机构")
        self._publish_mapping()

    def _publish_mapping(self) -> None:
        self.svc.register_grade_scale(
            self.admin, "IT-OCC", "v2025", ["D", "C", "B", "A"]
        )
        self.svc.publish_mapping(self.admin, "IT-OCC", "v2025", [
            {"grade_min": "D", "grade_max": "D", "credits": 4},
            {"grade_min": "C", "grade_max": "C", "credits": 8},
            {"grade_min": "B", "grade_max": "A", "credits": 12},
        ])

    def apply(self, case_id: str, project: str, *, grade: str = "B",
              requested: int = 12, student: Principal | None = None) -> dict:
        return self.svc.submit_application(student or self.student, {
            "case_id": case_id,
            "student_id": (student or self.student).user_id,
            "project_code": project,
            "certificate_digest": DIGEST,
            "issuer_code": "ISSUER-GLOBALIT",
            "issuer_verification_ref": "VRF-2026-0001",
            "standard_code": "IT-OCC",
            "standard_version": "v2025",
            "grade": grade,
            "valid_from": "2025-01-01",
            "valid_until": "2028-01-01",
            "requested_credits": requested,
        })

    def authorize(self, case_id: str, reviewer: Principal | None = None) -> None:
        self.svc.grant_reviewer_access(
            self.supervisor, (reviewer or self.reviewer).user_id, case_id
        )
        if reviewer is None:
            # 重新构造审核员身份以载入刚授予的案件范围
            self.reviewer = Principal(
                "REV-01", Role.REVIEWER, "审核员甲",
                authorized_case_ids=frozenset({case_id})
                if not self.reviewer.authorized_case_ids
                else self.reviewer.authorized_case_ids | {case_id},
            )

    def verify(self, case_id: str) -> None:
        self.svc.verify_certificate(
            self.issuer, case_id, True, "evd://vrf/2026/0001", "VRF-2026-0001"
        )

    def approve(self, case_id: str, mode: str = "full",
                credits: int | None = None) -> dict:
        return self.svc.decide(self.reviewer, case_id, mode,
                               credits=credits, reason="材料齐全")


class MappingTest(RecognitionTestBase):
    def test_submit_computes_credits_from_published_mapping(self) -> None:
        view = self.apply("C-D", "P1", grade="D")
        self.assertEqual(view["mapped_credits"], 4)
        view_b = self.apply("C-B2", "PX", grade="B")
        self.assertEqual(view_b["mapped_credits"], 12)

    def test_unpublished_standard_version_rejected(self) -> None:
        with self.assertRaises(NoPublishedMappingError):
            self.svc.submit_application(self.student, {
                "case_id": "C-X", "student_id": "2026001",
                "project_code": "P1", "certificate_digest": DIGEST,
                "issuer_code": "ISSUER-GLOBALIT",
                "issuer_verification_ref": "VRF-1",
                "standard_code": "IT-OCC", "standard_version": "v1999",
                "grade": "B", "valid_from": "2025-01-01",
                "valid_until": "2028-01-01",
            })

    def test_grade_not_in_scale_rejected(self) -> None:
        with self.assertRaises(InvalidCertificateError):
            self.apply("C-G", "P1", grade="Z")

    def test_published_mapping_is_frozen(self) -> None:
        with self.assertRaises(MappingConflictError):
            self.svc.publish_mapping(self.admin, "IT-OCC", "v2025", [
                {"grade_min": "D", "grade_max": "D", "credits": 5},
            ])
        with self.assertRaises(MappingConflictError):
            self.svc.register_grade_scale(
                self.admin, "IT-OCC", "v2025", ["D", "C", "B", "A", "S"]
            )

    def test_overlapping_intervals_rejected(self) -> None:
        self.svc.register_grade_scale(self.admin, "LNG", "v1", ["1", "2", "3"])
        with self.assertRaises(MappingConflictError):
            self.svc.publish_mapping(self.admin, "LNG", "v1", [
                {"grade_min": "1", "grade_max": "2", "credits": 3},
                {"grade_min": "2", "grade_max": "3", "credits": 6},
            ])


class CertificateValidationTest(RecognitionTestBase):
    def test_bad_digest(self) -> None:
        with self.assertRaises(InvalidCertificateError):
            self.svc.submit_application(self.student, {
                "case_id": "C-BAD", "student_id": "2026001",
                "project_code": "P1", "certificate_digest": "raw-text",
                "issuer_code": "ISSUER-GLOBALIT",
                "issuer_verification_ref": "VRF-1",
                "standard_code": "IT-OCC", "standard_version": "v2025",
                "grade": "B", "valid_from": "2025-01-01",
                "valid_until": "2028-01-01",
            })

    def test_expired_certificate_rejected(self) -> None:
        from recognition.errors import CertificateExpiredError
        with self.assertRaises(CertificateExpiredError):
            self.svc.submit_application(self.student, {
                "case_id": "C-EXP", "student_id": "2026001",
                "project_code": "P1", "certificate_digest": DIGEST,
                "issuer_code": "ISSUER-GLOBALIT",
                "issuer_verification_ref": "VRF-1",
                "standard_code": "IT-OCC", "standard_version": "v2025",
                "grade": "B", "valid_from": "2020-01-01",
                "valid_until": "2026-01-01",
            })

    def test_cannot_apply_for_others(self) -> None:
        with self.assertRaises(PermissionDeniedError):
            self.svc.submit_application(self.student, {
                "case_id": "C-OTHER", "student_id": "2026999",
                "project_code": "P1", "certificate_digest": DIGEST,
                "issuer_code": "ISSUER-GLOBALIT",
                "issuer_verification_ref": "VRF-1",
                "standard_code": "IT-OCC", "standard_version": "v2025",
                "grade": "B", "valid_from": "2025-01-01",
                "valid_until": "2028-01-01",
            })


class HeadlineScenarioTest(RecognitionTestBase):
    """同一张境外证书在两个项目申请减免：第二个项目批准时发现权益已使用。"""

    def test_second_project_cannot_consume_same_credits(self) -> None:
        self.apply("CASE-A", "PROJECT-A")
        self.apply("CASE-B", "PROJECT-B")
        self.authorize("CASE-A")
        self.authorize("CASE-B")
        self.verify("CASE-A")
        self.approve("CASE-A")

        ent = self.svc.get_entitlement(self.student, DIGEST)
        self.assertEqual(ent["total_credits"], 12)
        self.assertEqual(ent["available_credits"], 0)
        self.assertEqual(ent["status"], "consumed")
        self.assertEqual(len(ent["usages"]), 1)

        self.verify("CASE-B")
        with self.assertRaises(CreditAlreadyConsumedError) as ctx:
            self.approve("CASE-B")
        self.assertEqual(ctx.exception.details["available_credits"], 0)

        case_b = self.svc.get_case(self.reviewer, "CASE-B")
        self.assertEqual(case_b["status"], "under_review")  # 批准整体回滚
        self.assertIsNone(case_b["consumption"])

    def test_partial_recognition_leaves_balance_for_other_project(self) -> None:
        self.apply("CASE-A", "PROJECT-A")
        self.apply("CASE-B", "PROJECT-B")
        self.authorize("CASE-A")
        self.authorize("CASE-B")
        self.verify("CASE-A")
        self.verify("CASE-B")
        result = self.approve("CASE-A", mode="partial", credits=5)
        self.assertEqual(result["granted_credits"], 5)
        self.assertEqual(result["status"], "partially_approved")

        # 剩余 7 学分：申请 12 被拒，申请 7 可成
        with self.assertRaises(CreditAlreadyConsumedError):
            self.approve("CASE-B", mode="full")
        result_b = self.approve("CASE-B", mode="partial", credits=7)
        self.assertEqual(result_b["granted_credits"], 7)
        ent = self.svc.get_entitlement(self.student, DIGEST)
        self.assertEqual(ent["available_credits"], 0)
        self.assertEqual({u["project_code"] for u in ent["usages"]},
                         {"PROJECT-A", "PROJECT-B"})

    def test_concurrent_approvals_never_double_spend(self) -> None:
        # 余额只够一个案件：并发批准两个项目，恰好一个成功
        self.apply("CASE-T1", "PROJECT-T1")
        self.apply("CASE-T2", "PROJECT-T2")
        self.authorize("CASE-T1")
        self.authorize("CASE-T2")
        self.verify("CASE-T1")
        self.verify("CASE-T2")

        outcomes: list[str] = []
        barrier = threading.Barrier(2)

        def run(case_id: str) -> None:
            barrier.wait()
            try:
                self.svc.decide(self.reviewer, case_id, "full")
                outcomes.append("ok:" + case_id)
            except CreditAlreadyConsumedError:
                outcomes.append("denied:" + case_id)
            except DomainError:
                outcomes.append("error:" + case_id)

        t1 = threading.Thread(target=run, args=("CASE-T1",))
        t2 = threading.Thread(target=run, args=("CASE-T2",))
        t1.start(); t2.start(); t1.join(); t2.join()

        self.assertEqual(len(outcomes), 2)
        self.assertEqual(sum(o.startswith("ok") for o in outcomes), 1)
        ent = self.svc.get_entitlement(self.student, DIGEST)
        self.assertEqual(ent["available_credits"], 0)
        self.assertEqual(len(ent["usages"]), 1)


class DuplicateApplicationTest(RecognitionTestBase):
    def test_same_project_open_application_blocks_duplicate(self) -> None:
        self.apply("CASE-D1", "PROJECT-D")
        from recognition.errors import DuplicateApplicationError
        with self.assertRaises(DuplicateApplicationError):
            self.apply("CASE-D2", "PROJECT-D")

    def test_cross_project_allowed(self) -> None:
        self.apply("CASE-X1", "PROJECT-X")
        view = self.apply("CASE-X2", "PROJECT-Y")
        self.assertEqual(view["status"], "submitted")


class WorkflowConstraintTest(RecognitionTestBase):
    def test_supplement_flow(self) -> None:
        self.apply("CASE-S", "PROJECT-S")
        self.authorize("CASE-S")
        self.svc.request_supplement(
            self.reviewer, "CASE-S", "成绩单原件", "等级页不清晰"
        )
        # 待补交期间不得作出决定
        with self.assertRaises(IllegalStateError):
            self.approve("CASE-S")
        # 非本人不得补交
        with self.assertRaises(PermissionDeniedError):
            self.svc.submit_supplement(
                self.student2, "CASE-S", "transcript", "evd://new"
            )
        result = self.svc.submit_supplement(
            self.student, "CASE-S", "transcript", "evd://new-1", "已补传"
        )
        self.assertEqual(result["status"], "under_review")
        # 非待补交状态再次补交被拒
        with self.assertRaises(IllegalStateError):
            self.svc.submit_supplement(
                self.student, "CASE-S", "transcript", "evd://new-2"
            )

    def test_cannot_approve_without_issuer_verification(self) -> None:
        self.apply("CASE-V", "PROJECT-V")
        self.authorize("CASE-V")
        with self.assertRaises(IllegalStateError):
            self.approve("CASE-V")

    def test_verification_requires_matching_issuer_and_ref(self) -> None:
        self.apply("CASE-I", "PROJECT-I")
        other_issuer = Principal("ISSUER-OTHER", Role.ISSUER)
        with self.assertRaises(PermissionDeniedError):
            self.svc.verify_certificate(
                other_issuer, "CASE-I", True, "evd://x", "VRF-2026-0001"
            )
        with self.assertRaises(PermissionDeniedError):
            self.svc.verify_certificate(
                self.issuer, "CASE-I", True, "evd://x", "WRONG-REF"
            )

    def test_reject_creates_no_entitlement(self) -> None:
        self.apply("CASE-R", "PROJECT-R")
        self.authorize("CASE-R")
        self.verify("CASE-R")
        result = self.svc.decide(self.reviewer, "CASE-R", "reject",
                                 reason="标准不符")
        self.assertEqual(result["status"], "rejected")
        with self.assertRaises(Exception):
            self.svc.get_entitlement(self.student, DIGEST)
        # 已结案不能重复决定
        with self.assertRaises(IllegalStateError):
            self.svc.decide(self.reviewer, "CASE-R", "full")

    def test_partial_credits_must_be_below_cap(self) -> None:
        self.apply("CASE-P", "PROJECT-P")
        self.authorize("CASE-P")
        self.verify("CASE-P")
        with self.assertRaises(ValidationError):
            self.approve("CASE-P", mode="partial", credits=12)
        with self.assertRaises(ValidationError):
            self.approve("CASE-P", mode="partial", credits=0)

    def test_unauthorized_reviewer_cannot_decide_or_see(self) -> None:
        self.apply("CASE-U", "PROJECT-U")
        outsider = Principal("REV-99", Role.REVIEWER, "审核员乙",
                             authorized_case_ids=frozenset({"OTHER"}))
        with self.assertRaises(PermissionDeniedError):
            self.svc.decide(outsider, "CASE-U", "full")
        with self.assertRaises(PermissionDeniedError):
            self.svc.get_case(outsider, "CASE-U")


class RevocationTest(RecognitionTestBase):
    def _approved_case(self, case_id: str, project: str,
                       credits: int | None = None) -> None:
        self.apply(case_id, project, requested=credits or 12)
        self.authorize(case_id)
        self.verify(case_id)
        self.approve(case_id,
                     mode="partial" if credits else "full",
                     credits=credits)

    def test_revoke_refunds_credits_within_window(self) -> None:
        self._approved_case("CASE-K", "PROJECT-K", credits=6)
        self.clock.value += timedelta(days=10)
        result = self.svc.revoke(self.reviewer, "CASE-K", "发现材料造假")
        self.assertEqual(result["refunded_credits"], 6)
        ent = self.svc.get_entitlement(self.student, DIGEST)
        self.assertEqual(ent["available_credits"], 12)
        case = self.svc.get_case(self.reviewer, "CASE-K")
        self.assertEqual(case["status"], "revoked")
        self.assertEqual(case["granted_credits"], 0)

    def test_revoke_after_window_rejected(self) -> None:
        self._approved_case("CASE-W", "PROJECT-W", credits=6)
        self.clock.value += timedelta(days=31)
        with self.assertRaises(IllegalStateError):
            self.svc.revoke(self.reviewer, "CASE-W", "逾期撤销")

    def test_double_revoke_rejected(self) -> None:
        self._approved_case("CASE-2", "PROJECT-2", credits=6)
        self.svc.revoke(self.reviewer, "CASE-2", "理由")
        with self.assertRaises(IllegalStateError):
            self.svc.revoke(self.reviewer, "CASE-2", "再次撤销")

    def test_refunded_credits_usable_elsewhere(self) -> None:
        self._approved_case("CASE-M", "PROJECT-M")  # 用满 12
        self.apply("CASE-N", "PROJECT-N")
        self.authorize("CASE-N")
        self.verify("CASE-N")
        with self.assertRaises(CreditAlreadyConsumedError):
            self.approve("CASE-N")
        self.svc.revoke(self.reviewer, "CASE-M", "撤销首案")
        result = self.approve("CASE-N")
        self.assertEqual(result["status"], "approved")
        self.assertEqual(result["granted_credits"], 12)


class PrivacyTest(RecognitionTestBase):
    def test_sensitive_fields_only_for_holder_and_authorized_reviewer(self) -> None:
        view = self.apply("CASE-SEC", "PROJECT-SEC")
        self.assertIn("issuer_verification_ref", view)

        # 非本人毕业生
        with self.assertRaises(PermissionDeniedError):
            self.svc.get_case(self.student2, "CASE-SEC")

        # 未授权审核员
        with self.assertRaises(PermissionDeniedError):
            self.svc.get_case(self.reviewer, "CASE-SEC")

        # 授权后可见
        self.authorize("CASE-SEC")
        reviewer_view = self.svc.get_case(self.reviewer, "CASE-SEC")
        self.assertIn("issuer_verification_ref", reviewer_view)

        # 认证机构不得查看案件敏感证据
        with self.assertRaises(PermissionDeniedError):
            self.svc.get_case(self.issuer, "CASE-SEC")

    def test_entitlement_visible_only_to_holder(self) -> None:
        # 先产生一笔权益
        self.apply("CASE-E", "PROJECT-E")
        self.authorize("CASE-E")
        self.verify("CASE-E")
        self.approve("CASE-E")
        with self.assertRaises(PermissionDeniedError):
            self.svc.get_entitlement(self.reviewer, DIGEST)
        with self.assertRaises(PermissionDeniedError):
            self.svc.get_entitlement(self.student2, DIGEST)

    def test_listing_hides_sensitive_fields(self) -> None:
        self.apply("CASE-L", "PROJECT-L")
        self.authorize("CASE-L")
        rows = self.svc.list_cases(self.reviewer)
        row = next(r for r in rows if r["case_id"] == "CASE-L")
        self.assertNotIn("issuer_verification_ref", row)
        self.assertNotIn("verification_evidence_ref", row)


class HistoryTest(RecognitionTestBase):
    def test_full_history_is_append_only_and_ordered(self) -> None:
        self.apply("CASE-H", "PROJECT-H")
        self.authorize("CASE-H")
        self.svc.request_supplement(self.reviewer, "CASE-H", "材料", "缺页")
        self.svc.submit_supplement(
            self.student, "CASE-H", "transcript", "evd://h-1"
        )
        self.verify("CASE-H")
        self.approve("CASE-H")
        self.svc.revoke(self.reviewer, "CASE-H", "审计抽样撤销")

        history = self.svc.history(self.student, "CASE-H")
        types = [e["event_type"] for e in history["events"]]
        self.assertEqual(types, [
            "application_submitted",
            "reviewer_access_granted",
            "supplement_requested",
            "supplement_submitted",
            "certificate_verified",
            "approved_full",
            "decision_revoked",
            "credits_refunded",
        ])
        # 追加式日志禁止改写
        import sqlite3
        with self.assertRaises(sqlite3.DatabaseError):
            with self.svc.store.lock:
                self.svc.store._conn.execute(
                    "UPDATE audit_events SET event_type='x' WHERE id=1"
                )

    def test_history_requires_access(self) -> None:
        self.apply("CASE-HH", "PROJECT-HH")
        with self.assertRaises(PermissionDeniedError):
            self.svc.history(self.student2, "CASE-HH")


if __name__ == "__main__":
    unittest.main()
