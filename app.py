"""
五子棋 FastAPI 后端 - 阶段 1
- 单机 AI 对战（POST /api/move, /api/undo, /api/resign）
- 阶段 2 会在此基础上加 Socket.IO 房间系统
"""
import asyncio
import copy
import time
import uuid
from typing import Dict, List, Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import engine as e

app = FastAPI(title="Gomoku", version="1.0")

# CORS: 允许直接访问（如果将来需要跨域）
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ─────────────── 单机 AI 对战房间 ───────────────
# key: game_id (UUID 字符串)
# 内存存储，进程重启清空
AI_GAMES: Dict[str, dict] = {}

MAX_UNDO_PER_GAME = 3
LEVEL_TIME_LIMITS = {
    "easy": 100,      # ms，前端跑不需要限制
    "medium": 2000,   # 2s
    "hard": 3500,     # 3.5s
}


class NewGameReq(BaseModel):
    level: str = "medium"  # easy | medium | hard


class NewGameResp(BaseModel):
    game_id: str
    level: str
    board: List[List[int]]
    your_color: int  # 固定 1=黑
    ai_color: int    # 固定 2=白
    undo_left: int


class MoveReq(BaseModel):
    game_id: str
    x: int
    y: int


class MoveResp(BaseModel):
    # 玩家落子结果
    player_x: int
    player_y: int
    # AI 回应
    ai_x: int
    ai_y: int
    ai_thinking_ms: int
    # 状态
    board: List[List[int]]
    winner: int  # 0=继续, 1=黑胜, 2=白胜, 3=平局
    win_line: Optional[List[List[int]]]  # [[x,y], ...]
    last_move: List[int]  # [x, y]，用于高亮
    undo_left: int


class UndoResp(BaseModel):
    board: List[List[int]]
    undo_left: int
    last_move: Optional[List[int]]  # 悔棋后，最后一手是哪个


class ResignResp(BaseModel):
    winner: int  # 对手胜
    board: List[List[int]]


# ─────────────── 辅助函数 ───────────────
def _empty_board() -> List[List[int]]:
    return [[0] * e.BOARD_SIZE for _ in range(e.BOARD_SIZE)]


def _check_full(board: List[List[int]]) -> bool:
    for row in board:
        for c in row:
            if c == e.EMPTY:
                return False
    return True


def _new_ai_game(level: str) -> dict:
    return {
        "level": level,
        "board": _empty_board(),
        "history": [],          # [(x, y, color), ...]
        "undo_left": MAX_UNDO_PER_GAME,
        "last_move": None,      # (x, y)
        "winner": 0,            # 0=进行中, 1=黑胜, 2=白胜, 3=平局
        "win_line": None,
        "created_at": time.time(),
    }


def _serialize(g: dict) -> dict:
    return {
        "level": g["level"],
        "board": g["board"],
        "history": g["history"],
        "undo_left": g["undo_left"],
        "last_move": g["last_move"],
        "winner": g["winner"],
        "win_line": g["win_line"],
    }


# ─────────────── API 路由 ───────────────
@app.post("/api/new_game", response_model=NewGameResp)
def new_game(req: NewGameReq):
    if req.level not in LEVEL_TIME_LIMITS:
        raise HTTPException(400, f"level 必须为 easy/medium/hard，收到 {req.level}")
    gid = uuid.uuid4().hex
    AI_GAMES[gid] = _new_ai_game(req.level)
    g = AI_GAMES[gid]
    return NewGameResp(
        game_id=gid,
        level=g["level"],
        board=g["board"],
        your_color=e.BLACK,
        ai_color=e.WHITE,
        undo_left=g["undo_left"],
    )


