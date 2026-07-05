# -*- coding: utf-8 -*-
"""
企业微信消息推送后台管理系统
启动: python run.py
"""
import logging
from app import create_app
from app.scheduler import init_scheduler, load_all_jobs

import logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
# Werkzeug 自带的请求日志与 access logger 重复，静默掉
logging.getLogger('werkzeug').setLevel(logging.WARNING)

app = create_app()

if __name__ == '__main__':
    scheduler = init_scheduler(app)
    load_all_jobs(app)
    print("\n🚀 企业微信推送管理后台已启动")
    print("📌 访问地址: http://localhost:5000\n")
    # threaded=True（默认）：Flask 为每个 HTTP 请求分配独立线程，
    # 与 APScheduler 的后台线程池互不干扰，推送任务不会阻塞页面访问。
    app.run(host='0.0.0.0', port=5000, debug=False, use_reloader=False, threaded=True)
