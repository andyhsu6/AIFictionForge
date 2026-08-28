"""pytest 配置：确保 backend 可导入，并固定数据库/模型导入顺序避免循环导入。"""
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

# 先完整初始化 app.database -> app.models 链，避免测试模块导入 service 时
# 出现 "cannot import name 'Project' from partially initialized module" 循环导入
import app.database  # noqa: E402,F401