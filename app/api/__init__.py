# -*- coding: utf-8 -*-
import secrets
import base64
from io import BytesIO
from flask import Blueprint, request, jsonify, session
from flask_login import login_user, logout_user, login_required, current_user
from app.models import db, User, PushConfig, ScheduledTask, PushLog, Anniversary

api = Blueprint('api', __name__, url_prefix='/api')


def ok(data=None, msg='ok'):
    return jsonify({'code': 0, 'msg': msg, 'data': data})


def err(msg='error', code=400):
    return jsonify({'code': 1, 'msg': msg}), code


def get_real_ip() -> str:
    xff = request.headers.get('X-Forwarded-For', '')
    if xff:
        ip = xff.split(',')[0].strip()
        if ip:
            return ip
    xri = request.headers.get('X-Real-IP', '').strip()
    if xri:
        return xri
    return request.remote_addr or '0.0.0.0'


def _check_register_ip_limit() -> tuple[bool, str]:
    import pytz
    from datetime import datetime
    from app.models import RegisterLog
    sh = pytz.timezone('Asia/Shanghai')
    today = datetime.now(sh).strftime('%Y-%m-%d')
    ip = get_real_ip()
    exists = RegisterLog.query.filter_by(ip=ip, reg_date=today).first()
    if exists:
        return False, f'该 IP 今日已注册过账号，每个 IP 每天限注册一次'
    return True, ''


def _get_ip_location(ip: str) -> str:
    """查询 IP 归属地，ipinfo.io 为主，太平洋网络接口为备，失败返回空串。"""
    import logging as _log
    _logger = _log.getLogger(__name__)

    # 局域网 / 本地地址判断（IPv4 + IPv6）
    local_prefixes = ('127.', '192.168.', '10.', '172.')
    ipv6_local = ('::1', 'fe80', 'fc', 'fd')
    ip_lower = ip.lower()
    if (any(ip.startswith(p) for p in local_prefixes) or
            any(ip_lower.startswith(p) for p in ipv6_local)):
        return '局域网'

    import requests as _req

    # ── 主：ipinfo.io（免费 5万次/月，支持 IPv4+IPv6，HTTPS）──
    try:
        res = _req.get(
            f'https://ipinfo.io/{ip}/json',
            timeout=(8, 12), verify=False
        ).json()
        region = res.get('region', '')
        city   = res.get('city', '')
        if region or city:
            # 去掉"省/市"后缀，城市与省份相同时只显示一个
            region = region.replace('Province', '').replace('Municipality', '').strip()
            if city and city != region:
                return f"{region} {city}".strip()
            return region or city
    except Exception as e:
        _logger.warning('ipinfo.io 查询 %s 异常: %s', ip, e)

    # ── 备：太平洋网络（国内 IP 准确，仅支持 IPv4）──
    try:
        if ':' not in ip:   # 跳过 IPv6
            res = _req.get(
                f'https://whois.pconline.com.cn/ipJson.jsp',
                params={'ip': ip, 'json': 'true'},
                timeout=(8, 12), verify=False
            )
            res.encoding = 'gbk'
            data = res.json()
            addr = data.get('addr', '').strip()
            # 格式如 "中国 重庆 重庆市" 取前两段
            parts = [p for p in addr.split() if p and p != '中国']
            if parts:
                return ' '.join(parts[:2])
    except Exception as e:
        _logger.warning('pconline 查询 %s 异常: %s', ip, e)

    return ''


def _record_register_ip(username: str):
    """写今日限流记录（无归属地，归属地在注册成功后写入 User）"""
    import pytz
    from datetime import datetime
    from app.models import RegisterLog
    sh = pytz.timezone('Asia/Shanghai')
    today = datetime.now(sh).strftime('%Y-%m-%d')
    ip = get_real_ip()
    log = RegisterLog(ip=ip, reg_date=today, username=username)
    db.session.add(log)


