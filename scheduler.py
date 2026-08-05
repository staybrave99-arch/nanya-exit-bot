"""常駐排程器 —— Fly.io 上跑的就是這支。

為什麼不用 Fly Machines 內建的 schedule：它只支援 hourly/daily/weekly，
**不能指定幾點**。台股收盤巡檢必須在 20:00（台北）跑，所以自己帶排程器。

一台 shared-cpu-1x 常駐即可，成本極低。狀態寫在 volume 掛載的 /data，
機器重建也不會掉。
"""
from __future__ import annotations

import datetime as dt
import logging
import os
import sys
from zoneinfo import ZoneInfo

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from nanya_exit.cli import main as run_cli
from nanya_exit.config import Settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger("scheduler")


def job() -> None:
    log.info("─── 開始每日巡檢 ───")
    try:
        code = run_cli(["run"])
        log.info("─── 巡檢結束，exit=%s ───", code)
    except Exception:                                    # noqa: BLE001
        log.exception("巡檢拋出未預期的例外（排程器繼續存活）")


def main() -> None:
    st = Settings()
    tz = ZoneInfo(st.timezone)
    hour, minute = (int(x) for x in st.run_at.split(":"))

    if os.getenv("RUN_ON_BOOT", "").lower() in ("1", "true", "yes"):
        log.info("RUN_ON_BOOT 已設定，先跑一次。")
        job()

    trigger = CronTrigger(day_of_week="mon-fri", hour=hour, minute=minute, timezone=tz)
    sched = BlockingScheduler(timezone=tz)
    sched.add_job(
        job,
        trigger,
        id="daily_check",
        max_instances=1,
        coalesce=True,          # 機器睡著錯過幾次 → 醒來只補跑一次
        misfire_grace_time=3600,
    )
    # job.next_run_time 要排程器真的 start() 跑起來後才會有值（APScheduler
    # 3.11 是這樣），這裡是 start() 之前，所以直接問 trigger 本身算下次時間，
    # 不要去讀 job 屬性 —— 部署後曾經因為這樣在開機時就 AttributeError 崩潰。
    nxt = trigger.get_next_fire_time(None, dt.datetime.now(tz))
    log.info("排程已啟動：每週一~五 %s（%s）。狀態檔：%s",
             st.run_at, st.timezone, st.state_path)
    log.info("ntfy：%s/%s", st.ntfy_server, st.ntfy_topic)
    if nxt:
        log.info("下次執行：%s", nxt)
    try:
        sched.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("收到中斷訊號，關閉排程器。")


if __name__ == "__main__":
    main()
