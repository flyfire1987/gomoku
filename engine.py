"""
五子棋 AI 引擎
- 棋盘 15x15
- 评估函数：基于"棋型"打分（连五/活四/冲四/活三/眠三/活二/眠二）
- 搜索：alpha-beta + 候选点剪枝 + 启发式排序
- 三档：萌新（前端跑）/ 老手（depth=4）/ 大师（depth=6）
"""
from __future__ import annotations
import time
from typing import List, Tuple, Optional

BOARD_SIZE = 15
WIN_LEN = 5
EMPTY = 0
BLACK = 1
WHITE = 2

# ─────────────── 棋型评估 ───────────────
# 五子棋基础棋型（不含禁手，因为本项目无禁手规则）
# 评分采用"己方优势值 - 对方优势值"的方式
# 棋型分值：参考经典五子棋评估表，单位为"分"
# - 连五(赢): 1_000_000
# - 活四:    100_000
# - 冲四:    10_000
# - 活三:    5_000
# - 眠三:    500
# - 活二:    500
# - 眠二:    50
# - 活一:    10
# - 眠一:    1
#
# 注意：这里的活三/活二区分是基于"开放端"的，详见 _pattern_score

SCORE_WIN = 1_000_000
SCORE_LIVE_FOUR = 100_000
SCORE_RUSH_FOUR = 10_000
SCORE_LIVE_THREE = 8_000          # 活三: 下一步可成活四 (重点提升, 防止 AI 看不到威胁)
SCORE_SLEEP_THREE = 500           # 眠三: 被一端堵的冲三
SCORE_LIVE_TWO = 200              # 活二: 远离已有棋子的低价值, 避免干扰 AI 救火
SCORE_SLEEP_TWO = 20
SCORE_LIVE_ONE = 5
SCORE_SLEEP_ONE = 1


def opponent(color: int) -> int:
    return WHITE if color == BLACK else BLACK


# ─────────────── 棋盘工具 ───────────────
def in_bounds(x: int, y: int) -> bool:
    return 0 <= x < BOARD_SIZE and 0 <= y < BOARD_SIZE


def neighbors(x: int, y: int, radius: int = 2) -> List[Tuple[int, int]]:
    """返回 (x, y) 周围 radius 距离内的所有点（用于启发式候选点）"""
    result = []
    for dx in range(-radius, radius + 1):
        for dy in range(-radius, radius + 1):
            if dx == 0 and dy == 0:
                continue
            nx, ny = x + dx, y + dy
            if in_bounds(nx, ny):
                result.append((nx, ny))
    return result


# ─────────────── 评估函数 ───────────────
def _line_score(count: int, open_ends: int, block: int) -> int:
    """
    单条线（4个方向中的一条）的棋型打分
    count: 我方连续棋子数
    open_ends: 两端开放数量（0/1/2）
    block: 已经被对方封堵的方向数（0/1/2），通常 2 - open_ends

    返回：对应棋型的分值
    """
    if count >= 5:
        return SCORE_WIN

    if block == 2:
        # 两端都被堵死了，不构成威胁
        return 0

    # 一端被堵
    if block == 1:
        if count == 4:
            return SCORE_RUSH_FOUR // 2  # 冲四被挡后价值减半
        if count == 3:
            return SCORE_SLEEP_THREE
        if count == 2:
            return SCORE_SLEEP_TWO
        if count == 1:
            return SCORE_SLEEP_ONE
        return 0

    # 两端开放（活）
    if count == 4:
        return SCORE_LIVE_FOUR  # 必胜
    if count == 3:
        return SCORE_LIVE_THREE
    if count == 2:
        return SCORE_LIVE_TWO
    if count == 1:
        return SCORE_LIVE_ONE
    return 0


def _evaluate_line(board: List[List[int]], x: int, y: int, dx: int, dy: int, color: int) -> int:
    """
    评估 (x,y) 位置在方向 (dx,dy) 上对 color 的贡献
    重要：x,y 必须是 color 的棋子
    思路：从 (x,y) 沿两个方向延伸，统计连子总数和两端开放性
    """
    opp = opponent(color)
    # 沿正向延伸到边界/异色/空白
    count_forward = 0
    nx, ny = x + dx, y + dy
    while in_bounds(nx, ny) and board[ny][nx] == color:
        count_forward += 1
        nx += dx
        ny += dy
    # 正向的“下一位”是边界/异色/空白
    block_forward = 1 if (not in_bounds(nx, ny) or board[ny][nx] == opp) else 0

    # 沿反向延伸
    count_backward = 0
    nx, ny = x - dx, y - dy
    while in_bounds(nx, ny) and board[ny][nx] == color:
        count_backward += 1
        nx -= dx
        ny -= dy
    block_backward = 1 if (not in_bounds(nx, ny) or board[ny][nx] == opp) else 0

    # 连子总数（包含 (x,y) 本身）
    count = 1 + count_forward + count_backward
    open_ends = 2 - block_forward - block_backward
    return _line_score(count, open_ends, 2 - open_ends)