def _validate_username(username: str) -> tuple[bool, str]:
    import re
    if len(username) < 4:
        return False, '账号长度不能少于 4 位'
    if len(username) > 64:
        return False, '账号长度不能超过 64 位'
    # 邮箱格式
    email_pattern = re.compile(r'^[a-zA-Z0-9][a-zA-Z0-9._\-]*@[a-zA-Z0-9\-]+(\.[a-zA-Z0-9\-]+)*\.[a-zA-Z]{2,}$')
    # 普通账号：只允许字母、数字、下划线、短横线
    normal_pattern = re.compile(r'^[a-zA-Z0-9][a-zA-Z0-9_\-]*$')
    if email_pattern.match(username) or normal_pattern.match(username):
        return True, ''
    return False, '账号只支持字母、数字、下划线"_"、短横线"-"或邮箱格式，不支持其他特殊字符'


def _validate_password(password: str) -> tuple[bool, str]:
    if len(password) < 8:
        return False, '密码长度不能少于 8 位'
    if not any(c.isalpha() for c in password):
        return False, '密码必须包含至少一个英文字母'
    if not any(c.isdigit() for c in password):
        return False, '密码必须包含至少一个数字'
    return True, ''


# ─────────────────────── 验证码 ───────────────────────

def _gen_captcha():
    """生成4位数字验证码，返回 (code_str, base64_svg)"""
    code = str(secrets.randbelow(9000) + 1000)  # 1000-9999
    # 纯 SVG 验证码，无需 Pillow
    chars = list(code)
    svg_parts = [
        '<svg xmlns="http://www.w3.org/2000/svg" width="120" height="40">',
        '<rect width="120" height="40" rx="6" fill="#1a1e2a"/>',
    ]
    # 干扰线
    import random
    rnd = random.Random(secrets.token_hex(4))
    for _ in range(4):
        x1, y1 = rnd.randint(0,120), rnd.randint(0,40)
        x2, y2 = rnd.randint(0,120), rnd.randint(0,40)
        opacity = rnd.uniform(0.15, 0.35)
        svg_parts.append(f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="#4f8ef7" stroke-width="1" opacity="{opacity:.2f}"/>')
    # 干扰点
    for _ in range(20):
        cx, cy = rnd.randint(0,120), rnd.randint(0,40)
        svg_parts.append(f'<circle cx="{cx}" cy="{cy}" r="1" fill="#4f8ef7" opacity="0.2"/>')
    # 字符
    colors = ['#4f8ef7','#7b5cf0','#3dd68c','#f5a623','#e879f9']
    for i, ch in enumerate(chars):
        x = 12 + i * 26 + rnd.randint(-3, 3)
        y = 28 + rnd.randint(-4, 4)
        rot = rnd.randint(-12, 12)
        color = rnd.choice(colors)
        svg_parts.append(
            f'<text x="{x}" y="{y}" font-size="22" font-family="monospace" font-weight="bold" '
            f'fill="{color}" transform="rotate({rot},{x},{y})">{ch}</text>'
        )
    svg_parts.append('</svg>')
    svg_str = ''.join(svg_parts)
    b64 = base64.b64encode(svg_str.encode()).decode()
    return code, f'data:image/svg+xml;base64,{b64}'


@api.get('/captcha')
def get_captcha():
    code, img = _gen_captcha()
    session.permanent = True          # 确保 Set-Cookie 被写入响应
    session['captcha'] = code.lower()
    session.modified = True
    resp = ok({'img': img})
    return resp


def _verify_captcha(user_input: str) -> bool:
    expected = session.pop('captcha', None)
    if expected is None:
        return False  # session 中无验证码（cookie 问题或已过期）
    return user_input.strip().lower() == expected.lower()


# ─────────────────────── 认证 ───────────────────────

@api.post('/auth/register')
def register():
    body = request.json or {}
    username = (body.get('username') or '').strip()
    password = (body.get('password') or '').strip()
    captcha  = (body.get('captcha') or '').strip()

    if not username or not password:
        return err('用户名和密码不能为空')
    valid_uname, uname_msg = _validate_username(username)
    if not valid_uname:
        return err(uname_msg)
    if not _verify_captcha(captcha):
        return err('验证码错误或已过期')

    valid_pwd, pwd_msg = _validate_password(password)
    if not valid_pwd:
        return err(pwd_msg)
    if User.query.filter_by(username=username).first():
        return err('用户名已存在')

    allowed, limit_msg = _check_register_ip_limit()
    if not allowed:
        return err(limit_msg)

    user = User(username=username, reg_ip=get_real_ip())
    user.set_password(password)
    if User.query.count() == 0:
        user.is_admin = True
    db.session.add(user)
    db.session.flush()

    cfg = PushConfig(user_id=user.id)
    db.session.add(cfg)
    _record_register_ip(username)
    db.session.commit()

    # 归属地查询放后台线程，不阻塞注册响应
    user_id = user.id
    reg_ip  = user.reg_ip
    def _fetch_and_save_location(uid, ip):
        try:
            from app import create_app
            _app = create_app()
            with _app.app_context():
                loc = _get_ip_location(ip)
                if loc:
                    u = db.session.get(User, uid)
                    if u:
                        u.ip_location = loc
                        db.session.commit()
        except Exception as e:
            import logging; logging.getLogger(__name__).warning('归属地写入失败: %s', e)
    import threading
    threading.Thread(target=_fetch_and_save_location, args=(user_id, reg_ip), daemon=True).start()

    return ok(None, '注册成功')   # 不自动登录，让前端跳回登录页


@api.post('/auth/login')
def login():
    body = request.json or {}
    username = (body.get('username') or '').strip()
    password = (body.get('password') or '').strip()
    captcha  = (body.get('captcha') or '').strip()

    if not _verify_captcha(captcha):
        return err('验证码错误或已过期')

    user = User.query.filter_by(username=username).first()
    if not user or not user.check_password(password):
        return err('账号或密码错误')
    login_user(user, remember=True)
    return ok(user.to_dict(), '登录成功')


@api.post('/auth/logout')
@login_required
def logout():
    logout_user()
    return ok(msg='已退出登录')


@api.get('/auth/me')
def me():
    """
    查询当前登录状态。
    始终返回 200，通过 data.logged_in 区分是否已登录，
    避免浏览器把 401 记录为 "Failed to load resource" 控制台报错。
    """
    from flask_login import current_user
    if current_user.is_authenticated:
        return ok({**current_user.to_dict(), 'logged_in': True})
    return ok({'logged_in': False})


@api.post('/auth/change-password')
@login_required
def change_password():
    body = request.json or {}
    old_pwd = body.get('old_password', '')
    new_pwd = body.get('new_password', '')
    if not current_user.check_password(old_pwd):
        return err('原密码错误')
    valid_pwd, pwd_msg = _validate_password(new_pwd)
    if not valid_pwd:
        return err(pwd_msg)
    current_user.set_password(new_pwd)
    db.session.commit()
    return ok(msg='密码修改成功')


# ─────────────────────── 推送配置 ───────────────────────

@api.get('/config')
@login_required
def get_config():
    cfg = PushConfig.query.filter_by(user_id=current_user.id).first()
    if not cfg:
        cfg = PushConfig(user_id=current_user.id)
        db.session.add(cfg)
        db.session.commit()
    return ok(cfg.to_dict())


@api.put('/config')
@login_required
def update_config():
    cfg = PushConfig.query.filter_by(user_id=current_user.id).first()
    if not cfg:
        cfg = PushConfig(user_id=current_user.id)
        db.session.add(cfg)

    body = request.json or {}
    fields = [
        'corpid', 'corpsecret', 'agentid', 'qweather_key', 'qweather_host', 'city',
        'msgtype', 'pictype', 'title', 'content', 'call',
        'tian_key', 'tian_type', 'tian_astro'
    ]
    for f in fields:
        if f in body:
            setattr(cfg, f, body[f])
    db.session.commit()
    return ok(cfg.to_dict(), '配置已保存')


# ─────────────────────── 纪念日 ───────────────────────

@api.get('/anniversaries')
@login_required
def list_anniversaries():
    items = Anniversary.query.filter_by(user_id=current_user.id)\
                .order_by(Anniversary.sort_order, Anniversary.id).all()
    return ok([i.to_dict() for i in items])


@api.post('/anniversaries')
@login_required
def create_anniversary():
    body = request.json or {}
    name     = (body.get('name') or '').strip()
    ann_type = body.get('ann_type', 'yearly')
    date_str = (body.get('date_str') or '').strip()
    if not name:
        return err('名称不能为空')
    if ann_type not in ('yearly', 'once'):
        return err('类型无效')
    if not date_str:
        return err('日期不能为空')
    count = Anniversary.query.filter_by(user_id=current_user.id).count()
    item = Anniversary(
        user_id=current_user.id,
        name=name,
        ann_type=ann_type,
        date_str=date_str,
        sort_order=count,
    )
    db.session.add(item)
    db.session.commit()
    return ok(item.to_dict(), '纪念日已添加')


@api.put('/anniversaries/<int:ann_id>')
@login_required
def update_anniversary(ann_id):
    item = Anniversary.query.filter_by(id=ann_id, user_id=current_user.id).first()
    if not item:
        return err('记录不存在', 404)
    body = request.json or {}
    if 'name' in body:
        item.name = (body['name'] or '').strip() or item.name
    if 'ann_type' in body and body['ann_type'] in ('yearly', 'once'):
        item.ann_type = body['ann_type']
    if 'date_str' in body:
        item.date_str = (body['date_str'] or '').strip() or item.date_str
    db.session.commit()
    return ok(item.to_dict(), '已更新')


@api.delete('/anniversaries/<int:ann_id>')
@login_required
def delete_anniversary(ann_id):
    item = Anniversary.query.filter_by(id=ann_id, user_id=current_user.id).first()
    if not item:
        return err('记录不存在', 404)
    db.session.delete(item)
    db.session.commit()
    return ok(msg='已删除')


# ─────────────────────── 定时任务 ───────────────────────

def _enrich_tasks(tasks):
    from app.scheduler import scheduler, get_job_id, compute_next_run
    import pytz
    sh = pytz.timezone('Asia/Shanghai')
    result = []
    for t in tasks:
        d = t.to_dict()
        next_run = None
        if scheduler:
            job = scheduler.get_job(get_job_id(t.id))
            if job and job.next_run_time:
                next_run = job.next_run_time.astimezone(sh).strftime('%Y-%m-%d %H:%M:%S')
        if next_run is None and t.enabled:
            next_run = compute_next_run(t.cron_hour, t.cron_minute)
        d['next_run'] = next_run
        result.append(d)
    return result


@api.get('/tasks')
@login_required
def list_tasks():
    tasks = ScheduledTask.query.filter_by(user_id=current_user.id).all()
    return ok(_enrich_tasks(tasks))


@api.post('/tasks')
@login_required
def create_task():
    body = request.json or {}
    name = body.get('name', '每日推送').strip() or '每日推送'
    try:
        hour   = int(body.get('cron_hour', 8))
        minute = int(body.get('cron_minute', 0))
    except (TypeError, ValueError):
        return err('时间格式无效')
    if not (0 <= hour <= 23):   return err('小时必须在 0-23 之间')
    if not (0 <= minute <= 59): return err('分钟必须在 0-59 之间')
    if ScheduledTask.query.filter_by(user_id=current_user.id).count() >= 5:
        return err('最多创建 5 个定时任务')
    dup = ScheduledTask.query.filter_by(user_id=current_user.id, cron_hour=hour, cron_minute=minute).first()
    if dup:
        return err(f'定时任务已存在：{hour:02d}:{minute:02d} 「{dup.name}」')

    task = ScheduledTask(
        user_id=current_user.id, name=name,
        cron_hour=hour, cron_minute=minute,
        enabled=body.get('enabled', True)
    )
    db.session.add(task)
    db.session.commit()
    if task.enabled:
        from app.scheduler import add_or_update_job
        add_or_update_job(task)
    return ok(_enrich_tasks([task])[0], '任务已创建')


@api.put('/tasks/<int:task_id>')
@login_required
def update_task(task_id):
    task = ScheduledTask.query.filter_by(id=task_id, user_id=current_user.id).first()
    if not task: return err('任务不存在', 404)
    body = request.json or {}
    if 'name' in body:
        task.name = (body['name'] or '').strip() or task.name
    if 'cron_hour' in body:
        try:
            h = int(body['cron_hour'])
            if not (0 <= h <= 23): return err('小时必须在 0-23 之间')
            task.cron_hour = h
        except (TypeError, ValueError): return err('小时格式无效')
    if 'cron_minute' in body:
        try:
            m = int(body['cron_minute'])
            if not (0 <= m <= 59): return err('分钟必须在 0-59 之间')
            task.cron_minute = m
        except (TypeError, ValueError): return err('分钟格式无效')
    if 'enabled' in body:
        task.enabled = bool(body['enabled'])
    db.session.commit()
    from app.scheduler import add_or_update_job, remove_job
    if task.enabled: add_or_update_job(task)
    else: remove_job(task.id)
    return ok(_enrich_tasks([task])[0], '任务已更新')


@api.delete('/tasks/<int:task_id>')
@login_required
def delete_task(task_id):
    task = ScheduledTask.query.filter_by(id=task_id, user_id=current_user.id).first()
    if not task: return err('任务不存在', 404)
    from app.scheduler import remove_job
    remove_job(task.id)
    db.session.delete(task)
    db.session.commit()
    return ok(msg='任务已删除')


@api.post('/tasks/<int:task_id>/toggle')
@login_required
def toggle_task(task_id):
    task = ScheduledTask.query.filter_by(id=task_id, user_id=current_user.id).first()
    if not task: return err('任务不存在', 404)
    task.enabled = not task.enabled
    db.session.commit()
    from app.scheduler import add_or_update_job, remove_job
    if task.enabled: add_or_update_job(task)
    else: remove_job(task.id)
    return ok(_enrich_tasks([task])[0], '任务已' + ('启用' if task.enabled else '暂停'))


# ─────────────────────── 立即推送 ───────────────────────

@api.post('/push/now')
@login_required
def push_now():
    cfg = PushConfig.query.filter_by(user_id=current_user.id).first()
    if not cfg: return err('请先完成推送配置')
    from app.push_engine import send_wecom_message
    import json
    success, msg, steps = send_wecom_message(cfg)
    log = PushLog(user_id=current_user.id, trigger='manual',
                  status='success' if success else 'failed', message=msg,
                  steps=json.dumps(steps, ensure_ascii=False))
    db.session.add(log)
    # 顺带清理 7 天前的旧日志
    from datetime import datetime, timedelta
    import pytz
    cutoff = datetime.now(pytz.timezone('Asia/Shanghai')) - timedelta(days=7)
    cutoff_naive = cutoff.replace(tzinfo=None)
    PushLog.query.filter(
        PushLog.user_id == current_user.id,
        PushLog.created_at < cutoff_naive
    ).delete(synchronize_session=False)
    db.session.commit()
    if success: return ok(msg=msg)
    return err(msg)


# ─────────────────────── 推送日志 ───────────────────────

@api.get('/logs')
@login_required
def get_logs():
    page = int(request.args.get('page', 1))
    per_page = int(request.args.get('per_page', 20))
    query = PushLog.query.filter_by(user_id=current_user.id)
    total = query.count()
    logs = query.order_by(PushLog.id.desc()).offset((page-1)*per_page).limit(per_page).all()
    return ok({'total': total, 'page': page, 'per_page': per_page,
               'items': [l.to_dict() for l in logs]})


# ─────────────────────── 管理员接口 ───────────────────────

@api.get('/admin/users')
@login_required
def admin_list_users():
    if not current_user.is_admin: return err('无权限', 403)

    page     = max(1, int(request.args.get('page', 1)))
    per_page = min(50, max(1, int(request.args.get('per_page', 20))))
    search   = (request.args.get('search') or '').strip()

    query = User.query
    if search:
        query = query.filter(User.username.ilike(f'%{search}%'))

    total = query.count()
    users = query.order_by(User.id.asc()).offset((page - 1) * per_page).limit(per_page).all()

    # 一次性批量查询本页用户的统计数据，避免 N+1 查询
    from sqlalchemy import func
    from app.models import ScheduledTask
    user_ids = [u.id for u in users]

    task_counts = {r.user_id: r.cnt for r in
        db.session.query(ScheduledTask.user_id, func.count(ScheduledTask.id).label('cnt'))
        .filter(ScheduledTask.user_id.in_(user_ids)).group_by(ScheduledTask.user_id).all()}

    log_counts = {r.user_id: r.cnt for r in
        db.session.query(PushLog.user_id, func.count(PushLog.id).label('cnt'))
        .filter(PushLog.user_id.in_(user_ids)).group_by(PushLog.user_id).all()}

    # 每个用户最新一条推送日志（用 ROW_NUMBER 子查询避免 N 次查询）
    from sqlalchemy import desc
    latest_logs = {}
    if user_ids:
        subq = (db.session.query(
                    PushLog.user_id,
                    func.max(PushLog.created_at).label('latest'))
                .filter(PushLog.user_id.in_(user_ids))
                .group_by(PushLog.user_id).subquery())
        for row in db.session.query(subq).all():
            latest_logs[row.user_id] = row.latest.strftime('%Y-%m-%d %H:%M:%S') if row.latest else None

    result = []
    for u in users:
        d = u.to_dict()   # 已含 ip_location
        d['task_count']   = task_counts.get(u.id, 0)
        d['log_count']    = log_counts.get(u.id, 0)
        d['last_push_at'] = latest_logs.get(u.id)
        result.append(d)

    return ok({'items': result, 'total': total, 'page': page, 'per_page': per_page})


@api.delete('/admin/users/<int:user_id>')
@login_required
def admin_delete_user(user_id):
    if not current_user.is_admin: return err('无权限', 403)
    if user_id == current_user.id: return err('不能删除自己')
    user = db.session.get(User, user_id)
    if not user: return err('用户不存在', 404)
    from app.scheduler import remove_job
    for task in user.tasks:
        remove_job(task.id)
    db.session.delete(user)
    db.session.commit()
    return ok(msg='用户已删除')


@api.get('/admin/logs')
@login_required
def admin_all_logs():
    if not current_user.is_admin: return err('无权限', 403)
    page = int(request.args.get('page', 1))
    per_page = int(request.args.get('per_page', 30))
    total = PushLog.query.count()
    logs = PushLog.query.order_by(PushLog.id.desc()).offset((page-1)*per_page).limit(per_page).all()
    return ok({'total': total, 'items': [l.to_dict() for l in logs]})


@api.get('/admin/scheduler/jobs')
@login_required
def admin_scheduler_jobs():
    if not current_user.is_admin: return err('无权限', 403)
    from app.scheduler import list_jobs
    return ok(list_jobs())


@api.get('/admin/register-logs')
@login_required
def admin_register_logs():
    """只返回今日的注册限流记录"""
    if not current_user.is_admin: return err('无权限', 403)
    import pytz
    from datetime import datetime
    from app.models import RegisterLog
    today = datetime.now(pytz.timezone('Asia/Shanghai')).strftime('%Y-%m-%d')
    logs = RegisterLog.query.filter_by(reg_date=today).order_by(RegisterLog.id.desc()).all()
    return ok({'total': len(logs), 'items': [l.to_dict() for l in logs]})


@api.post('/admin/users/refetch-locations')
@login_required
def admin_refetch_user_locations():
    """对 ip_location 为空的用户批量补查归属地，每次最多 30 条"""
    if not current_user.is_admin: return err('无权限', 403)
    import time
    empty_users = User.query.filter(
        (User.ip_location == None) | (User.ip_location == '')
    ).filter(User.reg_ip != '').order_by(User.id.asc()).limit(30).all()
    updated = 0
    for u in empty_users:
        loc = _get_ip_location(u.reg_ip)
        if loc:
            u.ip_location = loc
            updated += 1
        time.sleep(1.5)
    db.session.commit()
    return ok(msg=f'已补全 {updated} 条，共扫描 {len(empty_users)} 条')


@api.delete('/admin/register-logs/ip/<path:ip>')
@login_required
def admin_unblock_ip(ip):
    if not current_user.is_admin: return err('无权限', 403)
    import pytz
    from datetime import datetime
    from app.models import RegisterLog
    sh = pytz.timezone('Asia/Shanghai')
    today = datetime.now(sh).strftime('%Y-%m-%d')
    deleted = RegisterLog.query.filter_by(ip=ip, reg_date=today).delete()
    db.session.commit()
    if deleted: return ok(msg=f'已解封 IP {ip}，今日可重新注册')
    return err(f'IP {ip} 今日无注册记录', 404)


@api.get('/server/egress-ip')
@login_required
def get_egress_ip():
    """获取服务器出口 IP（通过请求外部服务探测）"""
    import urllib.request
    services = [
        'https://api.ipify.org',
        'https://ipecho.net/plain',
        'https://icanhazip.com',
    ]
    for url in services:
        try:
            with urllib.request.urlopen(url, timeout=4) as r:
                ip = r.read().decode().strip()
                if ip:
                    return ok({'ip': ip})
        except Exception:
            continue
    return err('无法获取出口 IP，请稍后重试')


@api.post('/admin/sync-users')
@login_required
def admin_sync_users():
    """执行用户数据同步脚本（仅管理员）"""
    if not current_user.is_admin:
        return err('无权限', 403)
    import subprocess, shlex
    script = '/usr/bin/wecom-admin-syncsync'
    try:
        result = subprocess.run(
            [script],
            capture_output=True,
            text=True,
            timeout=60,
            env={**__import__('os').environ}
        )
        output = (result.stdout or '') + (result.stderr or '')
        if result.returncode == 0:
            return ok({'output': output.strip() or '同步完成'})
        else:
            return err(f'脚本退出码 {result.returncode}：{output.strip()[:500]}')
    except FileNotFoundError:
        return err(f'脚本不存在：{script}')
    except subprocess.TimeoutExpired:
        return err('同步超时（60s），请检查脚本')
    except Exception as e:
        return err(f'执行失败：{str(e)}')


@api.post('/admin/sync')
@login_required
def admin_sync():
    """执行数据同步脚本（仅管理员）"""
    if not current_user.is_admin:
        return err('无权限', 403)
    import subprocess, shutil
    script = '/usr/bin/wecom-admin-syncsync'
    if not shutil.which(script) and not __import__('os').path.isfile(script):
        return err(f'同步脚本不存在：{script}')
    try:
        result = subprocess.run(
            [script],
            capture_output=True,
            text=True,
            timeout=60,
            env={**__import__('os').environ, 'PATH': '/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin'},
        )
        output = (result.stdout + result.stderr).strip() or '（无输出）'
        if result.returncode == 0:
            return ok({'output': output}, msg='同步成功')
        else:
            return err(f'脚本退出码 {result.returncode}：{output}')
    except subprocess.TimeoutExpired:
        return err('同步超时（超过 60 秒），请检查脚本')
    except PermissionError:
        return err(f'脚本无执行权限，请运行：chmod +x {script}')
    except Exception as e:
        return err(f'执行失败：{e}')
