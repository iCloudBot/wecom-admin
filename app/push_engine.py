# -*- coding: utf-8 -*-
"""
消息推送核心引擎
所有数据获取与消息组装逻辑，从 config.py 移除，改为接收配置对象
"""
import re
import random
import logging
import requests
from flask import current_app
import time
import functools


def _retry(max_tries=3, delay=1.5, exceptions=(Exception,)):
    """
    通用重试装饰器。
    max_tries: 最大尝试次数（含第1次）
    delay:     每次重试前等待秒数
    exceptions: 触发重试的异常类型
    """
    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            last_exc = None
            for attempt in range(1, max_tries + 1):
                try:
                    return fn(*args, **kwargs)
                except exceptions as e:
                    last_exc = e
                    if attempt < max_tries:
                        logger.warning(
                            "%s 第%d次失败，%.1fs后重试: %s",
                            fn.__name__, attempt, delay, e
                        )
                        time.sleep(delay)
                    else:
                        logger.warning(
                            "%s 已重试%d次，放弃: %s",
                            fn.__name__, max_tries, e
                        )
            return None  # 全部失败返回 None，不抛出异常
        return wrapper
    return decorator

from concurrent.futures import ThreadPoolExecutor as FetchExecutor, as_completed

logger = logging.getLogger(__name__)
from datetime import datetime, date, timedelta

try:
    from zhdate import ZhDate
    HAS_ZHDATE = True
except ImportError:
    HAS_ZHDATE = False

import pytz

SHANGHAI_TZ = pytz.timezone('Asia/Shanghai')


def now_sh():
    return datetime.now(SHANGHAI_TZ)


# ─────────────────────── 工具函数 ───────────────────────

def split_list(value: str):
    """&&分割字符串为列表"""
    if not value:
        return []
    return [v.strip() for v in value.split('&&') if v.strip()]


# ─────────────────────── 天气模块 ───────────────────────

WEATHER_ICON_LIST = ["☀️", "☁️", "⛅️", "☃️", "⛈️", "🏜️", "🏜️", "🌫️", "🌫️", "🌪️", "🌧️"]
WEATHER_TYPE = ["晴", "阴", "云", "雪", "雷", "沙", "尘", "雾", "霾", "风", "雨"]


def get_weather_icon(text):
    for idx, item in enumerate(WEATHER_TYPE):
        if re.search(item, text):
            return WEATHER_ICON_LIST[idx]
    return "🌤️"


@_retry(max_tries=3, delay=3)
def _fetch_weather_once(city_name: str, qweather_key: str, qweather_host: str = ''):
    """单次天气请求，超时/网络错误直接抛出，由调用方重试。"""
    parts = city_name.split('-')
    if len(parts) < 2:
        return None
    city, county = parts[0], parts[1]

    # 新接口（自定义 Host）vs 旧接口（公共域名）
    if qweather_host:
        host = qweather_host.strip().rstrip('/')
        geo_url     = f"https://{host}/geo/v2/city/lookup"
        weather_url_tpl = f"https://{host}/v7/weather/3d"
        life_url_tpl    = f"https://{host}/v7/indices/1d"
    else:
        geo_url     = "https://geoapi.qweather.com/v2/city/lookup"
        weather_url_tpl = "https://devapi.qweather.com/v7/weather/3d"
        life_url_tpl    = "https://devapi.qweather.com/v7/indices/1d"

    city_json = requests.get(
        geo_url,
        params={"adm": city, "location": county, "key": qweather_key},
        timeout=(10, 20)
    ).json()
    if city_json.get('code') != '200':
        return None
    city_id = city_json['location'][0]['id']

    weather_json = requests.get(
        weather_url_tpl,
        params={"key": qweather_key, "location": city_id},
        timeout=(10, 20)
    ).json()
    weather_list = []
    if weather_json.get('code') == '200':
        temp = weather_json['daily'][0]
        icon = get_weather_icon(temp['textDay'])
        tip = f"{icon} {county}{temp['textDay']}，{temp['tempMin']}~{temp['tempMax']}℃"
        weather_list.append(tip)

    type_id = random.randint(1, 16)
    life_json = requests.get(
        life_url_tpl,
        params={"type": type_id, "location": city_id, "key": qweather_key},
        timeout=(10, 20)
    ).json()
    if life_json.get('code') == '200':
        weather_list.append("👔 " + life_json['daily'][0]['text'])

    return '\n'.join(weather_list) if weather_list else None


