"""rosbag2 (sqlite3 / .db3) reader.

ROS 2 をインストールせずに rosbag2 を読むための最小実装。
- フォルダ（metadata.yaml あり / なし）または .db3 単体を入力にできる
- 分割 bag（*_0.db3, *_1.db3, ...）を順番に読む
- 圧縮（ファイル単位 .db3.zstd / メッセージ単位 zstd）に対応
- メッセージは CDR バイト列のままデコードせずに返す
"""

from __future__ import annotations

import re
import sqlite3
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator

import yaml
import zstandard

ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"


class BagError(Exception):
    """入力 bag を読めないときの例外（利用者に見せるメッセージ）。"""


@dataclass
class Topic:
    name: str
    type: str
    serialization_format: str
    offered_qos_profiles: str
    type_description_hash: str = ""


@dataclass
class BagFile:
    path: Path
    compressed: bool  # ファイル単位 zstd 圧縮（.db3.zstd）


@dataclass
class BagInfo:
    """入力 bag の構成情報。"""

    root: Path
    files: list[BagFile]
    message_compression: bool  # メッセージ単位 zstd 圧縮
    metadata_found: bool
    metadata_text: str = ""
    warnings: list[str] = field(default_factory=list)


def _natural_key(p: Path) -> list:
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", p.name)]


def open_bag(path: str | Path) -> BagInfo:
    """入力パスを解釈して BagInfo を返す。"""
    path = Path(path)
    if not path.exists():
        raise BagError(f"入力が見つかりません: {path}")

    if path.is_file():
        if path.name == "metadata.yaml":
            return open_bag(path.parent)
        if not (path.suffix == ".db3" or path.name.endswith(".db3.zstd")):
            raise BagError(f".db3 ファイルまたは bag フォルダを指定してください: {path}")
        return BagInfo(
            root=path.parent,
            files=[BagFile(path, path.name.endswith(".zstd"))],
            message_compression=False,  # メッセージ単位圧縮は内容から自動判定する
            metadata_found=False,
            warnings=["db3 ファイル単体を変換します（同じフォルダの他の分割ファイルは含みません）。"],
        )

    meta_path = path / "metadata.yaml"
    if meta_path.is_file():
        text = meta_path.read_text(encoding="utf-8")
        try:
            meta = yaml.safe_load(text)["rosbag2_bagfile_information"]
        except Exception as e:  # noqa: BLE001
            raise BagError(f"metadata.yaml を解釈できません: {e}") from e
        storage = meta.get("storage_identifier", "sqlite3")
        if storage != "sqlite3":
            raise BagError(f"sqlite3 形式ではない bag です（storage_identifier={storage}）。")
        mode = (meta.get("compression_mode") or "").upper()
        fmt = (meta.get("compression_format") or "").lower()
        if mode and fmt and fmt != "zstd":
            raise BagError(f"未対応の圧縮形式です: {fmt}")
        files: list[BagFile] = []
        warnings: list[str] = []
        for rel in meta.get("relative_file_paths") or []:
            p = path / Path(rel).name
            if not p.is_file():
                warnings.append(f"metadata.yaml に記載されたファイルがありません: {p.name}")
                continue
            files.append(BagFile(p, p.name.endswith(".zstd")))
        if not files:
            files = _glob_files(path)
        if not files:
            raise BagError(f"db3 ファイルが見つかりません: {path}")
        return BagInfo(
            root=path,
            files=files,
            message_compression=(mode == "MESSAGE"),
            metadata_found=True,
            metadata_text=text,
            warnings=warnings,
        )

    files = _glob_files(path)
    if not files:
        raise BagError(f"フォルダ内に db3 ファイルがありません: {path}")
    return BagInfo(
        root=path,
        files=files,
        message_compression=False,
        metadata_found=False,
        warnings=["metadata.yaml がありません（記録が途中で終了した可能性）。フォルダ内の db3 を名前順に変換します。"],
    )


