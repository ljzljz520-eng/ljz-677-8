"""运行配置：全部可通过环境变量覆盖。"""
import os

# 平台自身服务地址 / 端口
APP_HOST = os.environ.get("APP_HOST", "0.0.0.0")
APP_PORT = int(os.environ.get("APP_PORT", "8000"))

# 监管接口（默认指向同进程内置的监管端模拟器）
REGULATOR_URL = os.environ.get("REGULATOR_URL", "http://127.0.0.1:8001/v1/prescriptions")
REGULATOR_TIMEOUT = float(os.environ.get("REGULATOR_TIMEOUT", "10"))
ORG_CODE = os.environ.get("ORG_CODE", "H110108001")

# 每批（每个监管请求）最多多少行
CHUNK_SIZE = int(os.environ.get("CHUNK_SIZE", "20"))

# SQLite 数据库文件
DATA_DIR = os.environ.get("DATA_DIR", os.path.join(os.path.dirname(os.path.dirname(__file__)), "data"))
DB_PATH = os.environ.get("DB_PATH", os.path.join(DATA_DIR, "platform.db"))

# 内置监管端模拟器
MOCK_HOST = os.environ.get("MOCK_HOST", "127.0.0.1")
MOCK_PORT = int(os.environ.get("MOCK_PORT", "8001"))
MOCK_DB_PATH = os.environ.get("MOCK_DB_PATH", os.path.join(DATA_DIR, "regulator.db"))
MOCK_LATENCY_MS = int(os.environ.get("MOCK_LATENCY_MS", "0"))  # 演示用网络延迟