@app.post("/api/move", response_model=MoveResp)
def move(req: MoveReq):
    if req.game_id not in AI_GAMES:
        raise HTTPException(404, "游戏不存在或已结束")
    g = AI_GAMES[req.game_id]
    if g["winner"] != 0:
        raise HTTPException(400, "对局已结束，请开新局")

    # 1. 玩家落子
    if not (0 <= req.x < e.BOARD_SIZE and 0 <= req.y < e.BOARD_SIZE):
        raise HTTPException(400, "坐标越界")
    if g["board"][req.y][req.x] != e.EMPTY:
        raise HTTPException(400, "该位置已有棋子")

    g["board"][req.y][req.x] = e.BLACK
    g["history"].append((req.x, req.y, e.BLACK))
    g["last_move"] = (req.x, req.y)

    # 玩家五连判胜
    win_line = None
    if e.check_win(g["board"], req.x, req.y, e.BLACK):
        g["winner"] = e.BLACK
        win_line = e.find_winning_line(g["board"], req.x, req.y, e.BLACK)
        g["win_line"] = win_line
        return MoveResp(
            player_x=req.x, player_y=req.y,
            ai_x=-1, ai_y=-1, ai_thinking_ms=0,
            board=g["board"],
            winner=g["winner"],
            win_line=win_line,
            last_move=list(g["last_move"]),
            undo_left=g["undo_left"],
        )

    # 玩家落子后棋盘满 → 平局
    if _check_full(g["board"]):
        g["winner"] = 3  # 平局
        return MoveResp(
            player_x=req.x, player_y=req.y,
            ai_x=-1, ai_y=-1, ai_thinking_ms=0,
            board=g["board"],
            winner=g["winner"],
            win_line=None,
            last_move=list(g["last_move"]),
            undo_left=g["undo_left"],
        )

    # 2. AI 落子
    t0 = time.time()
    time_limit = LEVEL_TIME_LIMITS[g["level"]]
    ax, ay, _ = e.ai_move(g["board"], e.WHITE, g["level"], time_limit_ms=time_limit)
    thinking_ms = int((time.time() - t0) * 1000)

    g["board"][ay][ax] = e.WHITE
    g["history"].append((ax, ay, e.WHITE))
    g["last_move"] = (ax, ay)

    if e.check_win(g["board"], ax, ay, e.WHITE):
        g["winner"] = e.WHITE
        g["win_line"] = e.find_winning_line(g["board"], ax, ay, e.WHITE)
    elif _check_full(g["board"]):
        g["winner"] = 3

    return MoveResp(
        player_x=req.x, player_y=req.y,
        ai_x=ax, ai_y=ay, ai_thinking_ms=thinking_ms,
        board=g["board"],
        winner=g["winner"],
        win_line=g["win_line"],
        last_move=list(g["last_move"]),
        undo_left=g["undo_left"],
    )


@app.post("/api/undo", response_model=UndoResp)
def undo(req: MoveReq):
    if req.game_id not in AI_GAMES:
        raise HTTPException(404, "游戏不存在或已结束")
    g = AI_GAMES[req.game_id]
    if g["winner"] != 0:
        raise HTTPException(400, "对局已结束，无法悔棋")
    if g["undo_left"] <= 0:
        raise HTTPException(400, "悔棋次数已用完")
    # 历史至少要 2 手（玩家 + AI）
    if len(g["history"]) < 2:
        raise HTTPException(400, "对局刚开始，无棋可悔")

    # 撤销最近 2 手（AI 上一手 + 玩家上一手）
    for _ in range(2):
        if not g["history"]:
            break
        x, y, _ = g["history"].pop()
        g["board"][y][x] = e.EMPTY

    g["undo_left"] -= 1
    # 更新 last_move 为玩家撤销后那一手
    if g["history"]:
        last = g["history"][-1]
        g["last_move"] = (last[0], last[1])
    else:
        g["last_move"] = None

    return UndoResp(
        board=g["board"],
        undo_left=g["undo_left"],
        last_move=list(g["last_move"]) if g["last_move"] else None,
    )


@app.post("/api/resign", response_model=ResignResp)
def resign(req: MoveReq):
    if req.game_id not in AI_GAMES:
        raise HTTPException(404, "游戏不存在或已结束")
    g = AI_GAMES[req.game_id]
    if g["winner"] != 0:
        # 已结束的状态下投降不报错，幂等
        return ResignResp(winner=g["winner"], board=g["board"])
    g["winner"] = e.WHITE  # AI 胜
    return ResignResp(winner=g["winner"], board=g["board"])


@app.post("/api/restart", response_model=NewGameResp)
def restart(req: NewGameReq):
    """重开一局（投降后或正常结束后调用）"""
    if req.level not in LEVEL_TIME_LIMITS:
        raise HTTPException(400, f"level 必须为 easy/medium/hard")
    gid = uuid.uuid4().hex
    AI_GAMES[gid] = _new_ai_game(req.level)
    g = AI_GAMES[gid]
    return NewGameResp(
        game_id=gid,
        level=g["level"],
        board=g["board"],
        your_color=e.BLACK,
        ai_color=e.WHITE,
        undo_left=g["undo_left"],
    )


@app.get("/api/health")
def health():
    return {"ok": True, "ai_games": len(AI_GAMES)}


# ─────────────── 静态文件 (放在最后避免被 API 路由拦截) ───────────────
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
def index():
    """五子棋入口（也兼容 /gomoku/ 反代后的 / 路径）"""
    return FileResponse("static/index.html")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=3100, log_level="info")
