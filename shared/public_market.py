# -*- coding: utf-8 -*-
"""兼容入口: 转发到 vendor/shared/public_market.py"""
from pathlib import Path
import runpy
_target = Path(__file__).resolve().parents[1] / "vendor" / "shared" / "public_market.py"
_globals = runpy.run_path(str(_target))
globals().update({k: v for k, v in _globals.items() if not k.startswith("__")})
