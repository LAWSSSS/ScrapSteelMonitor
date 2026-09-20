"""镔鑫料型枚举与映射。

AI 侧 steel_type_stat / intelli_task_check 用 (steel_type, steel_level) 两个码表示料型，
人工侧 material_detail_info 用 (material_name, level)。两边都归到同一个 Key 上再比较。

三条归并规则：

1. 重废在库里分 1、2 级，但计算、展示、比较都当一类「重废」，所以 (3, 任意等级) 合并。
2. 参与主料型判定的只有精炉料一/二/三级和重废四种，即 AI 能输出的那四种。
3. 其余料型（人工侧的压块、中废、车轮毂、精炉料（杂模）等，AI 侧的 steel_type 4/5/6）
   一律归到 OTHER。OTHER 不参与主料型判定，但必须留在占比里参与规范化的补/扣分配，
   否则补扣的去向和人工占比的总和都会算错。
"""

from __future__ import annotations

from typing import Final

#: (steel_type, steel_level)
Key = tuple[int, int]

JINGLU: Final = 1
ZHONGFEI: Final = 3

#: 重废合并后统一用这个 key。
ZHONGFEI_KEY: Final[Key] = (ZHONGFEI, 1)

#: 白名单外的料型全归到这里。不能直接丢弃：鲁Q398NP 的人工是精炉料（杂模）90% +
#: 精炉料三 10%，丢掉杂模就会把 10% 的精炉料三当成主料型，去追一个根本追不到的目标。
OTHER: Final[Key] = (0, 0)

MATERIAL_NAMES: Final[dict[Key, str]] = {
    (JINGLU, 1): "精炉料1",
    (JINGLU, 2): "精炉料2",
    (JINGLU, 3): "精炉料3",
    ZHONGFEI_KEY: "重废",
    OTHER: "其他料型",
}

#: 人工评级并列时按这个顺序定主料型，靠前的优先。
MAIN_TYPE_PRIORITY: Final[tuple[Key, ...]] = (
    (JINGLU, 1),
    (JINGLU, 2),
    (JINGLU, 3),
    ZHONGFEI_KEY,
)

#: 可以作为主料型参与差异率判定的料型。
ALLOWED_MAIN_TYPES: Final[frozenset[Key]] = frozenset(MAIN_TYPE_PRIORITY)

_LEVEL_WORDS: Final[dict[str, int]] = {"一": 1, "二": 2, "三": 3}
_MANUAL_NAMES: Final[dict[str, int]] = {"精炉料": JINGLU, "重废": ZHONGFEI}


def label(key: Key) -> str:
    return MATERIAL_NAMES.get(key, f"料型{key[0]}-{key[1]}")


def is_graded(key: Key) -> bool:
    """是否参与主料型判定。"""
    return key in ALLOWED_MAIN_TYPES


def priority_rank(key: Key) -> int:
    """并列时的排序键，越小越优先。表外料型排在最后。"""
    try:
        return MAIN_TYPE_PRIORITY.index(key)
    except ValueError:
        return len(MAIN_TYPE_PRIORITY)


def canonical(steel_type: int | None, steel_level: int | None) -> Key:
    """AI 侧两个码 → 归并后的 Key。只并重废等级，别的料型原样保留。

    白名单外的 steel_type（数据里是 4、5、6，占比都在 0.03% 以下）不能在这里归成 OTHER：
    修正时要按真实的两个码回写 intelli_task_check，这些抓斗我们不动，但得认得出来。
    是否参与主料型判定由 is_graded 决定。
    """
    code = int(steel_type or 0)
    if code == ZHONGFEI:
        return ZHONGFEI_KEY
    return (code, int(steel_level or 0))


def from_manual(material_name: str, level: int | str | None) -> Key:
    """material_detail_info 的 (material_name, level) → 归并后的 Key。

    认不出或不在白名单里的返回 OTHER，调用方据此判断这辆车的人工主料型是否在范围内。
    """
    code = _MANUAL_NAMES.get(str(material_name).strip())
    if code is None:
        return OTHER
    if code == ZHONGFEI:
        return ZHONGFEI_KEY
    try:
        key = canonical(code, int(level))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return OTHER
    return key if is_graded(key) else OTHER


def from_manual_text(text: str) -> Key:
    """"精炉料等级二" / "重废等级一" → Key。用于核对接口返回的 manualResults。"""
    name, _, level_word = str(text).strip().partition("等级")
    code = _MANUAL_NAMES.get(name.strip())
    if code is None:
        return OTHER
    if code == ZHONGFEI:
        return ZHONGFEI_KEY
    level = _LEVEL_WORDS.get(level_word.strip())
    if not level:
        return OTHER
    key = canonical(code, level)
    return key if is_graded(key) else OTHER
