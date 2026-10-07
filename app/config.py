"""运行期配置，全部来自环境变量，均有默认值。"""
import os
from pathlib import Path


def _env(name: str, default: str) -> str:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


# SQLite 数据库文件路径；容器内默认 /data/routing.db
DB_PATH = Path(_env("APP_DB_PATH", str(Path(__file__).resolve().parent.parent / "data" / "routing.db")))

# HTTP 服务监听地址 / 端口
HTTP_HOST = _env("APP_HOST", "0.0.0.0")
HTTP_PORT = int(_env("APP_PORT", "8080"))

# 启动时若数据库为空，自动导入种子图 / 封闭记录（JSON 文件路径，留空则不导入）
SEED_GRAPH_FILE = _env("APP_SEED_GRAPH", "")
SEED_CLOSURES_FILE = _env("APP_SEED_CLOSURES", "")

# 自动初始化：种子图为空时是否使用内置示例数据
SEED_SAMPLE = _env("APP_SEED_SAMPLE", "1") not in ("0", "false", "False", "no")
