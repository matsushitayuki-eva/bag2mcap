"""定義パック（メッセージ定義をまとめた JSON）の読み込み。

フォーマットは docs/SPEC.md の「定義パック仕様」を参照。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

SUPPORTED_FORMAT_VERSION = 1
SUPPORTED_ENCODINGS = ("ros2msg", "ros2idl")


class MsgDefsError(Exception):
    """定義パックを読めないときの例外。"""


@dataclass
class MsgDefPack:
    path: Path | None
    source: dict
    ros_distro: str
    types: dict[str, tuple[str, str]] = field(default_factory=dict)  # 型名 → (encoding, 定義全文)

    @property
    def label(self) -> str:
        """画面表示用の短い説明。"""
        if self.path is None:
            return "（定義パックなし）"
        src = self.source
        parts = [src.get("repository", ""), src.get("ref", ""), src.get("generated_at", "")[:10]]
        text = " / ".join(p for p in parts if p)
        return f"{text}（{len(self.types)} 型, {self.ros_distro}）"

    def get(self, typename: str) -> tuple[str, str] | None:
        return self.types.get(normalize_typename(typename))


def normalize_typename(name: str) -> str:
    """'pkg/Type' を 'pkg/msg/Type' に揃える。"""
    parts = name.split("/")
    if len(parts) == 2:
        return f"{parts[0]}/msg/{parts[1]}"
    return name


def empty_pack() -> MsgDefPack:
    return MsgDefPack(path=None, source={}, ros_distro="")


def load_pack(path: str | Path) -> MsgDefPack:
    path = Path(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as e:
        raise MsgDefsError(f"定義パックが見つかりません: {path}") from e
    except (OSError, ValueError) as e:
        raise MsgDefsError(f"定義パックを読み込めません: {path}: {e}") from e

    if not isinstance(data, dict) or "types" not in data:
        raise MsgDefsError(f"定義パックの形式が正しくありません: {path}")
    ver = data.get("format_version")
    if ver != SUPPORTED_FORMAT_VERSION:
        raise MsgDefsError(
            f"未対応の定義パック形式です（format_version={ver}）。新しい bag2mcap を使用してください。"
        )

    types: dict[str, tuple[str, str]] = {}
    for name, entry in data["types"].items():
        enc = entry.get("encoding")
        definition = entry.get("definition")
        if enc not in SUPPORTED_ENCODINGS or not isinstance(definition, str):
            raise MsgDefsError(f"定義パック内の型 {name} の形式が正しくありません。")
        types[normalize_typename(name)] = (enc, definition)

    return MsgDefPack(
        path=path,
        source=data.get("source") or {},
        ros_distro=data.get("ros_distro", ""),
        types=types,
    )
