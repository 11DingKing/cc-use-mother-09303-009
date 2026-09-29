"""HTTP API 端到端测试：真实 socket 调用，覆盖鉴权、头条场景与错误码。"""
from __future__ import annotations

import json
import sys
import threading
import unittest
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from recognition.app import ApiApp, make_server
from recognition.models import Role
from recognition.service import build_service

DIGEST = "sha256:" + "b" * 64


class ApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = build_service(":memory:", clock=lambda: date(2026, 9, 29))
        self.app = ApiApp(self.svc)
        self.app.tokens.register("tok-admin", "REG-ADMIN", Role.REGISTRY_ADMIN)
        self.app.tokens.register("tok-grad", "2026001", Role.GRADUATE)
        self.app.tokens.register("tok-grad2", "2026002", Role.GRADUATE)
        self.app.tokens.register("tok-rev", "REV-01", Role.REVIEWER)
        self.app.tokens.register("tok-boss", "REV-ADMIN", Role.REVIEWER,
                                 supervisor=True)
        self.app.tokens.register("tok-issuer", "ISSUER-GLOBALIT", Role.ISSUER)
        self.server = make_server(self.app, "127.0.0.1", 0)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def call(self, method: str, path: str, token: str | None = None,
             body: dict | None = None) -> tuple[int, dict]:
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def _publish(self) -> None:
        self.call("POST", "/admin/grade-scales", "tok-admin", {
            "standard_code": "IT-OCC", "standard_version": "v2025",
            "ordered_grades": ["D", "C", "B", "A"],
        })
        self.call("POST", "/admin/mappings/publish", "tok-admin", {
            "standard_code": "IT-OCC", "standard_version": "v2025",
            "rules": [
                {"grade_min": "D", "grade_max": "D", "credits": 4},
                {"grade_min": "C", "grade_max": "C", "credits": 8},
                {"grade_min": "B", "grade_max": "A", "credits": 12},
            ],
        })

    def _apply(self, case_id: str, project: str) -> tuple[int, dict]:
        return self.call("POST", "/applications", "tok-grad", {
            "case_id": case_id, "student_id": "2026001",
            "project_code": project, "certificate_digest": DIGEST,
            "issuer_code": "ISSUER-GLOBALIT",
            "issuer_verification_ref": "VRF-9",
            "standard_code": "IT-OCC", "standard_version": "v2025",
            "grade": "B", "valid_from": "2025-01-01",
            "valid_until": "2028-01-01", "requested_credits": 12,
        })

    def test_auth_required(self) -> None:
        status, body = self.call("GET", "/applications")
        self.assertEqual(status, 401)
        self.assertEqual(body["error"], "unauthorized")
        status, _ = self.call("GET", "/applications", "tok-grad")
        self.assertEqual(status, 200)

    def test_end_to_end_headline(self) -> None:
        self._publish()
        mappings = self.call("GET", "/mappings", "tok-grad")[1]["mappings"]
        self.assertEqual(len(mappings), 3)

        self.assertEqual(self._apply("CASE-A", "PROJ-A")[0], 201)
        self.assertEqual(self._apply("CASE-B", "PROJ-B")[0], 201)

        # 主管向审核员逐案授权（授权即时生效，无需重新登录）
        for cid in ("CASE-A", "CASE-B"):
            status, _ = self.call("POST", "/reviewers/access-grants",
                                  "tok-boss",
                                  {"reviewer_id": "REV-01", "case_id": cid})
            self.assertEqual(status, 201)
        # 普通审核员不能授权
        status, body = self.call("POST", "/reviewers/access-grants", "tok-rev",
                                 {"reviewer_id": "REV-02", "case_id": "CASE-A"})
        self.assertEqual(status, 403)

        # 未授权前，审核员对案件详情不可见；授权后可见敏感字段
        self._apply_other = self.call(
            "POST", "/applications", "tok-grad2",
            {"case_id": "CASE-Z", "student_id": "2026002",
             "project_code": "PROJ-Z", "certificate_digest": "sha256:" + "c" * 64,
             "issuer_code": "ISSUER-GLOBALIT", "issuer_verification_ref": "VRF-Z",
             "standard_code": "IT-OCC", "standard_version": "v2025",
             "grade": "C", "valid_from": "2025-01-01",
             "valid_until": "2028-01-01"},
        )
        case_a = self.call("GET", "/applications/CASE-A", "tok-rev")[1]
        self.assertIn("issuer_verification_ref", case_a)
        status, _ = self.call("GET", "/applications/CASE-Z", "tok-rev")
        self.assertEqual(status, 403)

        # 机构核验（回执必须匹配）
        status, body = self.call("POST", "/applications/CASE-A/verification",
                                 "tok-issuer",
                                 {"passed": True, "evidence_ref": "evd://1",
                                  "issuer_verification_ref": "WRONG"})
        self.assertEqual(status, 403)
        for cid in ("CASE-A", "CASE-B"):
            status, _ = self.call("POST", f"/applications/{cid}/verification",
                                  "tok-issuer",
                                  {"passed": True, "evidence_ref": "evd://ok",
                                   "issuer_verification_ref": "VRF-9"})
            self.assertEqual(status, 200)

        # 项目 A 全额批准
        status, body = self.call("POST", "/applications/CASE-A/decisions",
                                 "tok-rev", {"mode": "full"})
        self.assertEqual(status, 200)
        self.assertEqual(body["granted_credits"], 12)

        # 项目 B 批准时发现权益已被使用，整体回滚
        status, body = self.call("POST", "/applications/CASE-B/decisions",
                                 "tok-rev", {"mode": "full"})
        self.assertEqual(status, 409)
        self.assertEqual(body["error"], "credit_already_consumed")
        self.assertEqual(body["details"]["available_credits"], 0)

        case_b = self.call("GET", "/applications/CASE-B", "tok-rev")[1]
        self.assertEqual(case_b["status"], "under_review")
        self.assertIsNone(case_b["consumption"])

        # 权益仅本人可见；跨项目使用完整列示
        status, _ = self.call("GET", f"/entitlements/{DIGEST}", "tok-rev")
        self.assertEqual(status, 403)
        ent = self.call("GET", f"/entitlements/{DIGEST}", "tok-grad")[1]
        self.assertEqual(ent["available_credits"], 0)
        self.assertEqual(len(ent["usages"]), 1)
        self.assertEqual(ent["usages"][0]["project_code"], "PROJ-A")

        # 历史可完整追溯
        history = self.call("GET", "/applications/CASE-A/history",
                            "tok-grad")[1]
        types = [e["event_type"] for e in history["events"]]
        self.assertEqual(types, [
            "application_submitted",
            "reviewer_access_granted",
            "certificate_verified",
            "approved_full",
        ])

    def test_supplement_and_reject_flow(self) -> None:
        self._publish()
        self._apply("CASE-S", "PROJ-S")
        self.call("POST", "/reviewers/access-grants", "tok-boss",
                  {"reviewer_id": "REV-01", "case_id": "CASE-S"})
        status, _ = self.call("POST",
                              "/applications/CASE-S/supplement-requests",
                              "tok-rev",
                              {"material_type": "transcript", "reason": "缺页"})
        self.assertEqual(status, 201)
        # 毕业生本人补交
        status, body = self.call("POST", "/applications/CASE-S/supplements",
                                 "tok-grad",
                                 {"material_type": "transcript",
                                  "evidence_ref": "evd://new"})
        self.assertEqual(status, 201)
        self.assertEqual(body["status"], "under_review")
        # 未核验直接拒绝允许（reject 不要求核验）
        status, body = self.call("POST", "/applications/CASE-S/decisions",
                                 "tok-rev",
                                 {"mode": "reject", "reason": "不适用"})
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "rejected")

    def test_revocation_flow(self) -> None:
        self._publish()
        self._apply("CASE-R", "PROJ-R")
        self.call("POST", "/reviewers/access-grants", "tok-boss",
                  {"reviewer_id": "REV-01", "case_id": "CASE-R"})
        self.call("POST", "/applications/CASE-R/verification", "tok-issuer",
                  {"passed": True, "evidence_ref": "evd://r",
                   "issuer_verification_ref": "VRF-9"})
        self.call("POST", "/applications/CASE-R/decisions", "tok-rev",
                  {"mode": "full"})
        status, body = self.call("POST", "/applications/CASE-R/revocation",
                                 "tok-rev", {"reason": "材料不实"})
        self.assertEqual(status, 200)
        self.assertEqual(body["refunded_credits"], 12)
        # 重复撤销
        status, body = self.call("POST", "/applications/CASE-R/revocation",
                                 "tok-rev", {"reason": "再撤"})
        self.assertEqual(status, 409)


if __name__ == "__main__":
    unittest.main()
