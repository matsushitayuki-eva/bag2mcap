"""PyInstaller 用のエントリポイント。"""

import sys

from bag2mcap.__main__ import main

if __name__ == "__main__":
    sys.exit(main())
