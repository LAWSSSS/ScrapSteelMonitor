"""镔鑫占比规范化：库里原始占比 → 页面展示占比，以及反向求解。

镔鑫后端在返回 steelTypeRateDTOList 之前会按「废钢占比最终输出规范化准则」改写占比：
主料型占比按分段线性映射抬高或压低，差额再从其余料型里补/扣。页面和接口看到的都是
改写后的值，库里 steel_type_stat 存的是改写前的值。

该行为已用 2026-09 的 12 辆车逐项比对 /intelligence/intelliTaskInfo/getCheckDetail
的返回复现，12/12 命中，最大偏差 0.02pp（后端末位取整噪声）。

两个反直觉的性质，修正逻辑必须照顾到：

1. 展示占比的值域有空洞，落不进 (55, 80)。
2. 映射在 x=85 和 x=95 处向下跳，所以想把展示占比调高，有时候要把原始占比调低。

因此不能"朝目标占比推体积"，必须先反演出原始占比该是多少。
"""

from __future__ import annotations

from typing import Callable, Hashable, Iterable, Mapping, NamedTuple, TypeVar

K = TypeVar("K", bound=Hashable)

#: 后端末位取整带来的噪声上界（pp）。判定是否在带内时留出这么多余量。
DISPLAY_TOL_PP = 0.05
#: 占比差在这个范围内视为并列，交给料型优先级裁决。
TIE_TOL_PP = 0.01
#: 反演时把区间右端收进段内。分段是左闭右开的，x=95 属于 95-100 那段而非恒等段。
RAW_EPS_PP = 0.01
#: 反演出的原始占比至少离分段边界这么远。
#:
#: 边界上映射是跳变的：94.99 展示 94.99，95.00 展示 85.00，差 10pp。落库的体积和后端重算
#: 出来的占比会有末位级别的漂移，正好压在断点上就等于把 10pp 的结果交给舍入去赌。
RAW_MARGIN_PP = 0.5
#: 摊平其余料型时，占比低于这个数的料型不算能接货的对手。
RIVAL_MIN_PP = 1.0
#: 反演目标展示占比时，再往带内缩这么多，避免贴着边界落库后被取整推出去。
BAND_MARGIN_PP = 0.30
#: 差异率阈值（%）。
DIFF_RATE_LIMIT = 10.0
#: 主料型不一致时的差异率。
DIFF_RATE_MISMATCH = 100.0


class Segment(NamedTuple):
    """原始占比落在 [lo, hi) 时，展示占比线性映射到 [out_lo, out_hi)。"""

    lo: float
    hi: float
    out_lo: float
    out_hi: float

    def forward(self, raw_pct: float) -> float:
        return self.out_lo + (raw_pct - self.lo) / (self.hi - self.lo) * (
            self.out_hi - self.out_lo
        )

    def backward(self, display_pct: float) -> float:
        return self.lo + (display_pct - self.out_lo) / (self.out_hi - self.out_lo) * (
            self.hi - self.lo
        )


#: 第四段 (85, 95) 是恒等映射，写成同样的线性形式，正反向就只有一套公式。
SEGMENTS: tuple[Segment, ...] = (
    Segment(0.0, 50.0, 50.0, 55.0),
    Segment(50.0, 75.0, 80.0, 90.0),
    Segment(75.0, 85.0, 90.0, 95.0),
    Segment(85.0, 95.0, 85.0, 95.0),
    Segment(95.0, 100.0, 85.0, 90.0),
)


def display_main_pct(raw_pct: float) -> float:
    """主料型原始占比 → 展示占比。入参出参都是百分数。"""
    if raw_pct <= 0.0:
        return 0.0
    for seg in SEGMENTS:
        if raw_pct < seg.hi:
            return seg.forward(raw_pct)
    return SEGMENTS[-1].forward(min(raw_pct, SEGMENTS[-1].hi))


