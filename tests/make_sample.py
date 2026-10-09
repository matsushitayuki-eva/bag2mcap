"""動作確認用のサンプル bag と定義パックを作る（CI の exe 動作確認でも使用）。

    python tests/make_sample.py <出力フォルダ>
"""

import sys
from pathlib import Path

sys.path[:0] = [str(Path(__file__).parent), str(Path(__file__).parents[1] / "tools")]

import build_msgdefs  # noqa: E402
import conftest  # noqa: E402


class _Factory:
    def __init__(self, base: Path):
        self.base = base

    def mktemp(self, name: str) -> Path:
        p = self.base / name
        p.mkdir(parents=True, exist_ok=True)
        return p


def main(out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    src = conftest.msg_src.__wrapped__(_Factory(out / "work"))
    build_msgdefs.main(["build", "--src", str(src), "--out", str(out / "msgdefs_sample.json"), "--ref", "sample"])
    make = conftest.make_bag.__wrapped__(conftest.typestore.__wrapped__(), out)
    make(name="sample_bag")
    make(name="sample_bag_msgzstd", compression="message")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
