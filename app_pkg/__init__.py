"""Workbench 应用包。

为开源做准备：把原先 3.2 万行的单文件 app.py 渐进式拆成领域模块。
- core.py    无业务依赖的内核：路径、日志、DB、通用工具
- (后续)     各业务领域模块：aihot / knowledge / market / learning ...
- main.py    FastAPI 应用实例与启动（阶段二引入）
- __init__.py 统一 re-export，保持 `from app import x` / `uvicorn app:app` 兼容
"""

from .core import *  # noqa: F401,F403
