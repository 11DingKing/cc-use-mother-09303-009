"""HTTP API：仅依赖标准库。

鉴权：``Authorization: Bearer <api-key>``。
敏感证据（证据正文）只能通过专门接口，且服务层再次做本人/授权审核员校验。
"""
from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import sys
from pathlib import Path
from urllib.parse import urlparse

if __package__:
    from . import db as db_module
    from .errors import DomainError, PermissionDenied, ValidationError
    from .service import Service
else:  # 允许直接以脚本方式启动：python src/recognition/app.py
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from recognition import db as db_module  # type: ignore
    from recognition.errors import DomainError, PermissionDenied, ValidationError  # type: ignore
    from recognition.service import Service  # type: ignore


def create_server(db_path: str, host: str = "127.0.0.1", port: int = 8080) -> ThreadingHTTPServer:
    bootstrap = db_module.connect(db_path)
    db_module.init_schema(bootstrap)
    if not db_module.is_seeded(bootstrap):
        db_module.seed(bootstrap)
    bootstrap.close()
    service = Service()

    class Handler(BaseHTTPRequestHandler):
        server_version = "RecognitionAPI/1.0"

        def log_message(self, fmt, *args):  # 静默访问日志，测试输出更干净
            pass

        def _send(self, status: int, payload) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _actor(self, conn):
            auth = self.headers.get("Authorization", "")
            if not auth.startswith("Bearer "):
                raise PermissionDenied("缺少 Bearer 凭证")
            return service.authenticate(conn, auth[len("Bearer "):].strip())

        def _read_json(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0:
                return {}
            raw = self.rfile.read(length)
            try:
                data = json.loads(raw.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                raise ValidationError("请求体不是合法 JSON")
            if not isinstance(data, dict):
                raise ValidationError("请求体必须是 JSON 对象")
            return data

        def do_GET(self):
            self._handle("GET")

        def do_POST(self):
            self._handle("POST")

        def _handle(self, method: str) -> None:
            # 每请求一个连接：事务互不串扰，数据库唯一索引在请求间真正生效。
            conn = db_module.connect(db_path)
            try:
                actor = self._actor(conn)
                path = urlparse(self.path).path.rstrip("/") or "/"
                body = self._read_json() if method == "POST" else {}
                self._route(conn, method, path, actor, body)
            except DomainError as exc:
                self._send(exc.http_status, exc.to_dict())
            except Exception as exc:  # noqa: BLE001
                self._send(500, {"error": "internal_error", "message": str(exc)})
            finally:
                conn.close()

        def _route(self, conn, method, path, actor, body):
            s = service

            if method == "GET" and path == "/me":
                return self._send(200, s.me(conn, actor))

            if method == "POST" and path == "/eligibility/calculate":
                return self._send(200, s.eligibility(conn, actor, body))

            if method == "POST" and path == "/cases":
                return self._send(201, s.create_case(conn, actor, body))

            if method == "GET" and path == "/cases":
                return self._send(200, {"cases": s.list_cases(conn, actor)})

            if method == "GET" and path.startswith("/cases/"):
                return self._send(200, s.get_case(conn, path.split("/")[2], actor))

            case_actions = {
                "/verification": s.record_verification,
                "/supplement/request": s.request_supplement,
                "/supplement/submit": s.submit_supplement,
                "/evaluation/start": s.start_evaluation,
                "/evaluation/decision": s.decide,
                "/reject": s.reject_case,
                "/revoke": s.revoke,
                "/ledger/post": s.post_credits,
                "/evidence": s.upload_evidence,
            }
            if method == "POST" and path.startswith("/cases/"):
                parts = path.split("/")
                # /cases/{id}/<action>[/<sub>]
                if len(parts) >= 4:
                    case_id = parts[2]
                    action = "/" + "/".join(parts[3:])
                    if action in case_actions:
                        result = case_actions[action](conn, case_id, actor, body)
                        return self._send(200, result)

            # 敏感证据正文：独立路径，服务层做本人/授权审核员校验。
            if method == "GET" and path.startswith("/evidence/"):
                evidence_id = path.split("/")[2]
                return self._send(200, s.download_evidence(conn, evidence_id, actor))

            if method == "GET" and path == "/entitlements/mine":
                return self._send(200, {"entitlements": s.my_entitlements(conn, actor)})

            if method == "GET" and path == "/reference/mappings":
                return self._send(200, {"mappings": self._public_mappings(conn)})

            self._send(404, {"error": "not_found", "message": "接口不存在"})

        @staticmethod
        def _public_mappings(conn) -> list[dict]:
            rows = conn.execute(
                "SELECT m.mapping_id, m.version_id, v.authority_id, m.credential_title, "
                "c.program_id, c.course_id, c.course_code, c.course_name, c.credits, "
                "m.min_score, m.credits_granted, m.published_at "
                "FROM mappings m "
                "JOIN standard_versions v ON v.version_id = m.version_id "
                "JOIN target_courses c ON c.course_id = m.course_id "
                "WHERE m.active = 1 ORDER BY m.mapping_id"
            ).fetchall()
            return [dict(r) for r in rows]

    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.db_path = db_path
    return httpd


def main() -> None:
    import argparse
    import os

    parser = argparse.ArgumentParser(description="技能证书互认申请后端")
    parser.add_argument("--db", default=os.environ.get("RECOGNITION_DB", "recognition.sqlite3"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()

    httpd = create_server(args.db, args.host, args.port)
    print(f"服务已启动：http://{args.host}:{args.port}  数据库：{args.db}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
