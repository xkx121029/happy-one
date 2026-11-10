"""仓库级 pytest 配置。

各线测试目录内可能有同目录辅助模块（如 tests/captcha/support.py），
importlib 导入模式下不会自动加入 sys.path；这里统一补充各测试子目录。
"""

from __future__ import annotations

import sys
from pathlib import Path

_TESTS_DIR = Path(__file__).parent / "tests"
if _TESTS_DIR.is_dir():
    for _path in sorted(_TESTS_DIR.iterdir()):
        if _path.is_dir() and str(_path) not in sys.path:
            sys.path.append(str(_path))
