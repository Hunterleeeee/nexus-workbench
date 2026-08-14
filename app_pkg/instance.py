"""FastAPI 应用实例：所有领域模块共享的注册目标。

拆分里程碑：app 实例从 app.py 抽出，领域模块（含路由）通过
`from .instance import app` 注册自己的路由，app.py 只负责组装。

middleware / 静态挂载 / startup-shutdown 事件仍留在 app.py（import 本模块
的 app 后装饰即可）。
"""

from fastapi import FastAPI

from .core import WORKBENCH_VERSION

app = FastAPI(title="Workbench", version=WORKBENCH_VERSION)
