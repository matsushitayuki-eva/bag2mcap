"""テスト用に ROS 2 Humble (rosbag2 0.15) と同じ形式の bag を生成する。"""

from __future__ import annotations

import sqlite3
import sys
import textwrap
from pathlib import Path

import pytest
import yaml
import zstandard
from rosbags.typesys import Stores, get_types_from_idl, get_types_from_msg, get_typestore

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

QOS = (
    "- history: 3\n  depth: 0\n  reliability: 1\n  durability: 2\n"
    "  deadline:\n    sec: 2147483647\n    nsec: 4294967295\n"
    "  lifespan:\n    sec: 2147483647\n    nsec: 4294967295\n  liveliness: 1\n"
    "  liveliness_lease_duration:\n    sec: 2147483647\n    nsec: 4294967295\n"
    "  avoid_ros_namespace_conventions: false"
)

VELOCITY_MSG = "std_msgs/Header header\nfloat32 longitudinal_velocity\nfloat32 lateral_velocity\nfloat32 heading_rate\n"
STATE_IDL = textwrap.dedent(
    """\
    #include "builtin_interfaces/msg/Time.idl"
    module test_vehicle_msgs {
      module msg {
        module GearState_Constants {
          const uint8 PARK = 22;
        };
        @verbatim (language="comment", text=
          " multi-line"
          " comment")
        struct GearState {
          builtin_interfaces::msg::Time stamp;
          uint8 report;
          double covariance[9];
        };
      };
    };
    """
)

TOPICS = [
    ("/sensing/lidar/concatenated/pointcloud", "sensor_msgs/msg/PointCloud2"),
    ("/vehicle/status/velocity_status", "test_vehicle_msgs/msg/VelocityReport"),
    ("/vehicle/status/gear_status", "test_vehicle_msgs/msg/GearState"),
    ("/diagnostics", "diagnostic_msgs/msg/DiagnosticArray"),
    ("/unknown/topic", "unknown_msgs/msg/Unknown"),
]
MSGS_PER_TOPIC_PER_FILE = 25
T0 = 1_700_000_000_000_000_000


@pytest.fixture(scope="session")
def msg_src(tmp_path_factory) -> Path:
    """ROS パッケージ構成の .msg / .idl ソース。"""
    src = tmp_path_factory.mktemp("src")
    pkg = src / "repo" / "test_vehicle_msgs"
    (pkg / "msg").mkdir(parents=True)
    (pkg / "package.xml").write_text("<package><name>test_vehicle_msgs</name></package>")
    (pkg / "msg" / "VelocityReport.msg").write_text(VELOCITY_MSG)
    (pkg / "msg" / "GearState.idl").write_text(STATE_IDL)
    (pkg / "srv").mkdir()
    (pkg / "srv" / "Ignored.srv").write_text("int32 a\n---\nint32 b\n")
    return src


@pytest.fixture(scope="session")
def pack_path(msg_src, tmp_path_factory) -> Path:
    import build_msgdefs

    out = tmp_path_factory.mktemp("pack") / "msgdefs_test.json"
    rc = build_msgdefs.main(["build", "--src", str(msg_src), "--out", str(out), "--ref", "v-test", "--strict"])
    assert rc == 0
    return out


@pytest.fixture(scope="session")
def typestore():
    ts = get_typestore(Stores.ROS2_HUMBLE)
    ts.register(get_types_from_msg(VELOCITY_MSG, "test_vehicle_msgs/msg/VelocityReport"))
    import build_msgdefs

    ts.register(get_types_from_idl(build_msgdefs.array_members_to_typedef(build_msgdefs.strip_idl_annotations(STATE_IDL))))
    ts.register(get_types_from_msg("int32 value\n", "unknown_msgs/msg/Unknown"))
    return ts


