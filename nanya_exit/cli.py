"""命令列介面。

  python -m nanya_exit run            # 每日主流程（排程呼叫的就是這個）
  python -m nanya_exit run --offline  # 不連網，只用本地資料（沙箱測試用）
  python -m nanya_exit replay         # 從頭重建狀態
  python -m nanya_exit status         # 看目前狀態
  python -m nanya_exit mark-filled R1 --price 471.5 --date 2026-08-07
  python -m nanya_exit test-notify
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import Plan, Settings
from .engine import process_day, replay as do_replay
from .notify import NotifyError, push
from .report import console, notification
from .state import State
from .twse import refresh

log = logging.getLogger("nanya_exit")


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def _today(settings: Settings, override: str | None) -> dt.date:
    if override:
        return dt.date.fromisoformat(override)
    return dt.datetime.now(ZoneInfo(settings.timezone)).date()


def cmd_run(args) -> int:
    plan = Plan.load(args.plan)
    st = Settings()
    today = _today(st, args.date)
    state = State.load(st.state_path, plan)

    if state.closed and not args.force:
        log.info("部位已全部出清（%s），無事可做。", state.data.get("closed_reason"))
        return 0

    bars, has_today = refresh(
        plan.symbol, today, st.seed_csv, st.resolved_cache_csv(),
        timeout=st.http_timeout, offline=args.offline,
    )
    if not bars:
        log.error("完全沒有價格資料，無法計算。")
        return 2

    if not has_today:
        log.info("%s 沒有新的收盤資料（休市或證交所尚未更新），不推播。最新一筆：%s",
                 today, bars[-1].date)
        if not args.use_latest:
            return 0
        log.info("--use-latest：改用最新一筆 %s 繼續。", bars[-1].date)

    index = len(bars) - 1
    if has_today:
        index = next(i for i, b in enumerate(bars) if b.date == today.isoformat())

    if args.force and state.already_processed(bars[index].date):
        state.data["last_processed_date"] = None      # 允許重跑同一天

    res = process_day(state, bars, plan, index)
    print(console(res, state, plan))

    title, body, tags, priority = notification(res, state, plan)
    pushed, err = False, None
    if args.no_notify:
        log.info("--no-notify：略過推播。")
    elif res.skipped and not args.force:
        log.info("今日無需處理，略過推播。")
    else:
        try:
            push(st.ntfy_server, st.ntfy_topic, title, body, tags, priority,
                 st.ntfy_token, st.http_timeout, dry_run=args.dry_run)
            pushed = True
            if not args.dry_run:
                state.data["last_notified_date"] = res.date
                log.info("已推播到 %s/%s", st.ntfy_server, st.ntfy_topic)
        except NotifyError as e:
            err = str(e)
            log.error("%s", e)
            print("\n── 推播失敗，訊息內容如下（可手動轉貼）──")
            print(f"[{title}]\n{body}")

    state.log_run({
        "ran_at": dt.datetime.now(ZoneInfo(st.timezone)).isoformat(timespec="seconds"),
        "bar_date": res.date, "close": res.snap.close,
        "fills": [f.tranche_id for f in res.fills],
        "queued": [o.tranche_id for o in res.queued],
        "remaining": state.remaining,
        "notified": pushed, "notify_error": err,
    })
    if not args.dry_run:
        state.save(st.state_path)
    return 0 if (pushed or args.no_notify or args.dry_run or res.skipped) else 1


def cmd_replay(args) -> int:
    plan = Plan.load(args.plan)
    st = Settings()
    today = _today(st, args.date)
    bars, _ = refresh(plan.symbol, today, st.seed_csv, st.resolved_cache_csv(),
                      timeout=st.http_timeout, offline=args.offline)
    state = State.new(plan)
    results = do_replay(state, bars, plan, upto=args.upto)

    print(f"重播 {len(results)} 個交易日"
          f"（{plan.start_date} 之後 → {results[-1].date if results else '無'}）\n")
    for r in results:
        if r.fills or r.queued or r.notes:
            print(f"  {r.date}  收 {r.snap.close:>7.1f}", end="")
            bits = [f"{f.tranche_id}×{f.lots}@{f.price:.1f}" for f in r.fills]
            bits += [f"排隊:{o.tranche_id}" for o in r.queued]
            print("   " + "  ".join(bits) if bits else "")
    print()
    print(console(results[-1], state, plan) if results else "（起算日之後還沒有交易日）")

    if args.write:
        state.save(st.state_path)
        print(f"\n已寫入 {st.state_path}")
    else:
        print("\n（未寫檔；加 --write 才會覆蓋 state.json）")
    return 0


def cmd_status(args) -> int:
    plan = Plan.load(args.plan)
    st = Settings()
    state = State.load(st.state_path, plan)
    d = state.data
    print(f"{plan.name} {plan.symbol}　總 {d['total_lots']} 張　"
          f"已出 {state.sold_lots}　剩 {state.remaining}"
          + ("　【已出清】" if state.closed else ""))
    print(f"最後處理日：{d['last_processed_date']}　最後推播日：{d['last_notified_date']}")
    avg = state.realised_avg_price()
    if avg:
        print(f"已實現均價：{avg:.2f}")
    print("\n批次：")
    for t in d["tranches"]:
        trig = t["trigger"]
        desc = (f"{trig.get('price'):.0f}" if trig.get("price")
                else f"bias≥{trig.get('threshold'):.0%}" if trig.get("threshold")
                else f"k={trig.get('k')}")
        lots = "剩餘" if t["lots"] is None else f"{t['lots']:>2}"
        fill = (f"@ {t['fill_price']:.1f} ({t['fill_date']})"
                if t["status"] == "filled" else "")
        print(f"  {t['id']:<5} {t['engine']:<17} {desc:<10} {lots} 張  "
              f"{t['status']:<10}{fill}")
    if d["pending_next_session"]:
        print("\n明日開盤待執行：")
        for o in d["pending_next_session"]:
            print(f"  {o['tranche_id']}  {o['reason']}")
    if args.json:
        print("\n" + json.dumps(d, ensure_ascii=False, indent=2))
    return 0


def cmd_mark_filled(args) -> int:
    """人工修正：券商實際成交價與規則價不同時用這個。"""
    plan = Plan.load(args.plan)
    st = Settings()
    state = State.load(st.state_path, plan)
    t = state.tranche(args.tranche_id)
    if t is None:
        print(f"找不到批次 {args.tranche_id}", file=sys.stderr)
        return 2
    lots = args.lots if args.lots is not None else (t["lots"] or state.remaining)
    f = state.fill(args.tranche_id, lots, args.price, args.date, "人工標記成交")
    state.save(st.state_path)
    print(f"已標記 {f.tranche_id}：{f.lots} 張 @ {f.price}（{f.date}），剩 {state.remaining} 張")
    return 0


def cmd_test_notify(args) -> int:
    st = Settings()
    plan = Plan.load(args.plan)
    try:
        push(st.ntfy_server, st.ntfy_topic,
             f"{plan.name} {plan.symbol} 連線測試",
             f"排程機器人已就緒\ntopic：{st.ntfy_topic}\n"
             f"每日 {st.run_at}（{st.timezone}）執行",
             "white_check_mark", 3, st.ntfy_token, st.http_timeout, args.dry_run)
        print(f"已送出到 {st.ntfy_server}/{st.ntfy_topic}")
        return 0
    except NotifyError as e:
        print(e, file=sys.stderr)
        return 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="nanya_exit", description="南亞科 2408 分批出場機器人")
    p.add_argument("--plan", default=None, help="plan.json 路徑")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="每日主流程")
    r.add_argument("--date", help="指定交易日（YYYY-MM-DD），預設今天")
    r.add_argument("--offline", action="store_true", help="不連網，只用本地資料")
    r.add_argument("--dry-run", action="store_true", help="不推播、不寫狀態")
    r.add_argument("--no-notify", action="store_true")
    r.add_argument("--force", action="store_true", help="即使已處理過也重跑")
    r.add_argument("--use-latest", action="store_true",
                   help="今日無資料時改用最新一筆（測試用）")
    r.set_defaults(func=cmd_run)

    rp = sub.add_parser("replay", help="從計畫起算日重建狀態")
    rp.add_argument("--date", help="今天是哪天（影響抓資料範圍）")
    rp.add_argument("--upto", help="重播到哪一天為止")
    rp.add_argument("--offline", action="store_true")
    rp.add_argument("--write", action="store_true", help="把結果寫入 state.json")
    rp.set_defaults(func=cmd_replay)

    s = sub.add_parser("status", help="顯示目前狀態")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_status)

    m = sub.add_parser("mark-filled", help="人工標記某批已成交")
    m.add_argument("tranche_id")
    m.add_argument("--price", type=float, required=True)
    m.add_argument("--date", required=True)
    m.add_argument("--lots", type=int)
    m.set_defaults(func=cmd_mark_filled)

    t = sub.add_parser("test-notify", help="送一則測試推播")
    t.add_argument("--dry-run", action="store_true")
    t.set_defaults(func=cmd_test_notify)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    _setup_logging(args.verbose)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
