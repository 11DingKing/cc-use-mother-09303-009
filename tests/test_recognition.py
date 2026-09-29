"""后端业务规则测试：服务层（含受控时钟）与 HTTP 端到端。"""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from recognition import db as db_module
from recognition.app import create_server
from recognition.enums import CaseStatus, EntitlementStatus
from recognition.errors import DuplicateEntitlement
from recognition.service import Service

VERSION = "VER-IDSB-2024"
TITLE_SWE = "国际软件工程师认证"
KEY_LI = "sk-applicant-li"
KEY_WANG = "sk-applicant-wang"
KEY_CHEN = "sk-reviewer-chen"
KEY_ZHAO = "sk-registrar-zhao"


def cert_body(program: str, *, score=88.0, title=TITLE_SWE, serial="SWE-2026-0001",
              issued="2025-06-01", valid_from="2025-06-01", valid_until="2028-06-01",
              verification="VERIFIED", holder_name="李娜"):
    return {
        "program_id": program,
        "version_id": VERSION,
        "certificate": {
            "holder_name": holder_name,
            "serial_number": serial,
            "title": title,
            "issued_at": issued,
            "valid_from": valid_from,
            "valid_until": valid_until,
        },
        "verification": {"status": verification, "reference": "AUTH-RCP-001",
                         "method": "API", "verified_at": "2026-09-20"},
        "score": {"achieved": score, "scale_min": 0.0, "scale_max": 100.0},
    }


def make_service_db(*, frozen: datetime | None = None):
    tmp = tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False)
    tmp.close()
    conn = db_module.connect(tmp.name)
    db_module.init_schema(conn)
    db_module.seed(conn)
    clock = (lambda: frozen) if frozen else None
    return conn, Service(clock=clock), Path(tmp.name)


class ApiClient:
    def __init__(self, base_url: str, key: str | None):
        self.base = base_url
        self.key = key

    def call(self, method: str, path: str, body=None):
        url = self.base + path
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        if self.key:
            req.add_header("Authorization", f"Bearer {self.key}")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode())


