"""rosbag2 (.db3) → MCAP 変換処理。"""

from __future__ import annotations

import datetime as dt
import os
import shutil
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable

from mcap.reader import make_reader
from mcap.writer import CompressionType, Writer

from . import __version__
from .bagreader import BagError, BagInfo, Db3File, decompress_file, maybe_decompress_message, open_bag
from .msgdefs import MsgDefPack, empty_pack

CHUNK_SIZE = 4 * 1024 * 1024


class Compression(str, Enum):
    ZSTD = "zstd"
    LZ4 = "lz4"
    NONE = "none"

    def to_mcap(self) -> CompressionType:
        return {
            Compression.ZSTD: CompressionType.ZSTD,
            Compression.LZ4: CompressionType.LZ4,
            Compression.NONE: CompressionType.NONE,
        }[self]


class Cancelled(Exception):
    pass


@dataclass
class TopicReport:
    name: str
    type: str
    schema_source: str  # "bag" / "pack" / "none"
    channel_id: int = 0
    in_count: int = 0
    out_count: int = 0


@dataclass
class Report:
    input: Path
    output: Path | None = None
    pack_label: str = ""
    topics: dict[tuple[str, str], TopicReport] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    error: str = ""
    elapsed: float = 0.0
    started_at: str = ""

    @property
    def ok(self) -> bool:
        return not self.error

    @property
    def missing_types(self) -> list[str]:
        return sorted({t.type for t in self.topics.values() if t.schema_source == "none"})

    @property
    def total_in(self) -> int:
        return sum(t.in_count for t in self.topics.values())

    @property
    def total_out(self) -> int:
        return sum(t.out_count for t in self.topics.values())

    @property
    def count_mismatch(self) -> list[TopicReport]:
        return [t for t in self.topics.values() if t.in_count != t.out_count]

    def to_text(self) -> str:
        lines = [
            f"bag2mcap {__version__} 変換ログ",
            f"開始時刻    : {self.started_at}",
            f"入力        : {self.input}",
            f"出力        : {self.output or '-'}",
            f"定義パック  : {self.pack_label}",
            f"処理時間    : {self.elapsed:.1f} 秒",
            f"結果        : {'成功' if self.ok else '失敗: ' + self.error}",
            f"メッセージ数: 入力 {self.total_in} / 出力 {self.total_out}",
            "",
        ]
        if self.warnings:
            lines.append("[警告]")
            lines += [f"  - {w}" for w in self.warnings]
            lines.append("")
        if self.missing_types:
            lines.append("[定義が見つからない型]（スキーマなしで出力。Lichtblick ではデコードできません）")
            lines += [f"  - {t}" for t in self.missing_types]
            lines.append("")
        lines.append("[トピック一覧]  入力数 / 出力数 / スキーマ / 型 / トピック")
        for t in sorted(self.topics.values(), key=lambda x: x.name):
            mark = "" if t.in_count == t.out_count else "  ← 不一致"
            lines.append(f"  {t.in_count:>9} / {t.out_count:>9} / {t.schema_source:<4} / {t.type} / {t.name}{mark}")
        return "\n".join(lines) + "\n"


ProgressCallback = Callable[[int, int, str], None]


def default_output_path(bag: BagInfo, input_path: Path, output_dir: Path | None) -> Path:
    input_path = Path(input_path)
    if input_path.is_file():
        stem = input_path.name.split(".db3")[0]
        base_dir = input_path.parent
    else:
        stem = input_path.name
        base_dir = input_path.parent
    return (Path(output_dir) if output_dir else base_dir) / f"{stem}.mcap"


def log_path_for(output: Path) -> Path:
    return output.with_name(output.stem + "_bag2mcap_log.txt")


def convert(
    input_path: str | Path,
    output_path: str | Path | None = None,
    *,
    output_dir: str | Path | None = None,
    pack: MsgDefPack | None = None,
    compression: Compression = Compression.ZSTD,
    overwrite: bool = False,
    progress: ProgressCallback | None = None,
    cancel: threading.Event | None = None,
    write_log: bool = True,
) -> Report:
    """1 つの bag を MCAP に変換する。例外は投げず Report.error に格納して返す。"""
    pack = pack or empty_pack()
    input_path = Path(input_path)
    report = Report(input=input_path, pack_label=pack.label, started_at=dt.datetime.now().isoformat(timespec="seconds"))
    t0 = time.monotonic()
    tmp_out: Path | None = None
    workdir: Path | None = None
    try:
        bag = open_bag(input_path)
        report.warnings += bag.warnings
        out = Path(output_path) if output_path else default_output_path(bag, input_path, Path(output_dir) if output_dir else None)
        report.output = out
        if out.exists() and not overwrite:
            raise BagError(f"出力ファイルが既に存在します: {out}")
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp_out = out.with_name(out.name + ".part")
        workdir = out.parent / f".bag2mcap_tmp_{os.getpid()}"
        _convert(bag, tmp_out, workdir, pack, compression, report, progress, cancel)
        _verify(tmp_out, report)
        os.replace(tmp_out, out)
        tmp_out = None
    except Cancelled:
        report.error = "利用者により中止されました"
    except BagError as e:
        report.error = str(e)
    except Exception as e:  # noqa: BLE001  予期しないエラーも利用者に見せる
        report.error = f"{type(e).__name__}: {e}"
    finally:
        if tmp_out is not None and tmp_out.exists():
            try:
                tmp_out.unlink()
            except OSError:
                pass
        if workdir is not None and workdir.exists():
            shutil.rmtree(workdir, ignore_errors=True)
        report.elapsed = time.monotonic() - t0

    if write_log and report.output is not None:
        try:
            log_path_for(report.output).write_text(report.to_text(), encoding="utf-8-sig")
        except OSError as e:
            report.warnings.append(f"ログを保存できませんでした: {e}")
    return report


