# -*- coding: utf-8 -*-
import os
from flask import Flask, send_from_directory
from flask_login import LoginManager
from app.models import db, User


def create_app(config=None):
    app = Flask(__name__, static_folder='static', template_folder='templates')

    # ─── 基础配置 ───
    BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    DB_PATH = os.path.join(BASE_DIR, 'data', 'wecom_admin.db')
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

    app.config.update(
        SECRET_KEY=os.environ.get('SECRET_KEY', 'wecom-admin-secret-key-change-in-prod'),
        SQLALCHEMY_DATABASE_URI=f'sqlite:///{DB_PATH}',
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE='Lax',
        SESSION_COOKIE_SECURE=False,   # 兼容 http 部署；https 时改为 True
        SESSION_COOKIE_NAME='wechat_push_sess',
        PERMANENT_SESSION_LIFETIME=86400 * 7,
    )
    if config:
        app.config.update(config)

    # ─── 初始化扩展 ───
    db.init_app(app)

    # SQLite WAL 模式：允许读写并发，减少线程间锁等待
    # 资源受限环境下多线程推送任务同时写 push_logs 时不再互相阻塞
    from sqlalchemy import event
    from sqlalchemy.engine import Engine
    import sqlite3

    @event.listens_for(Engine, "connect")
    def set_sqlite_pragma(dbapi_conn, connection_record):
        if isinstance(dbapi_conn, sqlite3.Connection):
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")   # 写不阻塞读
            cursor.execute("PRAGMA synchronous=NORMAL") # 比 FULL 快，比 OFF 安全
            cursor.execute("PRAGMA cache_size=-16000")  # 16MB 页缓存
            cursor.execute("PRAGMA temp_store=MEMORY")  # 临时表放内存
            cursor.close()

    login_manager = LoginManager(app)
    login_manager.login_view = None  # API 模式，不跳转

    @login_manager.user_loader
    def load_user(user_id):
        return db.session.get(User, int(user_id))

    @login_manager.unauthorized_handler
    def unauthorized():
        from flask import jsonify
        return jsonify({'code': 401, 'msg': '请先登录'}), 401

    # ─── 注册蓝图 ───
    from app.api import api
    app.register_blueprint(api)

    # ─── 静态资源路由（favicon / manifest 等，优先于 SPA catch-all） ───
    @app.route('/favicon.ico')
    def favicon():
        return send_from_directory(app.static_folder, 'favicon.ico',
                                   mimetype='image/vnd.microsoft.icon')

    @app.route('/robots.txt')
    def robots():
        from flask import Response
        return Response('User-agent: *\nDisallow: /api/\n',
                        mimetype='text/plain')

    # ─── 简介首页 ───
    @app.route('/', methods=['GET', 'HEAD'])
    def landing():
        return send_from_directory(app.static_folder, 'landing.html')

    # ─── SPA 登录注册页 /auth ───
    @app.route('/auth', defaults={"_p": ""}, methods=['GET', 'POST', 'HEAD'])
    @app.route('/auth/<path:_p>', methods=['GET', 'POST', 'HEAD'])
    def serve_spa(_p):
        return send_from_directory(app.static_folder, 'index.html')

    # ─── 静态资源 & SPA 兜底 ───
    @app.route('/<path:path>', methods=['GET', 'POST', 'HEAD'])
    def serve_static(path):
        static_file = os.path.join(app.static_folder, path)
        if os.path.exists(static_file):
            return send_from_directory(app.static_folder, path)
        return send_from_directory(app.static_folder, 'index.html')

    # ─── 建表 + 自动迁移 ───
    with app.app_context():
        db.create_all()
        _migrate_db(db)


    # ─── 请求访问日志（带用户名）───
    import logging as _logging
    _access_log = _logging.getLogger('access')

    @app.after_request
    def log_request(resp):
        from flask import request as _req
        from flask_login import current_user
        from app.api import get_real_ip
        try:
            user = current_user.username if current_user and current_user.is_authenticated else 'anonymous'
        except Exception:
            user = 'anonymous'
        if not _req.path.startswith('/static'):
            _access_log.info('[%s] - [%s] %s %s %s', get_real_ip(), user, _req.method, _req.path, resp.status_code)
        return resp

    # ─── 静态资源缓存头 ───
    @app.after_request
    def set_cache_headers(resp):
        path = resp.request.path if hasattr(resp, 'request') else ''
        from flask import request as _req
        p = _req.path
        # JS/CSS/图片/字体：缓存 30 天（内容不变时浏览器直接用缓存）
        if p.endswith(('.js', '.css', '.jpg', '.jpeg', '.png', '.gif', '.webp', '.ico', '.woff2', '.woff')):
            resp.headers['Cache-Control'] = 'public, max-age=2592000, immutable'
        # HTML 页面：不缓存，保证每次拿最新版
        elif p in ('/', '/auth') or p.startswith('/auth/'):
            resp.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
            resp.headers['Pragma'] = 'no-cache'
        return resp

    return app


def _migrate_db(db):
    """启动时自动补全缺失的列/表，兼容旧数据库，无需手动 ALTER TABLE"""
    from sqlalchemy import text, inspect
    import logging
    log = logging.getLogger(__name__)
    inspector = inspect(db.engine)

    # ── push_configs 新增列 ──
    if 'push_configs' in inspector.get_table_names():
        existing_pc = {c['name'] for c in inspector.get_columns('push_configs')}
        pc_new_cols = [
            ('tian_type',  'VARCHAR(32)',  "'zaoan'"),
            ('tian_astro', 'VARCHAR(16)',  "'双鱼座'"),
        ]
        with db.engine.connect() as conn:
            for col_name, col_type, default in pc_new_cols:
                if col_name not in existing_pc:
                    conn.execute(text(
                        f"ALTER TABLE push_configs ADD COLUMN {col_name} {col_type} DEFAULT {default}"
                    ))
                    conn.commit()
                    log.info(f"DB迁移：push_configs 新增列 {col_name}")

    # ── users 新增 reg_ip 列 ──
    if 'users' in inspector.get_table_names():
        existing_u = {c['name'] for c in inspector.get_columns('users')}
        if 'reg_ip' not in existing_u:
            with db.engine.connect() as conn:
                conn.execute(text("ALTER TABLE users ADD COLUMN reg_ip VARCHAR(64) DEFAULT ''"))
                conn.commit()
                log.info("DB迁移：users 新增列 reg_ip")

    # ── anniversaries / register_logs 表由 db.create_all() 自动创建 ──
