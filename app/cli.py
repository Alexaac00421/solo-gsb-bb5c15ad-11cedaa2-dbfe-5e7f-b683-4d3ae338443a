"""命令行入口：初始化数据、启动服务。

用法::

    python -m app.cli init-db                 # 建库并导入内置示例数据
    python -m app.cli import-graph graph.json
    python -m app.cli import-closures closures.json
    python -m app.cli restore 2             # 把历史版本 v2 整体恢复为新的生效版本
    python -m app.cli serve                 # 启动 HTTP 服务
"""
import argparse
import json
import sys
from pathlib import Path

from . import config
from .db import (
    VersionNotFoundError,
    connect,
    current_version,
    init_db,
    publish_closures,
    publish_graph,
    restore_version,
)
from .schemas import ValidationError


def _load_json(path: str):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"读取/解析 {path} 失败: {exc}", file=sys.stderr)
        raise SystemExit(2)


def cmd_init_db(args) -> int:
    conn = connect()
    init_db(conn)
    if current_version(conn) != 0 and not args.force:
        print(f"数据库已存在数据版本 v{current_version(conn)}，跳过初始化（--force 可重建）")
        return 0
    if args.force:
        conn.executescript(
            "DELETE FROM trip_segments; DELETE FROM trips; "
            "DELETE FROM closures; DELETE FROM reverse_windows; DELETE FROM edges; "
            "DELETE FROM nodes; DELETE FROM versions; "
            "UPDATE meta SET value='0' WHERE key='current_version';"
        )
    graph_file = args.graph or config.SEED_GRAPH_FILE
    closures_file = args.closures or config.SEED_CLOSURES_FILE
    if graph_file:
        v = publish_graph(conn, _load_json(graph_file))
        print(f"已发布步道图 v{v}")
    else:
        from .sample_data import SAMPLE_GRAPH

        v = publish_graph(conn, SAMPLE_GRAPH)
        print(f"已发布内置示例步道图 v{v}")
    if closures_file:
        v = publish_closures(conn, _load_json(closures_file))
        print(f"已发布封闭记录 v{v}")
    elif not graph_file:
        from .sample_data import SAMPLE_CLOSURES

        v = publish_closures(conn, SAMPLE_CLOSURES)
        print(f"已发布内置示例封闭记录 v{v}")
    print(f"数据库路径: {config.DB_PATH}")
    return 0


def cmd_import_graph(args) -> int:
    conn = connect()
    init_db(conn)
    old = current_version(conn)
    try:
        new = publish_graph(conn, _load_json(args.file))
    except ValidationError as exc:
        print(f"导入被拒绝（旧版本 v{old} 保留）: {exc}", file=sys.stderr)
        return 1
    print(f"步道图已原子发布: v{old} -> v{new}")
    return 0


def cmd_import_closures(args) -> int:
    conn = connect()
    init_db(conn)
    old = current_version(conn)
    try:
        new = publish_closures(conn, _load_json(args.file))
    except ValidationError as exc:
        print(f"导入被拒绝（旧版本 v{old} 保留）: {exc}", file=sys.stderr)
        return 1
    print(f"封闭记录已原子发布: v{old} -> v{new}")
    return 0


def cmd_restore(args) -> int:
    conn = connect()
    init_db(conn)
    old = current_version(conn)
    try:
        new = restore_version(conn, args.version)
    except (ValidationError, VersionNotFoundError) as exc:
        print(f"恢复被拒绝（当前版本 v{old} 保持不变）: {exc}", file=sys.stderr)
        return 1
    print(f"已把 v{args.version} 整体恢复为新的生效版本: v{old} -> v{new}")
    return 0


def cmd_serve(args) -> int:
    import uvicorn

    conn = connect()
    init_db(conn)
    # 本地直接启动时也支持种子初始化
    if current_version(conn) == 0:
        graph_file = config.SEED_GRAPH_FILE
        if graph_file:
            publish_graph(conn, _load_json(graph_file))
        elif config.SEED_SAMPLE:
            from .sample_data import SAMPLE_CLOSURES, SAMPLE_GRAPH

            publish_graph(conn, SAMPLE_GRAPH)
            publish_closures(conn, SAMPLE_CLOSURES)
        closures_file = config.SEED_CLOSURES_FILE
        if closures_file and current_version(conn) != 0:
            publish_closures(conn, _load_json(closures_file))
    conn.close()

    print(f"服务启动: http://{config.HTTP_HOST}:{config.HTTP_PORT}")
    uvicorn.run("app.main:app", host=config.HTTP_HOST, port=config.HTTP_PORT, reload=False)
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="app", description="园区访客通行规划服务")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_init = sub.add_parser("init-db", help="建库并初始化数据")
    p_init.add_argument("--graph", help="自定义步道图 JSON（默认内置示例）")
    p_init.add_argument("--closures", help="自定义封闭记录 JSON")
    p_init.add_argument("--force", action="store_true", help="清空旧数据后重建")
    p_init.set_defaults(func=cmd_init_db)

    p_g = sub.add_parser("import-graph", help="原子导入步道图 JSON")
    p_g.add_argument("file")
    p_g.set_defaults(func=cmd_import_graph)

    p_c = sub.add_parser("import-closures", help="原子导入封闭记录 JSON")
    p_c.add_argument("file")
    p_c.set_defaults(func=cmd_import_closures)

    p_r = sub.add_parser("restore", help="把历史版本整体恢复为新的生效版本")
    p_r.add_argument("version", type=int, help="要恢复的历史版本号（正整数）")
    p_r.set_defaults(func=cmd_restore)

    p_s = sub.add_parser("serve", help="启动 HTTP 服务")
    p_s.set_defaults(func=cmd_serve)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
