"""
五子棋房间系统 - 阶段 2

房间数据 (内存)：
{
  "room_id": "1234",                  # 4 位数字字符串
  "host_sid": "socket_session_id",    # 房主 socket id
  "guest_sid": null,                 # 加入者 socket id
  "host_name": "玩家A",
  "guest_name": null,
  "board": [[0]*15 for _ in range(15)],
  "history": [(x, y, color), ...],
  "last_move": null,
  "winner": 0,                       # 0=进行中, 1=黑胜, 2=白胜, 3=平局
  "win_line": null,
  "current_turn": 1,                 # 当前该谁下 (1=黑 2=白)
  "undo_left": {1: 3, 2: 3},         # 每人剩余悔棋次数
  "pending_undo": null,              # 待对方同意的悔棋请求
  "pending_restart": null,           # 待对方同意的重开请求
  "created_at": 1234567890,          # 用于 5 分钟 TTL
  "host_connected": True,
  "guest_connected": False,
}

房间状态机：
  WAITING  → 房主创建, 等人加入 (5 分钟 TTL)
  PLAYING  → 2 人到齐, 游戏中
  FINISHED → 有人五胜/平局, 等待重开
  CLOSED   → 任一方离开/超时, 清理
"""
import asyncio
import random
import time
import string
from typing import Dict, Optional, List, Tuple

import engine as e
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import socketio

# ─────────────── 房间存储 ───────────────
ROOMS: Dict[str, dict] = {}
ROOM_LOCK = asyncio.Lock()
ROOM_TTL_SECONDS = 300  # 5 分钟没人加入 → 自动解散


def _new_room(room_id: str, host_sid: str, host_name: str) -> dict:
    return {
        "room_id": room_id,
        "host_sid": host_sid,
        "guest_sid": None,
        "host_name": host_name,
        "guest_name": None,
        "board": [[0] * e.BOARD_SIZE for _ in range(e.BOARD_SIZE)],
        "history": [],
        "last_move": None,
        "winner": 0,
        "win_line": None,
        "current_turn": e.BLACK,
        "undo_left": {e.BLACK: 3, e.WHITE: 3},
        "pending_undo": None,
        "pending_restart": None,
        "created_at": time.time(),
        "host_connected": True,
        "guest_connected": False,
        "status": "WAITING",  # WAITING/PLAYING/FINISHED/CLOSED
    }


def _generate_room_id() -> str:
    """生成 4 位数字房间号 (1000-9999)"""
    return str(random.randint(1000, 9999))


def _serialize_room(room: dict) -> dict:
    return {
        "room_id": room["room_id"],
        "host_name": room["host_name"],
        "guest_name": room["guest_name"],
        "board": room["board"],
        "history": room["history"],
        "last_move": room["last_move"],
        "winner": room["winner"],
        "win_line": room["win_line"],
        "current_turn": room["current_turn"],
        "undo_left": room["undo_left"],
        "status": room["status"],
    }


# ─────────────── FastAPI + Socket.IO ───────────────
app = FastAPI(title="Gomoku P2P", version="2.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

sio = socketio.AsyncServer(
    async_mode="asgi",
    cors_allowed_origins="*",
    logger=False,
    engineio_logger=False,
)
socket_app = socketio.ASGIApp(sio)
app.mount("/socket.io", socket_app)


# ─────────────── HTTP API: 单机 AI 模式（保留） ───────────────
# 阶段 1 已经在用, 这里再保留一份

AI_GAMES: Dict[str, dict] = {}
MAX_UNDO_PER_GAME = 3
LEVEL_TIME_LIMITS = {"easy": 100, "medium": 2000, "hard": 3500}


class NewGameReq(BaseModel):
    level: str = "medium"


class MoveReq(BaseModel):
    game_id: str
    x: int
    y: int


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
        "history": [],
        "undo_left": MAX_UNDO_PER_GAME,
        "last_move": None,
        "winner": 0,
        "win_line": None,
        "created_at": time.time(),
    }


@app.post("/api/new_game")
def ai_new_game(req: NewGameReq):
    if req.level not in LEVEL_TIME_LIMITS:
        raise HTTPException(400, f"level 必须为 easy/medium/hard")
    import uuid
    gid = uuid.uuid4().hex
    AI_GAMES[gid] = _new_ai_game(req.level)
    g = AI_GAMES[gid]
    return {
        "game_id": gid,
        "level": g["level"],
        "board": g["board"],
        "your_color": e.BLACK,
        "ai_color": e.WHITE,
        "undo_left": g["undo_left"],
    }


