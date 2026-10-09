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
