from rosbags.typesys import get_types_from_idl

import build_msgdefs as b


def test_array_members_to_typedef():
    idl = (
        '#include "geometry_msgs/msg/Point.idl"\n'
        "module a_msgs { module msg {\n"
        "  struct X {\n    geometry_msgs::msg::Point pts[3];\n    double c[9];\n    double d[9];\n  };\n"
        "  struct Y { int32 z[2]; };\n"
        "}; };\n"
    )
    types = get_types_from_idl(b.array_members_to_typedef(idl))
    x = dict(types["a_msgs/msg/X"][1])
    assert x["pts"][1][1] == 3
    assert x["c"][1][1] == 9 and x["d"][1][1] == 9
    assert dict(types["a_msgs/msg/Y"][1])["z"][1][1] == 2


def test_strip_verbatim():
    idl = '@verbatim (language="comment", text=\n  " a (b)"\n  " c \\" d")\nstruct S { int32 v; };'
    assert b.strip_idl_annotations(idl).strip() == "struct S { int32 v; };"


def test_missing_dependency_is_reported(tmp_path):
    pkg = tmp_path / "x_msgs"
    (pkg / "msg").mkdir(parents=True)
    (pkg / "package.xml").write_text("<package><name>x_msgs</name></package>")
    (pkg / "msg" / "A.msg").write_text("geographic_msgs/GeoPoint p\n")
    (pkg / "msg" / "B.msg").write_text("int32 v\n")
    out = tmp_path / "p.json"
    assert b.main(["build", "--src", str(tmp_path), "--out", str(out), "--ref", "r", "--strict"]) == 1
    report = (tmp_path / "p_report.txt").read_text(encoding="utf-8")
    assert "geographic_msgs/msg/GeoPoint" in report
    import json

    types = json.loads(out.read_text(encoding="utf-8"))["types"]
    assert "x_msgs/msg/B" in types and "x_msgs/msg/A" not in types


def test_msg_in_subfolder(tmp_path):
    """msg/object_recognition/Foo.msg のようなサブフォルダ内の定義も pkg/msg/Foo として集める。"""
    pkg = tmp_path / "repo" / "y_msgs"
    (pkg / "msg" / "object_recognition").mkdir(parents=True)
    (pkg / "srv").mkdir()
    (pkg / "package.xml").write_text("<package><name>y_msgs</name></package>")
    (pkg / "msg" / "object_recognition" / "Feature.msg").write_text("int32 v\n")
    (pkg / "msg" / "Top.msg").write_text("y_msgs/Feature f\n")
    (pkg / "srv" / "S.msg").write_text("int32 ignored\n")
    files, _ = b.collect_sources([tmp_path])
    assert sorted(p.name for p, _ in files) == ["Feature.msg", "Top.msg"]
    out = tmp_path / "p.json"
    assert b.main(["build", "--src", str(tmp_path), "--out", str(out), "--ref", "r", "--strict"]) == 0
    import json

    types = json.loads(out.read_text(encoding="utf-8"))["types"]
    assert "MSG: y_msgs/Feature" in types["y_msgs/msg/Top"]["definition"]


def test_audit_and_check_metadata_yaml(tmp_path, pack_path):
    src = tmp_path / "src"
    node = src / "my_node"
    (node / "src").mkdir(parents=True)
    (node / "package.xml").write_text(
        "<package><name>my_node</name><depend>test_vehicle_msgs</depend><depend>missing_msgs</depend>"
        "<depend>srv_only_msgs</depend></package>"
    )
    (node / "src" / "a.py").write_text("from other_msgs.msg import Foo\n")
    srv = src / "srv_only_msgs"
    (srv / "srv").mkdir(parents=True)
    (srv / "package.xml").write_text("<package><name>srv_only_msgs</name></package>")
    assert b.main(["audit", "--pack", str(pack_path), "--src", str(src)]) == 1

    meta = tmp_path / "metadata.yaml"
    meta.write_text(
        "rosbag2_bagfile_information:\n  topics_with_message_count:\n"
        "    - topic_metadata:\n        name: /a\n        type: test_vehicle_msgs/msg/VelocityReport\n"
        "    - topic_metadata:\n        name: /b\n        type: rosbridge_msgs/msg/ConnectedClients\n",
        encoding="utf-8",
    )
    assert b.main(["check", "--pack", str(pack_path), "--bag", str(meta)]) == 1
