"""rebas 命令行入口。

  rebas status    查看配置与数据池状态
  rebas collect   跑一轮采集（M1）
  rebas publish   出刊管线：主编 → 核查 → 撰写 → 渲染（M3/M4）
  rebas render    只重渲染，不重跑 agent（M4）
"""

from __future__ import annotations

from collections import Counter

import typer

from rebas import config as cfg
from rebas import db as database

app = typer.Typer(help="rebas_daily — 个人每日信息刊物管线", no_args_is_help=True)


@app.command()
def status() -> None:
    """显示配置概况与原始池统计。"""
    conf = cfg.load_config()
    sources = cfg.load_sources()
    enabled = [s for s in sources if s.enabled]
    by_board = Counter(s.board for s in enabled)
    boards_str = ", ".join(f"{b} {n}" for b, n in sorted(by_board.items())) or "无"

    typer.echo("rebas_daily 状态")
    typer.echo(f"  信息源: 共 {len(sources)} 个，启用 {len(enabled)} 个（{boards_str}）")
    typer.echo(f"  出刊板块: {', '.join(conf.publish_boards)}")
    typer.echo(f"  LLM 后端: {conf.llm_backend} (CODEX_HOME={conf.codex_home})")

    for board in conf.publish_boards:
        profile = cfg.load_profile(board)
        typer.echo(
            f"  画像[{board}]: {len(profile.interests)} 个方向, "
            f"预筛关键词 {len(profile.all_keywords())} 个"
        )

    conn = database.init_db(conf.db_path)
    total = conn.execute("SELECT COUNT(*) FROM raw_items").fetchone()[0]
    typer.echo(f"  数据库: {conf.db_path}（raw_items {total} 条）")
    if total:
        rows = conn.execute(
            "SELECT board, status, COUNT(*) AS n FROM raw_items GROUP BY board, status"
        ).fetchall()
        for r in rows:
            typer.echo(f"    - {r['board']}/{r['status']}: {r['n']}")
    conn.close()


@app.command()
def collect(
    force: bool = typer.Option(False, "--force", help="忽略抓取间隔，全部源立即抓一轮"),
) -> None:
    """跑一轮采集入库：到期源 → 并发抓取 → 解析 → 去重入库。"""
    from rebas.collect.runner import run_collect

    stats = run_collect(force=force)
    ran = [s for s in stats if s.status not in ("unsupported",)]
    skipped_srcs = len(stats) - len(ran)
    typer.echo(f"采集完成：{len(ran)} 个源" + (f"（{skipped_srcs} 个类型未支持，跳过）" if skipped_srcs else ""))
    for s in ran:
        mark = {"ok": "✓", "304": "=", "error": "✗"}.get(s.status, "?")
        typer.echo(f"  {mark} {s.source_id:22s} {s.counts_line()}")
    total_new = sum(s.new + s.revived for s in ran)
    errors = sum(1 for s in ran if s.status == "error")
    typer.echo(f"合计新增 {total_new} 条" + (f"；{errors} 个源出错" if errors else ""))
    if errors:
        raise typer.Exit(1)


@app.command()
def backfill(
    date: str = typer.Option(..., "--date", help="回填日期 YYYY-MM-DD（arXiv 按提交日）"),
    source: str = typer.Option(None, "--source", help="只回填指定源 id（默认全部启用的 arXiv 源）"),
) -> None:
    """arXiv 按日回填——调整画像关键词后的后悔药。"""
    from rebas.collect.runner import run_backfill

    for s in run_backfill(date, source_id=source):
        typer.echo(f"回填 {date} [{s.source_id}]: {s.counts_line()}")


@app.command()
def prune(
    days: int = typer.Option(7, "--days", help="清空 N 天前条目的大字段（保留题录）"),
    vacuum: bool = typer.Option(False, "--vacuum", help="随后执行 VACUUM 回收空间"),
) -> None:
    """存储瘦身：出刊窗口之外的条目只留题录（title/来源/信号）。"""
    from rebas.collect.runner import run_prune

    count = run_prune(days, vacuum=vacuum)
    typer.echo(f"已瘦身 {count} 条（>{days} 天前的大字段已清空）")


