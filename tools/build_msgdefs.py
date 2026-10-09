"""定義パック（msgdefs_*.json）を作成する管理者用スクリプト。

pilot-auto.x1.eve の autoware.repos で取得したソース一式から .msg / .idl を集め、
ROS 2 標準メッセージ（rosbags 内蔵の Humble 定義）と合わせて、
依存する型まで展開した ros2msg 形式の定義を 1 つの JSON にまとめる。

使い方は docs/msgdefs_admin_guide.md を参照。

    python tools/build_msgdefs.py --src <src フォルダ> --out msgdefs/msgdefs_<version>.json \
        --repository tier4/pilot-auto.x1.eve --ref <タグ or コミット>
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

from rosbags.typesys import Stores, get_types_from_idl, get_types_from_msg, get_typestore

FORMAT_VERSION = 1
STORES = {"humble": Stores.ROS2_HUMBLE, "jazzy": Stores.ROS2_JAZZY}
SKIP_DIRS = {".git", "build", "install", "log", "test", "tests"}


def find_package(path: Path, cache: dict[Path, str]) -> tuple[str, Path] | None:
    """path を含む ROS パッケージの (名前, ルートフォルダ) を返す。名前は package.xml の <name>。"""
    for d in path.parents:
        if d in cache:
            return cache[d], d
        pxml = d / "package.xml"
        if pxml.is_file():
            try:
                name = ET.parse(pxml).getroot().findtext("name")
            except ET.ParseError:
                name = None
            cache[d] = (name or d.name).strip()
            return cache[d], d
    return None


def collect_sources(src_dirs: list[Path]) -> tuple[list[tuple[Path, str]], list[str]]:
    """パッケージの msg フォルダ以下（サブフォルダを含む）の .msg / .idl を集める。

    rosidl は msg/object_recognition/Foo.msg のようなサブフォルダ内のファイルも
    pkg/msg/Foo として生成するため、サブフォルダも対象にする。
    """
    found: list[tuple[Path, str]] = []
    warnings: list[str] = []
    cache: dict[Path, str] = {}
    for src in src_dirs:
        for p in sorted(src.rglob("*")):
            if p.suffix not in (".msg", ".idl") or not p.is_file():
                continue
            if any(part in SKIP_DIRS for part in p.relative_to(src).parts[:-1]):
                continue
            if "msg" not in p.relative_to(src).parts[:-1]:
                continue
            pkg = find_package(p, cache)
            if pkg is None:
                warnings.append(f"package.xml が見つからないためスキップ: {p}")
                continue
            name, root = pkg
            if p.relative_to(root).parts[0] != "msg":
                continue  # srv / action、ビルド生成物などは対象外
            found.append((p, name))
    return found, warnings


def strip_idl_annotations(text: str) -> str:
    """rosbags の IDL パーサが解釈できない @verbatim（コメント用の注釈）を取り除く。

    @verbatim はドキュメント用の注釈で、メッセージの構造には影響しない。
    文字列の連結（"a" "b"）を含む書き方が解析エラーになるため、括弧ごと削除する。
    """
    out: list[str] = []
    i = 0
    while True:
        j = text.find("@verbatim", i)
        if j < 0:
            out.append(text[i:])
            return "".join(out)
        out.append(text[i:j])
        k = text.find("(", j)
        if k < 0 or text[j + len("@verbatim") : k].strip():
            out.append("@verbatim")
            i = j + len("@verbatim")
            continue
        depth, in_str, k2 = 0, False, k
        while k2 < len(text):
            ch = text[k2]
            if in_str:
                if ch == "\\":
                    k2 += 1
                elif ch == '"':
                    in_str = False
            elif ch == '"':
                in_str = True
            elif ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    break
            k2 += 1
        i = k2 + 1


_ARRAY_MEMBER = re.compile(r"(?:^|(?<=[{;]))(\s*)([A-Za-z_][\w:]*(?:<[^>]*>)?)\s+(\w+)\s*\[(\d+)\]\s*;", re.M)


def _alias(typ: str, size: str) -> str:
    return re.sub(r"\W", "_", typ) + f"__{size}"


def array_members_to_typedef(text: str) -> str:
    """`double covariance[9];` のような固定長配列メンバーを typedef 経由の書き方に変換する。

    rosbags の IDL パーサは rosidl が生成する typedef 形式しか解釈できないため。
    typedef はメンバーを含む struct の直前に挿入する。
    """
    matches = list(_ARRAY_MEMBER.finditer(text))
    if not matches:
        return text
    inserts: dict[int, list[str]] = {}
    seen: set[str] = set()
    for m in matches:
        _, typ, _, size = m.groups()
        alias = _alias(typ, size)
        if alias not in seen:
            pos = max(text.rfind("struct", 0, m.start()), 0)
            inserts.setdefault(pos, []).append(f"typedef {typ} {alias}[{size}];\n")
            seen.add(alias)

    events = sorted([(pos, 0, None) for pos in inserts] + [(m.start(), 1, m) for m in matches], key=lambda e: (e[0], e[1]))
    out, last = [], 0
    for pos, kind, m in events:
        out.append(text[last:pos])
        if kind == 0:
            out.extend(inserts[pos])
            last = pos
        else:
            indent, typ, name, size = m.groups()
            out.append(f"{indent}{_alias(typ, size)} {name};")
            last = m.end()
    out.append(text[last:])
    return "".join(out)


def parse_sources(files: list[tuple[Path, str]]) -> tuple[dict, dict[str, Path], list[str]]:
    types: dict = {}
    origin: dict[str, Path] = {}
    errors: list[str] = []
    # 同じ型の .msg と .idl が両方ある場合は .msg を優先する（.idl は rosidl が生成したもののことが多い）
    files = sorted(files, key=lambda x: (x[0].suffix != ".msg", str(x[0])))
    for path, pkg in files:
        text = path.read_text(encoding="utf-8", errors="replace")
        try:
            if path.suffix == ".msg":
                parsed = get_types_from_msg(text, f"{pkg}/msg/{path.stem}")
            else:
                parsed = get_types_from_idl(array_members_to_typedef(strip_idl_annotations(text)))
        except Exception as e:  # noqa: BLE001
            errors.append(f"解析失敗: {path}: {e}")
            continue
        for name, typ in parsed.items():
            if name in types:
                if origin[name].suffix == ".msg" and path.suffix == ".idl":
                    continue
                if types[name] != typ:
                    errors.append(f"型の重複（内容が異なる）: {name}: {origin[name]} と {path}（先に見つかった方を採用）")
                continue
            types[name] = typ
            origin[name] = path
    return types, origin, errors


def build_pack(args: argparse.Namespace) -> int:
    src_dirs = [Path(s) for s in args.src]
    for s in src_dirs:
        if not s.is_dir():
            print(f"[ERROR] フォルダがありません: {s}", file=sys.stderr)
            return 2

    files, warnings = collect_sources(src_dirs)
    custom, origin, errors = parse_sources(files)

    store = get_typestore(STORES[args.ros_distro])
    standard = set(store.fielddefs)
    for name, typ in sorted(custom.items()):
        try:
            store.register({name: typ})
        except Exception as e:  # noqa: BLE001
            errors.append(f"登録失敗（標準型との衝突など）: {name}: {e}")

    out_types: dict[str, dict[str, str]] = {}
    for name in sorted(store.fielddefs):
        if name.split("/")[1] != "msg":
            continue
        try:
            definition, _ = store.generate_msgdef(name, ros_version=2)
        except Exception as e:  # noqa: BLE001
            errors.append(f"定義の生成に失敗（依存型が見つからない可能性）: {name}: {e}")
            continue
        out_types[name] = {"encoding": "ros2msg", "definition": definition}

    pack = {
        "format_version": FORMAT_VERSION,
        "ros_distro": args.ros_distro,
        "source": {
            "repository": args.repository,
            "ref": args.ref,
            "generated_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
            "generated_by": args.generated_by,
            "note": args.note,
            "custom_type_count": len([n for n in out_types if n not in standard]),
            "standard_type_count": len([n for n in out_types if n in standard]),
        },
        "types": out_types,
    }

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(pack, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")

    packages = sorted({n.split("/")[0] for n in out_types if n not in standard})
    report = [
        f"出力: {out}",
        f"収集ファイル数: {len(files)}",
        f"独自型: {pack['source']['custom_type_count']} / 標準型: {pack['source']['standard_type_count']}",
        f"独自パッケージ ({len(packages)}): {', '.join(packages)}",
        f"警告: {len(warnings)} 件 / エラー: {len(errors)} 件",
        *[f"[WARN] {w}" for w in warnings],
        *[f"[ERROR] {e}" for e in errors],
    ]
    report_text = "\n".join(report) + "\n"
    out.with_name(out.stem + "_report.txt").write_text(report_text, encoding="utf-8")
    print(report_text)
    return 1 if errors and args.strict else 0


def check_bag_types(args: argparse.Namespace) -> int:
    """bag（metadata.yaml / db3）の型が定義パックに全て含まれるか確認する。"""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    from bag2mcap.bagreader import Db3File, open_bag  # noqa: PLC0415
    from bag2mcap.msgdefs import load_pack  # noqa: PLC0415

    pack = load_pack(args.pack)
    bag = open_bag(args.bag)
    types: set[str] = set()
    for bf in bag.files:
        if bf.compressed:
            continue
        db = Db3File(bf.path)
        types |= {t.type for t in db.topics().values()}
        db.close()
    if bag.metadata_text:
        types |= set(re.findall(r"type:\s*(\S+/msg/\S+)", bag.metadata_text))
    missing = sorted(t for t in types if pack.get(t) is None)
    print(f"bag 内の型: {len(types)} / 定義パックに無い型: {len(missing)}")
    for t in missing:
        print(f"  - {t}")
    return 1 if missing else 0


def _safe_stdio() -> None:
    """出力先の文字コードで表せない文字（cp1252 へのリダイレクト時の日本語など）で落ちないようにする。"""
    for s in (sys.stdout, sys.stderr):
        if s is not None and hasattr(s, "reconfigure"):
            s.reconfigure(errors="replace")


def main(argv: list[str] | None = None) -> int:
    _safe_stdio()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd")

    b = sub.add_parser("build", help="定義パックを作成する（既定）")
    b.add_argument("--src", action="append", required=True, help="ソースフォルダ（複数指定可）")
    b.add_argument("--out", required=True, help="出力する JSON のパス")
    b.add_argument("--repository", default="tier4/pilot-auto.x1.eve")
    b.add_argument("--ref", required=True, help="pilot-auto.x1.eve のタグまたはコミット")
    b.add_argument("--ros-distro", choices=sorted(STORES), default="humble")
    b.add_argument("--generated-by", default="")
    b.add_argument("--note", default="")
    b.add_argument("--strict", action="store_true", help="エラーがあれば終了コード 1 を返す")

    c = sub.add_parser("check", help="bag 内の型が定義パックに全て含まれるか確認する")
    c.add_argument("--pack", required=True)
    c.add_argument("--bag", required=True)

    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] not in ("build", "check", "-h", "--help"):
        argv.insert(0, "build")
    args = ap.parse_args(argv)
    if args.cmd == "check":
        return check_bag_types(args)
    return build_pack(args)


if __name__ == "__main__":
    sys.exit(main())
