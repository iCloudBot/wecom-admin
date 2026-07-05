# 企业微信推送后台管理系统

基于 Flask + SQLite + APScheduler 构建的多用户企业微信消息定时推送管理平台。

## ✨ 功能特性

- **用户系统**：注册 / 登录 / 注销 / 修改密码，首个注册用户自动成为管理员
- **推送配置**：每用户独立配置企业微信应用、天气、纪念日、消息样式等
- **多用户定时任务**：APScheduler 驱动，时区 Asia/Shanghai，进程重启自动恢复
- **立即推送**：后台一键手动触发推送
- **推送日志**：记录每次推送的触发方式、状态与结果
- **管理员面板**：查看 / 删除所有用户及其数据

## 🗂️ 项目结构

```
wecom-admin/
├── run.py                  # 启动入口
├── requirements.txt
├── Dockerfile              # Docker 镜像构建文件
├── docker-compose.yml      # Docker Compose 编排文件
├── .env.example            # 环境变量示例
├── .dockerignore
├── data/                   # SQLite 数据库（自动创建）
│   └── wecom_admin.db
└── app/
    ├── __init__.py         # Flask 工厂函数
    ├── push_engine.py      # 核心推送引擎（天气/日期/API/组装/发送）
    ├── scheduler.py        # APScheduler 多用户定时任务管理器
    ├── models/
    │   └── __init__.py     # SQLAlchemy 数据模型
    ├── api/
    │   └── __init__.py     # REST API 蓝图
    └── static/
        └── index.html      # 前端 SPA（Vue 3 CDN）
```

## 🗄️ 数据库设计

| 表名 | 说明 |
|------|------|
| `users` | 用户账号 |
| `push_configs` | 每用户推送配置 |
| `scheduled_tasks` | 定时任务（hour/minute + 上海时区） |
| `push_logs` | 推送记录 |

## ⏰ 定时任务下次执行时间规则

以**创建或修改任务的当前时刻**为基准：

| 场景 | 举例 | 下次执行时间 |
|------|------|-------------|
| 设定时间**还未到** | 现在 11:13，设定 12:00 | 今天 `2026-05-14 12:00:00` |
| 设定时间**已过去** | 现在 11:13，设定 11:00 | 明天 `2026-05-15 11:00:00` |

进程重启（`load_all_jobs`）时同样以**重启时刻**为基准重新计算。

## 🚀 本地快速启动

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 启动服务
python run.py

# 3. 浏览器访问
open http://localhost:5000
```

## 🐳 Docker 部署

### 方式一：Docker Compose（推荐）

```bash
# 1. 复制并配置环境变量
cp .env.example .env
# 编辑 .env，将 SECRET_KEY 替换为强随机密钥
# 生成方法：python -c "import secrets; print(secrets.token_hex(32))"

# 2. 构建并启动
docker compose up -d

# 3. 查看日志
docker compose logs -f

# 4. 停止服务
docker compose down

# 5. 停止并清除数据卷（数据将丢失，谨慎使用）
docker compose down -v
```

服务启动后访问 **http://localhost:5000**

### 方式二：手动 Docker 命令

```bash
# 构建镜像
docker build -t wecom-admin:latest .

# 创建数据卷
docker volume create wecom_data

# 运行容器
docker run -d \
  --name wecom-admin \
  --restart unless-stopped \
  -p 5000:5000 \
  -v wecom_data:/app/data \
  -e SECRET_KEY=your-strong-secret-key \
  -e TZ=Asia/Shanghai \
  cleverest/wecom-admin:latest

# 查看日志
docker logs -f wecom-admin
```

### 数据持久化说明

SQLite 数据库文件存储在 `/app/data/wecom_admin.db`，通过 Docker Volume `wecom_data` 持久化到宿主机。容器重建、镜像升级均不会丢失数据。

升级镜像步骤：
```bash
docker compose pull   # 或重新 build
docker compose up -d  # 自动重建容器，数据 volume 保留
```

## ⚙️ 环境变量

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `SECRET_KEY` | Flask Session 密钥 | 内置默认值（**生产环境务必修改**） |
| `TZ` | 容器时区 | `Asia/Shanghai` |

## 📋 API 一览

### 认证
- `POST /api/auth/register` — 注册
- `POST /api/auth/login` — 登录
- `POST /api/auth/logout` — 注销
- `GET  /api/auth/me` — 获取当前用户
- `POST /api/auth/change-password` — 修改密码

### 推送配置
- `GET  /api/config` — 获取配置
- `PUT  /api/config` — 保存配置

### 定时任务
- `GET    /api/tasks` — 任务列表（含 `next_run` 下次执行时间）
- `POST   /api/tasks` — 创建任务
- `PUT    /api/tasks/:id` — 更新任务
- `DELETE /api/tasks/:id` — 删除任务
- `POST   /api/tasks/:id/toggle` — 启用/暂停

### 推送
- `POST /api/push/now` — 立即推送

### 日志
- `GET /api/logs?page=1&per_page=20` — 我的日志

### 管理员
- `GET    /api/admin/users` — 所有用户
- `DELETE /api/admin/users/:id` — 删除用户
- `GET    /api/admin/logs` — 所有日志
- `GET    /api/admin/scheduler/jobs` — 查看调度器任务

## 🔧 多用户定时任务实现原理

```
用户A创建任务 08:00  →  APScheduler.add_job(id="push_task_1", cron hour=8)
用户B创建任务 09:30  →  APScheduler.add_job(id="push_task_2", cron hour=9, minute=30)

触发时：
_run_push_job(user_id, task_id)
  └── 读取该用户的 PushConfig
  └── 调用 push_engine.send_wecom_message(cfg)
  └── 写入 PushLog
```

每个任务用独立 job_id（`push_task_{task_id}`），互不干扰。