def get_weather(city_name: str, qweather_key: str, qweather_host: str = '', max_tries: int = 3, retry_delay: float = 3.0):
    """带重试的天气获取，超时后最多重试 max_tries 次。"""
    last_exc = None
    for attempt in range(1, max_tries + 1):
        try:
            return _fetch_weather_once(city_name, qweather_key, qweather_host)
        except Exception as e:
            last_exc = e
            if attempt < max_tries:
                logger.warning("获取天气数据错误 [%s]（第%d次，共%d次）: %s，%.1fs 后重试",
                               city_name, attempt, max_tries, e, retry_delay)
                import time; time.sleep(retry_delay)
            else:
                logger.warning("获取天气数据错误 [%s]（第%d次，已放弃）: %s", city_name, attempt, e)
    return None


def get_map_weather(cfg):
    if not cfg.qweather_key or not cfg.city:
        return None
    host = getattr(cfg, 'qweather_host', '') or ''
    city_list = split_list(cfg.city)
    results = [get_weather(c, cfg.qweather_key, host) for c in city_list]
    results = [r for r in results if r]
    return '\n'.join(results) if results else None


# ─────────────────────── 日期模块 ───────────────────────

EMOTICONS = [
    "(￣▽￣)~*", "(～￣▽￣)～", "~(￣▽￣)~*", "(oﾟ▽ﾟ)o",
    "ヾ(✿ﾟ▽ﾟ)ノ", "٩(๑❛ᴗ❛๑)۶", "ヾ(◍°∇°◍)ﾉﾞ", "ヾ(๑╹◡╹)ﾉ",
    "(๑´ㅂ`๑)", "(*´ﾟ∀ﾟ｀)ﾉ", "(´▽`)ﾉ", "o(*￣▽￣*)o",
]

WEEK_LIST = ["星期日", "星期一", "星期二", "星期三", "星期四", "星期五", "星期六"]


def get_emoticon():
    return random.choice(EMOTICONS)


def get_today(call: str = ''):
    ndt = now_sh()
    d = ndt.strftime("%Y{y}%m{m}%d{d}").format(y='年', m='月', d='日')
    w = int(ndt.strftime("%w"))
    today_date = f"{d} {WEEK_LIST[w]}"
    hour = ndt.hour
    if hour < 6:
        time_tip = "凌晨好"
    elif hour < 9:
        time_tip = "早上好"
    elif hour < 12:
        time_tip = "上午好"
    elif hour < 13:
        time_tip = "中午好"
    elif hour < 18:
        time_tip = "下午好"
    else:
        time_tip = "晚上好"
    time_tip = f"{time_tip} ~ {get_emoticon()}"
    # 称呼末尾自动补中文顿号（若用户未手动加标点）
    if call:
        call_clean = call.rstrip()
        if call_clean and call_clean[-1] not in '，,、！!～~':
            call_clean += '，'
        today_tip = f"{call_clean}{time_tip}"
    else:
        today_tip = time_tip
    return {"today_date": today_date, "today_tip": today_tip}


def get_remain(target_day: str, target_name: str):
    """计算每年循环纪念日"""
    ndt = now_sh()
    today = date(ndt.year, ndt.month, ndt.day)
    this_year = ndt.year
    parts = target_day.split('-')
    is_lunar = parts[0].startswith('n')
    try:
        if is_lunar:
            lunar_month = int(parts[1])
            lunar_day = int(parts[2])
            if not HAS_ZHDATE:
                return None
            this_date = ZhDate(this_year, lunar_month, lunar_day).to_datetime().date()
        else:
            this_date = date(this_year, int(parts[1]), int(parts[2]))

        if today == this_date:
            return (f"🎂 今天是{target_name}，生日快乐！🎉", 0)
        elif today > this_date:
            if is_lunar:
                next_date = ZhDate(this_year + 1, lunar_month, lunar_day).to_datetime().date()
            else:
                next_date = date(this_year + 1, int(parts[1]), int(parts[2]))
            remain = (next_date - today).days
            return (f"📆 距离{target_name}还有 {remain} 天 ✨", remain)
        else:
            remain = (this_date - today).days
            return (f"📆 距离{target_name}还有 {remain} 天 ✨", remain)
    except Exception as e:
        logger.warning("计算纪念日错误 [%s]: %s", target_name, e)
        return None


