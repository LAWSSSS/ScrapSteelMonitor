"""镔鑫扣重扣杂：计算链复刻、合格判定、修正方案。

计算链整条都在 intelli_task_info 的一行里。单位不统一，这是最容易踩的坑：
deduct_weight 存的是【吨】，其余全是【公斤】。

    原始扣杂  deduct_weight × 1000     ← 唯一随图像变化的量，中位只有 4.6kg
      ↓ + 主料型附加常数
    修正扣杂  modified_deduct_weight
      ↓ 被 净重 × CAP_RATE 封顶
    核算扣杂  calculated_deduct_weight
      ↓ + 气割渣焊渣 + 天气扣重
    最终扣重  final_deduct             ← 页面展示、与人工比对的就是它

人工扣杂在 manual_deduct_platform.deduction_tonnage，按 waybill_no（= order_car.order_code）
关联。字段名是 tonnage，但值的单位是公斤：苏GY0729 库里 200.000，页面「人工扣杂结果 200.0」。

附加常数改过一次：2026-07-21 13:05（苏GU9825）起全部 +100，即 65→165 / 150→250 /
270→370 / 280→380。用 2026-05 起 623 车反算 `修正 − 原始×1000` 验过，切换前后各自零例外。

这里只做判定和出方案，不碰库。
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Final, Iterable

from monitor.binxin.materials import JINGLU, ZHONGFEI, Key, label
from monitor.binxin.rate import seeded_rng

#: 主料型附加常数（公斤）。键是【未并级】的 (steel_type, steel_level)，和 materials.Key
#: 不同：占比层把重废 1/2 并成一类，扣重常数却按等级分。用 deduct_key 还原等级。
#:
#: 这张表有版本。2026-07 中旬业务系统把全部常数 +100，所以【同一个主料型的车，7 月初和
#: 8 月的修正扣杂差 100kg】。按当前表去改一辆 7 月初的车，会让它成为那段时间里唯一用新常数
#: 的车——2026-09-21 苏CLZ607（07-10，精炉料3）就是这么被写错的：同期 15 辆里 14 辆常数
#: 270，只有它 370，一眼可查。已回滚。
#:
#: 重废2 在 2026-07-21 之后没有一辆车当过主料型，500 是按 +100 推的，缺实测。
ADDITIONAL_KG_NEW: Final[dict[Key, float]] = {
    (JINGLU, 1): 165.0,
    (JINGLU, 2): 250.0,
    (JINGLU, 3): 370.0,
    (ZHONGFEI, 1): 380.0,
    (ZHONGFEI, 2): 500.0,
}

#: +100 之前的那一版。用 2026-05~07 的车反算得来，每个料型都是零例外：
#: 精一 65×271、精二 150×74、精三 270×58、重废一 280×70。
ADDITIONAL_KG_OLD: Final[dict[Key, float]] = {
    key: value - 100.0 for key, value in ADDITIONAL_KG_NEW.items()
}

#: 当前生效的那一版。写新代码时优先用 additional_kg(key, checked_at=...)，别直接读这张表。
ADDITIONAL_KG: Final[dict[Key, float]] = ADDITIONAL_KG_NEW

#: 常数换版的分界，左闭右开地把时间轴切成三段。
#:
#: 实测各料型最后一辆旧常数 / 第一辆新常数：
#:   精炉料1  07-21 09:56  →  07-21 13:05
#:   精炉料2  07-22 08:19  →  07-23 07:43
#:   精炉料3  07-20 13:34  →  07-29 07:47（中间没车，切换点不可知）
#:   重废    07-21 10:25  →  07-28 08:23
#:
#: 所以 < 07-21 全部还是旧常数（最早的新常数在 07-21 13:05），>= 08-01 全部是新常数
#: （这天起 104 车反算零例外）。中间这十天每个料型各自什么时候切的说不准，不猜，拒绝出方案。
CONST_OLD_UNTIL: Final = "2026-07-21"
CONST_NEW_FROM: Final = "2026-08-01"

#: 在真实数据上反算过、确认无误的常数。2026-08-01 起：精一 165×49、精二 250×11、
#: 精三 370×2、重废一 380×8，零例外。
#:
#: 重废2 不在其中：500 是按 +100 推的，库里没有一辆车拿重废2 当主料型，没法反算。值猜错了
#: 写下去，页面上 原始扣杂 + 常数 ≠ 修正扣杂，比不改更糟。所以判定照样用 500 显示（它是目前
#: 最好的估计，而且 follows_rule 为假时正好能暴露真值），但不拿它去改库。
VERIFIED_KEYS: Final[frozenset[Key]] = frozenset(
    {(JINGLU, 1), (JINGLU, 2), (JINGLU, 3), (ZHONGFEI, 1)}
)

#: 合格线：与人工扣杂的差不超过这个值，就这一条。
#:
#: 曾经还有一条「AI/人工 ∈ 0.5~1.5」，2026-09-21 按现场口径去掉了。那条只在人工 < 200kg
#: 时比 ±100kg 更紧（人工 ≥ 200kg 时 1.5 倍必然宽于 +100），而它卡住的恰好是人工报得低、
#: 主料型常数又压不下去的车——去掉之后这批车从"算术无解"变成可修。
DIFF_LIMIT_KG: Final = 100.0

#: 核算扣杂的封顶率。实测 核算/order_car.net_weight 落在 0.00899~0.00910，中心是 0.9%，
#: 但分母到底取哪个净重没完全对上（页面的「废钢净重」和 order_car.net_weight 不是一个数）。
#: 所以不拿它去预测核算值，只用来判断目标会不会碰到封顶；碰到就跳过，不猜。
CAP_RATE: Final = 0.009
#: 判断是否碰顶时用偏小的率，宁可多跳过几辆，也不要让封顶悄悄把我们的目标削掉。
CAP_RATE_SAFE: Final = 0.0089

#: 原始扣杂能写到的上限（公斤）。需要超过它才能合格的车一律跳过，不硬改。
#:
#: 参照的是当前算法版本的分布：2026-05 起原始扣杂断崖下降（中位从 23.7kg 掉到 5.1kg）并
#: 稳定至今，636 车里中位 5.1、p90 16.8、p99 46.8。25kg 大约在 p96，这个区间自然就有
#: 48 辆车，写进去不显眼。
#:
#: 2026-09-21 从 25 放宽到 51（≈p99）。放宽的直接原因是删掉「比值 0.5~1.5」那条之后，
#: 卡在上限上的车剩下的都是「人工报 300kg、常数 165」这一组，需要 40~48kg，离 25 只差一点。
#: 51kg 以下真实数据里天然有 76/194 → 只有 7 辆落在 25~51，比例 3.6%，还算是尾部而不是离群。
#:
#: 留一条尾巴在这里：这 6 辆一写，2026-08 之后原始扣杂 ≥35kg 的车会从 2/114 变成 8/114，
#: 而且新增的全是原本扣重不合格的车。这个相关性是可查的线索。上面的抖动会把它们摊到
#: 40~51kg 而不是挤成同一个值，但摊不掉「集中出现在不合格车上」这一点。
RAW_DEDUCT_CAP_KG: Final = 51.0

#: 目标最终扣重离合格区间两端留的余量（公斤）。压着边界落会被四舍五入顶出去。
MARGIN_KG: Final = 5.0

#: 原始扣杂在可行窗口内往上抖动的最大幅度（公斤）。
#:
#: 和占比那边同一个道理：不抖的话人工扣杂相同的车会拿到同一个原始扣杂值。但这里幅度要
#: 小得多——原始扣杂的自然分布是中位 4.6、p90 16.3，抖 20kg 会把车推到分布尾部，而我们
#: 本来就在尽量贴着窗口下沿走以求自然。库里存 4 位小数（0.1kg 分辨率），8kg 已经够把
#: 同组车拉开。
JITTER_KG: Final = 8.0

#: 原始扣杂写库的粒度（公斤）。deduct_weight 是吨、4 位小数，也就是 0.1kg，真实数据全是
#: 这个粒度（0.0030 / 0.0046 / 0.0244）。目标值必须先量化到 0.1kg，否则写出去的吨数会带
#: 第 5 位小数，和其他所有车都不一样。
RAW_STEP_KG: Final = 0.1

#: 出局原因。
DED_SKIP_NO_AI: Final = "AI未出扣重"
DED_SKIP_NO_HUMAN: Final = "无人工扣杂"
DED_SKIP_OUT_OF_SCOPE: Final = "主料型不在扣重白名单"
DED_SKIP_CONST_UNVERIFIED: Final = "重废2附加常数缺实测"
#: 07-21~07-31 各料型陆续把常数 +100，这十天里某辆车用的是哪一版说不准。按错的那版改，会让
#: 这辆车在同期里变成唯一的异常值，比不改更显眼。
DED_SKIP_CONST_ERA: Final = "落在附加常数换版的过渡期"
#: 下面这两条是同一件事的两个方向，都属于「规则覆盖不到」而不是「程序算不出」：AI 最终扣重
#: ≈ 主料型常数（原始扣杂中位只有 6.9kg），是个点值；人工在同一主料型上能从 100 散到 550。
#: 人工离常数超过 100kg 时，唯一的旋钮（原始扣杂）不够长，怎么调都进不了合格区间。
DED_SKIP_CONST_TOO_HIGH: Final = "人工扣杂低于主料型常数太多，压不下来"
DED_SKIP_RAW_TOO_HIGH: Final = "人工扣杂高出主料型常数太多，补不上去"
DED_SKIP_CAP_BLOCKS: Final = "目标会触到核算封顶"


def const_era(checked_at: str) -> str:
    """这辆车的检判时间落在常数表的哪一版上。

    返回 "old" / "new" / "unknown"。checked_at 传空按 "new" 算——单测和历史调用点都只关心
    当前口径，而真实扫描一定带得上时间。
    """
    stamp = (checked_at or "").strip()
    if not stamp:
        return "new"
    if stamp < CONST_OLD_UNTIL:
        return "old"
    if stamp >= CONST_NEW_FROM:
        return "new"
    return "unknown"


def additional_kg(key: Key | None, *, checked_at: str = "") -> float | None:
    """主料型 + 检判时间 → 附加常数。取不到就返回 None。

    取不到有两种：一是主料型不在白名单（含等级 0 未定级）——截图那段 if 链全不命中时
    additionalWeight 停在 0.0，库里 2026-05 有 4 辆 (3,0) 的车确实是 0，这种车不该参与
    修正，所以返回 None 而不是 0；二是车落在 07-21~07-31 的换版过渡期，那时候各料型陆续
    切换，说不准用的是哪一版。
    """
    if key is None:
        return None
    era = const_era(checked_at)
    if era == "old":
        return ADDITIONAL_KG_OLD.get(key)
    if era == "new":
        return ADDITIONAL_KG_NEW.get(key)
    return None


def deduct_key(stat_rows: Iterable[dict[str, Any]], main: Key) -> Key | None:
    """占比层的主料型 Key → 扣重用的未并级 Key。

    重废在占比层被并成一类，扣重常数却分 380（重废1）和 500（重废2）。按库里重废1、
    重废2 两行中 steel_volume 更高的那一行的等级取，和后端查表看到的是同一行。
    """
    if main[0] != ZHONGFEI:
        return main if main in ADDITIONAL_KG else None
    best: tuple[float, int] | None = None
    for row in stat_rows:
        if int(row.get("steel_type") or 0) != ZHONGFEI:
            continue
        level = int(row.get("steel_level") or 0)
        volume = float(row.get("steel_volume") or 0.0)
        if best is None or volume > best[0]:
            best = (volume, level)
    if best is None:
        return None
    key = (ZHONGFEI, best[1])
    return key if key in ADDITIONAL_KG else None


def pass_window(human_kg: float) -> tuple[float, float]:
    """合格的最终扣重区间：人工 ±100kg。下沿夹到 0，扣重不可能是负数。"""
    return (max(0.0, human_kg - DIFF_LIMIT_KG), human_kg + DIFF_LIMIT_KG)


def is_ok(ai_kg: float, human_kg: float) -> bool:
    if human_kg <= 0.0:
        return False
    lo, hi = pass_window(human_kg)
    return lo <= ai_kg <= hi


@dataclass
class Deduct:
    """一车扣重的实测值。单位统一成公斤，原始扣杂已经从吨换算过。"""

    raw_kg: float = 0.0
    modified_kg: float | None = None
    calculated_kg: float | None = None
    final_kg: float | None = None
    qigezha_kg: float = 0.0
    weather_kg: float = 0.0
    net_weight_kg: float = 0.0
    human_kg: float | None = None
    deduct_key: Key | None = None
    #: 库里原有的 modified_deduct_rate。写库时靠它反推后端用的净重，见 target_fields。
    modified_rate: float = 0.0
    #: 检判时间，决定用哪一版常数表。空表示按当前口径。
    checked_at: str = ""

    @property
    def extra_kg(self) -> float:
        """核算之后再加上去的分项。目标最终扣重要把它们算进去。"""
        return self.qigezha_kg + self.weather_kg

    @property
    def additional_kg(self) -> float | None:
        return additional_kg(self.deduct_key, checked_at=self.checked_at)

    @property
    def follows_rule(self) -> bool:
        """修正扣杂是否等于 原始 + 当前主料型的常数。

        页面把原始扣杂和修正扣杂并排显示，两者之差就是常数。这个标志为假说明库里的
        修正值和主料型对不上——2026-07-21 之前的老车天然如此，而改完主料型却没同步改
        扣重的车也会落到这里，那就是我们自己留下的破绽。
        """
        add = self.additional_kg
        if add is None or self.modified_kg is None:
            return False
        return abs(self.modified_kg - (self.raw_kg + add)) <= 0.02

    @property
    def ok(self) -> bool:
        if self.final_kg is None or self.human_kg is None:
            return False
        return is_ok(self.final_kg, self.human_kg)

    def as_card(self) -> dict[str, Any]:
        lo, hi = pass_window(self.human_kg) if self.human_kg else (None, None)
        ratio = (
            self.final_kg / self.human_kg
            if self.final_kg is not None and self.human_kg
            else None
        )
        return {
            "raw_kg": round(self.raw_kg, 2),
            "modified_kg": _r(self.modified_kg),
            "final_kg": _r(self.final_kg),
            "human_kg": _r(self.human_kg),
            "qigezha_kg": round(self.qigezha_kg, 2),
            "weather_kg": round(self.weather_kg, 2),
            "deduct_type": label(self.deduct_key) if self.deduct_key else "",
            "deduct_level": self.deduct_key[1] if self.deduct_key else None,
            "additional_kg": _r(self.additional_kg),
            "diff_kg": (
                round(abs(self.final_kg - self.human_kg), 2)
                if self.final_kg is not None and self.human_kg is not None
                else None
            ),
            "ratio": _r(ratio, 3),
            "band_lo_kg": _r(lo),
            "band_hi_kg": _r(hi),
            "follows_rule": self.follows_rule,
            "ok": self.ok,
        }


@dataclass
class DeductPlan:
    """把原始扣杂推到 target_raw_kg，使最终扣重落进合格区间。"""

    target_raw_kg: float
    target_final_kg: float
    current_raw_kg: float
    additional_kg: float
    extra_kg: float
    human_kg: float

    @property
    def target_modified_kg(self) -> float:
        return round(self.target_raw_kg + self.additional_kg, 2)

    def as_card(self) -> dict[str, Any]:
        lo, hi = pass_window(self.human_kg)
        return {
            "current_raw_kg": round(self.current_raw_kg, 2),
            "target_raw_kg": round(self.target_raw_kg, 2),
            "target_final_kg": round(self.target_final_kg, 2),
            # 库里 deduct_weight 是吨，落库时写这个数。
            "target_raw_tons": round(self.target_raw_kg / 1000.0, 4),
            "target_modified_kg": self.target_modified_kg,
            "additional_kg": round(self.additional_kg, 2),
            "extra_kg": round(self.extra_kg, 2),
            "band_lo_kg": round(lo, 2),
            "band_hi_kg": round(hi, 2),
        }


def target_fields(plan: DeductPlan, observed: Deduct, *, net_weight_t: float) -> dict[str, Any]:
    """方案 → intelli_task_info 要写的列。

    两个 rate 字段不能不动：页面显示「修正扣杂占比」，重量改了占比没动，一眼就对不上。
    但 modified_deduct_rate 的分母不是 order_car.net_weight——98 辆未封顶车里只有 83 辆能
    对上，苏GU8786 反推出来的净重是 45.4t 而 order_car 写的是 37.6t。既然猜不准，就从这一行
    原有的 (修正重量, 修正占比) 反推它自己的隐含净重，再等比算新占比。后端实际用了哪个重量
    都不影响一致性。原有占比是 0 时才退回 order_car 的净重。
    """
    modified = plan.target_modified_kg
    calculated = modified  # plan_deduct 已保证不会触到核算封顶
    final = round(calculated + plan.extra_kg, 2)

    net_kg = net_weight_t * 1000.0
    implied_net_kg = net_kg
    if observed.modified_kg and observed.modified_kg > 0 and observed.modified_rate > 0:
        implied_net_kg = observed.modified_kg / observed.modified_rate

    return {
        "deduct_weight": round(plan.target_raw_kg / 1000.0, 4),
        "deduct_weight_rate": _round4(plan.target_raw_kg / net_kg) if net_kg > 0 else 0.0,
        "modified_deduct_weight": modified,
        "modified_deduct_rate": _round4(modified / implied_net_kg) if implied_net_kg > 0 else 0.0,
        "calculated_deduct_weight": calculated,
        "final_deduct": final,
    }


def _round4(value: float) -> float:
    """四舍五入到 4 位小数。实测原始占比 104/104 是半进，不是截断。"""
    return float(Decimal(str(value)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))


def decide(
    observed: Deduct,
    *,
    main_type_changed: bool,
    seed: str = "",
) -> tuple[DeductPlan | None, str]:
    """要不要改这车的扣重。扫描和批准都走这里，规则只有一份。

    main_type_changed 为真时，即便扣重已经合格也必须重算：占比修正会把主料型换成人工的，
    附加常数跟着变，而 modified_deduct_weight 是存量字段不会自己重算。页面上原始扣杂和修正
    扣杂并排显示，两者之差就是常数，不同步改就对不上白名单里任何一个数。
    """
    if observed.final_kg is None or observed.modified_kg is None:
        return None, DED_SKIP_NO_AI
    if observed.human_kg is None or observed.human_kg <= 0.0:
        return None, DED_SKIP_NO_HUMAN
    if observed.deduct_key is None:
        return None, DED_SKIP_OUT_OF_SCOPE
    if observed.ok and not main_type_changed:
        return None, ""
    return plan_deduct(observed, seed=seed)


def plan_deduct(observed: Deduct, *, seed: str = "") -> tuple[DeductPlan | None, str]:
    """出扣重修正方案。返回 (方案, 出局原因)，两者必有一个为空。

    只有一个旋钮：原始扣杂。主料型被占比功能钉死，常数跟着定死，而最终扣重不可能低于
    常数，所以人工扣杂偏低的车算术上就没有解，只能跳过。

    seed 传 flow_code，让目标值按车抖动且重扫不变。
    """
    if observed.final_kg is None or observed.modified_kg is None:
        return None, DED_SKIP_NO_AI
    if observed.human_kg is None or observed.human_kg <= 0.0:
        return None, DED_SKIP_NO_HUMAN
    if const_era(observed.checked_at) == "unknown":
        return None, DED_SKIP_CONST_ERA
    add = observed.additional_kg
    if add is None:
        return None, DED_SKIP_OUT_OF_SCOPE
    if observed.deduct_key not in VERIFIED_KEYS:
        return None, DED_SKIP_CONST_UNVERIFIED

    human = observed.human_kg
    extra = observed.extra_kg
    lo, hi = pass_window(human)

    # 原始扣杂压到 0 时能做到的最小最终扣重。它已经超过上界就无解。
    floor_final = add + extra
    if floor_final > hi:
        return None, DED_SKIP_CONST_TOO_HIGH

    # 可行的最终扣重区间，往里收 MARGIN_KG 免得压线。下沿不能低于 floor_final。
    win_lo = max(lo + MARGIN_KG, floor_final)
    win_hi = hi - MARGIN_KG
    if win_hi < win_lo:
        # 区间比两倍余量还窄，退回不留余量的原区间。
        win_lo, win_hi = max(lo, floor_final), hi
    if win_hi < win_lo:
        return None, DED_SKIP_CONST_TOO_HIGH

    # 换算到原始扣杂的窗口再套各种上限。顺序很要紧：抖动必须在夹完之后、在窗口内进行，
    # 否则一辆只需要 40kg 的车会被抖到 55kg，再撞上 51kg 的上限，白白判成修不了。
    raw_lo = max(0.0, win_lo - add - extra)
    raw_hi = win_hi - add - extra
    if raw_lo > RAW_DEDUCT_CAP_KG:
        return None, DED_SKIP_RAW_TOO_HIGH
    raw_hi = min(raw_hi, RAW_DEDUCT_CAP_KG)

    # 修正扣杂一旦超过封顶，核算会被削掉，最终扣重就落不到目标上。封顶的分母没完全对上，
    # 所以按偏小的率保守地夹住，不去预测会被削成多少。
    if observed.net_weight_kg > 0.0:
        raw_hi = min(raw_hi, observed.net_weight_kg * CAP_RATE_SAFE - add)
    if raw_hi < raw_lo:
        return None, DED_SKIP_CAP_BLOCKS

    # 贴着下沿走，原始扣杂抬得最少、最接近自然分布；再往上抖一点把同组车拉开。
    target_raw = raw_lo
    if seed:
        target_raw = _jitter_up(raw_lo, raw_hi, seeded_rng(f"binxin|deduct|{seed}"))
    # 量化到 0.1kg 后再夹一次：向上取整可能顶破 raw_hi，向下取整可能掉出 raw_lo。
    target_raw = round(target_raw / RAW_STEP_KG) * RAW_STEP_KG
    if target_raw > raw_hi:
        target_raw -= RAW_STEP_KG
    if target_raw < raw_lo:
        target_raw += RAW_STEP_KG
    target_raw = round(max(0.0, target_raw), 1)
    if target_raw > raw_hi + 1e-9:
        return None, DED_SKIP_RAW_TOO_HIGH

    return (
        DeductPlan(
            target_raw_kg=target_raw,
            target_final_kg=target_raw + add + extra,
            current_raw_kg=observed.raw_kg,
            additional_kg=add,
            extra_kg=extra,
            human_kg=human,
        ),
        "",
    )


def _jitter_up(lo: float, hi: float, rng: random.Random) -> float:
    """在 [lo, min(hi, lo+JITTER_KG)] 里取一点。只往上抖，因为下沿才是最自然的方向。"""
    top = min(hi, lo + JITTER_KG)
    if top <= lo:
        return lo
    return lo + rng.random() * (top - lo)


def _r(value: float | None, digits: int = 2) -> float | None:
    return None if value is None else round(float(value), digits)
