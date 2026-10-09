"""簡易 GUI（tkinter）。"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

from . import __version__
from .converter import Compression, Report, convert, log_path_for
from .msgdefs import MsgDefPack, MsgDefsError, empty_pack, load_pack

try:  # ドラッグ&ドロップ（任意機能）
    from tkinterdnd2 import DND_FILES, TkinterDnD
except Exception:  # noqa: BLE001
    TkinterDnD = None
    DND_FILES = None

APP_TITLE = f"bag2mcap {__version__}  -  rosbag2 (.db3) → MCAP 変換"


def settings_path() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home() / ".config")
    return Path(base) / "bag2mcap" / "settings.json"


def load_settings() -> dict:
    try:
        return json.loads(settings_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_settings(data: dict) -> None:
    try:
        p = settings_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError:
        pass


def app_dir() -> Path:
    """exe（または本パッケージ）が置かれているフォルダ。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


def find_bundled_pack() -> Path | None:
    """exe と同じ場所（または msgdefs フォルダ）にある定義パックのうち名前が最後のものを返す。"""
    cands: list[Path] = []
    for d in (app_dir(), app_dir() / "msgdefs"):
        if d.is_dir():
            cands += [p for p in d.glob("msgdefs*.json") if not p.name.endswith("_report.json")]
    return sorted(cands, key=lambda p: p.name)[-1] if cands else None


