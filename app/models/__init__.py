from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime
import pytz

db = SQLAlchemy()
SHANGHAI_TZ = pytz.timezone('Asia/Shanghai')


def now_shanghai():
    return datetime.now(SHANGHAI_TZ).replace(tzinfo=None)


class User(UserMixin, db.Model):
    __tablename__ = 'users'
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(64), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    is_admin = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=now_shanghai)
    reg_ip      = db.Column(db.String(64),  default='')   # 注册时的 IP
    ip_location = db.Column(db.String(128), default='')   # 注册 IP 归属地

    configs = db.relationship('PushConfig', backref='user', lazy='dynamic', cascade='all, delete-orphan')
    tasks = db.relationship('ScheduledTask', backref='user', lazy='dynamic', cascade='all, delete-orphan')
    anniversaries = db.relationship('Anniversary', backref='user', lazy='dynamic', cascade='all, delete-orphan')

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    def to_dict(self):
        return {
            'id': self.id,
            'username': self.username,
            'is_admin': self.is_admin,
            'created_at': self.created_at.strftime('%Y-%m-%d %H:%M:%S') if self.created_at else '',
            'reg_ip':      self.reg_ip or '',
            'ip_location': self.ip_location or '',
        }


class PushConfig(db.Model):
    """企业微信推送配置，每个用户可有一套配置"""
    __tablename__ = 'push_configs'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)

    # 企业微信配置
    corpid = db.Column(db.String(128), default='')
    corpsecret = db.Column(db.String(256), default='')
    agentid = db.Column(db.String(32), default='')

    # 和风天气
    qweather_key  = db.Column(db.String(128), default='')
    qweather_host = db.Column(db.String(256), default='')
    city          = db.Column(db.String(256), default='')

    # 消息样式
    msgtype = db.Column(db.String(4), default='3')
    pictype = db.Column(db.String(64), default='fengjing')
    title = db.Column(db.String(256), default='')
    content = db.Column(db.Text, default='')
    call = db.Column(db.String(64), default='')

    # 天行API
    tian_key   = db.Column(db.String(128), default='')
    tian_type  = db.Column(db.String(32),  default='zaoan')
    tian_astro = db.Column(db.String(16),  default='双鱼座')

    updated_at = db.Column(db.DateTime, default=now_shanghai, onupdate=now_shanghai)

    def to_dict(self):
        return {
            'id': self.id,
            'user_id': self.user_id,
            'corpid': self.corpid,
            'corpsecret': self.corpsecret,
            'agentid': self.agentid,
            'qweather_key':  self.qweather_key,
            'qweather_host': self.qweather_host,
            'city':          self.city,
            'msgtype': self.msgtype,
            'pictype': self.pictype,
            'title': self.title,
            'content': self.content,
            'call': self.call,
            'tian_key':   self.tian_key,
            'tian_type':  self.tian_type,
            'tian_astro': self.tian_astro,
        }


class Anniversary(db.Model):
    """
    纪念日记录，每条独立存储，取代旧的 &&分隔方式。
    ann_type:
      'yearly'  每年循环（生日、结婚纪念日等）
      'once'    单次纪念日（在一起xx天、宝宝出生xx天）
    date_str: 日期字符串，农历前缀 n，如 n1999-09-09 或 2020-02-02
    """
    __tablename__ = 'anniversaries'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    name = db.Column(db.String(64), nullable=False, default='')
    ann_type = db.Column(db.String(8), default='yearly')  # yearly / once
    date_str = db.Column(db.String(32), default='')       # 2020-02-02 或 n1999-09-09
    sort_order = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=now_shanghai)

    def to_dict(self):
        return {
            'id': self.id,
            'user_id': self.user_id,
            'name': self.name,
            'ann_type': self.ann_type,
            'date_str': self.date_str,
            'sort_order': self.sort_order,
            'created_at': self.created_at.strftime('%Y-%m-%d %H:%M:%S') if self.created_at else '',
        }


class ScheduledTask(db.Model):
    """定时推送任务"""
    __tablename__ = 'scheduled_tasks'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    name = db.Column(db.String(128), default='每日推送')
    cron_hour = db.Column(db.Integer, default=8)
    cron_minute = db.Column(db.Integer, default=0)
    enabled = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=now_shanghai)
    updated_at = db.Column(db.DateTime, default=now_shanghai, onupdate=now_shanghai)

    def to_dict(self):
        return {
            'id': self.id,
            'user_id': self.user_id,
            'name': self.name,
            'cron_hour': self.cron_hour,
            'cron_minute': self.cron_minute,
            'enabled': self.enabled,
            'time_str': f"{self.cron_hour:02d}:{self.cron_minute:02d}",
            'created_at': self.created_at.strftime('%Y-%m-%d %H:%M:%S') if self.created_at else '',
        }


class PushLog(db.Model):
    """推送记录"""
    __tablename__ = 'push_logs'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    trigger = db.Column(db.String(16), default='manual')
    status = db.Column(db.String(16), default='success')
    message = db.Column(db.Text, default='')
    steps = db.Column(db.Text, default='')   # JSON: [{name, status, msg}]
    created_at = db.Column(db.DateTime, default=now_shanghai)

    user = db.relationship('User', backref=db.backref('logs', lazy='dynamic'))

    def to_dict(self):
        import json
        steps_data = []
        if self.steps:
            try:
                steps_data = json.loads(self.steps)
            except Exception:
                pass
        return {
            'id': self.id,
            'user_id': self.user_id,
            'username': self.user.username if self.user else '',
            'trigger': self.trigger,
            'status': self.status,
            'message': self.message,
            'steps': steps_data,
            'created_at': self.created_at.strftime('%Y-%m-%d %H:%M:%S') if self.created_at else '',
        }


class RegisterLog(db.Model):
    """今日注册限流凭证（仅当天有效，零点后自动清理）"""
    __tablename__ = 'register_logs'
    id         = db.Column(db.Integer, primary_key=True)
    ip         = db.Column(db.String(64), nullable=False, index=True)
    reg_date   = db.Column(db.String(10), nullable=False)
    username   = db.Column(db.String(64), default='')
    created_at = db.Column(db.DateTime, default=now_shanghai)

    __table_args__ = (
        db.UniqueConstraint('ip', 'reg_date', name='uq_ip_date'),
    )

    def to_dict(self):
        return {
            'id':         self.id,
            'ip':         self.ip,
            'reg_date':   self.reg_date,
            'username':   self.username,
            'created_at': self.created_at.strftime('%Y-%m-%d %H:%M:%S') if self.created_at else '',
        }