@app.post("/api/move")
def ai_move(req: MoveReq):
    if req.game_id not in AI_GAMES:
        raise HTTPException(404, "游戏不存在")
    g = AI_GAMES[req.game_id]
    if g["winner"] != 0:
        raise HTTPException(400, "对局已结束")

    if not (0 <= req.x < e.BOARD_SIZE and 0 <= req.y < e.BOARD_SIZE):
        raise HTTPException(400, "坐标越界")
    if g["board"][req.y][req.x] != e.EMPTY:
        raise HTTPException(400, "该位置已有棋子")

    g["board"][req.y][req.x] = e.BLACK
    g["history"].append((req.x, req.y, e.BLACK))
    g["last_move"] = (req.x, req.y)

    win_line = None
    if e.check_win(g["board"], req.x, req.y, e.BLACK):
        g["winner"] = e.BLACK
        win_line = e.find_winning_line(g["board"], req.x, req.y, e.BLACK)
        g["win_line"] = win_line
        return {
            "player_x": req.x, "player_y": req.y,
            "ai_x": -1, "ai_y": -1, "ai_thinking_ms": 0,
            "board": g["board"], "winner": g["winner"],
            "win_line": win_line, "last_move": list(g["last_move"]),
            "undo_left": g["undo_left"],
        }

    if _check_full(g["board"]):
        g["winner"] = 3
        return {
            "player_x": req.x, "player_y": req.y,
            "ai_x": -1, "ai_y": -1, "ai_thinking_ms": 0,
            "board": g["board"], "winner": g["winner"],
            "win_line": None, "last_move": list(g["last_move"]),
            "undo_left": g["undo_left"],
        }

    t0 = time.time()
    ax, ay, _ = e.ai_move(g["board"], e.WHITE, g["level"], time_limit_ms=LEVEL_TIME_LIMITS[g["level"]])
    thinking_ms = int((time.time() - t0) * 1000)
    g["board"][ay][ax] = e.WHITE
    g["history"].append((ax, ay, e.WHITE))
    g["last_move"] = (ax, ay)

    if e.check_win(g["board"], ax, ay, e.WHITE):
        g["winner"] = e.WHITE
        g["win_line"] = e.find_winning_line(g["board"], ax, ay, e.WHITE)
    elif _check_full(g["board"]):
        g["winner"] = 3

    return {
        "player_x": req.x, "player_y": req.y,
        "ai_x": ax, "ai_y": ay, "ai_thinking_ms": thinking_ms,
        "board": g["board"], "winner": g["winner"],
        "win_line": g["win_line"], "last_move": list(g["last_move"]),
        "undo_left": g["undo_left"],
    }


@app.post("/api/undo")
def ai_undo(req: MoveReq):
    if req.game_id not in AI_GAMES:
        raise HTTPException(404, "游戏不存在")
    g = AI_GAMES[req.game_id]
    if g["winner"] != 0:
        raise HTTPException(400, "对局已结束，无法悔棋")
    if g["undo_left"] <= 0:
        raise HTTPException(400, "悔棋次数已用完")
    if len(g["history"]) < 2:
        raise HTTPException(400, "对局刚开始，无棋可悔")

    for _ in range(2):
        if not g["history"]:
            break
        x, y, _ = g["history"].pop()
        g["board"][y][x] = e.EMPTY

    g["undo_left"] -= 1
    if g["history"]:
        last = g["history"][-1]
        g["last_move"] = (last[0], last[1])
    else:
        g["last_move"] = None

    return {
        "board": g["board"], "undo_left": g["undo_left"],
        "last_move": list(g["last_move"]) if g["last_move"] else None,
    }


@app.post("/api/resign")
def ai_resign(req: MoveReq):
    if req.game_id not in AI_GAMES:
        raise HTTPException(404, "游戏不存在")
    g = AI_GAMES[req.game_id]
    if g["winner"] != 0:
        return {"winner": g["winner"], "board": g["board"]}
    g["winner"] = e.WHITE
    return {"winner": g["winner"], "board": g["board"]}


@app.get("/api/health")
def health():
    return {"ok": True, "ai_games": len(AI_GAMES), "pvp_rooms": len(ROOMS)}


# ─────────────── HTTP API: PVP 房间 ───────────────
# 这些只用于 Socket.IO 不可用时查询/创建房间。
# 真实游戏走 Socket.IO（实时性更好）。

