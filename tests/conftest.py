import sys
from pathlib import Path

# 定位项目根目录与其下的 src 目录，供测试直接导入源码模块
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_SRC_DIR = _PROJECT_ROOT / "src"
# 仅在尚未加入时插入，避免重复修改 sys.path
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))
