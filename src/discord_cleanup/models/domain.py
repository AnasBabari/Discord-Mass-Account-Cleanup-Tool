from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any


class RelationshipType(IntEnum):
    NONE = 0
    FRIEND = 1
    BLOCKED = 2
    INCOMING_REQUEST = 3
    OUTGOING_REQUEST = 4

    @classmethod
    def from_value(cls, val: int | None) -> RelationshipType:
        try:
            return cls(val or 0)
        except ValueError:
            return cls.NONE


@dataclass(frozen=True)
class User:
    id: str
    username: str
    discriminator: str = "0"
    global_name: str | None = None
    avatar: str | None = None
    bot: bool = False

    @property
    def display_name(self) -> str:
        return self.global_name or self.username or f"User {self.id}"

    @property
    def tag(self) -> str:
        if self.discriminator and self.discriminator != "0":
            return f"{self.username}#{self.discriminator}"
        return f"@{self.username}"

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> User:
        return cls(
            id=str(data.get("id", "")),
            username=str(data.get("username", "Unknown")),
            discriminator=str(data.get("discriminator", "0")),
            global_name=data.get("global_name"),
            avatar=data.get("avatar"),
            bot=bool(data.get("bot", False)),
        )


@dataclass(frozen=True)
class Guild:
    id: str
    name: str
    owner: bool = False
    permissions: int = 0
    icon: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Guild:
        name = data.get("properties", {}).get("name") if isinstance(data.get("properties"), dict) else None
        if not name:
            name = data.get("name", "Unknown Server")
        return cls(
            id=str(data.get("id", "")),
            name=str(name),
            owner=bool(data.get("owner", False)),
            permissions=int(data.get("permissions", 0) or 0),
            icon=data.get("icon"),
        )


@dataclass(frozen=True)
class Relationship:
    id: str
    user: User
    rel_type: RelationshipType = RelationshipType.NONE
    nickname: str | None = None
    since: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Relationship:
        user_data = data.get("user") or {}
        if not isinstance(user_data, dict):
            user_data = {"id": data.get("id", ""), "username": "Unknown"}
        return cls(
            id=str(data.get("id", user_data.get("id", ""))),
            user=User.from_dict(user_data),
            rel_type=RelationshipType.from_value(data.get("type")),
            nickname=data.get("nickname"),
            since=data.get("since"),
        )


@dataclass(frozen=True)
class ReadStateEntry:
    channel_id: str
    message_id: str
    read_state_type: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "channel_id": self.channel_id,
            "message_id": self.message_id,
            "read_state_type": self.read_state_type,
        }


@dataclass(frozen=True)
class OperationPreview:
    action_name: str
    target_count: int
    target_descriptions: list[str] = field(default_factory=list)
    is_dry_run: bool = False


@dataclass
class OperationResult:
    success_count: int = 0
    failure_count: int = 0
    errors: list[str] = field(default_factory=list)
    cancelled: bool = False
    details: str = ""

    @property
    def total_processed(self) -> int:
        return self.success_count + self.failure_count


@dataclass(frozen=True)
class ProgressUpdate:
    current: int
    total: int
    message: str