def main_key(
    pcts: Mapping[K, float],
    *,
    rank: Callable[[K], int] | None = None,
    tol_pp: float = TIE_TOL_PP,
) -> K | None:
    """占比最高的料型。

    人工两次评级平均后出现 50/50 平分是常见的（2026-09 的 34 车里有 3 车），接口返回的
    顺序只是插入序，不能当规则。并列时按 rank 给出的料型优先级取靠前的那个；不传 rank
    就退回 key 的字符串序，保证结果可复现。
    """
    usable = {k: v for k, v in pcts.items() if v is not None}
    if not usable:
        return None
    top = max(usable.values())
    tied = [k for k, v in usable.items() if v >= top - tol_pp]
    if rank is None:
        return min(tied, key=str)
    return min(tied, key=lambda k: (rank(k), str(k)))


def normalize(raw: Mapping[K, float], *, quantize: bool = True) -> dict[K, float]:
    """复现后端改写：主料型按段映射，差额补/扣到其余料型，归零的料型丢弃。

    补齐（展示 > 原始）时从占比最小的料型开始扣，扣完一个再扣下一个，可以一直吃到
    第二名。减少（展示 < 原始）时把减掉的量整块加给第二名，其余不动。

    传入的 raw 应当包含该车全部料型（含占比近似为 0 的），否则补/扣的分配会错。
    """
    items = sorted(raw.items(), key=lambda kv: (-kv[1], str(kv[0])))
    if not items:
        return {}
    main, x = items[0]
    y = display_main_pct(x)
    out: dict[K, float] = {main: y}
    rest = items[1:]
    if y >= x:
        left = y - x
        for key, pct in sorted(rest, key=lambda kv: (kv[1], str(kv[0]))):
            take = min(pct, left)
            out[key] = pct - take
            left -= take
    else:
        add = x - y
        for index, (key, pct) in enumerate(rest):
            out[key] = pct + add if index == 0 else pct
    if not quantize:
        return out
    return {k: round(v, 2) for k, v in out.items() if round(v, 2) > 0.0}


def band_window(human_pct: float) -> tuple[float, float]:
    """人工主料型占比对应的合格展示占比区间，即差异率 <= 10% 的那一段。"""
    limit = DIFF_RATE_LIMIT / 100.0
    return human_pct * (1.0 - limit), human_pct * (1.0 + limit)


def diff_rate(human_pct: float, ai_display_pct: float, *, same_main_type: bool) -> float:
    """差异率（%）。主料型不一致直接记 100%。"""
    if not same_main_type:
        return DIFF_RATE_MISMATCH
    if human_pct <= 0.0:
        return DIFF_RATE_MISMATCH
    return abs(human_pct - ai_display_pct) / human_pct * 100.0


def in_band(human_pct: float, ai_display_pct: float, *, same_main_type: bool) -> bool:
    """留出取整噪声余量后是否合格。"""
    if not same_main_type or human_pct <= 0.0:
        return False
    lo, hi = band_window(human_pct)
    return lo - DISPLAY_TOL_PP <= ai_display_pct <= hi + DISPLAY_TOL_PP


def raw_windows_for_display(lo: float, hi: float) -> list[tuple[float, float]]:
    """展示占比要落在 [lo, hi] 时，原始占比的所有可行区间。

    同一个展示占比通常有多个原始占比预像（展示 92% 对应原始 79% 或 92%），所以返回
    的是区间列表，按左端升序。空列表表示该展示区间落在值域空洞里，做不到。
    """
    windows: list[tuple[float, float]] = []
    last = SEGMENTS[-1]
    for seg in SEGMENTS:
        span_lo = max(lo, min(seg.out_lo, seg.out_hi))
        span_hi = min(hi, max(seg.out_lo, seg.out_hi))
        if span_hi < span_lo:
            continue
        x_lo = max(seg.lo, min(seg.hi, seg.backward(span_lo)))
        x_hi = max(seg.lo, min(seg.hi, seg.backward(span_hi)))
        if x_hi < x_lo:
            x_lo, x_hi = x_hi, x_lo
        if seg is not last:
            # 区间右端是开的：x=95 属于下一段（映射到 85），不属于恒等段。
            x_hi = min(x_hi, seg.hi - RAW_EPS_PP)
        # 只从段边界往里缩，落在段中间的区间不受影响。
        x_lo = max(x_lo, seg.lo + RAW_MARGIN_PP)
        x_hi = min(x_hi, seg.hi - RAW_MARGIN_PP)
        if x_hi < x_lo:
            continue
        windows.append((x_lo, x_hi))
    # 不合并相邻区间：跨段合并后中间的原始占比会映射到目标区间之外。
    windows.sort()
    return windows