def evaluate_point(board: List[List[int]], x: int, y: int, color: int) -> int:
    """
    评估在 (x,y) 落 color 子后，对 color 的局面优势值
    返回：color 自身优势分 - opponent 优势分（防守时反向）

    重要：防守权重是 1.0（不是 0.8）。救活三/活四必须等价于
    自己冲四/活四，否则 AI 会选择扩张不救火。
    """
    directions = [(1, 0), (0, 1), (1, 1), (1, -1)]
    my_score = 0
    opp = opponent(color)
    opp_score = 0

    # 模拟落子: 算己方在这里的进攻价值
    board[y][x] = color
    for dx, dy in directions:
        my_score += _evaluate_line(board, x, y, dx, dy, color)
    board[y][x] = EMPTY

    # 防守分: 在这里落 opp 子，看能给 opp 创造多少威胁
    board[y][x] = opp
    for dx, dy in directions:
        opp_score += _evaluate_line(board, x, y, dx, dy, opp)
    board[y][x] = EMPTY

    # 防守权 1.0: 救对方的活三 (=对方的增量) 必须 = 自己的活四价值
    return my_score - opp_score


def _opp_active_threats_at(board: List[List[int]], x: int, y: int, color: int) -> int:
    """
    检查 (x,y) 点是否「破」了对方已有的活三/活四。
    关键：站在对手视角, 模拟 opp 落 (x,y) 后能凑出几连。
    如果任何方向上 opp 凑出 4-5 连, 这一格就是救点。
    """
    opp = opponent(color)
    if board[y][x] != EMPTY:
        return 0  # 不是空点, 不算

    directions = [(1, 0), (0, 1), (1, 1), (1, -1)]
    best_threat = 0  # 取所有方向上「最高」的威胁（不是叠加）

    # 模拟 opp 落子
    board[y][x] = opp
    for dx, dy in directions:
        # 从 (x,y) 沿 (dx,dy) 算 opp 连子数
        cnt = 1
        nx, ny = x + dx, y + dy
        while in_bounds(nx, ny) and board[ny][nx] == opp:
            cnt += 1
            nx += dx
            ny += dy
        fwd_end = (nx, ny)  # 正向停止点

        nx, ny = x - dx, y - dy
        while in_bounds(nx, ny) and board[ny][nx] == opp:
            cnt += 1
            nx -= dx
            ny -= dy
        back_end = (nx, ny)  # 反向停止点

        # 两端开放性
        fwd_open = 1 if (in_bounds(fwd_end[0], fwd_end[1]) and board[fwd_end[1]][fwd_end[0]] == EMPTY) else 0
        back_open = 1 if (in_bounds(back_end[0], back_end[1]) and board[back_end[1]][back_end[0]] == EMPTY) else 0

        # 评分
        if cnt >= 5:
            score = SCORE_WIN
        elif cnt == 4:
            if fwd_open and back_open:
                score = SCORE_LIVE_FOUR
            elif fwd_open or back_open:
                score = SCORE_RUSH_FOUR
            else:
                score = 0
        elif cnt == 3:
            if fwd_open and back_open:
                score = SCORE_LIVE_THREE
            else:
                score = SCORE_SLEEP_THREE
        else:
            score = 0

        if score > best_threat:
            best_threat = score
    board[y][x] = EMPTY

    return best_threat


