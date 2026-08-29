# AIFictionForge 项目约定

## 项目定位
- **仅本地开发**：本项目只在本机运行，不涉及 PR/branch 工作流、不推送远程、不创建 feature 分支。
- 修改直接提交到 `main` 分支（用户明确要求时除外）。

## 服务管理（aistoryforge.sh）
项目根目录提供 `aistoryforge.sh` 服务管理脚本，管理后端（uvicorn:8008）与前端（vite:5173）：

```bash
cd /Users/andyhsu/codehouse/MuMuAINovel

./aistoryforge.sh status              # 查看两个服务状态（PID + 健康检查）
./aistoryforge.sh start               # 启动全部（后端 + 前端）
./aistoryforge.sh stop                # 停止全部
./aistoryforge.sh restart             # 重启全部
./aistoryforge.sh restart backend     # 仅重启后端
./aistoryforge.sh start frontend      # 仅启动前端
./aistoryforge.sh stop backend        # 仅停止后端
```

- 日志：后端 `/tmp/aistoryforge-backend.log`、前端 `/tmp/aistoryforge-frontend.log`
- 后端健康检查：`curl http://localhost:8008/health` 应返回 `{"status":"ok"}`

## 环境约定
- **端口**：后端 8008（`.env` 的 `APP_PORT`；8000 被本机 SillyTavern 占用）、前端 5173（Vite dev，代理指向 8008）
- **数据库**：SQLite `backend/data/mumuai_novel.db`（本地开发不用 PostgreSQL）
- **后端启动**：必须从项目根目录以 `PYTHONPATH=backend` 启动（否则 pydantic 读不到根目录 `.env`）；venv 在 `backend/.venv`（Python 3.12）
- **前端**：`frontend/` 下 `npm run dev`；`vite.config.ts` 代理 `/api` 与 `/generated-assets` 到 8008
- **测试**：`cd backend && .venv/bin/python -m pytest tests/ -v`（pytest 仅装在 venv，不进 requirements.txt）
- **Embedding 模型**：已缓存于 `backend/embedding/onnx/`，启动自动加载

## 工作偏好
- **不要反复询问**：服务管理（启动/停止/重启/状态）、本地提交、常规运维操作直接执行，不要每次征求同意。
- 涉及删除数据、修改 `.env`、改动数据库 schema 等破坏性操作时仍需确认。
