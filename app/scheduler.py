# -*- coding: utf-8 -*-
"""
多用户定时任务管理器
使用 APScheduler + 内存 jobstore，进程重启后通过 load_all_jobs() 从业务 DB 重新注册。

【下次执行时间计算规则】
  以"注册/更新任务时的当前时刻"为基准：
  - 若设定时间还未到（如现在 11:13，设定 12:00）→ 今天 12:00:00
  - 若设定时间已过去（如现在 11:13，设定 11:00）→ 明天 11:00:00
  APScheduler CronTrigger 已内置此逻辑，本模块无需额外处理。

【并发排队机制】
  _PUSH_SEMAPHORE 控制同一时刻最多同时执行的推送任务数。
  超出限制的任务在信号量处阻塞排队，不丢失、不跳过，
  执行完一个立刻释放名额给下一个。

  APScheduler 线程池（max_workers）是任务的"调度容量"，
  信号量是任务的"执行节流阀"，两者配合：
    - 线程池大 → 所有任务快速进入线程，在信号量前有序排队
    - 信号量小 → 同时只有 N 个任务真正占用网络和内存
"""
import threading
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.triggers.cron import CronTrigger
import pytz
import logging
from datetime import datetime

logger = logging.getLogger(__name__)
SHANGHAI_TZ = pytz.timezone('Asia/Shanghai')

scheduler  = None
_flask_app = None   # 持有主 app 引用，供子线程复用

# ── 全局推送并发限制 ──────────────────────────────────────────────────────
# 同一时刻最多 MAX_CONCURRENT_PUSH 个推送任务真正执行（拉 API + 发消息）。
# 其余任务在此阻塞排队，先到先得，执行完立即释放名额。
#
# 资源参考（0.1 CPU / 512 MB 容器）：
#   每个推送任务占用：~3 个 fetch 线程 × 6MB + SQLite 写入 ≈ 25 MB
#   4 个并发：~100 MB，Flask + SQLite + OS 约占 150 MB，合计 ~250 MB，留有余量。
#   如果宿主机资源充裕，可调大此值。
MAX_CONCURRENT_PUSH = 4
_PUSH_SEMAPHORE = threading.Semaphore(MAX_CONCURRENT_PUSH)
# ─────────────────────────────────────────────────────────────────────────


def init_scheduler(app):
    global scheduler, _flask_app
    _flask_app = app

    executors = {
        # 线程池设为比 MAX_CONCURRENT_PUSH 大，让所有任务快速进入线程等待信号量，
        # 而不是在 APScheduler 队列里等待线程空缺（两层排队会导致日志难以追踪）。
        'default': ThreadPoolExecutor(max_workers=MAX_CONCURRENT_PUSH * 4)
    }
    job_defaults = {
        'coalesce':           True,   # 错过多次只补跑一次
        'max_instances':      1,      # 同一任务不重复执行
        'misfire_grace_time': 300     # 5 分钟内的迟发仍算有效
    }
    scheduler = BackgroundScheduler(
        jobstores={},
        executors=executors,
        job_defaults=job_defaults,
        timezone=SHANGHAI_TZ
    )
    scheduler.start()
    logger.info(
        "APScheduler 已启动（时区：Asia/Shanghai，最大并发推送：%d）",
        MAX_CONCURRENT_PUSH
    )
    return scheduler


def _run_push_job(user_id: int, task_id: int):
    """
    定时任务执行函数，在 APScheduler 线程池中运行。

    执行流程：
      1. 快速进入线程（不阻塞 APScheduler 调度循环）
      2. 在信号量处排队，等待并发名额（先到先得）
      3. 获得名额后执行推送，完成后释放名额
    """
    # ── 排队等待并发名额 ──────────────────────────────────────────────────
    queue_start = datetime.now(SHANGHAI_TZ)
    logger.info("任务排队中: push_task_%d [用户:%d]", task_id, user_id)
    _PUSH_SEMAPHORE.acquire()
    wait_secs = (datetime.now(SHANGHAI_TZ) - queue_start).total_seconds()
    if wait_secs > 1:
        logger.info(
            "任务获得执行名额: push_task_%d [用户:%d]，排队等待 %.1fs",
            task_id, user_id, wait_secs
        )
    # ─────────────────────────────────────────────────────────────────────

    try:
        if _flask_app is None:
            logger.error("_flask_app 未初始化，跳过推送")
            return

        with _flask_app.app_context():
            from app.models import db, PushConfig, PushLog, ScheduledTask
            from app.push_engine import send_wecom_message

            task = db.session.get(ScheduledTask, task_id)
            if not task or not task.enabled:
                logger.info("任务 %d 已禁用或不存在，跳过", task_id)
                return

            cfg = PushConfig.query.filter_by(user_id=user_id).first()
            if not cfg:
                logger.warning("用户 %d 没有推送配置，跳过", user_id)
                return

            import json as _json
            success, msg, steps = send_wecom_message(cfg)
            log = PushLog(
                user_id=user_id,
                trigger='scheduled',
                status='success' if success else 'failed',
                message=msg,
                steps=_json.dumps(steps, ensure_ascii=False)
            )
            db.session.add(log)
            # 每次推送后顺带清理 7 天前的旧日志，不需要额外定时任务
            _cleanup_old_logs(db, user_id)
            db.session.commit()
            logger.info("定时推送 [用户:%d 任务:%d] → %s", user_id, task_id, msg)

    finally:
        # 无论成功/失败/异常，必须释放信号量，否则名额永久泄漏
        _PUSH_SEMAPHORE.release()
        logger.debug("任务释放执行名额: push_task_%d [用户:%d]", task_id, user_id)


