"""EdgeOne Pages 云函数入口（路由 /*）：加载仓库根目录的 index.py 并导出 Flask app。

若运行时环境无法访问仓库根目录文件，请将根目录 index.py 完整复制到本文件。
"""

import importlib.util
import os

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location(
    "mangadex_api_root", os.path.join(_ROOT, "index.py")
)
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)

app = _module.app
