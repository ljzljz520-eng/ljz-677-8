"""平台侧 SQLite 持久化层。

一个批次(batches)对应一次导入，内含多行处方(prescriptions)。
每行处方独立记录报送状态，支持"只重送失败行"。
"""
import json
import os
import sqlite3
import time
from contextlib import contextmanager

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS batches (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    filename      TEXT NOT NULL,
    total_rows    INTEGER NOT NULL DEFAULT 0,
    status        TEXT NOT NULL DEFAULT 'pending',
    created_at    TEXT NOT NULL,
    submitted_at  TEXT,
    UNIQUE(id)
);

CREATE TABLE IF NOT EXISTS prescriptions (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id         INTEGER NOT NULL REFERENCES batches(id),
    row_no           INTEGER NOT NULL,          -- 导入文件内的行号(含数据起始行)
    rx_no            TEXT,                     -- 处方号(监管幂等键)
    patient_id       TEXT,
    drug             TEXT,
    quantity         TEXT,                     -- 原始文本
    fee              TEXT,                     -- 原始文本
    raw              TEXT NOT NULL,            -- 原始行全部字段 JSON
    status           TEXT NOT NULL DEFAULT 'pending',
    error_code       TEXT,
    error_message    TEXT,
    regulator_id     TEXT,                     -- 监管端受理号
    attempts         INTEGER NOT NULL DEFAULT 0,
    last_sent_at     TEXT,
    last_result_at   TEXT,
    UNIQUE(batch_id, row_no)
);

CREATE INDEX IF NOT EXISTS idx_rx_batch ON prescriptions(batch_id, status);
CREATE INDEX IF NOT EXISTS idx_rx_no ON prescriptions(rx_no);

CREATE TABLE IF NOT EXISTS audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id    INTEGER,
    rx_no       TEXT,
    event       TEXT NOT NULL,     -- import / submit / retry / success / failed / invalid / blocked
    detail      TEXT,
    created_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_audit_batch ON audit_log(batch_id, id);
"""


def now_ts():
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())


def init_db(db_path=None):
    db_path = db_path or config.DB_PATH
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.executescript(SCHEMA)
        conn.commit()


@contextmanager
def get_conn(db_path=None):
    conn = sqlite3.connect(db_path or config.DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def log_audit(conn, event, batch_id=None, rx_no=None, detail=None):
    conn.execute(
        "INSERT INTO audit_log(batch_id, rx_no, event, detail, created_at) VALUES(?,?,?,?,?)",
        (batch_id, rx_no, event, detail if isinstance(detail, str) or detail is None
         else json.dumps(detail, ensure_ascii=False), now_ts()),
    )
