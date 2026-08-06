"""命令列介面。

  python -m nanya_exit run            # 每日主流程（排程呼叫的就是這個）
  python -m nanya_exit run --offline  # 不連網，只用本地資料（沙箱測試用）
  python -m nanya_exit replay         # 從頭重建狀態
  python -m nanya_exit status         # 看目前狀態
  python -m nanya_exit mark-filled R1 --price 471.5 --date 2026-08-07
  python -m nanya_exit dequeue SLOW --reason "臨時決定不出清"
  python -m nanya_exit unfill R1 --reason "當天忘了掛單，沒真的成交"
  python -m nanya_exit chart-data --out docs/chart-data.json
  python -m nanya_exit backtest --paths 200
  python -m nanya_exit test-notify
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import math
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

from .backtest import SCENARIOS, compare_strategies
from .config import Plan, Settings
from .engine import process_day, replay as do_replay
from .indicators import snapshot
from .notify import NotifyError, push
from .report import console, notification
from .state import State
from .strategies import STRATEGIES
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

    title, body, tags, priority = notification(res, state, plan, st.chart_url)
    pushed, err = False, None
    if args.no_notify:
        log.info("--no-notify：略過推播。")
    elif res.skipped and not args.force:
        log.info("今日無需處理，略過推播。")
    else:
        try:
            push(st.ntfy_server, st.ntfy_topic, title, body, tags, priority,
                 st.ntfy_token, st.http_timeout, dry_run=args.dry_run,
                 click=st.chart_url)
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


def cmd_dequeue(args) -> int:
    """撤掉一筆收盤已觸發、明日開盤還沒真的執行的排隊單（還沒寫進 fills，最安全的介入點）。"""
    plan = Plan.load(args.plan)
    st = Settings()
    state = State.load(st.state_path, plan)
    try:
        result = state.dequeue_next_session(args.tranche_id, args.reason)
    except (KeyError, ValueError) as e:
        print(e, file=sys.stderr)
        return 2
    state.save(st.state_path)
    msg = f"已撤掉排隊單 {args.tranche_id}"
    if result["restored"]:
        msg += f"，並復原被連帶取消的批次：{', '.join(result['restored'])}"
    print(msg)
    return 0


def cmd_unfill(args) -> int:
    """撤銷一筆已經被標記成交、但實際沒有成交的批次（人工校正用）。"""
    plan = Plan.load(args.plan)
    st = Settings()
    state = State.load(st.state_path, plan)
    try:
        result = state.unfill(args.tranche_id, args.reason)
    except (KeyError, ValueError) as e:
        print(e, file=sys.stderr)
        return 2
    state.save(st.state_path)
    removed = result["removed"]
    print(f"已撤銷 {args.tranche_id} 的成交紀錄："
         f"{removed['lots']} 張 @ {removed['price']}（{removed['date']}），"
         f"剩 {state.remaining} 張")
    return 0


def cmd_chart_data(args) -> int:
    """把近況（收盤價序列 + 當下算出的出場價位）寫成靜態 JSON，給 docs/index.html 抓。

    跟 state.json／實際持倉完全無關——只讀市場資料算指標，方便給
    GitHub Actions 排程呼叫，不會動到部位追蹤。
    """
    plan = Plan.load(args.plan)
    st = Settings()
    today = _today(st, args.date)
    bars, _ = refresh(plan.symbol, today, st.seed_csv, st.resolved_cache_csv(),
                      timeout=st.http_timeout, offline=args.offline)
    if not bars:
        log.error("完全沒有價格資料，無法產生圖表資料。")
        return 2

    snap = snapshot(bars, plan, len(bars) - 1)
    recent = bars[-args.lookback:]

    up_lines = [{"price": r.price, "label": r.note, "id": r.id} for r in plan.ladder]
    down_lines = []
    if snap.fast_stop is not None:
        down_lines.append({"price": round(snap.fast_stop, 1),
                           "label": f"快停利 {plan.fast_k:.1f} ATR"})
    if snap.slow_stop is not None:
        down_lines.append({"price": round(snap.slow_stop, 1),
                           "label": f"慢停利 {plan.slow_k:.1f} ATR"})
    down_lines.append({"price": plan.hard_floor, "label": "硬地板"})

    bias_lines = []
    for b in plan.bias_steps:
        price = snap.bias_price(b.threshold)
        if price is not None:
            bias_lines.append({"price": round(price, 1),
                               "label": f"乖離 ≥{b.threshold:+.0%}", "id": b.id})

    data = {
        "generated_at": dt.datetime.now(ZoneInfo(st.timezone)).isoformat(timespec="seconds"),
        "symbol": plan.symbol,
        "name": plan.name,
        "as_of": snap.date,
        "series": [{"date": b.date, "close": b.close} for b in recent],
        "up_lines": up_lines,
        "down_lines": down_lines,
        "bias_lines": bias_lines,
    }

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"已寫入 {out}（{len(recent)} 筆，最新 {recent[-1].date}，"
         f"快停利 {_fmt(snap.fast_stop)}／慢停利 {_fmt(snap.slow_stop)}）")
    return 0


def _fmt(v: float | None) -> str:
    return "—" if v is None else f"{v:.1f}"


def cmd_backtest(args) -> int:
    """用 block bootstrap 合成多條路徑，比較候選策略的穩健度（不動 state.json）。

    合成路徑都從「今天實際收盤價」往前接，用近期歷史的振幅/跳空特性
    重新洗牌組出很多條劇本，同一組劇本讓每個策略都跑過一次——目的是
    看哪個策略在不同劇本下結果比較穩定，而不是只看單一數字最高。

    預設會把「偏空／中性／偏多」三種方向假設都跑一遍（--scenario 可只跑
    一種）——不是預測未來會怎麼走，是讓你自己決定要相信哪個情境，同時
    看得出結論對「未來方向」這個假設有多敏感。
    """
    plan = Plan.load(args.plan)
    st = Settings()
    today = _today(st, args.date)
    bars, _ = refresh(plan.symbol, today, st.seed_csv, st.resolved_cache_csv(),
                      timeout=st.http_timeout, offline=args.offline)
    if not bars:
        log.error("完全沒有價格資料，無法回測。")
        return 2

    names = args.strategies or list(STRATEGIES.keys())
    unknown = [n for n in names if n not in STRATEGIES]
    if unknown:
        print(f"不認識的策略：{', '.join(unknown)}（可用：{', '.join(STRATEGIES)}）",
             file=sys.stderr)
        return 2
    strategies = {name: STRATEGIES[name](plan) for name in names}

    scenario_names = args.scenarios or list(SCENARIOS.keys())
    unknown_sc = [s for s in scenario_names if s not in SCENARIOS]
    if unknown_sc:
        print(f"不認識的情境：{', '.join(unknown_sc)}（可用：{', '.join(SCENARIOS)}）",
             file=sys.stderr)
        return 2

    print(f"合成路徑：{args.paths} 條／情境　每條 {args.length} 個交易日"
         f"（區塊長度 {args.block_size}，種子 {args.seed}）")
    print(f"錨定收盤價：{bars[-1].close:.1f}（{bars[-1].date}）")
    print("情境不是預測——是固定的方向假設，用來看結論對「未來怎麼走」有多敏感。\n")

    winners = {}
    for sc_name in scenario_names:
        drift = SCENARIOS[sc_name]
        annualized = (math.exp(drift * 252) - 1) * 100
        results = compare_strategies(strategies, bars, n_paths=args.paths,
                                     path_length=args.length, block_size=args.block_size,
                                     seed=args.seed, target_drift=drift)
        # 主排序依變異係數（CV）由小到大——優先看「結果穩不穩」，不是誰的均值最高
        ordered = sorted(results.items(),
                         key=lambda kv: (kv[1]["capture_ratio"]["cv"] is None,
                                         kv[1]["capture_ratio"]["cv"] or 0))
        winners[sc_name] = ordered[0][0]

        print(f"── {sc_name}（日均漂移 {drift:+.4f}，約年化 {annualized:+.0f}%）──")
        header = f"{'策略':<20}{'出清率':>8}{'capture均':>10}{'capture CV':>12}{'vs持有均':>10}"
        print(header)
        print("─" * len(header))
        for name, r in ordered:
            cr, vb = r["capture_ratio"], r["vs_buy_hold"]
            cr_mean = "—" if cr["mean"] is None else f"{cr['mean']:.3f}"
            cr_cv = "—" if cr["cv"] is None else f"{cr['cv']:.3f}"
            vb_mean = "—" if vb["mean"] is None else f"{vb['mean']:+.1%}"
            print(f"{name:<20}{r['closed_rate']:>7.0%}{cr_mean:>10}{cr_cv:>12}{vb_mean:>10}")
        print()

    print("capture 均 = 已實現(+未實現mark-to-market)均價 ／ 該路徑期間最高收盤，越接近 1 越好")
    print("capture CV = 標準差／平均，越小代表這個策略在不同劇本下結果越穩定（本次排序依據）")
    print("vs持有均 = 跟「全程只抱著不賣」比較的平均超額報酬\n")

    if len(scenario_names) > 1:
        unique_winners = set(winners.values())
        if len(unique_winners) == 1:
            print(f"三種情境下排名第一（CV 最低）的都是「{unique_winners.pop()}」——結論不太受你對後市方向的看法影響。")
        else:
            print("不同情境下排名第一的策略不一樣：")
            for sc_name, w in winners.items():
                print(f"　{sc_name} → {w}")
            print("代表這個結論對「你相信未來會怎麼走」很敏感，選哪個策略要看你自己對後市的判斷。")
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

    dq = sub.add_parser("dequeue", help="撤掉一筆收盤觸發、明日開盤還沒執行的排隊單")
    dq.add_argument("tranche_id")
    dq.add_argument("--reason", required=True, help="為什麼要撤（會寫進 state.json 的稽核紀錄）")
    dq.set_defaults(func=cmd_dequeue)

    uf = sub.add_parser("unfill", help="撤銷一筆已標記成交、但實際沒有成交的批次")
    uf.add_argument("tranche_id")
    uf.add_argument("--reason", required=True, help="為什麼要撤（會寫進 state.json 的稽核紀錄）")
    uf.set_defaults(func=cmd_unfill)

    cd = sub.add_parser("chart-data", help="產生 docs/index.html 用的圖表資料 JSON")
    cd.add_argument("--date", help="指定交易日（YYYY-MM-DD），預設今天")
    cd.add_argument("--offline", action="store_true", help="不連網，只用本地資料")
    cd.add_argument("--out", default="docs/chart-data.json", help="輸出路徑")
    cd.add_argument("--lookback", type=int, default=60, help="要輸出近幾個交易日")
    cd.set_defaults(func=cmd_chart_data)

    bt = sub.add_parser("backtest", help="用合成路徑比較候選策略的穩健度")
    bt.add_argument("--date", help="指定交易日（YYYY-MM-DD），預設今天")
    bt.add_argument("--offline", action="store_true", help="不連網，只用本地資料")
    bt.add_argument("--strategies", nargs="+", help=f"要比較哪幾個策略，預設全部（可用：{', '.join(STRATEGIES)}）")
    bt.add_argument("--scenarios", nargs="+",
                    help=f"要跑哪幾種方向假設，預設全部（可用：{', '.join(SCENARIOS)}）")
    bt.add_argument("--paths", type=int, default=200, help="合成路徑數量")
    bt.add_argument("--length", type=int, default=180, help="每條路徑幾個交易日")
    bt.add_argument("--block-size", type=int, default=10, help="bootstrap 區塊長度（交易日）")
    bt.add_argument("--seed", type=int, default=42, help="隨機種子，固定的話結果可重現")
    bt.set_defaults(func=cmd_backtest)

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
