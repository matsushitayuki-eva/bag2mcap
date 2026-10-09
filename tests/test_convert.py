from __future__ import annotations

import json
import threading

import pytest
from mcap.reader import make_reader
from mcap_ros2.decoder import DecoderFactory

from bag2mcap.converter import Compression, convert, log_path_for
from bag2mcap.msgdefs import MsgDefsError, load_pack

from conftest import MSGS_PER_TOPIC_PER_FILE, TOPICS

DECODABLE = [t for t in TOPICS if not t[1].startswith("unknown_msgs")]


def read_mcap(path):
    """全メッセージを ROS 2 デコーダでデコードし、トピックごとの件数などを返す。"""
    counts: dict[str, int] = {}
    decoded: dict[str, list] = {}
    with open(path, "rb") as f:
        reader = make_reader(f, decoder_factories=[DecoderFactory()])
        header = reader.get_header()
        summary = reader.get_summary()
        metadata = {m.name: m.metadata for m in reader.iter_metadata()}
        for schema, channel, message, ros_msg in reader.iter_decoded_messages(
            topics=[t for t, _ in DECODABLE], log_time_order=True
        ):
            counts[channel.topic] = counts.get(channel.topic, 0) + 1
            decoded.setdefault(channel.topic, []).append((message.log_time, ros_msg))
    return header, summary, metadata, counts, decoded


def assert_full_bag(path, files=2, embedded=False):
    header, summary, metadata, counts, decoded = read_mcap(path)
    n = files * MSGS_PER_TOPIC_PER_FILE
    assert header.profile == "ros2"
    assert counts == {t: n for t, _ in DECODABLE}
    assert summary.statistics.message_count == n * len(TOPICS)
    by_topic = {c.topic: c for c in summary.channels.values()}
    assert by_topic["/unknown/topic"].schema_id == 0  # 定義なしはスキーマなし
    assert "reliability: 1" in by_topic["/diagnostics"].metadata["offered_qos_profiles"]
    assert summary.schemas[by_topic["/diagnostics"].schema_id].encoding == "ros2msg"
    # デコード内容の確認
    vel = decoded["/vehicle/status/velocity_status"]
    assert [round(m.longitudinal_velocity, 3) for _, m in vel] == [round(i * 0.1, 3) for i in range(n)]
    assert vel[0][1].header.frame_id == "base_link"
    gear = decoded["/vehicle/status/gear_status"][0][1]
    assert gear.report == 22 and list(gear.covariance) == [float(i) for i in range(9)]
    pc = decoded["/sensing/lidar/concatenated/pointcloud"][-1][1]
    assert pc.width == 200 and len(pc.data) == 200 * 12
    assert decoded["/diagnostics"][0][1].status[0].name == "test: 状態"
    times = [t for t, _ in vel]
    assert times == sorted(times)
    assert "bag2mcap" in metadata
    return metadata


@pytest.mark.parametrize("compression", [None, "message", "file"])
def test_convert_split_bag(make_bag, pack_path, compression):
    bag = make_bag(compression=compression)
    rep = convert(bag, pack=load_pack(pack_path))
    assert rep.ok, rep.error
    assert rep.output == bag.parent / f"{bag.name}.mcap"
    assert rep.missing_types == ["unknown_msgs/msg/Unknown"]
    assert not rep.count_mismatch
    metadata = assert_full_bag(rep.output)
    assert "rosbag2_bagfile_information" in metadata["rosbag2"]["serialized_metadata"]
    log = log_path_for(rep.output).read_text(encoding="utf-8-sig")
    assert "成功" in log and "unknown_msgs/msg/Unknown" in log
    assert not list(bag.parent.glob(".bag2mcap_tmp_*"))


@pytest.mark.parametrize("comp", list(Compression))
def test_mcap_compression_options(make_bag, pack_path, tmp_path, comp):
    bag = make_bag(files=1)
    rep = convert(bag, output_dir=tmp_path / "out", pack=load_pack(pack_path), compression=comp)
    assert rep.ok, rep.error
    assert rep.output.parent == tmp_path / "out"
    assert_full_bag(rep.output, files=1)


def test_without_metadata_yaml(make_bag, pack_path):
    """記録が途中で止まり metadata.yaml が無い bag。メッセージ単位圧縮も自動判定する。"""
    bag = make_bag(metadata=False, compression="message")
    rep = convert(bag, pack=load_pack(pack_path))
    assert rep.ok, rep.error
    assert any("metadata.yaml がありません" in w for w in rep.warnings)
    assert_full_bag(rep.output)


def test_single_db3(make_bag, pack_path):
    bag = make_bag()
    db3 = bag / f"{bag.name}_0.db3"
    rep = convert(db3, pack=load_pack(pack_path))
    assert rep.ok, rep.error
    assert rep.output == bag / f"{bag.name}_0.mcap"
    assert_full_bag(rep.output, files=1)


