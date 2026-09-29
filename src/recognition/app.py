"""HTTP API：标准库实现，零外部依赖。

认证：``Authorization: Bearer <token>``，令牌由 :class:`ApiApp` 注册，
映射到 :class:`~recognition.models.Principal`。审核员的逐案授权在每次请求时
从数据库实时解析，因此授权变更即时生效。

接口一览（详见 README）::

    POST   /admin/grade-scales            映射管理员：登记成绩等级表
    POST   /admin/mappings/publish        映射管理员：发布映射（冻结）
    GET    /mappings                      已发布映射
    POST   /applications                  毕业生：提交申请
    GET    /applications                  本人/授权案件列表
    GET    /applications/{id}             案件详情（敏感证据按权限裁剪）
    POST   /applications/{id}/supplements 毕业生：补交材料
    POST   /applications/{id}/supplement-requests 审核员：要求补交
    POST   /applications/{id}/verification 认证机构：核验
    POST   /applications/{id}/decisions   审核员：full/partial/reject
    POST   /applications/{id}/revocation  审核员：撤销（窗口内冲账）
    GET    /applications/{id}/history     完整处理追溯
    GET    /entitlements/{digest}         本人：权益余额与跨项目使用
    POST   /reviewers/access-grants       教务主管：逐案授权
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from .errors import DomainError
from .models import Principal, Role
from .service import RecognitionService


class TokenDirectory:
    """承载令牌 -> 身份。审核员授权范围以数据库为准，此处只给基础身份。"""

    def __init__(self) -> None:
        self._tokens: dict[str, tuple[str, Role, str, bool]] = {}
        self._lock = threading.Lock()

    def register(self, token: str, user_id: str, role: Role, name: str = "",
                 *, supervisor: bool = False) -> None:
        with self._lock:
            self._tokens[token] = (user_id, role, name, supervisor)

    def resolve(self, store, token: str) -> Principal | None:
        with self._lock:
            found = self._tokens.get(token)
        if found is None:
            return None
        user_id, role, name, supervisor = found
        authorized = None
        if role is Role.REVIEWER:
            if supervisor:
                authorized = None  # 教务主管：不限案件
            else:
                authorized = store.reviewer_authorized_case_ids(user_id)
        return Principal(user_id=user_id, role=role, name=name,
                         authorized_case_ids=authorized)


class ApiApp:
    def __init__(self, service: RecognitionService) -> None:
        self.service = service
        self.tokens = TokenDirectory()

    def resolve_principal(self, token: str) -> Principal | None:
        return self.tokens.resolve(self.service.store, token)

    # --------------------------------------------------------------- routing

    def handle(self, method: str, path: str, headers: dict,
               body: dict | None, principal: Principal) -> tuple[int, dict]:
        """纯函数式分发，便于直接单元测试，返回 (status, json_body)。"""
        s = self.service
        p = principal

        if method == "POST" and path == "/admin/grade-scales":
            return 201, s.register_grade_scale(
                p, body["standard_code"], body["standard_version"],
                list(body["ordered_grades"]),
            )
        if method == "POST" and path == "/admin/mappings/publish":
            return 201, s.publish_mapping(
                p, body["standard_code"], body["standard_version"],
                list(body["rules"]),
            )
        if method == "GET" and path == "/mappings":
            return 200, {"mappings": s.list_published_mappings(p)}

        if method == "POST" and path == "/applications":
            return 201, s.submit_application(p, body or {})
        if method == "GET" and path == "/applications":
            return 200, {"cases": s.list_cases(p)}

        # /applications/{id}/...
        prefix = "/applications/"
        if path.startswith(prefix):
            rest = path[len(prefix):]
            cid, _, sub = rest.partition("/")
            if not cid:
                return 404, {"error": "not_found"}
            if method == "GET" and not sub:
                return 200, s.get_case(p, cid)
            if method == "GET" and sub == "history":
                return 200, s.history(p, cid)
            if method == "POST" and sub == "supplements":
                return 201, s.submit_supplement(
                    p, cid, body["material_type"], body["evidence_ref"],
                    str(body.get("note", "")),
                )
            if method == "POST" and sub == "supplement-requests":
                return 201, s.request_supplement(
                    p, cid, body["material_type"], str(body.get("reason", "")),
                )
            if method == "POST" and sub == "verification":
                return 200, s.verify_certificate(
                    p, cid, bool(body["passed"]),
                    str(body.get("evidence_ref", "")),
                    body["issuer_verification_ref"],
                )
            if method == "POST" and sub == "decisions":
                return 200, s.decide(
                    p, cid, body["mode"],
                    credits=body.get("credits"),
                    reason=str(body.get("reason", "")),
                )
            if method == "POST" and sub == "revocation":
                return 200, s.revoke(p, cid, str(body.get("reason", "")))

        if method == "GET" and path.startswith("/entitlements/"):
            digest = path.rsplit("/", 1)[-1]
            return 200, s.get_entitlement(p, digest)
        if method == "POST" and path == "/reviewers/access-grants":
            return 201, s.grant_reviewer_access(
                p, body["reviewer_id"], body["case_id"],
            )

        return 404, {"error": "not_found", "path": path}


class _Handler(BaseHTTPRequestHandler):
    app: ApiApp = None  # type: ignore[assignment]

    def log_message(self, fmt: str, *args) -> None:  # 安静日志
        return

    def _send(self, status: int, payload: dict) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _handle(self, method: str) -> None:
        parsed = urlparse(self.path)
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            self._send(401, {"error": "unauthorized",
                             "message": "缺少 Bearer 令牌"})
            return
        principal = self.app.resolve_principal(auth[len("Bearer "):].strip())
        if principal is None:
            self._send(401, {"error": "unauthorized",
                             "message": "令牌无效"})
            return
        body = None
        if method == "POST":
            raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            if raw:
                try:
                    body = json.loads(raw.decode("utf-8"))
                except json.JSONDecodeError:
                    self._send(400, {"error": "validation_error",
                                     "message": "请求体不是合法 JSON"})
                    return
        try:
            status, payload = self.app.handle(
                method, parsed.path, dict(self.headers), body, principal
            )
        except DomainError as exc:
            self._send(exc.http_status,
                       {"error": exc.code, "message": exc.message,
                        **({"details": exc.details} if exc.details else {})})
        except KeyError as exc:
            self._send(400, {"error": "validation_error",
                             "message": f"缺少必填字段：{exc.args[0]}"})
        else:
            self._send(status, payload)

    def do_GET(self) -> None:
        self._handle("GET")

    def do_POST(self) -> None:
        self._handle("POST")


def make_server(app: ApiApp, host: str = "127.0.0.1", port: int = 8080) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (_Handler,), {"app": app})
    return ThreadingHTTPServer((host, port), handler)
