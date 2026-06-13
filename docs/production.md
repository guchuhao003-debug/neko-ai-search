# Neko AI Search 生产部署指南

这份文档用于上线前配置、启动和验证。当前推荐生产方案为单台 VPS、Docker Compose
和 Caddy，域名为 `conda.asia`。

## 1. 环境变量

后端复制示例文件：

```powershell
Copy-Item backend\.env.example backend\.env
```

生产环境至少需要配置：

- `FRONTEND_ORIGINS`：前端正式域名，例如 `https://search.example.com`。
- `TAVILY_API_KEY`：Tavily 搜索 API Key。
- `DEEPSEEK_API_KEY`：DeepSeek API Key。
- `SESSION_COOKIE_SECURE=true`：HTTPS 部署时必须开启。
- `ADMIN_EMAILS`：管理员邮箱，多个邮箱使用英文逗号分隔。
- `TRUSTED_PROXY_IPS`：反向代理、负载均衡或网关的真实直连 IP/CIDR。

前端复制示例文件：

```powershell
Copy-Item frontend\.env.example frontend\.env
```

如果前后端不同域名部署，将 `VITE_API_BASE_URL` 设置为后端公开地址，例如
`https://api.search.example.com`。

## 2. 本机生产构建检查

后端：

```powershell
cd backend
.\.venv\Scripts\python.exe -m pytest
```

前端：

```powershell
cd frontend
npm ci
npm run build
npm audit --audit-level=moderate
```

## 3. conda.asia 生产部署

生产部署使用 `docker-compose.prod.yml`，外部只开放 `80/443`，Caddy 负责 HTTPS 和反向
代理：

- `https://conda.asia`：前端页面。
- `https://conda.asia/api/*`：后端 API。
- `https://conda.asia/health`：后端健康检查。

服务器前置条件：

- 域名 `conda.asia` 的 A 记录已指向服务器 IP。
- 云服务器安全组已开放 `80/tcp`、`443/tcp`、`443/udp`。
- 服务器已安装 Docker 和 Docker Compose plugin。
- 项目代码已上传到服务器，例如 `/opt/neko-ai-search`。

首次部署时，在服务器项目目录执行：

```bash
cp deploy/backend.env.example deploy/backend.env
```

然后编辑 `deploy/backend.env`，填入真实的 `TAVILY_API_KEY` 和 `DEEPSEEK_API_KEY`。这个
文件已被 `deploy/.gitignore` 排除，不应提交到仓库。

启动生产服务：

```bash
docker compose -f docker-compose.prod.yml up --build -d
```

查看服务状态：

```bash
docker compose -f docker-compose.prod.yml ps
docker compose -f docker-compose.prod.yml logs -f caddy
docker compose -f docker-compose.prod.yml logs -f backend
```

停止服务：

```bash
docker compose -f docker-compose.prod.yml down
```

更新部署：

```bash
git pull
docker compose -f docker-compose.prod.yml up --build -d
```

默认本地开发端口仍由 `docker-compose.yml` 提供：

- 前端：`http://localhost:5173`
- 后端：`http://localhost:8000`
- 健康检查：`http://localhost:8000/health`

查看日志：

```powershell
docker compose logs -f backend
docker compose logs -f frontend
```

本地开发启动命令：

```powershell
docker compose up --build -d
```

## 4. 反向代理建议

生产环境默认使用 Caddy 统一终止 HTTPS，然后转发到前端和后端。上线时需要确保：

- `FRONTEND_ORIGINS` 只包含可信前端域名。
- `TRUSTED_PROXY_IPS` 只包含反向代理的直连 IP 或内网 CIDR。Docker 单机部署默认使用
  `172.16.0.0/12`，因为后端只在 Docker 网络中暴露。
- 不要直接信任公网传入的 `x-forwarded-for`。
- `SESSION_COOKIE_SECURE=true`，并使用 HTTPS 访问前端和 API。
- `backend/data` 使用持久化磁盘，避免用户、积分和限流数据随容器重建丢失。
- 不要把后端 `8000` 端口直接暴露到公网。

## 5. 缓存与积分规则

搜索请求必须登录。缓存命中时会返回已有回答，不扣积分，也不消耗 Tavily/DeepSeek 外部
调用配额；缓存未命中时会按模式扣积分：

- 快速模式：扣 `1` 积分。
- 深度模式：扣 `3` 积分。

这能避免匿名用户通过缓存读取历史结果，同时保留缓存节省成本的收益。

## 6. 上线前核对清单

- 后端测试全部通过。
- 前端构建通过。
- `npm audit --audit-level=moderate` 无中高危漏洞。
- `.env` 没有提交到仓库。
- 生产 Key 已在服务器环境配置，不写入镜像。
- 管理员邮箱已加入 `ADMIN_EMAILS`。
- 反向代理 IP 已加入 `TRUSTED_PROXY_IPS`。
- HTTPS 和安全 Cookie 已启用。
- `https://conda.asia/health` 返回 `{"status":"ok","service":"neko-ai-search"}`。