class CreateRoomReq(BaseModel):
    name: str = "玩家"


class JoinRoomReq(BaseModel):
    room_id: str
    name: str = "玩家"


@app.post("/api/pvp/create")
async def pvp_create(req: CreateRoomReq):
    """仅查询当前空闲房间号 (实际房间由 Socket.IO 创建)"""
    return {"ok": True}


# ─────────────── Socket.IO 事件 ───────────────
# 客户端→服务端:
#   create_room(name)  →  server_create_room_resp(room_id, color, board)
#   join_room(room_id, name) → server_join_room_resp(...) / error
#   move(x, y)         →  server_move_resp(...) / error + 广播 opponent_move
#   undo_request()     →  广播 undo_request
#   undo_response(agree) →  广播 undo_result
#   restart_request()  →  广播 restart_request
#   restart_response(agree) →  广播 restart_result
#   resign()           →  广播 game_over
#   disconnect         →  广播 opponent_disconnected

@sio.event
async def connect(sid, environ):
    print(f"[socket] connect: {sid}")


@sio.event
async def disconnect(sid):
    print(f"[socket] disconnect: {sid}")
    # 找房间, 标记离开 + 通知对手
    for rid, room in list(ROOMS.items()):
        if room["host_sid"] == sid:
            room["host_connected"] = False
            if room["guest_sid"]:
                await sio.emit("opponent_disconnected", {"reason": "host"}, to=room["guest_sid"])
        elif room["guest_sid"] == sid:
            room["guest_connected"] = False
            if room["host_sid"]:
                await sio.emit("opponent_disconnected", {"reason": "guest"}, to=room["host_sid"])


@sio.event
async def create_room(sid, data):
    name = (data or {}).get("name", "玩家")[:20]
    async with ROOM_LOCK:
        # 生成不冲突的 4 位数字房间号
        for _ in range(50):
            rid = _generate_room_id()
            if rid not in ROOMS:
                break
        else:
            await sio.emit("error", {"msg": "房间号生成失败，请重试"}, to=sid)
            return
        ROOMS[rid] = _new_room(rid, sid, name)
    print(f"[room] create {rid} by {sid} ({name})")
    await sio.emit("room_created", {
        "room_id": rid,
        "color": e.BLACK,  # 房主执黑
        "name": name,
    }, to=sid)


@sio.event
async def join_room(sid, data):
    rid = (data or {}).get("room_id", "").strip()
    name = (data or {}).get("name", "玩家")[:20]
    if not (rid.isdigit() and len(rid) == 4):
        await sio.emit("error", {"msg": "房间号必须是 4 位数字"}, to=sid)
        return
    async with ROOM_LOCK:
        room = ROOMS.get(rid)
        if not room:
            await sio.emit("error", {"msg": f"房间 {rid} 不存在"}, to=sid)
            return
        if room["status"] != "WAITING":
            await sio.emit("error", {"msg": f"房间 {rid} 已在游戏中"}, to=sid)
            return
        if room["guest_sid"]:
            await sio.emit("error", {"msg": "房间已满"}, to=sid)
            return
        # 加入成功
        room["guest_sid"] = sid
        room["guest_name"] = name
        room["guest_connected"] = True
        room["status"] = "PLAYING"

    # 通知房主
    await sio.emit("opponent_joined", {
        "name": name,
        "color": e.WHITE,
    }, to=room["host_sid"], namespace="/")
    # 通知加入者
    await sio.emit("room_joined", {
        "room_id": rid,
        "color": e.WHITE,  # 加入者执白
        "host_name": room["host_name"],
        "board": room["board"],
        "current_turn": room["current_turn"],
    }, to=sid, namespace="/")
    print(f"[room] {rid} joined by {sid} ({name})")