def evaluate_point_v2(board: List[List[int]], x: int, y: int, color: int) -> int:
    """
    升级版 evaluate_point: 把"我方进攻"和"对方威胁"分别考虑
    防止双重计算抵消: 原 v1 用 base + opp_threat, 当 base 本身
    已经含 opp_score (负数) 时会抵消 opp_threat (正数)

    公式：v2 = max(my_offense, opp_threat) * sign + ...
    - my_offense = 己方下到 (x,y) 后能获得的最高连子增量
    - opp_threat = 对方下到 (x,y) 后能获得的最高连子增量
    - 实际: AI 要选「我方进攻大」或「对方威胁大」中更大的那个

    简化公式:
      v2 = my_offense + opp_threat
    其中 my_offense = 己方连子增量 (正)
          opp_threat = 对方连子增量 (正)
    两个量是独立面, 不会互相抵消。
    """
    directions = [(1, 0), (0, 1), (1, 1), (1, -1)]
    opp = opponent(color)
    my_offense = 0
    opp_threat = 0

    # 我方下 (x,y)
    board[y][x] = color
    for dx, dy in directions:
        cnt = 1
        nx, ny = x + dx, y + dy
        while in_bounds(nx, ny) and board[ny][nx] == color:
            cnt += 1
            nx += dx
            ny += dy
        fwd_end = (nx, ny)
        nx, ny = x - dx, y - dy
        while in_bounds(nx, ny) and board[ny][nx] == color:
            cnt += 1
            nx -= dx
            ny -= dy
        back_end = (nx, ny)
        fwd_open = 1 if (in_bounds(fwd_end[0], fwd_end[1]) and board[fwd_end[1]][fwd_end[0]] == EMPTY) else 0
        back_open = 1 if (in_bounds(back_end[0], back_end[1]) and board[back_end[1]][back_end[0]] == EMPTY) else 0
        s = 0
        if cnt >= 5:
            s = SCORE_WIN
        elif cnt == 4:
            s = SCORE_LIVE_FOUR if (fwd_open and back_open) else (SCORE_RUSH_FOUR if (fwd_open or back_open) else 0)
        elif cnt == 3:
            s = SCORE_LIVE_THREE if (fwd_open and back_open) else (SCORE_SLEEP_THREE if (fwd_open or back_open) else 0)
        elif cnt == 2:
            s = SCORE_LIVE_TWO if (fwd_open and back_open) else (SCORE_SLEEP_TWO if (fwd_open or back_open) else 0)
        if s > my_offense:
            my_offense = s
    board[y][x] = EMPTY

    # 对方下 (x,y) - 使用已有 _opp_active_threats_at
    opp_threat = _opp_active_threats_at(board, x, y, color)

    return my_offense + opp_threat


# ─────────────── 胜负判定 ───────────────
def check_win(board: List[List[int]], x: int, y: int, color: int) -> bool:
    """在 (x,y) 落 color 子后，是否构成五连（仅检测 4 个方向）"""
    directions = [(1, 0), (0, 1), (1, 1), (1, -1)]
    for dx, dy in directions:
        count = 1
        # 正向
        nx, ny = x + dx, y + dy
        while in_bounds(nx, ny) and board[ny][nx] == color:
            count += 1
            nx += dx
            ny += dy
        # 反向
        nx, ny = x - dx, y - dy
        while in_bounds(nx, ny) and board[ny][nx] == color:
            count += 1
            nx -= dx
            ny -= dy
        if count >= WIN_LEN:
            return True
    return False


def find_winning_line(board: List[List[int]], x: int, y: int, color: int) -> Optional[List[Tuple[int, int]]]:
    """返回获胜的五连坐标，用于前端高亮"""
    directions = [(1, 0), (0, 1), (1, 1), (1, -1)]
    for dx, dy in directions:
        # 沿 (dx,dy) 方向统计连子
        line = [(x, y)]
        nx, ny = x + dx, y + dy
        while in_bounds(nx, ny) and board[ny][nx] == color:
            line.append((nx, ny))
            nx += dx
            ny += dy
        # 沿反方向
        nx, ny = x - dx, y - dy
        while in_bounds(nx, ny) and board[ny][nx] == color:
            line.append((nx, ny))
            nx -= dx
            ny -= dy
        if len(line) < 5:
            continue
        # 按 (x,y) 排序后取连续 5 个 - 取决于方向，需要对 axis-aligned 方向做正确排序
        if dx == 0:
            # 垂直：按 y 排序
            line.sort(key=lambda p: p[1])
        elif dy == 0:
            # 水平：按 x 排序
            line.sort(key=lambda p: p[0])
        else:
            # 对角：按 (x,y) 联合排序（x 和 y 同方向递增）
            line.sort(key=lambda p: (p[0], p[1]))
        # 找连续 5 个（步长都 1）
        for i in range(len(line) - 4):
            seg = line[i:i + 5]
            if dx == 0:
                # 垂直：x 相同，y 步长 1
                if all(seg[k][0] == seg[0][0] and seg[k + 1][1] - seg[k][1] == 1 for k in range(4)):
                    return seg
            elif dy == 0:
                # 水平：y 相同，x 步长 1
                if all(seg[k][1] == seg[0][1] and seg[k + 1][0] - seg[k][0] == 1 for k in range(4)):
                    return seg
            else:
                # 对角：x、y 步长都 1
                if all(seg[k + 1][0] - seg[k][0] == 1 and seg[k + 1][1] - seg[k][1] == 1 for k in range(4)):
                    return seg
        return line[:5]
    return None