def get_duration(begin_day: str, begin_name: str):
    """计算单日纪念日"""
    ndt = now_sh()
    today = date(ndt.year, ndt.month, ndt.day)
    parts = begin_day.split('-')
    is_lunar = parts[0].startswith('n')
    try:
        if is_lunar:
            year = int(parts[0][1:])
            lunar_month = int(parts[1])
            lunar_day = int(parts[2])
            if not HAS_ZHDATE:
                return None
            begin_date = ZhDate(year, lunar_month, lunar_day).to_datetime().date()
        else:
            begin_date = date(int(parts[0]), int(parts[1]), int(parts[2]))

        if today == begin_date:
            return (f"🌟 {begin_name}就是今天！🎊", 0)
        elif today > begin_date:
            days = (today - begin_date).days
            return (f"💑 {begin_name} {days} 天 💕", days)
        else:
            days = (begin_date - today).days
            return (f"⌛ 距离{begin_name}还有 {days} 天 🗓️", days)
    except Exception as e:
        logger.warning("计算单日错误 [%s]: %s", begin_name, e)
        return None


def get_map_days(cfg):
    """
    从 Anniversary 表读取纪念日，按天数排序拼接。
    必须在有 Flask app context 的线程里调用。
    build_messages() 在进入 FetchExecutor 之前同步调用此函数，保证 context 存在。
    """
    from app.models import Anniversary
    try:
        items = Anniversary.query.filter_by(user_id=cfg.user_id)\
                    .order_by(Anniversary.sort_order, Anniversary.id).all()
        days_list = []
        for item in items:
            if not item.date_str or not item.name:
                continue
            if item.ann_type == 'yearly':
                r = get_remain(item.date_str, item.name)
            else:
                r = get_duration(item.date_str, item.name)
            if r:
                days_list.append(r)
        if not days_list:
            return None
        days_list.sort(key=lambda x: x[1])
        return '\n'.join(x[0] for x in days_list)
    except Exception as e:
        logger.warning("get_map_days 查询失败: %s", e)
        return None


# ─────────────────────── 天行 API ───────────────────────

TIAN_API_TYPES = {
    'zaoan':     ('早安心语',   '🌅'),
    'healthtip': ('健康小提示', '💊'),
    'qiaomen':   ('生活小窍门', '🔑'),
    'pyqwenan':  ('朋友圈文案', '✨'),
    'tiangou':   ('舔狗日记',   '🐶'),
    'caihongpi': ('彩虹屁',     '🌈'),
    'star':      ('星座运势',   '⭐'),
}

STAR_LIST = ['白羊座','金牛座','双子座','巨蟹座','狮子座','处女座',
             '天秤座','天蝎座','射手座','摩羯座','水瓶座','双鱼座']


@_retry(max_tries=3, delay=3)
def get_chp(tian_key: str, tian_type: str = 'caihongpi', tian_astro: str = '双鱼座'):
    if not tian_key:
        return None
    api_type = tian_type if tian_type in TIAN_API_TYPES else 'caihongpi'
    icon = TIAN_API_TYPES[api_type][1]

    try:
        if api_type == 'star':
            # 星座运势接口参数不同，使用 apis.tianapi.com 域名
            astro = tian_astro if tian_astro in STAR_LIST else '双鱼座'
            date_str = now_sh().strftime('%Y-%m-%d')
            url = f"https://apis.tianapi.com/star/index?key={tian_key}&date={date_str}&astro={astro}"
            res = requests.get(url, timeout=(10, 15)).json()
            items = res['result']['list']
            # 只取"今日概述"的 content
            overview = next((i['content'] for i in items if i['type'] == '今日概述'), '')
            return f"{icon} {overview}"
        else:
            url = f"http://api.tianapi.com/{api_type}/index?key={tian_key}"
            res = requests.get(url, timeout=(10, 15)).json()
            return icon + ' ' + res['newslist'][0]['content']
    except Exception as e:
        logger.warning("获取天行API [%s] 错误: %s", api_type, e)
        return None


@_retry(max_tries=3, delay=3)
def get_bing():
    try:
        url = "https://cn.bing.com/HPImageArchive.aspx?format=js&idx=0&n=1"
        res = requests.get(url, timeout=(10, 15)).json()
        img = res['images'][0]
        pic = "https://cn.bing.com/" + img['url']
        title = img['title']
        return {"bing_pic": pic, "bing_tip": title}
    except Exception as e:
        logger.warning("获取必应数据错误: %s", e)
        return None