def open_in_explorer(path: Path) -> None:
    try:
        if sys.platform == "win32":
            if path.is_file():
                subprocess.Popen(["explorer", "/select,", str(path)])
            else:
                os.startfile(str(path))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path if path.is_dir() else path.parent)])
        else:
            subprocess.Popen(["xdg-open", str(path if path.is_dir() else path.parent)])
    except OSError:
        pass


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.settings = load_settings()
        self.pack: MsgDefPack = empty_pack()
        self.queue: queue.Queue = queue.Queue()
        self.worker: threading.Thread | None = None
        self.cancel_event = threading.Event()
        self.last_output: Path | None = None

        root.title(APP_TITLE)
        root.minsize(760, 560)
        self._build()
        self._init_pack()
        root.protocol("WM_DELETE_WINDOW", self._on_close)
        root.after(100, self._poll)

    # ---------- 画面 ----------
    def _build(self) -> None:
        pad = {"padx": 8, "pady": 4}
        frm = ttk.Frame(self.root, padding=8)
        frm.pack(fill="both", expand=True)
        frm.columnconfigure(1, weight=1)

        # 定義パック
        ttk.Label(frm, text="定義パック").grid(row=0, column=0, sticky="w", **pad)
        self.pack_var = tk.StringVar()
        ttk.Entry(frm, textvariable=self.pack_var, state="readonly").grid(row=0, column=1, sticky="ew", **pad)
        ttk.Button(frm, text="参照...", command=self._choose_pack).grid(row=0, column=2, **pad)
        self.pack_info = tk.StringVar(value="（未選択）")
        ttk.Label(frm, textvariable=self.pack_info, foreground="#555").grid(row=1, column=1, columnspan=2, sticky="w", padx=8)

        # 入力
        ttk.Label(frm, text="入力 bag").grid(row=2, column=0, sticky="nw", **pad)
        lf = ttk.Frame(frm)
        lf.grid(row=2, column=1, sticky="nsew", **pad)
        lf.columnconfigure(0, weight=1)
        lf.rowconfigure(0, weight=1)
        self.inputs = tk.Listbox(lf, height=6, selectmode="extended")
        self.inputs.grid(row=0, column=0, sticky="nsew")
        sb = ttk.Scrollbar(lf, orient="vertical", command=self.inputs.yview)
        sb.grid(row=0, column=1, sticky="ns")
        self.inputs.configure(yscrollcommand=sb.set)
        self.dnd_hint = tk.StringVar()
        ttk.Label(lf, textvariable=self.dnd_hint, foreground="#555").grid(row=1, column=0, sticky="w")
        bf = ttk.Frame(frm)
        bf.grid(row=2, column=2, sticky="n", **pad)
        ttk.Button(bf, text="フォルダ追加...", command=self._add_folder).pack(fill="x", pady=2)
        ttk.Button(bf, text="db3 追加...", command=self._add_files).pack(fill="x", pady=2)
        ttk.Button(bf, text="選択を削除", command=self._remove_selected).pack(fill="x", pady=2)
        ttk.Button(bf, text="全て削除", command=lambda: self.inputs.delete(0, "end")).pack(fill="x", pady=2)
        try:
            self.inputs.drop_target_register(DND_FILES)
            self.inputs.dnd_bind("<<Drop>>", self._on_drop)
            dnd_ok = True
        except Exception:  # noqa: BLE001  tkdnd が使えない環境では D&D なし
            dnd_ok = False
        self.dnd_hint.set("（ここにフォルダ / .db3 をドラッグ&ドロップ）" if dnd_ok else "")

        # 出力
        ttk.Label(frm, text="出力先").grid(row=3, column=0, sticky="w", **pad)
        of = ttk.Frame(frm)
        of.grid(row=3, column=1, columnspan=2, sticky="ew", **pad)
        of.columnconfigure(2, weight=1)
        self.out_mode = tk.StringVar(value=self.settings.get("out_mode", "same"))
        self.out_dir = tk.StringVar(value=self.settings.get("out_dir", ""))
        ttk.Radiobutton(of, text="入力と同じ場所", value="same", variable=self.out_mode).grid(row=0, column=0, sticky="w")
        ttk.Radiobutton(of, text="指定フォルダ:", value="dir", variable=self.out_mode).grid(row=0, column=1, sticky="w", padx=(12, 4))
        ttk.Entry(of, textvariable=self.out_dir).grid(row=0, column=2, sticky="ew")
        ttk.Button(of, text="参照...", command=self._choose_out).grid(row=0, column=3, padx=(4, 0))

        # オプション
        ttk.Label(frm, text="オプション").grid(row=4, column=0, sticky="w", **pad)
        opt = ttk.Frame(frm)
        opt.grid(row=4, column=1, columnspan=2, sticky="w", **pad)
        ttk.Label(opt, text="MCAP 圧縮:").pack(side="left")
        self.comp = tk.StringVar(value=self.settings.get("compression", Compression.ZSTD.value))
        ttk.Combobox(opt, textvariable=self.comp, values=[c.value for c in Compression], width=6, state="readonly").pack(side="left", padx=4)
        self.overwrite = tk.BooleanVar(value=False)
        ttk.Checkbutton(opt, text="既存ファイルを上書き", variable=self.overwrite).pack(side="left", padx=16)

        # 実行
        rf = ttk.Frame(frm)
        rf.grid(row=5, column=0, columnspan=3, sticky="ew", **pad)
        rf.columnconfigure(0, weight=1)
        self.progress = ttk.Progressbar(rf, mode="determinate", maximum=1000)
        self.progress.grid(row=0, column=0, sticky="ew")
        self.start_btn = ttk.Button(rf, text="変換開始", command=self._start)
        self.start_btn.grid(row=0, column=1, padx=(8, 0))
        self.cancel_btn = ttk.Button(rf, text="中止", command=self._cancel, state="disabled")
        self.cancel_btn.grid(row=0, column=2, padx=(4, 0))
        self.open_btn = ttk.Button(rf, text="出力先を開く", command=self._open_output, state="disabled")
        self.open_btn.grid(row=0, column=3, padx=(4, 0))
        self.status = tk.StringVar(value="待機中")
        ttk.Label(rf, textvariable=self.status).grid(row=1, column=0, columnspan=4, sticky="w", pady=(4, 0))

        # ログ
        self.log = ScrolledText(frm, height=12, state="disabled", font=("Consolas", 9))
        self.log.grid(row=6, column=0, columnspan=3, sticky="nsew", **pad)
        frm.rowconfigure(6, weight=1)
        frm.rowconfigure(2, weight=0)

    # ---------- 定義パック ----------
    def _init_pack(self) -> None:
        last = self.settings.get("msgdefs")
        cand = Path(last) if last and Path(last).is_file() else find_bundled_pack()
        if cand:
            self._set_pack(cand, quiet=True)

    def _choose_pack(self) -> None:
        p = filedialog.askopenfilename(title="定義パックを選択", filetypes=[("定義パック", "*.json"), ("すべて", "*.*")])
        if p:
            self._set_pack(Path(p))

    def _set_pack(self, path: Path, quiet: bool = False) -> None:
        try:
            self.pack = load_pack(path)
        except MsgDefsError as e:
            self.pack = empty_pack()
            self.pack_var.set("")
            self.pack_info.set("（未選択）")
            if not quiet:
                messagebox.showerror("定義パック", str(e))
            return
        self.pack_var.set(str(path))
        self.pack_info.set(self.pack.label)
        self.settings["msgdefs"] = str(path)
        save_settings(self.settings)

    # ---------- 入力 ----------
    def _add_paths(self, paths) -> None:
        existing = set(self.inputs.get(0, "end"))
        for p in paths:
            p = str(Path(p))
            if p not in existing:
                self.inputs.insert("end", p)
                existing.add(p)

    def _add_folder(self) -> None:
        p = filedialog.askdirectory(title="bag フォルダを選択")
        if p:
            self._add_paths([p])

    def _add_files(self) -> None:
        ps = filedialog.askopenfilenames(title="db3 ファイルを選択", filetypes=[("rosbag2", "*.db3 *.zstd"), ("すべて", "*.*")])
        self._add_paths(ps)

    def _remove_selected(self) -> None:
        for i in reversed(self.inputs.curselection()):
            self.inputs.delete(i)

    def _on_drop(self, event) -> None:
        self._add_paths(self.root.tk.splitlist(event.data))

    def _choose_out(self) -> None:
        p = filedialog.askdirectory(title="出力フォルダを選択")
        if p:
            self.out_dir.set(p)
            self.out_mode.set("dir")

    # ---------- 実行 ----------
    def _append_log(self, text: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _start(self) -> None:
        inputs = list(self.inputs.get(0, "end"))
        if not inputs:
            messagebox.showwarning("入力", "変換する bag フォルダまたは .db3 を追加してください。")
            return
        if self.pack.path is None and not messagebox.askyesno(
            "定義パック",
            "定義パックが選択されていません。\n定義が db3 に含まれない型（Humble の bag は全て）は"
            " Lichtblick で表示できません。\n\nこのまま変換しますか？",
        ):
            return
        out_dir = None
        if self.out_mode.get() == "dir":
            out_dir = self.out_dir.get().strip()
            if not out_dir:
                messagebox.showwarning("出力先", "出力フォルダを指定してください。")
                return
        self.settings.update(out_mode=self.out_mode.get(), out_dir=self.out_dir.get(), compression=self.comp.get())
        save_settings(self.settings)

        self.cancel_event.clear()
        self.start_btn.configure(state="disabled")
        self.cancel_btn.configure(state="normal")
        self.open_btn.configure(state="disabled")
        args = (inputs, out_dir, self.pack, Compression(self.comp.get()), self.overwrite.get())
        self.worker = threading.Thread(target=self._run, args=args, daemon=True)
        self.worker.start()

    def _run(self, inputs, out_dir, pack, comp, overwrite) -> None:
        results: list[Report] = []
        for n, inp in enumerate(inputs, start=1):
            prefix = f"[{n}/{len(inputs)}] "
            self.queue.put(("log", f"{prefix}{inp}"))

            def progress(done: int, total: int, msg: str, prefix=prefix) -> None:
                self.queue.put(("progress", done, total, prefix + msg))

            rep = convert(inp, output_dir=out_dir, pack=pack, compression=comp, overwrite=overwrite,
                          progress=progress, cancel=self.cancel_event)
            results.append(rep)
            self.queue.put(("report", rep))
            if self.cancel_event.is_set():
                break
        self.queue.put(("done", results))

    def _cancel(self) -> None:
        self.cancel_event.set()
        self.status.set("中止しています...")

    def _poll(self) -> None:
        try:
            while True:
                item = self.queue.get_nowait()
                kind = item[0]
                if kind == "log":
                    self._append_log(item[1])
                elif kind == "progress":
                    _, done, total, msg = item
                    if total:
                        self.progress.configure(mode="determinate", value=done * 1000 // total)
                        self.status.set(f"{msg}  {done:,} / {total:,} メッセージ")
                    else:
                        self.status.set(msg)
                elif kind == "report":
                    self._show_report(item[1])
                elif kind == "done":
                    self._finish(item[1])
        except queue.Empty:
            pass
        self.root.after(100, self._poll)

    def _show_report(self, rep: Report) -> None:
        if rep.ok:
            self._append_log(f"  ✔ 成功: {rep.output}（{rep.total_out:,} メッセージ, {rep.elapsed:.1f} 秒）")
            self.last_output = rep.output
        else:
            self._append_log(f"  ✖ 失敗: {rep.error}")
        for w in rep.warnings:
            self._append_log(f"  ⚠ {w}")
        if rep.missing_types:
            self._append_log(f"  ⚠ 定義が見つからない型 {len(rep.missing_types)} 件（Lichtblick で表示できません）:")
            for t in rep.missing_types:
                self._append_log(f"      - {t}")
        if rep.output is not None:
            self._append_log(f"  ログ: {log_path_for(rep.output)}")

    def _finish(self, results: list[Report]) -> None:
        ok = sum(r.ok for r in results)
        ng = len(results) - ok
        self.start_btn.configure(state="normal")
        self.cancel_btn.configure(state="disabled")
        self.open_btn.configure(state="normal" if self.last_output else "disabled")
        self.progress.configure(value=1000 if ng == 0 else self.progress["value"])
        self.status.set(f"完了: 成功 {ok} 件 / 失敗 {ng} 件")
        self._append_log(f"=== 完了: 成功 {ok} 件 / 失敗 {ng} 件 ===\n")
        if ng:
            messagebox.showwarning("変換結果", f"成功 {ok} 件 / 失敗 {ng} 件\n詳細はログを確認してください。")
        else:
            messagebox.showinfo("変換結果", f"{ok} 件の変換が完了しました。")

    def _open_output(self) -> None:
        if self.last_output:
            open_in_explorer(self.last_output)

    def _on_close(self) -> None:
        if self.worker and self.worker.is_alive():
            if not messagebox.askyesno("終了", "変換中です。中止して終了しますか？"):
                return
            self.cancel_event.set()
            self.worker.join(timeout=10)
        self.root.destroy()


def main() -> None:
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:  # noqa: BLE001
            pass
    global TkinterDnD
    root = None
    if TkinterDnD:
        try:
            root = TkinterDnD.Tk()
        except Exception:  # noqa: BLE001  tkdnd ライブラリが読めない環境では D&D なしで起動
            TkinterDnD = None
    if root is None:
        root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
