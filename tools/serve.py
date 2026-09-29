"""启动后端 HTTP 服务，可选写入演示种子数据。

用法：
    python3 tools/serve.py --db data/recognition.db --seed --port 8080
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from recognition.app import ApiApp, make_server
from recognition.models import Principal, Role
from recognition.service import build_service


# 演示用承载令牌（生产环境应由身份提供方签发）
SEED_TOKENS = {
    "tok-graduate": ("2026001", Role.GRADUATE, "毕业生"),
    "tok-graduate-2": ("2026002", Role.GRADUATE, "毕业生乙"),
    "tok-reviewer": ("REV-01", Role.REVIEWER, "审核员甲"),
    "tok-supervisor": ("REV-ADMIN", Role.REVIEWER, "教务主管"),
    "tok-issuer": ("ISSUER-GLOBALIT", Role.ISSUER, "国际技能认证机构"),
    "tok-registry": ("REG-ADMIN", Role.REGISTRY_ADMIN, "映射发布管理员"),
}


def seed(app: ApiApp) -> None:
    """写入一份已发布映射：IT 职业技能标准 v2025，等级 D~A。"""
    admin = Principal(user_id="REG-ADMIN", role=Role.REGISTRY_ADMIN)
    svc = app.service
    with svc.store.transaction() as conn:
        from recognition.registry import MappingRegistry
        reg = MappingRegistry(conn)
        reg.register_grade_scale("IT-OCC", "v2025", ["D", "C", "B", "A"])
        reg.publish(
            "IT-OCC", "v2025",
            [
                {"grade_min": "D", "grade_max": "D", "credits": 4},
                {"grade_min": "C", "grade_max": "C", "credits": 8},
                {"grade_min": "B", "grade_max": "A", "credits": 12},
            ],
            published_by="REG-ADMIN",
        )

    # 审核员甲获得演示案件的授权在申请发生后由主管授予；此处登记令牌
    for token, (uid, role, name) in SEED_TOKENS.items():
        app.tokens.register(token, uid, role, name,
                            supervisor=(uid == "REV-ADMIN"))
    print(json.dumps({"seeded": True, "tokens": list(SEED_TOKENS)},
                     ensure_ascii=False))


def main() -> None:
    parser = argparse.ArgumentParser(description="技能证书互认申请后端")
    parser.add_argument("--db", default=":memory:", help="SQLite 路径")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--seed", action="store_true", help="写入演示映射与令牌")
    args = parser.parse_args()

    if args.db != ":memory:":
        Path(args.db).parent.mkdir(parents=True, exist_ok=True)
    service = build_service(args.db, clock=date.today)
    app = ApiApp(service)
    if args.seed:
        seed(app)
    server = make_server(app, args.host, args.port)
    print(f"服务监听 http://{args.host}:{args.port}（Ctrl+C 退出）", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