def get_job_id(task_id: int) -> str:
    return f"push_task_{task_id}"


def compute_next_run(hour: int, minute: int) -> str:
    """
    根据当前上海时间，计算指定 hour:minute 的下次执行时间字符串。

    规则：
      - 当前时刻 < 今日 hour:minute  →  今天 YYYY-MM-DD HH:MM:SS
      - 当前时刻 >= 今日 hour:minute →  明天 YYYY-MM-DD HH:MM:SS
    """
    trigger = CronTrigger(hour=hour, minute=minute, second=0, timezone=SHANGHAI_TZ)
    now = datetime.now(SHANGHAI_TZ)
    next_fire = trigger.get_next_fire_time(None, now)
    if next_fire:
        return next_fire.astimezone(SHANGHAI_TZ).strftime('%Y-%m-%d %H:%M:%S')
    return ''


def add_or_update_job(task):
    """
    添加或更新一个定时任务。

    APScheduler CronTrigger 以注册时刻为基准自动决定首次触发时间：
      - 今天的时间点还未到 → 今天触发
      - 今天的时间点已过去 → 明天触发
    replace_existing=True 确保更新任务时重新计算下次触发时间。
    """
    if scheduler is None:
        return
    job_id = get_job_id(task.id)
    scheduler.add_job(
        func=_run_push_job,
        trigger='cron',
        hour=task.cron_hour,
        minute=task.cron_minute,
        second=0,
        timezone=SHANGHAI_TZ,
        id=job_id,
        args=[task.user_id, task.id],
        replace_existing=True
    )
    next_run = compute_next_run(task.cron_hour, task.cron_minute)
    logger.info(
        "任务已注册: %s -> 每天 %02d:%02d，下次执行: %s",
        job_id, task.cron_hour, task.cron_minute, next_run
    )


def remove_job(task_id: int):
    """移除定时任务"""
    if scheduler is None:
        return
    job_id = get_job_id(task_id)
    try:
        scheduler.remove_job(job_id)
        logger.info("任务已移除: %s", job_id)
    except Exception:
        pass


def pause_job(task_id: int):
    if scheduler is None:
        return
    try:
        scheduler.pause_job(get_job_id(task_id))
    except Exception:
        pass


def resume_job(task_id: int):
    if scheduler is None:
        return
    try:
        scheduler.resume_job(get_job_id(task_id))
    except Exception:
        pass


def _cleanup_register_logs(app):
    """零点清理昨日及更早的注册限流记录"""
    with app.app_context():
        import pytz
        from datetime import datetime
        from app.models import RegisterLog, db
        today = datetime.now(pytz.timezone('Asia/Shanghai')).strftime('%Y-%m-%d')
        deleted = db.session.query(RegisterLog).filter(RegisterLog.reg_date < today).delete(synchronize_session=False)
        db.session.commit()
        if deleted:
            logger.info('已清理 %d 条过期注册限流记录', deleted)


def load_all_jobs(app):
    """
    应用启动时加载所有启用的任务。
    以启动时刻为基准重新计算每个任务的下次触发时间。
    """
    with app.app_context():
        from app.models import ScheduledTask
        tasks = ScheduledTask.query.filter_by(enabled=True).all()
        for task in tasks:
            add_or_update_job(task)
        logger.info("已加载 %d 个定时任务", len(tasks))

    # 每天 00:01 清理过期注册限流记录
    if scheduler:
        scheduler.add_job(
            func=_cleanup_register_logs,
            trigger='cron',
            hour=0, minute=1, second=0,
            timezone=SHANGHAI_TZ,
            id='__cleanup_register_logs__',
            args=[app],
            replace_existing=True
        )
        logger.info("已注册每日注册记录清理任务（00:01）")


def list_jobs():
    if scheduler is None:
        return []
    return [
        {
            'id': job.id,
            'next_run': job.next_run_time.astimezone(SHANGHAI_TZ).strftime('%Y-%m-%d %H:%M:%S')
                        if job.next_run_time else None
        }
        for job in scheduler.get_jobs()
    ]


def _cleanup_old_logs(db, user_id: int, keep_days: int = 7):
    """删除指定用户 keep_days 天前的推送日志，在写入新日志的同一事务里执行。"""
    from app.models import PushLog
    from datetime import datetime, timedelta
    import pytz
    cutoff = datetime.now(pytz.timezone('Asia/Shanghai')) - timedelta(days=keep_days)
    cutoff_naive = cutoff.replace(tzinfo=None)
    db.session.query(PushLog).filter(
        PushLog.user_id == user_id,
        PushLog.created_at < cutoff_naive
    ).delete(synchronize_session=False)