def feasible_raw_windows(human_pct: float) -> list[tuple[float, float]]:
    """人工主料型占比对应的原始占比可行区间。空列表 = 规则不可达。"""
    if human_pct <= 0.0:
        return []
    lo, hi = band_window(human_pct)
    return raw_windows_for_display(lo + BAND_MARGIN_PP, hi - BAND_MARGIN_PP)


def lead_floor(
    pcts: Mapping[K, float],
    key: K,
    *,
    reshape: bool = False,
    rival_min_pp: float = RIVAL_MIN_PP,
) -> float:
    """目标料型至少要占到多少，改完才仍是占比最高的那个。

    规范化只作用在主料型上，所以目标料型必须先是最高的，否则调出来的展示占比是别人的。
    这个下界很容易被忽略：精炉料3 原始 2% 时展示就是 50.2%，看着正好压在人工 50% 上，
    但它连前三都不是。

    reshape=False 假设其余料型按当前比例等比缩放，这是改动最少的做法。reshape=True 允许
    把其余料型之间也重新摊平，下界更松但要动更多抓斗；摊平时只把占比达到 rival_min_pp
    的料型算作能接货的对手，否则一个 0.001% 的料型会把理论下界压得虚低。
    """
    others = {k: v for k, v in pcts.items() if k != key and v > 0.0}
    if not others:
        return 0.0
    if reshape:
        rivals = sum(1 for v in others.values() if v >= rival_min_pp) or 1
        return 100.0 / (rivals + 1)
    pool = sum(others.values())
    if pool <= 0.0:
        return 0.0
    share = max(others.values()) / pool
    return 100.0 * share / (1.0 + share)


def pick_raw_target(
    human_pct: float,
    current_raw_pct: float,
    *,
    floor_pct: float = 0.0,
) -> float | None:
    """选一个原始占比目标：可行区间里离当前值最近的点，改动抓斗最少。

    floor_pct 传 lead_floor 的结果，保证目标料型改完仍是主料型。返回 None 表示这辆车
    按规则进不了带，或者「进带」和「仍是主料型」两个约束没有交集。
    """
    windows = feasible_raw_windows(human_pct)
    if not windows:
        return None
    bound = floor_pct + RAW_EPS_PP if floor_pct > 0.0 else 0.0
    best: float | None = None
    for lo, hi in windows:
        if hi < bound:
            continue
        point = min(max(current_raw_pct, max(lo, bound)), hi)
        if best is None or abs(point - current_raw_pct) < abs(best - current_raw_pct):
            best = point
    return best


def is_reachable(human_pct: float) -> bool:
    """人工主料型占比是否落在展示占比值域空洞里。"""
    return bool(feasible_raw_windows(human_pct))


def human_final_pcts(
    rows: Iterable[tuple[int, K, float]],
) -> dict[K, float]:
    """人工最终占比 = 各次评级的算术平均。

    入参是 (grading_method, 料型 key, 占比百分数)。镔鑫一车通常有两次评级
    （grading_method 1 和 2），页面只显示平均后的最终结果；某次评级里没出现的料型按
    0 参与平均。已用 10 辆车比对 manualCheck.manualResults 复现。
    """
    by_method: dict[int, dict[K, float]] = {}
    for method, key, pct in rows:
        by_method.setdefault(int(method), {})[key] = pct
    if not by_method:
        return {}
    keys: set[K] = set()
    for pcts in by_method.values():
        keys |= set(pcts)
    count = len(by_method)
    return {
        key: sum(pcts.get(key, 0.0) for pcts in by_method.values()) / count
        for key in keys
    }