def make_message(ts, msgtype: str, i: int):
    Time = ts.types["builtin_interfaces/msg/Time"]
    Header = ts.types["std_msgs/msg/Header"]
    stamp = Time(sec=1_700_000_000 + i // 10, nanosec=(i % 10) * 100_000_000)
    header = Header(stamp=stamp, frame_id="base_link")
    if msgtype == "sensor_msgs/msg/PointCloud2":
        import numpy as np

        PF = ts.types["sensor_msgs/msg/PointField"]
        fields = [PF(name=n, offset=4 * k, datatype=7, count=1) for k, n in enumerate("xyz")]
        n = 200
        data = np.arange(n * 3, dtype=np.float32).tobytes()
        return ts.types[msgtype](
            header=header, height=1, width=n, fields=fields, is_bigendian=False,
            point_step=12, row_step=12 * n, data=np.frombuffer(data, dtype=np.uint8), is_dense=True,
        )
    if msgtype == "test_vehicle_msgs/msg/VelocityReport":
        return ts.types[msgtype](header=header, longitudinal_velocity=i * 0.1, lateral_velocity=0.0, heading_rate=0.01)
    if msgtype == "test_vehicle_msgs/msg/GearState":
        import numpy as np

        return ts.types[msgtype](stamp=stamp, report=22, covariance=np.arange(9, dtype=np.float64))
    if msgtype == "diagnostic_msgs/msg/DiagnosticArray":
        KV = ts.types["diagnostic_msgs/msg/KeyValue"]
        st = ts.types["diagnostic_msgs/msg/DiagnosticStatus"](
            level=0, name="test: 状態", message="OK", hardware_id="hw", values=[KV(key="k", value=str(i))]
        )
        return ts.types[msgtype](header=header, status=[st])
    if msgtype == "unknown_msgs/msg/Unknown":
        return ts.types[msgtype](value=i)
    raise ValueError(msgtype)


HUMBLE_SCHEMA = """
CREATE TABLE schema(schema_version INTEGER PRIMARY KEY,ros_distro TEXT NOT NULL);
CREATE TABLE metadata(id INTEGER PRIMARY KEY,metadata_version INTEGER NOT NULL,metadata TEXT NOT NULL);
CREATE TABLE topics(id INTEGER PRIMARY KEY,name TEXT NOT NULL,type TEXT NOT NULL,serialization_format TEXT NOT NULL,offered_qos_profiles TEXT NOT NULL);
CREATE TABLE messages(id INTEGER PRIMARY KEY,topic_id INTEGER NOT NULL,timestamp INTEGER NOT NULL, data BLOB NOT NULL);
CREATE INDEX timestamp_idx ON messages (timestamp ASC);
INSERT INTO schema VALUES (3, 'humble');
"""


@pytest.fixture
def make_bag(typestore, tmp_path):
    """make_bag(name, files=2, compression=None|'file'|'message', metadata=True, embed_defs=None) -> Path"""

    def _make(
        name: str = "rosbag2_2026_10_09-10_00_00",
        files: int = 2,
        compression: str | None = None,
        metadata: bool = True,
        embed_defs: dict[str, str] | None = None,
        parent: Path | None = None,
    ) -> Path:
        bag = (parent or tmp_path) / name
        bag.mkdir(parents=True)
        cctx = zstandard.ZstdCompressor()
        rel_paths = []
        i_global = 0
        for f in range(files):
            db_path = bag / f"{name}_{f}.db3"
            con = sqlite3.connect(db_path)
            con.executescript(HUMBLE_SCHEMA)
            if embed_defs is not None:
                # Iron 以降の形式（message_definitions テーブル）を模擬
                con.execute(
                    "CREATE TABLE message_definitions(id INTEGER PRIMARY KEY, topic_type TEXT NOT NULL, encoding TEXT NOT NULL,"
                    " encoded_message_definition TEXT NOT NULL, type_description_hash TEXT NOT NULL)"
                )
                for t, d in embed_defs.items():
                    con.execute("INSERT INTO message_definitions(topic_type, encoding, encoded_message_definition, type_description_hash) VALUES (?, 'ros2msg', ?, '')", (t, d))
            # 分割ファイルごとに topic の id が異なる場合を再現するため逆順で登録
            order = list(enumerate(TOPICS)) if f % 2 == 0 else list(reversed(list(enumerate(TOPICS))))
            ids = {}
            for k, (idx, (tname, ttype)) in enumerate(order, start=1):
                con.execute("INSERT INTO topics VALUES (?, ?, ?, 'cdr', ?)", (k, tname, ttype, QOS))
                ids[idx] = k
            for i in range(MSGS_PER_TOPIC_PER_FILE):
                for idx, (tname, ttype) in enumerate(TOPICS):
                    raw = bytes(typestore.serialize_cdr(make_message(typestore, ttype, i_global + i), ttype))
                    if compression == "message":
                        raw = cctx.compress(raw)
                    ts_ns = T0 + (i_global + i) * 100_000_000 + idx
                    con.execute("INSERT INTO messages(topic_id, timestamp, data) VALUES (?, ?, ?)", (ids[idx], ts_ns, raw))
            i_global += MSGS_PER_TOPIC_PER_FILE
            con.commit()
            con.close()
            if compression == "file":
                zpath = db_path.with_name(db_path.name + ".zstd")
                zpath.write_bytes(cctx.compress(db_path.read_bytes()))
                db_path.unlink()
                rel_paths.append(zpath.name)
            else:
                rel_paths.append(db_path.name)
        if metadata:
            count = files * MSGS_PER_TOPIC_PER_FILE
            meta = {
                "rosbag2_bagfile_information": {
                    "version": 5,
                    "storage_identifier": "sqlite3",
                    "duration": {"nanoseconds": i_global * 100_000_000},
                    "starting_time": {"nanoseconds_since_epoch": T0},
                    "message_count": count * len(TOPICS),
                    "topics_with_message_count": [
                        {
                            "topic_metadata": {"name": n, "type": t, "serialization_format": "cdr", "offered_qos_profiles": QOS},
                            "message_count": count,
                        }
                        for n, t in TOPICS
                    ],
                    "compression_format": "zstd" if compression else "",
                    "compression_mode": {"file": "FILE", "message": "MESSAGE"}.get(compression or "", ""),
                    "relative_file_paths": rel_paths,
                    "files": [],
                }
            }
            (bag / "metadata.yaml").write_text(yaml.safe_dump(meta, allow_unicode=True))
        return bag

    return _make