# ─────────────── 候选点生成 ───────────────
def has_neighbor(board: List[List[int]], x: int, y: int, radius: int = 1) -> bool:
    """(x,y) 周围 radius 距离内是否有棋子"""
    for nx, ny in neighbors(x, y, radius):
        if board[ny][nx] != EMPTY:
            return True
    return False


def candidate_moves(board: List[List[int]], color: int, top_k: int = 12) -> List[Tuple[int, int]]:
    """
    启发式候选点：只考虑已有棋子 1 距离内的空点
    按 evaluate_point_v2 评分排序，取 top_k
    """
    cands = []
    for y in range(BOARD_SIZE):
        for x in range(BOARD_SIZE):
            if board[y][x] != EMPTY:
                continue
            if not has_neighbor(board, x, y, radius=1):
                continue
            score = evaluate_point_v2(board, x, y, color)
            cands.append((score, x, y))
    cands.sort(key=lambda t: -t[0])
    return [(x, y) for _, x, y in cands[:top_k]]


# ─────────────── 搜索（alpha-beta） ───────────────
def _search(board: List[List[int]], depth: int, alpha: int, beta: int, maximizing: bool, ai_color: int, last_x: int, last_y: int, deadline: float) -> int:
    """
    极小极大搜索 + alpha-beta 剪枝
    ai_color: AI 执子颜色
    maximizing: True 表示当前层是 AI 走子
    last_x, last_y: 上一步落子点, 用于叶节点评估
    """
    if time.time() > deadline:
        # 超时：返回叶节点评估
        if last_x >= 0:
            return evaluate_point_v2(board, last_x, last_y, ai_color)
        return 0

    if depth == 0:
        # 叶节点: 评估刚落子这一点的总优势（含救火分）
        if last_x >= 0:
            return evaluate_point_v2(board, last_x, last_y, ai_color)
        return 0

    cur_color = ai_color if maximizing else opponent(ai_color)
    cands = candidate_moves(board, cur_color, top_k=10 if depth >= 4 else 8)

    if not cands:
        return 0

    if maximizing:
        best = -10**9
        for x, y in cands:
            board[y][x] = cur_color
            # 顺手检查 AI 必胜
            if check_win(board, x, y, cur_color):
                board[y][x] = EMPTY
                return SCORE_WIN
            score = _search(board, depth - 1, alpha, beta, False, ai_color, x, y, deadline)
            board[y][x] = EMPTY
            best = max(best, score)
            alpha = max(alpha, best)
            if alpha >= beta:
                break
        return best
    else:
        worst = 10**9
        for x, y in cands:
            board[y][x] = cur_color
            # 顺手检查对手必胜 (如果对手刚下这手就赢了, 这手就是必救点)
            if check_win(board, x, y, cur_color):
                board[y][x] = EMPTY
                return -SCORE_WIN
            score = _search(board, depth - 1, alpha, beta, True, ai_color, x, y, deadline)
            board[y][x] = EMPTY
            worst = min(worst, score)
            beta = min(beta, worst)
            if alpha >= beta:
                break
        return worst


