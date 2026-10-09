"""コマンドライン版（一括処理・動作確認用）。

    python -m bag2mcap --msgdefs msgdefs.json <bag フォルダ or .db3> [...]
"""

from __future__ import annotations

import argparse
import sys

from . import __version__
from .converter import Compression, convert, log_path_for
from .msgdefs import MsgDefsError, empty_pack, load_pack


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="bag2mcap", description="rosbag2 (.db3) を MCAP に変換します。")
    ap.add_argument("inputs", nargs="+", help="bag フォルダまたは .db3 ファイル（複数可）")
    ap.add_argument("-d", "--msgdefs", help="定義パック (msgdefs_*.json)")
    ap.add_argument("-o", "--output-dir", help="出力フォルダ（省略時は入力と同じ場所）")
    ap.add_argument("-c", "--compression", choices=[c.value for c in Compression], default="zstd")
    ap.add_argument("-y", "--overwrite", action="store_true", help="既存の出力ファイルを上書きする")
    ap.add_argument("-V", "--version", action="version", version=f"bag2mcap {__version__}")
    args = ap.parse_args(argv)

    try:
        pack = load_pack(args.msgdefs) if args.msgdefs else empty_pack()
    except MsgDefsError as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        return 2
    if not args.msgdefs:
        print("[WARN] 定義パックが指定されていません。db3 内に定義が無い型はスキーマなしで出力されます。", file=sys.stderr)

    failed = 0
    for inp in args.inputs:
        def progress(done: int, total: int, msg: str) -> None:
            pct = f"{done * 100 // total:3d}%" if total else "   "
            print(f"\r{pct} {msg[:60]:<60}", end="", file=sys.stderr, flush=True)

        rep = convert(
            inp,
            output_dir=args.output_dir,
            pack=pack,
            compression=Compression(args.compression),
            overwrite=args.overwrite,
            progress=progress,
        )
        print(file=sys.stderr)
        if rep.ok:
            print(f"[OK] {inp} -> {rep.output}（{rep.total_out} メッセージ, {rep.elapsed:.1f} 秒）")
        else:
            failed += 1
            print(f"[NG] {inp}: {rep.error}")
        for w in rep.warnings:
            print(f"  [WARN] {w}")
        if rep.missing_types:
            print(f"  [WARN] 定義が見つからない型 {len(rep.missing_types)} 件: {', '.join(rep.missing_types)}")
        if rep.output is not None:
            print(f"  ログ: {log_path_for(rep.output)}")
    return 1 if failed else 0