def _glob_files(folder: Path) -> list[BagFile]:
    files = [p for p in folder.iterdir() if p.is_file() and (p.suffix == ".db3" or p.name.endswith(".db3.zstd"))]
    files.sort(key=_natural_key)
    return [BagFile(p, p.name.endswith(".zstd")) for p in files]


class Db3File:
    """1 つの db3 ファイルを読み取り専用で開く。"""

    def __init__(self, path: Path):
        self.path = path
        wal = path.with_name(path.name + "-wal")
        # immutable=1 はロックファイルを作らず、読み取り専用メディアや共有フォルダでも開ける。
        # ただし -wal が残っている場合は WAL 内のデータを読むため通常の読み取り専用で開く。
        query = "mode=ro" if wal.exists() else "mode=ro&immutable=1"
        uri = path.resolve().as_uri() + "?" + query
        try:
            self.conn = sqlite3.connect(uri, uri=True)
            self.conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
        except sqlite3.DatabaseError as e:
            raise BagError(f"db3 を開けません（破損している可能性）: {path.name}: {e}") from e

    def close(self) -> None:
        self.conn.close()

    def _tables(self) -> set[str]:
        return {r[0] for r in self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}

    def topics(self) -> dict[int, Topic]:
        tables = self._tables()
        if "topics" not in tables or "messages" not in tables:
            raise BagError(f"rosbag2 の db3 ではありません: {self.path.name}")
        cols = [r[1] for r in self.conn.execute("PRAGMA table_info(topics)")]
        qos_col = "offered_qos_profiles" if "offered_qos_profiles" in cols else "''"
        hash_col = "type_description_hash" if "type_description_hash" in cols else "''"
        rows = self.conn.execute(
            f"SELECT id, name, type, serialization_format, {qos_col}, {hash_col} FROM topics ORDER BY id"
        )
        return {r[0]: Topic(r[1], r[2], r[3] or "cdr", r[4] or "", r[5] or "") for r in rows}

    def message_definitions(self) -> dict[str, tuple[str, str]]:
        """Iron 以降の db3 に含まれるメッセージ定義（型名 → (encoding, 定義)）。"""
        if "message_definitions" not in self._tables():
            return {}
        rows = self.conn.execute("SELECT topic_type, encoding, encoded_message_definition FROM message_definitions")
        return {r[0]: (r[1], r[2]) for r in rows if r[2]}

    def count(self) -> int:
        try:
            return self.conn.execute("SELECT count(*) FROM messages").fetchone()[0]
        except sqlite3.DatabaseError:
            return 0

    def messages(self) -> Iterator[tuple[int, int, bytes]]:
        """(topic_id, timestamp[ns], data) を時刻順に返す。"""
        cur = self.conn.execute("SELECT topic_id, timestamp, data FROM messages ORDER BY timestamp, id")
        while True:
            rows = cur.fetchmany(1000)
            if not rows:
                return
            yield from rows


def decompress_file(src: Path, workdir: Path, progress: Callable[[str], None] | None = None) -> Path:
    """.db3.zstd を作業フォルダに展開する。"""
    workdir.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(suffix=".db3", dir=workdir)
    dctx = zstandard.ZstdDecompressor()
    if progress:
        progress(f"圧縮ファイルを展開中: {src.name}")
    with open(src, "rb") as fin, open(fd, "wb") as fout:
        dctx.copy_stream(fin, fout)
    return Path(tmp)


_dctx = zstandard.ZstdDecompressor()


def maybe_decompress_message(data: bytes) -> bytes:
    """メッセージ単位 zstd 圧縮を解凍する（metadata.yaml が無くても判定できるようマジックナンバーで判定）。

    CDR データは必ず 4 バイトのカプセル化ヘッダ（00 01 00 00 など）で始まるため、
    zstd のマジックナンバーと衝突しない。
    """
    if data[:4] == ZSTD_MAGIC:
        return _dctx.decompressobj().decompress(data)
    return data
