"""门诊处方数据报送台 - 命令行入口。

用法：
  python -m rx_report init
  python -m rx_report import 文件.csv
  python -m rx_report submit [--batch-size 50] [--flaky 0.1] [--seed 42]
  python -m rx_report retry  [--batch-size 50] [--flaky 0.1] [--seed 42]
  python -m rx_report list [--status FAILED] [--limit 20]
  python -m rx_report stats
  python -m rx_report export 结果.csv
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .db import Repository
from .exporter import export_csv
from .importer import load_csv
from .models import STATUS_LABELS
from .regulator import RegulatorClient
from .service import submit

DEFAULT_DB = "rx_report.db"


def _client(args) -> RegulatorClient:
    flaky_rxnums = tuple(
        x.strip() for x in (args.flaky_rx or "").split(",") if x.strip())
    return RegulatorClient(
        flaky_rate=args.flaky, flaky_rxnums=flaky_rxnums, seed=args.seed)


def cmd_init(repo, args):
    repo.init_db()
    print(f"数据库已就绪: {Path(repo.db_path).resolve()}")


def cmd_import(repo, args):
    repo.init_db()
    rep = load_csv(args.file, repo)
    print(f"导入完成：新增 {rep.inserted} 行，"
          f"重复跳过 {rep.duplicates} 行，拒绝 {rep.rejected} 行")
    for e in rep.errors:
        print(f"  ! {e}")
    return 0 if rep.rejected == 0 else 2


def _run(repo, args, failed_only: bool) -> int:
    repo.init_db()
    client = _client(args)
    s = submit(repo, client, batch_size=args.batch_size,
               failed_only=failed_only)
    mode = "重送失败行" if failed_only else "全量报送"
    print(f"{mode}完成：共处理 {s.total} 行 / {s.batches} 个批次"
          + (f"（{s.batches_lost} 批传输失败）" if s.batches_lost else ""))
    print(f"  成功 {s.success}，业务失败 {s.failed}，"
          f"传输失败待重送 {s.transport_failed}")
    return 0


def cmd_submit(repo, args):
    return _run(repo, args, failed_only=False)


def cmd_retry(repo, args):
    return _run(repo, args, failed_only=True)


def cmd_list(repo, args):
    repo.init_db()
    rows = repo.list_rows(status=args.status, limit=args.limit)
    if not rows:
        print("（无记录）")
        return 0
    print(f"{'处方号':<12}{'患者':<10}{'药品':<16}{'状态':<8}"
          f"{'回执':<16}{'错误信息'}")
    for r in rows:
        print(f"{r.rx_no:<12}{r.patient_id:<10}{r.drug:<16}"
              f"{STATUS_LABELS.get(r.status, r.status):<8}"
              f"{(r.ack_code or '-'):<16}{r.error_message or ''}")
    print(f"\n共 {len(rows)} 行")
    return 0


def cmd_stats(repo, args):
    repo.init_db()
    c = repo.counts()
    print("处方报送统计")
    print(f"  总数     : {c['total']}")
    for st in ("PENDING", "SUCCESS", "FAILED", "TRANSPORT_FAILED"):
        print(f"  {STATUS_LABELS[st]:<6}: {c[st]}")
    return 0


def cmd_export(repo, args):
    repo.init_db()
    n = export_csv(repo, args.file)
    print(f"已导出 {n} 行到 {args.file}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="rx_report", description="门诊处方数据报送台")
    p.add_argument("--db", default=DEFAULT_DB, help="SQLite 数据库路径")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="初始化数据库")

    pi = sub.add_parser("import", help="导入处方 CSV")
    pi.add_argument("file")
    pi.set_defaults(func=cmd_import)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--batch-size", type=int, default=50,
                        help="每批行数（默认 50）")
    common.add_argument("--flaky", type=float, default=0.0,
                        help="模拟每批传输失败概率 0~1（默认 0）")
    common.add_argument("--flaky-rx", default="",
                        help="模拟首次传输失败的处方号，逗号分隔")
    common.add_argument("--seed", type=int, default=None,
                        help="随机故障种子（可复现）")

    ps = sub.add_parser("submit", parents=[common],
                        help="分批报送所有未成功行")
    ps.set_defaults(func=cmd_submit)

    pr = sub.add_parser("retry", parents=[common],
                        help="只重送失败行（成功行不重送）")
    pr.set_defaults(func=cmd_retry)

    pl = sub.add_parser("list", help="查看明细")
    pl.add_argument("--status", choices=[
        "PENDING", "SUCCESS", "FAILED", "TRANSPORT_FAILED"], default=None)
    pl.add_argument("--limit", type=int, default=20)
    pl.set_defaults(func=cmd_list)

    sub.add_parser("stats", help="统计").set_defaults(func=cmd_stats)

    pe = sub.add_parser("export", help="导出结果 CSV")
    pe.add_argument("file")
    pe.set_defaults(func=cmd_export)

    sub.choices["init"].set_defaults(func=cmd_init)
    return p


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    repo = Repository(args.db)
    try:
        return args.func(repo, args)
    finally:
        repo.close()


if __name__ == "__main__":
    sys.exit(main())