@_retry(max_tries=3, delay=3)
def get_random_pic(pictype: str = 'fengjing'):
    VALID_TYPES = {'meizi', 'dongman', 'fengjing', 'suiji'}
    types = [t.strip() for t in pictype.split('&&') if t.strip() in VALID_TYPES]
    p_type = random.choice(types) if types else 'fengjing'
    try:
        url = f"https://api.btstu.cn/sjbz/api.php?format=json&lx={p_type}"
        return requests.get(url, timeout=(10, 15)).json().get('imgurl')
    except Exception as e:
        logger.warning("获取随机图片错误: %s", e)
        return None


@_retry(max_tries=3, delay=3)
def get_ciba():
    try:
        res = requests.get("http://open.iciba.com/dsapi/", timeout=(10, 15)).json()
        tip = f"🔤 {res['content']}\n🀄️ {res['note']}"
        return {"ciba_tip": tip, "ciba_pic": res['fenxiang_img']}
    except Exception as e:
        logger.warning("获取金山词霸错误: %s", e)
        return None


# ─────────────────────── 消息组装 ───────────────────────

# 企业微信 news description 官方上限 512 字节，超出自动截断；代码用软截断处理


def _byte_len(s):
    """UTF-8 字节长度"""
    return len((s or '').encode('utf-8'))



# ─────────────────────── 发送 ───────────────────────

def get_wecom_token(corpid: str, corpsecret: str):
    try:
        url = "https://qyapi.weixin.qq.com/cgi-bin/gettoken"
        res = requests.get(url, params={"corpid": corpid, "corpsecret": corpsecret}, timeout=(10, 15)).json()
        if res.get('errcode') == 0:
            return res['access_token']
        logger.warning("获取企业微信Token失败: %s", res)
        return None
    except Exception as e:
        logger.warning("获取企业微信Token错误: %s", e)
        return None