@app.command()
def enrich() -> None:
    """出刊窗口内条目的外部指标增补（OpenAlex 作者 h 指数等），publish 也会自动跑。"""
    from rebas.agents.stages import stage_enrich

    conf = cfg.load_config()
    conn = database.init_db(conf.db_path)
    for board in conf.publish_boards:
        s = stage_enrich(conn, conf, board)
        typer.echo(f"[enrich] {board}: {s}")
    conn.close()


@app.command()
def publish(
    date: str = typer.Option(None, "--date", help="出刊日期 YYYY-MM-DD，默认今天（自动续跑未完成期次）"),
    force_stage: str = typer.Option(
        None, "--force-stage",
        help="从指定阶段强制重跑: enrich|screen|editor|fetch|checker|writer|render"),
    boards: str = typer.Option(
        None, "--boards",
        help="逗号分隔板块列表=部分模式（cron 分批备刊）：只跑这些板块，不推进状态不渲染"),
) -> None:
    """出刊管线：粗筛 → 主编 → 取材 → 核查 → 撰写 → 渲染。"""
    from rebas.pipeline import run_publish

    board_list = [b.strip() for b in boards.split(",") if b.strip()] if boards else None
    status = run_publish(date=date, force_stage=force_stage, boards=board_list,
                         log=typer.echo)
    typer.echo(f"publish 完成，issue status = {status}")


@app.command()
def render() -> None:
    """只重渲染静态页，不重跑 agent（调模板/样式/前端代码后用）。

    全量重建：导出所有期次 JSON → Astro 构建 web/ → site/。
    """
    from rebas.render.export import build_site

    conf = cfg.load_config()
    conn = database.init_db(conf.db_path)
    row = conn.execute(
        "SELECT issue_date FROM issues WHERE status IN ('written','rendered')"
        " ORDER BY issue_date DESC LIMIT 1").fetchone()
    if row is None:
        typer.echo("没有可渲染的期号（先跑 rebas publish）")
        raise typer.Exit(1)
    s = build_site(conn, conf)
    typer.echo(f"渲染完成: {s}")
    typer.echo(f"输出目录: {conf.site_dir}/index.html")


@app.command("admin-seed")
def admin_seed(
    email: str = typer.Option(..., "--email", help="管理后台登录邮箱"),
    password: str = typer.Option(..., "--password", prompt=True, hide_input=True,
                                 help="登录密码（不传参会交互式输入，不进 shell 历史）"),
) -> None:
    """创建/重置管理后台账号（scrypt 散列入库，不存明文）。"""
    from rebas.admin.auth import hash_password
    from rebas.collect.base import utcnow_iso

    conf = cfg.load_config()
    conn = database.init_db(conf.db_path)
    salt, h = hash_password(password)
    conn.execute(
        "INSERT INTO admin_users (email, pw_salt, pw_hash, created_at) VALUES (?,?,?,?)"
        " ON CONFLICT(email) DO UPDATE SET pw_salt=excluded.pw_salt,"
        " pw_hash=excluded.pw_hash",
        (email.strip().lower(), salt, h, utcnow_iso()))
    conn.commit()
    conn.close()
    typer.echo(f"✔ 管理账号已就绪: {email.strip().lower()}")


@app.command("admin-serve")
def admin_serve(
    host: str = typer.Option("127.0.0.1", "--host",
                             help="监听地址（保持回环，经 SSH 隧道或 CF Tunnel 访问）"),
    port: int = typer.Option(8787, "--port"),
) -> None:
    """启动管理后台（生产用 systemd 单元 scripts/rebas-admin.service）。"""
    import uvicorn

    uvicorn.run("rebas.admin.app:app", host=host, port=port, log_level="warning")


if __name__ == "__main__":
    app()