class ServiceLevelTest(unittest.TestCase):
    def setUp(self):
        self.conn, self.svc, _ = make_service_db(
            frozen=datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)
        )
        self.li = self.svc.authenticate(self.conn, KEY_LI)
        self.chen = self.svc.authenticate(self.conn, KEY_CHEN)
        self.zhao = self.svc.authenticate(self.conn, KEY_ZHAO)

    def tearDown(self):
        self.conn.close()

    def test_full_approval_atomically_grants_and_posts(self):
        case = self.svc.create_case(self.conn, self.li, cert_body("PRG-SE"))
        self.assertEqual(case["status"], CaseStatus.VERIFIED)
        self.assertEqual(len(case["items"]), 2)  # DATA501/DATA502 达标

        self.svc.start_evaluation(self.conn, case["case_id"], self.chen)
        decision = {"items": [
            {"course_id": "C-DS", "verdict": "APPROVED"},
            {"course_id": "C-DB", "verdict": "APPROVED"},
        ]}
        decided = self.svc.decide(self.conn, case["case_id"], self.chen, decision)
        self.assertEqual(decided["status"], CaseStatus.APPROVED)
        self.assertEqual({e["status"] for e in decided["entitlements"]},
                         {EntitlementStatus.GRANTED})

        posted = self.svc.post_credits(self.conn, case["case_id"], self.zhao,
                                       {"ledger_reference": "LED-2026-09-001"})
        self.assertEqual(posted["status"], CaseStatus.POSTED)
        self.assertTrue(all(e["status"] == EntitlementStatus.CONSUMED
                            for e in posted["entitlements"]))

        # 重复记账必须被阻止。
        from recognition.errors import StateConflict
        with self.assertRaises(StateConflict):
            self.svc.post_credits(self.conn, case["case_id"], self.zhao, {})

        # 历史完整可追溯。
        types = [e["event_type"] for e in
                 self.svc.get_case(self.conn, case["case_id"], self.li)["events"]]
        for expected in ["CASE_CREATED", "EVALUATION_STARTED", "EVALUATION_DECIDED",
                         "ENTITLEMENT_GRANTED", "CREDITS_POSTED"]:
            self.assertIn(expected, types)
        self.assertEqual(len(types), len(set(
            (e["seq"]) for e in self.svc.get_case(self.conn, case["case_id"], self.li)["events"]
        )))

    def test_cross_project_second_application_blocked_after_use(self):
        first = self.svc.create_case(self.conn, self.li, cert_body("PRG-SE"))
        self.svc.start_evaluation(self.conn, first["case_id"], self.chen)
        self.svc.decide(self.conn, first["case_id"], self.chen, {"items": [
            {"course_id": "C-DS", "verdict": "APPROVED"},
            {"course_id": "C-DB", "verdict": "APPROVED"},
        ]})

        # 同一证书在第二个项目申请：创建即被拒（权益已被使用）。
        with self.assertRaises(DuplicateEntitlement):
            self.svc.create_case(self.conn, self.li, cert_body("PRG-DS", score=90))

    def test_concurrent_approvals_only_one_wins(self):
        """两个项目各有一个未决案件，并发批准时数据库唯一索引兜底。"""
        tmp = tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False)
        tmp.close()
        shared = db_module.connect(tmp.name)
        db_module.init_schema(shared)
        db_module.seed(shared)

        c1 = self.svc.create_case(shared, self.li, cert_body("PRG-SE"))
        c2 = self.svc.create_case(shared, self.li,
                                  cert_body("PRG-DS", score=90))
        self.svc.start_evaluation(shared, c1["case_id"], self.chen)
        self.svc.start_evaluation(shared, c2["case_id"], self.chen)

        conns = [db_module.connect(tmp.name), db_module.connect(tmp.name)]
        results = []
        barrier = threading.Barrier(2)

        def approve(idx, case_id, courses):
            try:
                barrier.wait()
                self.svc.decide(conns[idx], case_id, self.chen,
                                {"items": [{"course_id": c, "verdict": "APPROVED"}
                                           for c in courses]})
                results.append("OK")
            except DuplicateEntitlement:
                results.append("DUP")
            except Exception as exc:  # noqa: BLE001
                results.append(f"ERR:{exc}")

        t1 = threading.Thread(target=approve, args=(0, c1["case_id"], ["C-DS", "C-DB"]))
        t2 = threading.Thread(target=approve, args=(1, c2["case_id"], ["C-ML"]))
        t1.start(); t2.start(); t1.join(); t2.join()
        self.assertEqual(sorted(results), ["DUP", "OK"])
        for c in conns:
            c.close()
        shared.close()

    def test_partial_approval(self):
        case = self.svc.create_case(self.conn, self.li, cert_body("PRG-SE", score=88))
        self.svc.start_evaluation(self.conn, case["case_id"], self.chen)
        out = self.svc.decide(self.conn, case["case_id"], self.chen, {"items": [
            {"course_id": "C-DS", "verdict": "APPROVED", "decided_credits": 2.0},
            {"course_id": "C-DB", "verdict": "REJECTED"},
        ], "comment": "数据库课程实践部分不足"})
        self.assertEqual(out["status"], CaseStatus.PARTIALLY_APPROVED)
        granted = [e for e in out["entitlements"]]
        self.assertEqual(len(granted), 1)
        self.assertEqual(granted[0]["credits"], 2.0)

    def test_reject_all_requires_reason_and_grants_nothing(self):
        case = self.svc.create_case(self.conn, self.li, cert_body("PRG-SE"))
        self.svc.start_evaluation(self.conn, case["case_id"], self.chen)
        from recognition.errors import ValidationError
        with self.assertRaises(ValidationError):
            self.svc.decide(self.conn, case["case_id"], self.chen, {"items": [
                {"course_id": "C-DS", "verdict": "REJECTED"},
                {"course_id": "C-DB", "verdict": "REJECTED"},
            ]})
        out = self.svc.decide(self.conn, case["case_id"], self.chen, {"items": [
            {"course_id": "C-DS", "verdict": "REJECTED"},
            {"course_id": "C-DB", "verdict": "REJECTED"},
        ], "comment": "证书与培养目标不符"})
        self.assertEqual(out["status"], CaseStatus.REJECTED)
        self.assertEqual(out["entitlements"], [])
        # 驳回后证书占用释放，可重新申请。
        again = self.svc.create_case(self.conn, self.li, cert_body("PRG-SE"))
        self.assertTrue(again["case_id"])

    def test_supplement_window_enforced(self):
        case = self.svc.create_case(self.conn, self.li, cert_body("PRG-SE"))
        self.svc.request_supplement(self.conn, case["case_id"], self.chen,
                                    {"reason": "缺成绩单公证件", "due_days": 5})
        self.assertEqual(self.svc.get_case(self.conn, case["case_id"], self.li)["status"],
                         CaseStatus.SUPPLEMENTING)

        # 截止日内可补交。
        out = self.svc.submit_supplement(self.conn, case["case_id"], self.li,
                                         {"note": "已补交公证件"})
        self.assertEqual(out["status"], CaseStatus.VERIFIED)

        # 超期补交：用一个截止日已过的新案件验证。
        self.svc.request_supplement(self.conn, case["case_id"], self.chen,
                                    {"reason": "还需原件", "due_days": 1})
        from recognition.errors import SupplementDeadlinePassed
        old_clock = self.svc.clock
        self.svc.clock = lambda: datetime(2026, 10, 2, tzinfo=timezone.utc)
        try:
            with self.assertRaises(SupplementDeadlinePassed):
                self.svc.submit_supplement(self.conn, case["case_id"], self.li,
                                           {"note": "晚到的材料"})
        finally:
            self.svc.clock = old_clock

    def test_verification_pending_blocks_evaluation(self):
        case = self.svc.create_case(self.conn, self.li,
                                    cert_body("PRG-SE", verification="PENDING"))
        self.assertEqual(case["status"], CaseStatus.SUBMITTED)
        from recognition.errors import VerificationInvalid
        with self.assertRaises(VerificationInvalid):
            self.svc.start_evaluation(self.conn, case["case_id"], self.chen)

        # 机构确认有效后放行。
        self.svc.record_verification(self.conn, case["case_id"], self.chen,
                                     {"status": "VERIFIED", "reference": "RCP-9"})
        started = self.svc.start_evaluation(self.conn, case["case_id"], self.chen)
        self.assertEqual(started["status"], CaseStatus.UNDER_EVALUATION)

    def test_expired_certificate_rejected_at_submission_and_approval(self):
        from recognition.errors import CredentialExpired
        with self.assertRaises(CredentialExpired):
            self.svc.create_case(self.conn, self.li,
                                 cert_body("PRG-SE", valid_until="2026-09-01"))

        # 批准时过期：证书在申请时有效，审批时钟越过有效期。
        case = self.svc.create_case(self.conn, self.li,
                                    cert_body("PRG-SE", valid_until="2026-09-30"))
        self.svc.start_evaluation(self.conn, case["case_id"], self.chen)
        self.svc.clock = lambda: datetime(2026, 10, 1, tzinfo=timezone.utc)
        with self.assertRaises(CredentialExpired):
            self.svc.decide(self.conn, case["case_id"], self.chen, {"items": [
                {"course_id": "C-DS", "verdict": "APPROVED"},
                {"course_id": "C-DB", "verdict": "APPROVED"},
            ]})

    def test_mapping_change_blocks_approval(self):
        case = self.svc.create_case(self.conn, self.li, cert_body("PRG-SE"))
        self.svc.start_evaluation(self.conn, case["case_id"], self.chen)
        self.conn.execute("UPDATE mappings SET active = 0 WHERE mapping_id = 'MAP-SWE-DS'")
        self.conn.commit()
        from recognition.errors import MappingChanged
        with self.assertRaises(MappingChanged):
            self.svc.decide(self.conn, case["case_id"], self.chen, {"items": [
                {"course_id": "C-DS", "verdict": "APPROVED"},
                {"course_id": "C-DB", "verdict": "APPROVED"},
            ]})

    def test_revoke_releases_credential_for_reapplication(self):
        case = self.svc.create_case(self.conn, self.li, cert_body("PRG-SE"))
        self.svc.start_evaluation(self.conn, case["case_id"], self.chen)
        self.svc.decide(self.conn, case["case_id"], self.chen, {"items": [
            {"course_id": "C-DS", "verdict": "APPROVED"},
            {"course_id": "C-DB", "verdict": "APPROVED"},
        ]})
        self.svc.post_credits(self.conn, case["case_id"], self.zhao, {})
        revoked = self.svc.revoke(self.conn, case["case_id"], self.chen,
                                  {"reason": "发现申请材料不实"})
        self.assertEqual(revoked["status"], CaseStatus.REVOKED)
        self.assertTrue(all(e["status"] == EntitlementStatus.REVOKED
                            for e in revoked["entitlements"]))
        # 撤销留墓碑、释放占用，允许重新申请。
        new_case = self.svc.create_case(self.conn, self.li, cert_body("PRG-SE"))
        self.assertNotEqual(new_case["case_id"], case["case_id"])

    def test_evidence_visible_only_to_owner_and_assigned_reviewer(self):
        wang = self.svc.authenticate(self.conn, KEY_WANG)
        case = self.svc.create_case(self.conn, self.li, cert_body("PRG-SE"))
        ev = self.svc.upload_evidence(self.conn, case["case_id"], self.li, {
            "kind": "transcript", "label": "成绩单扫描件", "content": "BASE64-SECRET",
        })

        # 本人可取正文。
        self.assertEqual(
            self.svc.download_evidence(self.conn, ev["evidence_id"], self.li)["content"],
            "BASE64-SECRET",
        )
        # 其他毕业生：404（不存在与无权不做区分）。
        from recognition.errors import NotFound
        with self.assertRaises(NotFound):
            self.svc.download_evidence(self.conn, ev["evidence_id"], wang)
        # 未被分配的审核员不能看；分配（发起补交）后可以。
        with self.assertRaises(NotFound):
            self.svc.download_evidence(self.conn, ev["evidence_id"], self.chen)
        self.svc.request_supplement(self.conn, case["case_id"], self.chen,
                                    {"reason": "补充原件", "due_days": 7})
        self.assertEqual(
            self.svc.download_evidence(self.conn, ev["evidence_id"], self.chen)["content"],
            "BASE64-SECRET",
        )
        # 记账岗始终不能取证据正文。
        with self.assertRaises(NotFound):
            self.svc.download_evidence(self.conn, ev["evidence_id"], self.zhao)
        # 案件列表/详情中的证据元数据不含正文。
        detail = self.svc.get_case(self.conn, case["case_id"], self.chen)
        self.assertNotIn("content", detail["evidence"][0])

    def test_eligibility_trial_calc(self):
        result = self.svc.eligibility(self.conn, self.li, {
            "program_id": "PRG-DS", "version_id": VERSION,
            "credential_title": TITLE_SWE, "score_achieved": 75.0,
            "serial_number": "SWE-2026-0001", "issued_at": "2025-06-01",
        })
        codes = {i["course_code"] for i in result["eligible"]}
        self.assertEqual(codes, set())  # 机器学习下限 80，成绩 75 不达标
        self.assertEqual(len(result["ineligible_below_min_score"]), 1)
        self.assertFalse(result["approvable"])


class HttpEndToEndTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.TemporaryDirectory()
        db_path = str(Path(cls.tmpdir.name) / "api.sqlite3")
        cls.httpd = create_server(db_path, "127.0.0.1", 0)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.tmpdir.cleanup()

    def client(self, key):
        return ApiClient(self.base, key)

    def test_01_auth_required(self):
        status, body = self.client(None).call("GET", "/me")
        self.assertEqual(status, 403)
        self.assertEqual(body["error"], "permission_denied")

    def test_02_cross_project_scenario_full_flow(self):
        li, chen, zhao = self.client(KEY_LI), self.client(KEY_CHEN), self.client(KEY_ZHAO)

        status, me = li.call("GET", "/me")
        self.assertEqual(status, 200)
        self.assertEqual(me["person_id"], "P-LI")

        # 项目一：申请 -> 评估 -> 批准 -> 记账。
        status, case1 = li.call("POST", "/cases", cert_body("PRG-SE"))
        self.assertEqual(status, 201)
        cid1 = case1["case_id"]
        self.assertEqual(case1["verification"]["status"], "VERIFIED")

        status, _ = chen.call("POST", f"/cases/{cid1}/evaluation/start")
        self.assertEqual(status, 200)
        status, decided = chen.call("POST", f"/cases/{cid1}/evaluation/decision", {
            "items": [
                {"course_id": "C-DS", "verdict": "APPROVED"},
                {"course_id": "C-DB", "verdict": "APPROVED"},
            ],
        })
        self.assertEqual(status, 200)
        self.assertEqual(decided["status"], "APPROVED")

        status, posted = zhao.call("POST", f"/cases/{cid1}/ledger/post",
                                   {"ledger_reference": "LED-1"})
        self.assertEqual(status, 200)
        self.assertEqual(posted["status"], "POSTED")

        # 同一张证书在项目二申请：409 防重复消费。
        status, err = li.call("POST", "/cases", cert_body("PRG-DS", score=92))
        self.assertEqual(status, 409)
        self.assertEqual(err["error"], "duplicate_entitlement")
        self.assertEqual(err["details"]["case_id"], cid1)

        # 权益列表反映已消费。
        status, mine = li.call("GET", "/entitlements/mine")
        self.assertEqual(status, 200)
        self.assertTrue(all(e["status"] == "CONSUMED" for e in mine["entitlements"]))

    def test_03_privacy_and_trace_over_http(self):
        li, wang, chen = self.client(KEY_LI), self.client(KEY_WANG), self.client(KEY_CHEN)
        _, case1 = li.call("GET", "/cases")
        cid1 = case1["cases"][0]["case_id"]

        # 他人不可见案件（404）。
        status, _ = wang.call("GET", f"/cases/{cid1}")
        self.assertEqual(status, 404)

        # 上传敏感证据并核对访问控制。
        status, ev = li.call("POST", f"/cases/{cid1}/evidence", {
            "kind": "certificate_copy", "label": "证书复印件", "content": "SECRET",
        })
        self.assertEqual(status, 409)  # 案件已 POSTED，结案不可上传

        # 用一个新案件做证据授权验证。
        _, c2 = li.call("POST", "/cases",
                        cert_body("PRG-DS", score=92, serial="SWE-2026-0002"))
        self.assertEqual(c2["status"], "VERIFIED")
        _, ev2 = li.call("POST", f"/cases/{c2['case_id']}/evidence", {
            "kind": "certificate_copy", "label": "证书复印件", "content": "SECRET-2",
        })
        status, body = wang.call("GET", f"/evidence/{ev2['evidence_id']}")
        self.assertEqual(status, 404)
        status, body = li.call("GET", f"/evidence/{ev2['evidence_id']}")
        self.assertEqual(status, 200)
        self.assertEqual(body["content"], "SECRET-2")

        # 审核员未被分配前不可看证据。
        status, _ = chen.call("GET", f"/evidence/{ev2['evidence_id']}")
        self.assertEqual(status, 404)

        # 追溯链完整。
        status, detail = li.call("GET", f"/cases/{c2['case_id']}")
        self.assertEqual(status, 200)
        seq = [e["seq"] for e in detail["events"]]
        self.assertEqual(seq, sorted(seq))
        self.assertIn("CASE_CREATED", [e["event_type"] for e in detail["events"]])

    def test_04_reference_data(self):
        status, body = self.client(KEY_LI).call("GET", "/reference/mappings")
        self.assertEqual(status, 200)
        self.assertGreaterEqual(len(body["mappings"]), 5)


if __name__ == "__main__":
    unittest.main()