@sio.event
async def move(sid, data):
    x, y = (data or {}).get("x"), (data or {}).get("y")
    if x is None or y is None or not (0 <= x < 15 and 0 <= y < 15):
        await sio.emit("move_error", {"msg": "坐标无效"}, to=sid)
        return
    # 找房间
    room = None
    color = None
    for rid, r in ROOMS.items():
        if r["host_sid"] == sid and r["host_connected"]:
            room, color = r, e.BLACK
            break
        if r["guest_sid"] == sid and r["guest_connected"]:
            room, color = r, e.WHITE
            break
    if not room:
        await sio.emit("move_error", {"msg": "你不在任何房间"}, to=sid)
        return
    if room["status"] != "PLAYING":
        await sio.emit("move_error", {"msg": "对局未在进行中"}, to=sid)
        return
    if room["current_turn"] != color:
        await sio.emit("move_error", {"msg": "不是你的回合"}, to=sid)
        return
    if room["board"][y][x] != e.EMPTY:
        await sio.emit("move_error", {"msg": "该位置已有棋子"}, to=sid)
        return
    if room["pending_undo"] is not None or room["pending_restart"] is not None:
        await sio.emit("move_error", {"msg": "有未处理的请求，请先回应"}, to=sid)
        return

    # 落子
    room["board"][y][x] = color
    room["history"].append((x, y, color))
    room["last_move"] = (x, y)

    win_line = None
    winner = 0
    if e.check_win(room["board"], x, y, color):
        winner = color
        win_line = e.find_winning_line(room["board"], x, y, color)
        room["status"] = "FINISHED"
    elif _check_full(room["board"]):
        winner = 3
        room["status"] = "FINISHED"
    room["winner"] = winner
    room["win_line"] = win_line
    room["current_turn"] = e.opponent(color)

    payload = {
        "x": x, "y": y, "color": color,
        "board": room["board"],
        "last_move": list(room["last_move"]),
        "current_turn": room["current_turn"],
        "winner": winner,
        "win_line": win_line,
    }
    # 广播两方
    await sio.emit("move_done", payload, to=room["host_sid"], namespace="/")
    if room["guest_sid"]:
        await sio.emit("move_done", payload, to=room["guest_sid"], namespace="/")


@sio.event
async def undo_request(sid, data):
    # 找房间 + 角色
    room, color = None, None
    for rid, r in ROOMS.items():
        if r["host_sid"] == sid and r["host_connected"]:
            room, color = r, e.BLACK
            break
        if r["guest_sid"] == sid and r["guest_connected"]:
            room, color = r, e.WHITE
            break
    if not room:
        return
    if room["status"] != "PLAYING":
        await sio.emit("undo_error", {"msg": "对局未在进行中"}, to=sid)
        return
    if room["undo_left"][color] <= 0:
        await sio.emit("undo_error", {"msg": "悔棋次数已用完"}, to=sid)
        return
    if len(room["history"]) < 2:
        await sio.emit("undo_error", {"msg": "对局刚开始，无棋可悔"}, to=sid)
        return
    if room["pending_undo"] is not None:
        await sio.emit("undo_error", {"msg": "已有待处理的悔棋请求"}, to=sid)
        return

    # 记录请求, 通知对方
    room["pending_undo"] = color
    other = e.opponent(color)
    other_sid = room["host_sid"] if other == e.BLACK else room["guest_sid"]
    if other_sid and (room["host_connected"] if other == e.BLACK else room["guest_connected"]):
        await sio.emit("undo_requested", {
            "from_color": color,
        }, to=other_sid, namespace="/")
    else:
        await sio.emit("undo_error", {"msg": "对方已离线，无法悔棋"}, to=sid)


@sio.event
async def undo_response(sid, data):
    agree = bool((data or {}).get("agree"))
    room, color = None, None
    for rid, r in ROOMS.items():
        if r["host_sid"] == sid and r["host_connected"]:
            room, color = r, e.BLACK
            break
        if r["guest_sid"] == sid and r["guest_connected"]:
            room, color = r, e.WHITE
            break
    if not room:
        return
    requester = room["pending_undo"]
    if requester is None:
        return  # 没有待处理请求
    if not agree:
        # 拒绝
        room["pending_undo"] = None
        requester_sid = room["host_sid"] if requester == e.BLACK else room["guest_sid"]
        if requester_sid:
            await sio.emit("undo_rejected", {}, to=requester_sid, namespace="/")
        return
    # 同意: 撤销 2 步
    if len(room["history"]) < 2:
        room["pending_undo"] = None
        return
    for _ in range(2):
        if not room["history"]:
            break
        hx, hy, _ = room["history"].pop()
        room["board"][hy][hx] = e.EMPTY
    room["undo_left"][requester] -= 1
    room["pending_undo"] = None
    if room["history"]:
        last = room["history"][-1]
        room["last_move"] = (last[0], last[1])
        room["current_turn"] = last[2]  # 轮到刚落子那人
    else:
        room["last_move"] = None
        room["current_turn"] = e.BLACK
    # 广播
    payload = {
        "board": room["board"],
        "last_move": list(room["last_move"]) if room["last_move"] else None,
        "current_turn": room["current_turn"],
        "undo_left": room["undo_left"],
    }
    await sio.emit("undo_done", payload, to=room["host_sid"], namespace="/")
    if room["guest_sid"]:
        await sio.emit("undo_done", payload, to=room["guest_sid"], namespace="/")


