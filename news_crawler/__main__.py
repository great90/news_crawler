"""CLI 入口。S1 提供 search 调试子命令；run 子命令随 S2 加入。"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import date

from .config import APP_DIR, Config, ConfigError, MissingConfigError, load
from .mailer import MailError, Mailer
from .pusher import notify
from .renderer import render_digest_html
from .runner import CapExceededError, build_digest, write_archive
from .search_client import BigModelSearchClient, DeepSeekSearchClient, SearchError
from .social_source import DailyHotApiClient, PLATFORM_LABELS
from .state import RunState
from .summarizer import SummarizeError, Summarizer
from .topics import TOPIC_LABELS, build_queries

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_CONFIG = 2


def _setup_logging(verbose: bool) -> None:
    """FR-9：控制台 + logs/YYYYMMDD.log 双写，计划任务场景可事后回溯定位环节。"""
    handlers = [logging.StreamHandler()]
    log_dir = os.path.join(APP_DIR, "logs")
    os.makedirs(log_dir, exist_ok=True)
    handlers.append(
        logging.FileHandler(os.path.join(log_dir, f"{date.today().isoformat()}.log"), encoding="utf-8")
    )
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
    )


def cmd_search(args: argparse.Namespace) -> int:
    try:
        config = load(profile="search")
    except MissingConfigError as exc:
        print("配置缺失，请检查 news_crawler/.env（模板见 .env.example）：", file=sys.stderr)
        for key in exc.missing:
            print(f"  - {key}", file=sys.stderr)
        return EXIT_CONFIG
    except ConfigError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return EXIT_CONFIG

    client = DeepSeekSearchClient(api_key=config.deepseek_api_key, base_url=config.deepseek_base_url)
    try:
        day = date.fromisoformat(args.date) if args.date else date.today()
    except ValueError:
        print("--date 格式应为 YYYY-MM-DD", file=sys.stderr)
        return EXIT_ERROR
    try:
        queries = build_queries(args.topic, day)
    except ValueError as exc:
        print(f"查询词构造失败：{exc}", file=sys.stderr)
        return EXIT_ERROR

    results: list[dict] = []
    seen_urls: set[str] = set()
    try:
        for query in queries:  # 与正式流程一致：双查询按 URL 合并
            for item in client.search(query, count=args.count):
                url = (item.get("url") or "").strip().rstrip("/").lower()
                if url and url in seen_urls:
                    continue
                seen_urls.add(url)
                results.append(item)
    except SearchError as exc:
        print(f"搜索失败：{exc}", file=sys.stderr)
        return EXIT_ERROR

    print(f"\n[{TOPIC_LABELS[args.topic]}] {day} 查询组（窗口 since {queries[0].split(' after:')[1].split(' lang')[0]}）：")
    for query in queries:
        print(f"  - {query}")
    print(f"合并后共 {len(results)} 条结果\n")
    for index, item in enumerate(results, 1):
        snippet = (item.get("snippet") or "").replace("\n", " ")
        print(f"{index}. {item.get('title', '')}")
        print(f"   {item.get('url', '')}")
        print(
            f"   date={item.get('date')} rerank={item.get('rerank_score')} "
            f"authority={item.get('authority')} site={item.get('site_name', '')}"
        )
        print(f"   {snippet[:120]}\n")
    return EXIT_OK


def cmd_run(args: argparse.Namespace) -> int:
    profile = "search" if args.dry_run else "full"
    try:
        config = load(profile=profile)
    except MissingConfigError as exc:
        print("配置缺失，请检查 news_crawler/.env（模板见 .env.example）：", file=sys.stderr)
        for key in exc.missing:
            print(f"  - {key}", file=sys.stderr)
        return EXIT_CONFIG
    except ConfigError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return EXIT_CONFIG

    try:
        day = date.fromisoformat(args.date) if args.date else date.today()
    except ValueError:
        print("--date 格式应为 YYYY-MM-DD", file=sys.stderr)
        return EXIT_ERROR

    if not args.dry_run and RunState().is_done(day):
        print(f"{day} 已成功发送，按幂等语义退出（FR-6）。")
        return EXIT_OK

    if config.search_provider == "deepseek":
        search_client = DeepSeekSearchClient(api_key=config.deepseek_api_key, base_url=config.deepseek_base_url)
    else:
        search_client = BigModelSearchClient(api_key=config.deepseek_api_key)
    summarizer = Summarizer(api_key=config.deepseek_api_key, base_url=config.deepseek_base_url,
                            model=config.llm_model)
    social_client = None
    if config.social_enabled:
        social_client = DailyHotApiClient(
            base_url=config.social_api_base,
            platforms=config.social_platforms,
            count=config.social_count,
        )
    try:
        digest, raw_by_topic = build_digest(
            day,
            search_client,
            summarizer,
            count=config.topic_count,
            call_cap=config.daily_call_cap,
            social_client=social_client,
        )
    except (SearchError, SummarizeError, CapExceededError) as exc:
        return _fail_with_alarm(config, day, exc, stage="采集/摘要")

    archive_path = write_archive(digest, raw_by_topic)
    _print_digest(digest)

    if args.dry_run:
        print(f"\n[dry-run] 已存档至 {archive_path}，未发送邮件。")
        return EXIT_OK

    try:
        mailer = _build_mailer(config)
        mailer.send_html(subject=f"每日情报 {digest.day}", html=render_digest_html(digest))
    except MailError as exc:
        return _fail_with_alarm(config, day, exc, stage="发送")

    RunState().mark_success(day)
    _notify_digest(config, digest)
    print(f"\n✅ 已发送至 {config.mail_to}；存档：{archive_path}")
    return EXIT_OK


def _notify_digest(config: Config, digest) -> None:
    """投递成功后按 PUSH_CHANNELS 推手机摘要；失败仅记日志，不影响主流程。"""
    if not config.push_channels:
        return
    news = sum(len(s.items) for s in digest.sections if s.kind == "news" and not s.error)
    social = sum(len(s.items) for s in digest.sections if s.kind == "social" and not s.error)
    degraded = [s.label for s in digest.sections if s.error]
    body = f"新闻 {news} 条；社媒 {social} 条" + (f"；降级：{'、'.join(degraded)}" if degraded else "")
    desp = f"**每日情报 {digest.day}**\n\n- 新闻 {news} 条\n- 社媒 {social} 条"
    if degraded:
        desp += f"\n\n> 降级：{'、'.join(degraded)}"
    notify(config.push_channels, config.bark_url, config.serverchan_send_key,
           title=f"每日情报 {digest.day} 已送达", body=body, desp_md=desp)


def _build_mailer(config: Config) -> Mailer:
    return Mailer(
        host=config.smtp_host,
        port=config.smtp_port,
        user=config.smtp_user,
        auth_code=config.smtp_auth_code,
        sender=config.mail_sender,
        recipient=config.mail_to,
    )


def _fail_with_alarm(config: Config, day, exc: Exception, stage: str) -> int:
    """FR-7：最终失败写日志；邮件通道可用时另发一封失败告警。"""
    print(f"运行失败（{stage}）：{exc}", file=sys.stderr)
    try:
        _build_mailer(config).send_text(
            subject=f"【news_crawler 失败告警】{day} {stage}失败",
            body=f"每日情报 {day} 在「{stage}」环节重试后仍失败：\n{exc}\n\n请查看日志与 .env 配置。",
        )
        print("失败告警邮件已发送。", file=sys.stderr)
    except Exception as alarm_error:  # 告警失败只记日志，不掩盖原始错误
        print(f"告警邮件发送失败（忽略）：{alarm_error}", file=sys.stderr)
    return EXIT_ERROR


def _print_digest(digest) -> None:
    print(f"\n==== 每日情报 {digest.day} ====")
    for section in digest.sections:
        if section.error:
            print(f"\n【{section.label}】❌ 暂不可用（{section.error}）")
            continue
        if not section.items:
            print(f"\n【{section.label}】本日无新增")
            continue
        print(f"\n【{section.label}】{len(section.items)} 条")
        for index, item in enumerate(section.items, 1):
            hot = f"  🔥{item.hot}" if item.hot else ""
            print(f" {index}. {item.title}{hot}")
            if item.points:
                print(f"    {item.points}")
            print(f"    {item.url}")
    stats = digest.stats
    line = (
        f"\n统计：新闻精选 {stats['final_total']} 条"
        f"；搜索 {stats['search_calls']} 次、LLM {stats['llm_calls']} 次"
    )
    if "social" in stats:
        line += f"；社媒精选 {stats['social']['final_total']} 条"
    print(line)


def cmd_social(args: argparse.Namespace) -> int:
    """调试：拉取全部启用平台的实时热榜（不经 LLM、不发邮件）。"""
    try:
        config = load(profile="search")
    except MissingConfigError as exc:
        print("配置缺失，请检查 news_crawler/.env：", file=sys.stderr)
        for key in exc.missing:
            print(f"  - {key}", file=sys.stderr)
        return EXIT_CONFIG
    if not config.social_enabled:
        print("SOCIAL_ENABLED 未启用，请先在 .env 中开启。", file=sys.stderr)
        return EXIT_CONFIG

    client = DailyHotApiClient(
        base_url=config.social_api_base, platforms=config.social_platforms, count=config.social_count
    )
    failed = 0
    for result in client.fetch_all():
        label = PLATFORM_LABELS.get(result.platform, result.platform)
        if result.error:
            failed += 1
            print(f"\n【{label}】❌ 暂不可用：{result.error}")
            continue
        print(f"\n【{label}】{len(result.items)} 条")
        for index, item in enumerate(result.items, 1):
            print(f" {index}. {item.title}  🔥{item.hot or '-'}")
            print(f"    {item.url}")
    print(f"\n平台可用 {len(config.social_platforms) - failed}/{len(config.social_platforms)}")
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="news_crawler", description="每日四主题资讯摘要邮件")
    subparsers = parser.add_subparsers(dest="command", required=True)

    p_search = subparsers.add_parser("search", help="调试：单主题当日搜索")
    p_search.add_argument("--topic", required=True, choices=sorted(TOPIC_LABELS))
    p_search.add_argument("--count", type=int, default=10)
    p_search.add_argument("--date", help="覆盖查询日期 YYYY-MM-DD（默认今天）")
    p_search.add_argument("-v", "--verbose", action="store_true")
    p_search.set_defaults(func=cmd_search)

    p_run = subparsers.add_parser("run", help="执行完整采集-摘要-发送流程")
    p_run.add_argument("--dry-run", action="store_true", help="只采集摘要并存档，不发送邮件")
    p_run.add_argument("--date", help="覆盖查询日期 YYYY-MM-DD（默认今天，调试用）")
    p_run.add_argument("-v", "--verbose", action="store_true")
    p_run.set_defaults(func=cmd_run)

    p_social = subparsers.add_parser("social", help="调试：拉取各平台实时热榜")
    p_social.add_argument("-v", "--verbose", action="store_true")
    p_social.set_defaults(func=cmd_social)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _setup_logging(getattr(args, "verbose", False))
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
