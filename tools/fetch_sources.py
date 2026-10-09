"""autoware.repos に記載されたリポジトリを指定バージョンで取得する（定義パック作成用）。

vcstool が無い環境（Windows など）でも使えるよう git だけで動く。履歴は取得せず、
指定コミット / タグのみを浅く取得する。

    python tools/fetch_sources.py --repos <pilot-auto.x1.eve>/autoware.repos --out work/src [--https]
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

import yaml


def to_https(url: str) -> str:
    if url.startswith("git@github.com:"):
        return "https://github.com/" + url[len("git@github.com:") :]
    return url


def git(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


def fetch(name: str, url: str, version: str, dest: Path) -> str | None:
    if (dest / ".git").is_dir():
        if git("rev-parse", "HEAD", cwd=dest).stdout.strip().startswith(version) or git(
            "describe", "--tags", "--exact-match", cwd=dest
        ).stdout.strip() == version:
            return None  # 取得済み
    dest.mkdir(parents=True, exist_ok=True)
    git("init", "-q", cwd=dest)
    git("remote", "remove", "origin", cwd=dest)
    git("remote", "add", "origin", url, cwd=dest)
    r = git("fetch", "-q", "--depth", "1", "origin", version, cwd=dest)
    if r.returncode != 0:
        return f"{name}: fetch に失敗しました（{url} @ {version}）: {r.stderr.strip()}"
    r = git("checkout", "-q", "--force", "FETCH_HEAD", cwd=dest)
    if r.returncode != 0:
        return f"{name}: checkout に失敗しました: {r.stderr.strip()}"
    return None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repos", required=True, help="autoware.repos のパス")
    ap.add_argument("--out", required=True, help="取得先フォルダ")
    ap.add_argument("--https", action="store_true", help="git@github.com: を https:// に読み替える")
    ap.add_argument("--only", nargs="*", help="名前に指定文字列を含むリポジトリだけ取得する")
    args = ap.parse_args(argv)

    repos = yaml.safe_load(Path(args.repos).read_text(encoding="utf-8"))["repositories"]
    out = Path(args.out)
    errors: list[str] = []
    tried = 0
    for name, info in repos.items():
        if info.get("type", "git") != "git":
            continue
        if args.only and not any(s in name for s in args.only):
            continue
        tried += 1
        url = to_https(info["url"]) if args.https else info["url"]
        print(f"取得中: {name} @ {info['version']}", flush=True)
        err = fetch(name, url, str(info["version"]), out / name)
        if err:
            errors.append(err)
            print(f"  [ERROR] {err}", flush=True)
    print(f"\n完了: {tried} 件中 エラー {len(errors)} 件")
    for e in errors:
        print(f"  - {e}")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