def ai_move(board: List[List[int]], ai_color: int, level: str, time_limit_ms: int = 2500) -> Tuple[int, int, int]:
    """
    AI 选点主入口
    level: 'easy' | 'medium' | 'hard'
    返回: (x, y, score)
    """
    # 第一手落在天元（避免无意义的随机开局）
    if all(board[y][x] == EMPTY for y in range(BOARD_SIZE) for x in range(BOARD_SIZE)):
        return (BOARD_SIZE // 2, BOARD_SIZE // 2, SCORE_LIVE_ONE)

    if level == 'easy':
        return _ai_easy(board, ai_color)
    elif level == 'medium':
        return _ai_search(board, ai_color, depth=4, time_limit_ms=time_limit_ms)
    else:
        return _ai_search(board, ai_color, depth=6, time_limit_ms=time_limit_ms)


def _ai_easy(board: List[List[int]], ai_color: int) -> Tuple[int, int, int]:
    """萌新档：纯评估函数 + 随机性（不搜索）"""
    cands = candidate_moves(board, ai_color, top_k=5)
    if not cands:
        return (BOARD_SIZE // 2, BOARD_SIZE // 2, 0)
    import random
    top = cands[:3] if len(cands) >= 3 else cands
    x, y = random.choice(top)
    score = evaluate_point_v2(board, x, y, ai_color)
    return (x, y, score)


def _ai_search(board: List[List[int]], ai_color: int, depth: int, time_limit_ms: int) -> Tuple[int, int, int]:
    """搜索：alpha-beta + 启发式排序 + 杀手剪枝 + root 评分"""
    deadline = time.time() + time_limit_ms / 1000.0

    # ── 杀手剪枝 1: 检查对手必杀/活四/冲四, 救点强制置顶 ──
    killer_moves = []
    for y in range(BOARD_SIZE):
        for x in range(BOARD_SIZE):
            if board[y][x] != EMPTY:
                continue
            if not has_neighbor(board, x, y, radius=2):
                continue
            opp_threat = _opp_active_threats_at(board, x, y, ai_color)
            if opp_threat >= SCORE_RUSH_FOUR:
                killer_moves.append((opp_threat, x, y))
    killer_moves.sort(key=lambda t: -t[0])

    # ── 候选点 ──
    cands = candidate_moves(board, ai_color, top_k=10)
    killer_set = {(x, y) for _, x, y in killer_moves}
    extra = [(x, y) for x, y in killer_set if (x, y) not in set(cands)]
    if extra:
        cands = cands + extra

    best_score = -10**9
    best_move = (BOARD_SIZE // 2, BOARD_SIZE // 2)
    alpha = -10**9
    beta = 10**9

    for x, y in cands:
        if time.time() > deadline:
            break
        board[y][x] = ai_color
        if check_win(board, x, y, ai_color):
            board[y][x] = EMPTY
            return (x, y, SCORE_WIN)
        # 重要: 根节点评分 = v2(AI 这一手) + 递归子节点评分
        # 这样救活三的 v2=100000 能体现, 不会被子节点 0 覆盖
        root_score = evaluate_point_v2(board, x, y, ai_color)
        # 强制: 必救点 (killer) 获得加成
        sub_score = _search(board, depth - 1, alpha, beta, False, ai_color, x, y, deadline)
        # 合并: root_score 是这一手的「净优势」, sub_score 是子节点的预期优势
        # 使用 root_score 作为主, sub_score 调整 - 取「最大」加 root
        score = root_score + int(sub_score * 0.5)
        board[y][x] = EMPTY
        if score > best_score:
            best_score = score
            best_move = (x, y)
            alpha = max(alpha, best_score)
        if best_score >= SCORE_RUSH_FOUR:
            break

    return (best_move[0], best_move[1], best_score)


# ─────────────── 必杀必防快查（大师档优化） ───────────────
def find_immediate_threats(board: List[List[int]], color: int) -> List[Tuple[int, int]]:
    """找出能立即成五的位置（自己用：进攻；对手用：防守）"""
    threats = []
    for y in range(BOARD_SIZE):
        for x in range(BOARD_SIZE):
            if board[y][x] != EMPTY:
                continue
            if not has_neighbor(board, x, y, radius=1):
                continue
            board[y][x] = color
            win = check_win(board, x, y, color)
            board[y][x] = EMPTY
            if win:
                threats.append((x, y))
    return threats


def has_four_threat(board: List[List[int]], color: int) -> List[Tuple[int, int]]:
    """找出能形成活四/冲四的位置（防守关键点）"""
    threats = []
    for y in range(BOARD_SIZE):
        for x in range(BOARD_SIZE):
            if board[y][x] != EMPTY:
                continue
            if not has_neighbor(board, x, y, radius=1):
                continue
            board[y][x] = color
            # 计算该点的分值（是否达到冲四/活四）
            score = 0
            for dx, dy in [(1, 0), (0, 1), (1, 1), (1, -1)]:
                score += _evaluate_line(board, x, y, dx, dy, color)
            board[y][x] = EMPTY
            if score >= SCORE_RUSH_FOUR // 2:
                threats.append((x, y))
    return threats