def _convert(
    bag: BagInfo,
    out: Path,
    workdir: Path,
    pack: MsgDefPack,
    compression: Compression,
    report: Report,
    progress: ProgressCallback | None,
    cancel: threading.Event | None,
) -> None:
    def notify(done: int, total: int, msg: str) -> None:
        if progress:
            progress(done, total, msg)

    def check_cancel() -> None:
        if cancel is not None and cancel.is_set():
            raise Cancelled()

    # 1. 各ファイルを開いて（必要なら展開して）総メッセージ数を数える
    notify(0, 0, "入力を確認中...")
    opened: list[tuple[str, Db3File]] = []
    try:
        for bf in bag.files:
            check_cancel()
            path = decompress_file(bf.path, workdir, lambda m: notify(0, 0, m)) if bf.compressed else bf.path
            try:
                opened.append((bf.path.name, Db3File(path)))
            except BagError as e:
                report.warnings.append(f"{e}（このファイルはスキップしました）")
        if not opened:
            raise BagError("読み込めた db3 ファイルがありません。")
        total = sum(f.count() for _, f in opened)

        with open(out, "wb") as stream:
            writer = Writer(stream, chunk_size=CHUNK_SIZE, compression=compression.to_mcap())
            writer.start(profile="ros2", library=f"bag2mcap {__version__}")

            schemas: dict[str, tuple[int, str]] = {}
            channels: dict[tuple[str, str], int] = {}
            seq: dict[int, int] = {}
            done = 0
            last_notify = 0.0

            for fname, db in opened:
                check_cancel()
                topics = db.topics()
                bag_defs = db.message_definitions()
                id_map: dict[int, tuple[int, TopicReport]] = {}
                for tid, topic in topics.items():
                    key = (topic.name, topic.type)
                    if key not in channels:
                        schema_id, source = _register_schema(writer, schemas, topic.type, bag_defs, pack)
                        channels[key] = writer.register_channel(
                            topic=topic.name,
                            message_encoding=topic.serialization_format or "cdr",
                            schema_id=schema_id,
                            metadata={"offered_qos_profiles": topic.offered_qos_profiles},
                        )
                        report.topics[key] = TopicReport(topic.name, topic.type, source, channels[key])
                    id_map[tid] = (channels[key], report.topics[key])

                for c in db.conn.execute("SELECT topic_id, count(*) FROM messages GROUP BY topic_id"):
                    if c[0] in id_map:
                        id_map[c[0]][1].in_count += c[1]

                try:
                    for topic_id, timestamp, data in db.messages():
                        entry = id_map.get(topic_id)
                        if entry is None:
                            continue
                        channel_id, treport = entry
                        s = seq.get(channel_id, 0)
                        seq[channel_id] = s + 1
                        writer.add_message(
                            channel_id=channel_id,
                            log_time=timestamp,
                            publish_time=timestamp,
                            data=maybe_decompress_message(bytes(data)),
                            sequence=s,
                        )
                        treport.out_count += 1
                        done += 1
                        if done & 0x3FF == 0:
                            check_cancel()
                            now = time.monotonic()
                            if now - last_notify > 0.2:
                                notify(done, total, f"変換中: {fname}")
                                last_notify = now
                except sqlite3.DatabaseError as e:
                    report.warnings.append(
                        f"{fname} の読み込み中にエラーが発生しました（ファイル末尾の破損の可能性）。"
                        f"読めた分までを出力しました: {e}"
                    )

            if bag.metadata_text:
                # rosbag2_storage_mcap と同じ形式で元の metadata.yaml を保持する
                writer.add_metadata("rosbag2", {"serialized_metadata": bag.metadata_text})
            writer.add_metadata(
                "bag2mcap",
                {
                    "version": __version__,
                    "source": str(bag.root),
                    "source_files": ",".join(n for n, _ in opened),
                    "msgdefs": report.pack_label,
                    "converted_at": report.started_at,
                },
            )
            notify(done, total, "仕上げ中...")
            writer.finish()
    finally:
        for _, db in opened:
            db.close()


def _register_schema(
    writer: Writer,
    schemas: dict[str, tuple[int, str]],
    typename: str,
    bag_defs: dict[str, tuple[str, str]],
    pack: MsgDefPack,
) -> tuple[int, str]:
    """スキーマを登録して (schema_id, 取得元) を返す。db3 内の定義 → 定義パック → なし の順に探す。"""
    if typename in schemas:
        return schemas[typename]
    found = None
    source = "none"
    if typename in bag_defs and bag_defs[typename][0] in ("ros2msg", "ros2idl"):
        found, source = bag_defs[typename], "bag"
    elif (p := pack.get(typename)) is not None:
        found, source = p, "pack"
    sid = 0 if found is None else writer.register_schema(name=typename, encoding=found[0], data=found[1].encode("utf-8"))
    schemas[typename] = (sid, source)
    return sid, source


def _verify(path: Path, report: Report) -> None:
    """出力した MCAP を読み直し、トピックごとのメッセージ数を照合する。"""
    with open(path, "rb") as f:
        summary = make_reader(f).get_summary()
    if summary is None or summary.statistics is None:
        raise BagError("出力した MCAP の検証に失敗しました（サマリーがありません）。")
    counts = summary.statistics.channel_message_counts
    for t in report.topics.values():
        written = counts.get(t.channel_id, 0)
        if written != t.out_count:
            raise BagError(f"出力 MCAP のメッセージ数が一致しません: {t.name}（書込 {t.out_count} / 読直し {written}）")
    for t in report.count_mismatch:
        report.warnings.append(f"入力と出力のメッセージ数が一致しません: {t.name}（入力 {t.in_count} / 出力 {t.out_count}）")
