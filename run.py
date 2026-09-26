#!/usr/bin/env python3
"""一键启动：监管端模拟器(端口 8001) + 报送平台(端口 8000)。"""
import threading
import time

from app import config, db
from app.webapp import make_server as make_platform
from app.mock_regulator import make_server as make_regulator


def main():
    db.init_db()
    reg = make_regulator()
    threading.Thread(target=reg.serve_forever, daemon=True, name="mock-regulator").start()
    print(f"[监管端模拟器] http://{config.MOCK_HOST}:{config.MOCK_PORT}/v1/prescriptions")

    # 等监管端就绪
    time.sleep(0.2)
    platform = make_platform()
    print(f"[门诊处方报送台] http://127.0.0.1:{config.APP_PORT}/")
    print(f"  机构代码={config.ORG_CODE}  每片行数={config.CHUNK_SIZE}  数据库={config.DB_PATH}")
    print("  Ctrl+C 退出")
    try:
        platform.serve_forever()
    except KeyboardInterrupt:
        print("\n正在关闭...")
        reg.shutdown()
        platform.shutdown()


if __name__ == "__main__":
    main()
