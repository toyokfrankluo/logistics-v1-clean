# Logistics V1

Python 3.11. 本地演示用 `sh run_local.sh`。自动化测试用
`python -B -m unittest discover -s tests -p 'test_*.py'`，测试使用临时库和模拟响应。

Koyeb 使用 `gunicorn --bind "0.0.0.0:$PORT" app:app`。设置 DATABASE_URL 为新数据库的连接地址，FLASK_SECRET 为新生成的随机密钥，PORT 与服务暴露端口一致。平台变量需在服务设置中注入，应用不会读取 .env。请不要在部署环境设置 LOGISTICS_ISOLATED。

Gunicorn 导入只注册应用与路由。建表、迁移及首个管理员需另行审批和准备，本版本启动时不会执行。物流商及 Supabase HTTP 调用在生产入口被禁用。不要配置旧数据库连接或旧集成凭据。

## Render 测试版代理 HTTP 访问

管理员在 /agents 保存 API URL、App Key 和 App Token 后，已启用且支持 API 的代理通过 carrier_http.py 发起请求。地址动态取自所选运单关联代理，不维护供应商域名白名单。现有物流商参数与认证编码由 app/views 原分支负责；新供应商仍须符合已有协议之一，配置地址并不自动生成新的协议适配器。

仅支持 HTTP/80、HTTPS/443。每次请求只解析一次 DNS，任一地址为内网、回环、链路本地、元数据或其他特殊地址即拒绝。连接使用已验证的数字 IP，直接建立 socket 并核对对端，HTTPS 使用原域名做 SNI 与证书校验。请求不使用环境代理，不跟随重定向，也不自动重试。隔离测试环境继续拒绝外网，公共查询 transport 和独立 17Track/Supabase 的通用网络禁用保持现有边界。

App Key/Token 存入现有数据库字段，查询时使用。/agents 列表只显示是否配置，编辑留空保留原值。Flask-Admin 排除凭据字段，修改凭据统一使用 /agents。此改动不提供数据库静态加密。HTTP 的凭据会经明文链路传输，优先为支持 HTTPS 的供应商配置 HTTPS。

RTB56 已选运单查询恢复旧 call_gettrack 的 tracking_number 请求方式。必须使用同一运单的代理凭据，且供应商返回的 server_hawbcode 必须与发送单号完全一致；缺失、不匹配、多票或未验证响应继续拒绝。同一代理配置下若另一票也使用此次查询单号，即使客户筛选只显示一票，也会在请求前拒绝。shipment_id 不作为自动回退单号。无域名白名单只解决安全网络传输，不代表已验证所有供应商协议。

本轮所有网络行为均用虚构响应和 socket/DNS 模拟测试，未连接真实物流商或业务数据库。

RTB56 恢复依据是旧版本代码及用户确认的旧系统可用行为，尚未通过真实供应商响应核验；若返回服务商单号与客户查询单号不同，本版本仍阻断，需要单独确认映射，不能自动归票。
