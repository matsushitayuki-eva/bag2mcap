"""引数なしで起動すると GUI、引数ありならコマンドライン版として動く。"""

import os
import sys


def _ensure_console() -> None:
    """GUI 用 exe（コンソールなし）からコマンドライン版を使うとき、呼び出し元のコンソールに出力する。"""
    if sys.stdout is not None and sys.stderr is not None:
        return
    if sys.platform == "win32":
        try:
            import ctypes

            if ctypes.windll.kernel32.AttachConsole(-1):
                sys.stdout = open("CONOUT$", "w", encoding="utf-8", errors="replace")  # noqa: SIM115
                sys.stderr = sys.stdout
                return
        except Exception:  # noqa: BLE001
            pass
    sys.stdout = sys.stdout or open(os.devnull, "w")  # noqa: SIM115
    sys.stderr = sys.stderr or sys.stdout


def main() -> int:
    if len(sys.argv) > 1:
        _ensure_console()
        from .cli import main as cli_main

        return cli_main()
    from .gui import main as gui_main

    gui_main()
    return 0


if __name__ == "__main__":
    sys.exit(main())
