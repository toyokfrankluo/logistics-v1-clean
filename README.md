# Logistics V1

Python 3.11. 本地演示用 `sh run_local.sh`。自动化测试用
`python -B -m unittest discover -s tests -p 'test_*.py'`，测试使用临时库和模拟响应。

Koyeb 使用 `gunicorn --bind "0.0.0.0:$PORT" app:app`。设置 DATABASE_URL 为新数据库的连接地址，FLASK_SECRET 为新生成的随机密钥，PORT 与服务暴露端口一致。平台变量需在服务设置中注入，应用不会读取 .env。请不要在部署环境设置 LOGISTICS_ISOLATED。

Gunicorn 导入只注册应用与路由。建表、迁移及首个管理员需另行审批和准备，本版本启动时不会执行。物流商及 Supabase HTTP 调用在生产入口被禁用。不要配置旧数据库连接或旧集成凭据。
