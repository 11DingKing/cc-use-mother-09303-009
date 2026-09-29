"""领域输入模型。"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Actor:
    """调用身份，由 API 密钥解析或测试直接构造。"""

    actor_id: str
    role: str

    @classmethod
    def of(cls, actor_id: str, role: str) -> "Actor":
        return cls(actor_id=actor_id, role=str(role))