@sio.event
async def restart_request(sid, data):
    room, color = None, None
    for rid, r in ROOMS.items():
        if r["host_sid"] == sid and r["host_connected"]:
            room, color = r, e.BLACK
            break
        if r["guest_sid"] == sid and r["guest_connected"]:
            room, color = r, e.WHITE
            break
    if not room:
        return
    if room["status"] != "FINISHED":
        await sio.emit("restart_error", {"msg": "对局未结束，无需重开"}, to=sid)
        return
    if room["pending_restart"] is not None:
        return  # 已申请过
    room["pending_restart"] = color
    other = e.opponent(color)
    other_sid = room["host_sid"] if other == e.BLACK else room["guest_sid"]
    if other_sid and (room["host_connected"] if other == e.BLACK else room["guest_connected"]):
        await sio.emit("restart_requested", {"from_color": color}, to=other_sid, namespace="/")


@sio.event
async def restart_response(sid, data):
    agree = bool((data or {}).get("agree"))
    room, color = None, None
    for rid, r in ROOMS.items():
        if r["host_sid"] == sid and r["host_connected"]:
            room, color = r, e.BLACK
            break
        if r["guest_sid"] == sid and r["guest_connected"]:
            room, color = r, e.WHITE
            break
    if not room:
        return
    if room["pending_restart"] is None:
        return
    if not agree:
        room["pending_restart"] = None
        requester = room["pending_restart"]
        requester_sid = room["host_sid"] if requester == e.BLACK else room["guest_sid"]
        if requester_sid:
            await sio.emit("restart_rejected", {}, to=requester_sid, namespace="/")
        return
    # 同意: 重置棋盘, 状态
    room["board"] = _empty_board()
    room["history"] = []
    room["last_move"] = None
    room["winner"] = 0
    room["win_line"] = None
    room["current_turn"] = e.BLACK
    room["undo_left"] = {e.BLACK: 3, e.WHITE: 3}
    room["pending_undo"] = None
    room["pending_restart"] = None
    room["status"] = "PLAYING"
    payload = {
        "board": room["board"],
        "current_turn": room["current_turn"],
        "undo_left": room["undo_left"],
    }
    await sio.emit("restart_done", payload, to=room["host_sid"], namespace="/")
    if room["guest_sid"]:
        await sio.emit("restart_done", payload, to=room["guest_sid"], namespace="/")


@sio.event
async def resign(sid, data):
    room, color = None, None
    for rid, r in ROOMS.items():
        if r["host_sid"] == sid and r["host_connected"]:
            room, color = r, e.BLACK
            break
        if r["guest_sid"] == sid and r["guest_connected"]:
            room, color = r, e.WHITE
            break
    if not room:
        return
    if room["status"] != "PLAYING":
        return
    winner = e.opponent(color)
    room["winner"] = winner
    room["status"] = "FINISHED"
    payload = {
        "winner": winner,
        "resigned_color": color,
    }
    await sio.emit("game_over", payload, to=room["host_sid"], namespace="/")
    if room["guest_sid"]:
        await sio.emit("game_over", payload, to=room["guest_sid"], namespace="/")


# ─────────────── 后台任务: TTL 清理 ───────────────
async def room_cleanup_loop():
    while True:
        await asyncio.sleep(30)
        now = time.time()
        to_close = []
        for rid, room in ROOMS.items():
            if room["status"] == "WAITING" and (now - room["created_at"]) > ROOM_TTL_SECONDS:
                to_close.append(rid)
        for rid in to_close:
            room = ROOMS.pop(rid, None)
            if not room:
                continue
            print(f"[room] TTL expired: {rid}")
            # 通知房主
            if room["host_sid"]:
                try:
                    await sio.emit("room_closed", {"reason": "timeout"}, to=room["host_sid"], namespace="/")
                except Exception:
                    pass


@app.on_event("startup")
async def on_startup():
    asyncio.create_task(room_cleanup_loop())


# ─────────────── 静态文件 ───────────────
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
def index():
    return FileResponse("static/index.html")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=3100, log_level="info")