def send_wecom_message(cfg):
    """
    执行推送（多条消息顺序发送），返回 (success: bool, message: str, steps: list)
    steps 格式: [{'name': str, 'status': 'ok'|'warn'|'skip'|'fail', 'msg': str}]
    """
    steps = []

    def step(name, status, msg):
        steps.append({'name': name, 'status': status, 'msg': msg})

    if not all([cfg.corpid, cfg.corpsecret, cfg.agentid]):
        step('企业微信配置', 'fail', '配置不完整（corpid/corpsecret/agentid）')
        return False, '企业微信配置不完整', steps

    # ── 1. 获取 Token ──
    token = get_wecom_token(cfg.corpid, cfg.corpsecret)
    if not token:
        step('获取 Token', 'fail', '获取 access_token 失败，请检查 corpid 和 corpsecret')
        return False, '获取企业微信 access_token 失败', steps
    step('获取 Token', 'ok', '成功')

    # ── 2. 组装消息（内部并发拉取各 API）──
    msgtype = str(cfg.msgtype) if cfg.msgtype else '1'
    pictype = cfg.pictype or 'fengjing'
    call    = cfg.call or ''

    today_data = get_today(call)
    today_date = today_data['today_date']
    _today_tip = today_data['today_tip']
    # 将用户自定义内容合并进日期问候，作为最高权重块
    today_tip = (_today_tip + '\n\n' + cfg.content) if cfg.content else _today_tip

    need_pic = (msgtype != '3' and pictype and pictype != 'none')
    days_tip = get_map_days(cfg)

    fetch_tasks = {
        'chp':      lambda: get_chp(cfg.tian_key, getattr(cfg, 'tian_type', 'caihongpi'), getattr(cfg, 'tian_astro', '双鱼座')),
        'weather':  lambda: get_map_weather(cfg),
        'ciba':     get_ciba,
        'rand_pic': (lambda: get_random_pic(pictype)) if need_pic else None,
        'bing':     get_bing if need_pic else None,
    }
    results = {}
    with FetchExecutor(max_workers=3) as pool:
        future_map = {pool.submit(fn): key for key, fn in fetch_tasks.items() if fn is not None}
        for future in as_completed(future_map, timeout=60):
            key = future_map[future]
            try:
                results[key] = future.result()
            except Exception as e:
                logger.warning("拉取 %s 数据失败: %s", key, e)
                results[key] = None

    weather_tip = results.get('weather')
    chp         = results.get('chp')
    ciba_data   = results.get('ciba')
    ciba_tip    = ciba_data['ciba_tip'] if ciba_data else None
    ciba_pic    = ciba_data['ciba_pic'] if ciba_data else None
    bing_data   = results.get('bing')
    bing_pic    = bing_data.get('bing_pic', '') if bing_data else ''
    art_pic     = results.get('rand_pic')
    cover       = art_pic or bing_pic or None

    # ── 记录各模块步骤 ──
    step('纪念日', 'ok' if days_tip else 'skip', '获取成功' if days_tip else '无纪念日数据')
    step('和风天气', 'ok' if weather_tip else 'warn', '获取成功' if weather_tip else '获取失败或未配置')
    step('天行数据', 'ok' if chp else ('skip' if not cfg.tian_key else 'warn'),
         '获取成功' if chp else ('未配置' if not cfg.tian_key else '获取失败'))
    step('金山词霸', 'ok' if ciba_tip else 'warn', '获取成功' if ciba_tip else '获取失败')
    if need_pic:
        step('封面图片', 'ok' if cover else 'warn', '获取成功' if cover else '获取失败，将无图发送')

    # ── 3. 构建消息体 ──
    # 权重顺序（高→低）：日期问候 > 和风天气 > 纪念日 > 天行数据 > 金山词霸
    # 规则：
    #   a. 只把成功获取到的内容放入候选列表
    #   b. 未超限时全部发送
    #   c. 超限时从权重最低的开始移除，直到不超限为止
    #   d. 高权重内容获取失败时，低权重内容可正常替补（不强制移除）

    art_title = (cfg.title + '\n' if cfg.title else '') + today_date
    base = {
        'touser': '@all', 'toparty': '', 'totag': '',
        'agentid': cfg.agentid,
        'enable_id_trans': 0,
        'enable_duplicate_check': 0,
        'duplicate_check_interval': 1800,
    }

    # 按权重从高到低排列，只收录成功获取的内容
    # 每项: (label, content)
    weighted_parts = []
    for label, content in [
        ('日期问候',   today_tip),
        ('和风天气',   weather_tip),
        ('纪念日',     days_tip),
        ('天行数据',   chp),
        ('金山词霸',   ciba_tip),
    ]:
        if content:
            weighted_parts.append((label, content))

    # 软截断：保留所有成功获取的内容，在字节边界截断到 512 字节
    def _soft_truncate(text: str, limit: int = 512) -> str:
        """按字节截断，确保不超过 limit，在完整字符边界截断，末尾加省略号。"""
        encoded = text.encode('utf-8')
        if len(encoded) <= limit:
            return text
        # 预留 3 字节给省略号 '…'
        truncated = encoded[:limit - 3].decode('utf-8', errors='ignore')
        return truncated + '…'

    if msgtype == '1':
        # 日期问候与和风天气之间单换行，其余块之间双换行
        parts_text = []
        prev_label = None
        for label, content in weighted_parts:
            if parts_text:
                sep = '\n' if (prev_label == '日期问候' and label == '和风天气') else '\n\n'
                parts_text.append(sep)
            parts_text.append(content)
            prev_label = label
        description = ''.join(parts_text)
        truncated = _soft_truncate(description, 512)
        if truncated != description:
            step('内容截断', 'warn', f'描述超过 512 字节，已在字节边界软截断')
        articles = [{'title': art_title, 'description': truncated, 'url': None, 'picurl': cover}]
        msg = {**base, 'msgtype': 'news', 'news': {'articles': articles}}

    elif msgtype == '2':
        articles = [{'title': art_title, 'description': today_tip, 'url': None, 'picurl': cover}]
        # 多图文每条单独一张卡片，按权重顺序追加（跳过日期问候，已作为第一条标题）
        for label, content in weighted_parts[1:]:
            articles.append({'title': content, 'description': content, 'url': None,
                              'picurl': ciba_pic if label == '金山词霸' else None})
        msg = {**base, 'msgtype': 'news', 'news': {'articles': articles}}

    else:
        full_text = art_title + '\n\n' + '\n\n'.join(c for _, c in weighted_parts)
        msg = {**base, 'msgtype': 'text', 'text': {'content': full_text}}

    # ── 4. 发送 ──
    url = f"https://qyapi.weixin.qq.com/cgi-bin/message/send?access_token={token}"
    try:
        res = requests.post(url, json=msg, timeout=(10, 15)).json()
        if res.get('errcode') == 0:
            step('企业微信发送', 'ok', '消息发送成功')
            return True, '推送成功', steps
        err_msg = f"errcode={res.get('errcode')} errmsg={res.get('errmsg','')}"
        step('企业微信发送', 'fail', f'发送失败：{err_msg}')
        return False, f'企业微信发送失败：{err_msg}', steps
    except Exception as e:
        step('企业微信发送', 'fail', f'发送异常：{e}')
        return False, f'企业微信发送异常：{e}', steps
