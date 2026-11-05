# Gomoku 五子棋

五子棋游戏，支持 **AI 对战** 和 **玩家开房间 PvP 对战**。

## 目录约定（重要）

| 路径 | 角色 |
|---|---|
| `/root/.openclaw/workspace/gomoku/` | **开发源目录**（真源）。所有代码改动都改这里。 |
| `/opt/gomoku/` | **部署副本**。systemd 服务的 `WorkingDirectory`（历史遗留）。 |

⚠️ **当前 systemd 服务的 `WorkingDirectory` 已指向本目录**（2026-10-15 改的），`/opt/gomoku/` 现在是过期的、不会自动同步的历史副本。**不要再去改 `/opt/gomoku/` 里的文件**——改了也不会生效，下次重启还会被覆盖。

如果你确实需要把代码再回滚到 `/opt`（比如临时调试），手动改回去即可：

```bash
# 回滚到 /opt
sudo sed -i 's|WorkingDirectory=/root/.openclaw/workspace/gomoku|WorkingDirectory=/opt/gomoku|' /etc/systemd/system/gomoku.service
sudo systemctl daemon-reload && sudo systemctl restart gomoku
```

## 部署

| 项 | 值 |
|---|---|
| 入口 URL | `http://106.54.216.226/gomoku/` |
| 后端监听 | `127.0.0.1:3100` (uvicorn) |
| systemd 服务 | `gomoku.service`（enabled） |
| Caddy 反代 | `/etc/caddy/Caddyfile:128-134` `handle_path /gomoku* → :3100` |
| 启动命令 | `python3 -u rooms.py` |

修改代码后的部署流程：

```bash
# 1. 改完代码后，重启服务即可
sudo systemctl restart gomoku
sudo systemctl status gomoku
# 2. 验证
curl http://127.0.0.1:3100/api/health
```

## 文件结构

```
gomoku/
├── README.md         本文件
├── app.py            早期版本，未使用（保留作参考）
├── engine.py         AI 引擎（20K，含 minimax/alpha-beta/评估函数）
├── rooms.py          FastAPI 主服务（22K，PvP 房间逻辑 + Socket.IO）
├── static/
│   └── index.html    前端单页应用（含棋盘 UI + 房间系统）
└── templates/        空（前端用 static/ 里的 index.html）
```

## 已知问题 / TODO

- [ ] FastAPI `@app.on_event("startup")` 已 deprecated，启动时会有 DeprecationWarning，建议改用 `lifespan` 事件
- [ ] `__pycache__/` 目录未加入忽略，部署时混进 .pyc 不影响但可以清理
- [ ] 没有 git 仓库（用户 2026-10-15 决定暂不引入 git）
- [ ] 房间和 AI 对战数据存内存 dict，重启即清空
- [ ] `app.py` 是早期未使用版本，可考虑删除或归档

## 端口冲突

`127.0.0.1:3100` 是本服务专用，**不要被其他应用占用**。systemd `Restart=always` + `RestartSec=5` 会自动拉起崩溃的进程。
