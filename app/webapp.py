"""平台 Web 服务：REST API + 前端静态文件（标准库 http.server）。"""
import csv
import io
import json
import os
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import config, db
from .importer import import_batch
from . import reporter
from . import regulator_client

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")

STATUS_LABEL = {
    "pending": "待报送",
    "processing": "报送中",
    "success": "已受理",
    "failed": "报送失败",
    "invalid": "本地无效",
    "completed": "已完成",
    "completed_with_errors": "完成(有失败)",
}


def recover_stuck_batches():
    """进程启动时把崩溃遗留的 processing 批次恢复成可操作状态。"""
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT id FROM batches WHERE status='processing'").fetchall()
        for r in rows:
            # 逐行重新汇总，行级状态仍是最真实的进度
            from .importer import _recompute_batch_status
            _recompute_batch_status(conn, r["id"])
            db.log_audit(conn, "recover", batch_id=r["id"],
                         detail="检测到上次报送中断，已按行状态恢复批次")


def _row_dict(r):
    return {
        "id": r["id"], "row_no": r["row_no"], "rx_no": r["rx_no"],
        "patient_id": r["patient_id"], "drug": r["drug"],
        "quantity": r["quantity"], "fee": r["fee"],
        "status": r["status"], "status_label": STATUS_LABEL.get(r["status"], r["status"]),
        "error_code": r["error_code"], "error_message": r["error_message"],
        "regulator_id": r["regulator_id"], "attempts": r["attempts"],
        "last_sent_at": r["last_sent_at"], "last_result_at": r["last_result_at"],
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "RxPlatform/1.0"

    def log_message(self, fmt, *args):
        print("[http] " + (fmt % args))

    # ---------- 基础工具 ----------
    def _json(self, status, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self):
        length = int(self.headers.get("Content-Length", 0))
        return self.rfile.read(length) if length else b""

    # ---------- 路由 ----------
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)

        if path in ("/", "/index.html"):
            return self._serve_static("index.html", "text/html; charset=utf-8")
        if path.startswith("/static/"):
            return self._serve_file(path[len("/static/"):])

        if path == "/api/health":
            return self._json(200, {"ok": True})

        if path == "/api/batches":
            return self._list_batches()

        # /api/batches/<id>/...
        parts = [p for p in path.split("/") if p]
        if len(parts) >= 3 and parts[:2] == ["api", "batches"]:
            try:
                bid = int(parts[2])
            except ValueError:
                return self._json(400, {"error": "批次ID无效"})
            if len(parts) == 3:
                return self._batch_detail(bid)
            if parts[3] == "export.csv":
                return self._export_csv(bid)
            if parts[3] == "audit":
                return self._audit(bid)
            if parts[3] == "failed":
                return self._list_failed(bid)
        return self._json(404, {"error": "NOT_FOUND"})

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/api/batches":
            return self._create_batch()
        if path == "/api/mock/reset":
            try:
                regulator_client.admin_reset()
                return self._json(200, {"ok": True})
            except Exception as e:
                return self._json(502, {"error": f"监管端重置失败: {e}"})

        parts = [p for p in path.split("/") if p]
        if len(parts) == 4 and parts[:2] == ["api", "batches"]:
            try:
                bid = int(parts[2])
            except ValueError:
                return self._json(400, {"error": "批次ID无效"})
            if parts[3] == "submit":
                return self._run(bid, "submit")
            if parts[3] == "retry":
                return self._run(bid, "retry_failed")
        return self._json(404, {"error": "NOT_FOUND"})

    # ---------- 业务处理 ----------
    def _create_batch(self):
        data = self._read_body()
        filename = self.headers.get("X-Filename", "upload.csv")
        if not data:
            return self._json(400, {"error": "未收到文件内容"})
        with db.get_conn() as conn:
            batch_id, info = import_batch(conn, filename, data)
        if batch_id is None:
            return self._json(400, info)
        return self._json(201, info)

    def _list_batches(self):
        with db.get_conn() as conn:
            rows = conn.execute(
                """SELECT b.*,
                        SUM(p.status='pending')  AS n_pending,
                        SUM(p.status='success')  AS n_success,
                        SUM(p.status='failed')   AS n_failed,
                        SUM(p.status='invalid')  AS n_invalid
                   FROM batches b LEFT JOIN prescriptions p ON p.batch_id=b.id
                   GROUP BY b.id ORDER BY b.id DESC"""
            ).fetchall()
        out = []
        for r in rows:
            out.append({
                "id": r["id"], "filename": r["filename"],
                "total_rows": r["total_rows"],
                "status": r["status"], "status_label": STATUS_LABEL.get(r["status"], r["status"]),
                "created_at": r["created_at"], "submitted_at": r["submitted_at"],
                "n_pending": r["n_pending"] or 0, "n_success": r["n_success"] or 0,
                "n_failed": r["n_failed"] or 0, "n_invalid": r["n_invalid"] or 0,
            })
        self._json(200, {"batches": out})

    def _batch_detail(self, bid):
        with db.get_conn() as conn:
            b = conn.execute("SELECT * FROM batches WHERE id=?", (bid,)).fetchone()
            if b is None:
                return self._json(404, {"error": "批次不存在"})
            rows = conn.execute(
                "SELECT * FROM prescriptions WHERE batch_id=? ORDER BY row_no", (bid,)).fetchall()
        self._json(200, {
            "batch": {
                "id": b["id"], "filename": b["filename"], "total_rows": b["total_rows"],
                "status": b["status"], "status_label": STATUS_LABEL.get(b["status"], b["status"]),
                "created_at": b["created_at"], "submitted_at": b["submitted_at"],
            },
            "rows": [_row_dict(r) for r in rows],
        })

    def _list_failed(self, bid):
        with db.get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM prescriptions WHERE batch_id=? AND status='failed' ORDER BY row_no",
                (bid,)).fetchall()
        self._json(200, {"rows": [_row_dict(r) for r in rows]})

    def _run(self, bid, mode):
        try:
            body = json.loads(self._read_body() or b"{}")
        except json.JSONDecodeError:
            body = {}
        result = reporter.run(
            bid, mode=mode,
            chunk_size=int(body["chunk_size"]) if body.get("chunk_size") else None,
            force_flaky=bool(body.get("force_flaky")),
            force_outage=bool(body.get("force_outage")),
            delay_ms=int(body["delay_ms"]) if str(body.get("delay_ms", "")).isdigit() else 0,
        )
        code = 409 if result.get("blocked") else (404 if result.get("status") == 404 else 200)
        self._json(code, result)

    def _export_csv(self, bid):
        with db.get_conn() as conn:
            b = conn.execute("SELECT * FROM batches WHERE id=?", (bid,)).fetchone()
            if b is None:
                return self._json(404, {"error": "批次不存在"})
            rows = conn.execute(
                "SELECT * FROM prescriptions WHERE batch_id=? ORDER BY row_no", (bid,)).fetchall()

        buf = io.StringIO()
        buf.write("﻿")
        w = csv.writer(buf)
        w.writerow(["处方号", "患者编号", "药品", "数量", "费用",
                    "报送状态", "错误代码", "错误说明", "监管受理号", "尝试次数",
                    "末次发送时间", "末次结果时间"])
        for r in rows:
            w.writerow([r["rx_no"] or "", r["patient_id"], r["drug"], r["quantity"],
                        r["fee"], STATUS_LABEL.get(r["status"], r["status"]),
                        r["error_code"] or "", r["error_message"] or "",
                        r["regulator_id"] or "", r["attempts"],
                        r["last_sent_at"] or "", r["last_result_at"] or ""])
        data = buf.getvalue().encode("utf-8")
        fname = urllib.parse.quote(f"batch-{bid}-报送结果.csv")
        self.send_response(200)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{fname}")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _audit(self, bid):
        with db.get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM audit_log WHERE batch_id=? ORDER BY id DESC LIMIT 200",
                (bid,)).fetchall()
        self._json(200, {"log": [dict(r) for r in rows]})

    # ---------- 静态文件 ----------
    def _serve_static(self, name, ctype):
        path = os.path.join(STATIC_DIR, name)
        try:
            with open(path, "rb") as f:
                data = f.read()
        except FileNotFoundError:
            return self._json(404, {"error": "NOT_FOUND"})
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _serve_file(self, rel):
        # 防路径穿越
        rel = rel.lstrip("/")
        base = os.path.realpath(STATIC_DIR)
        path = os.path.realpath(os.path.join(base, rel))
        if not path.startswith(base + os.sep) or not os.path.isfile(path):
            return self._json(404, {"error": "NOT_FOUND"})
        ctype = {
            ".html": "text/html; charset=utf-8", ".js": "application/javascript; charset=utf-8",
            ".css": "text/css; charset=utf-8", ".csv": "text/csv; charset=utf-8",
            ".json": "application/json",
        }.get(os.path.splitext(path)[1], "application/octet-stream")
        with open(path, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def make_server(host=None, port=None):
    db.init_db()
    recover_stuck_batches()
    return ThreadingHTTPServer(
        (host if host is not None else config.APP_HOST,
         port if port is not None else config.APP_PORT), Handler)