def test_japanese_path(make_bag, pack_path, tmp_path):
    bag = make_bag(name="走行ログ 2026", parent=tmp_path / "評価試験" / "市場不具合")
    rep = convert(bag, pack=load_pack(pack_path))
    assert rep.ok, rep.error
    assert_full_bag(rep.output)


def test_no_overwrite_by_default(make_bag, pack_path):
    bag = make_bag(files=1)
    assert convert(bag, pack=load_pack(pack_path)).ok
    rep = convert(bag, pack=load_pack(pack_path))
    assert not rep.ok and "既に存在" in rep.error
    assert convert(bag, pack=load_pack(pack_path), overwrite=True).ok


def test_input_is_never_modified(make_bag, pack_path):
    bag = make_bag(files=2)
    before = {p.name: p.read_bytes() for p in bag.iterdir()}
    assert convert(bag, pack=load_pack(pack_path)).ok
    assert {p.name: p.read_bytes() for p in bag.iterdir()} == before


def test_cancel_removes_partial_output(make_bag, pack_path):
    bag = make_bag(files=1)
    ev = threading.Event()
    ev.set()
    rep = convert(bag, pack=load_pack(pack_path), cancel=ev)
    assert not rep.ok and "中止" in rep.error
    assert not rep.output.exists()
    assert not list(bag.parent.glob("*.part"))


def test_without_pack_outputs_schemaless(make_bag):
    bag = make_bag(files=1)
    rep = convert(bag)
    assert rep.ok, rep.error
    assert len(rep.missing_types) == len({t for _, t in TOPICS})


def test_embedded_definitions_are_preferred(make_bag, tmp_path):
    """Iron 以降の db3（message_definitions テーブルあり）は db3 内の定義を使う。"""
    defs = {"unknown_msgs/msg/Unknown": "int32 value\n"}
    bag = make_bag(files=1, embed_defs=defs)
    rep = convert(bag)
    assert rep.ok, rep.error
    assert rep.topics[("/unknown/topic", "unknown_msgs/msg/Unknown")].schema_source == "bag"
    with open(rep.output, "rb") as f:
        reader = make_reader(f, decoder_factories=[DecoderFactory()])
        values = [m.value for _, _, _, m in reader.iter_decoded_messages(topics=["/unknown/topic"])]
    assert values == list(range(MSGS_PER_TOPIC_PER_FILE))


def test_corrupted_split_file_is_skipped(make_bag, pack_path):
    bag = make_bag(files=2)
    (bag / f"{bag.name}_1.db3").write_bytes(b"this is not sqlite" * 100)
    rep = convert(bag, pack=load_pack(pack_path))
    assert rep.ok, rep.error
    assert any("スキップ" in w for w in rep.warnings)
    assert_full_bag(rep.output, files=1)


def test_invalid_input(tmp_path):
    rep = convert(tmp_path / "nothing")
    assert not rep.ok and "見つかりません" in rep.error
    empty = tmp_path / "empty"
    empty.mkdir()
    rep = convert(empty)
    assert not rep.ok and "db3" in rep.error


def test_pack_errors(tmp_path, pack_path):
    with pytest.raises(MsgDefsError):
        load_pack(tmp_path / "none.json")
    bad = tmp_path / "bad.json"
    bad.write_text("{")
    with pytest.raises(MsgDefsError):
        load_pack(bad)
    data = json.loads(pack_path.read_text(encoding="utf-8"))
    data["format_version"] = 99
    bad.write_text(json.dumps(data))
    with pytest.raises(MsgDefsError, match="format_version"):
        load_pack(bad)


def test_pack_contents(pack_path):
    pack = load_pack(pack_path)
    assert pack.source["ref"] == "v-test"
    assert "v-test" in pack.label
    enc, d = pack.get("test_vehicle_msgs/GearState")
    assert enc == "ros2msg"
    assert "uint8 PARK=22" in d and "float64[9] covariance" in d and "MSG: builtin_interfaces/Time" in d
    assert pack.get("sensor_msgs/msg/PointCloud2") is not None
    assert pack.get("test_vehicle_msgs/srv/Ignored") is None


def test_cli(make_bag, pack_path, capsys):
    from bag2mcap.cli import main

    bag1, bag2 = make_bag(name="a", files=1), make_bag(name="b", files=1)
    assert main(["-d", str(pack_path), str(bag1), str(bag2)]) == 0
    out = capsys.readouterr().out
    assert out.count("[OK]") == 2
    assert main(["-d", str(pack_path), str(bag1)]) == 1  # 既存ファイルあり
