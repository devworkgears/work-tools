"""型費の載せ方と、部品から見た流用先の原価の差。

1個あたりの型費 ＝ 型費 ÷ 償却年数 ÷ 部品の年間の数
部品の年間の数 ＝ Σ（使う製品の年間台数 × その製品1台あたりの使用数）
"""

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Iterable

from .model import Code, CostHead, Version, VolumeKind
from .normalize import NumberLike, to_code, to_decimal
from .rollup import ScopePolicy, StructureOverrides, calculate_cost

DEFAULT_AMORTIZATION_YEARS = Decimal(2)


class Allocation(Enum):
    NEW_PRODUCTS_ONLY = "新製品だけに載せる"
    BY_USAGE = "使う数で分ける"
    NONE = "載せない"


@dataclass(frozen=True)
class MoldCost:
    part: str
    amount: Decimal
    years: Decimal = DEFAULT_AMORTIZATION_YEARS

    def __post_init__(self) -> None:
        object.__setattr__(self, "part", to_code(self.part))
        object.__setattr__(self, "amount", to_decimal(self.amount, name="型費"))
        object.__setattr__(self, "years", to_decimal(self.years, name="償却年数"))
        if self.years <= 0:
            raise ValueError("償却年数は正の数で渡す")

    @property
    def annual_amount(self) -> Decimal:
        return self.amount / self.years


@dataclass(frozen=True)
class ReuseTarget:
    """BOM の無い流用先。手で入れる。"""

    part: str
    product: str
    quantity_per_unit: Decimal
    replaced_part: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "part", to_code(self.part))
        object.__setattr__(self, "product", to_code(self.product, name="流用先の品番"))
        object.__setattr__(self, "quantity_per_unit", to_decimal(self.quantity_per_unit, name="使用数"))
        if self.replaced_part is not None:
            object.__setattr__(self, "replaced_part", to_code(self.replaced_part, name="古い部品の品番"))


@dataclass(frozen=True)
class PartUser:
    product: str
    volume_kind: VolumeKind
    annual: Decimal
    quantity_per_unit: Decimal
    via_reuse: bool  # 流用先として手で入れた使用数か

    @property
    def annual_count(self) -> Decimal:
        return self.annual * self.quantity_per_unit


@dataclass(frozen=True)
class ProductShare:
    product: str
    annual: Decimal
    quantity_per_unit: Decimal
    per_unit: Decimal | None  # 製品1台あたりの型費の分（円/台）。割る数が 0 なら None

    @property
    def annual_total(self) -> Decimal | None:
        return None if self.per_unit is None else self.annual * self.per_unit


@dataclass(frozen=True)
class MoldAllocation:
    part: str
    allocation: Allocation
    annual_mold_cost: Decimal
    annual_count: Decimal  # 全部の使う数（部品の年間の数）
    per_part: Decimal | None  # 載せ方での1個あたりの型費
    shares: tuple[ProductShare, ...]
    unknown_volume: tuple[str, ...]  # 年間台数が無く数えられなかった流用先

    def share_for(self, product: Code) -> ProductShare | None:
        product = to_code(product)
        return next((s for s in self.shares if s.product == product), None)


@dataclass(frozen=True)
class ReuseImpact:
    product: str
    current_cost: Decimal | None
    quantity_per_unit: Decimal
    new_part_cost: Decimal | None
    old_part_cost: Decimal | None
    mold_share: Decimal | None

    @property
    def part_difference(self) -> Decimal | None:
        if self.new_part_cost is None or self.old_part_cost is None:
            return None
        return self.quantity_per_unit * (self.new_part_cost - self.old_part_cost)

    @property
    def difference(self) -> Decimal | None:
        if self.part_difference is None or self.mold_share is None:
            return None
        return self.part_difference + self.mold_share

    @property
    def after(self) -> Decimal | None:
        if self.current_cost is None or self.difference is None:
            return None
        return self.current_cost + self.difference


def usage_per_unit(
    version: Version, product: Code, overrides: StructureOverrides | None = None
) -> dict[str, Decimal]:
    """製品1台あたりの各品目の使用数。数量を階層ごとに掛けてたどる（途中の組立品も含む）。"""
    over = {to_code(k): tuple(v) for k, v in (overrides or {}).items()}
    usage: dict[str, Decimal] = {}

    def walk(code: str, multiplier: Decimal, path: frozenset[str]) -> None:
        if code in path:
            raise ValueError(f"構成が循環している: {code}")
        lines = over[code] if code in over else version.children(code)
        for line in lines:
            qty = multiplier * line.quantity
            usage[line.child] = usage.get(line.child, Decimal(0)) + qty
            walk(line.child, qty, path | {code})

    walk(to_code(product), Decimal(1), frozenset())
    return usage


def part_users(
    version: Version,
    part: Code,
    reuse_targets: Iterable[ReuseTarget] = (),
    overrides: StructureOverrides | None = None,
) -> tuple[tuple[PartUser, ...], tuple[str, ...]]:
    """部品を使う製品と、年間台数が無く数えられなかった流用先。

    BOM に部品が入っている製品は BOM の使用数を使い、同じ製品の流用先の入力は使わない（二重に数えない）。
    """
    part = to_code(part)
    users: dict[str, PartUser] = {}
    for volume in version.volumes.values():
        qty = usage_per_unit(version, volume.product, overrides).get(part, Decimal(0))
        if qty != 0:
            users[volume.product] = PartUser(volume.product, volume.kind, volume.annual, qty, False)
    unknown: list[str] = []
    for target in reuse_targets:
        if target.part != part or target.product in users:
            continue
        volume = version.volume(target.product)
        if volume is None:
            unknown.append(target.product)
            continue
        users[target.product] = PartUser(
            target.product, volume.kind, volume.annual, target.quantity_per_unit, True
        )
    return tuple(users.values()), tuple(unknown)


def allocate_mold_cost(
    version: Version,
    mold: MoldCost,
    allocation: Allocation,
    reuse_targets: Iterable[ReuseTarget] = (),
    overrides: StructureOverrides | None = None,
) -> MoldAllocation:
    """型費を載せ方に応じて製品ごとの円/台に分ける。"""
    users, unknown = part_users(version, mold.part, reuse_targets, overrides)
    annual_count = sum((u.annual_count for u in users), Decimal(0))
    if allocation is Allocation.NONE:
        per_part: Decimal | None = Decimal(0)
        bearers = users
    else:
        bearers = (
            tuple(u for u in users if u.volume_kind is VolumeKind.PLAN)
            if allocation is Allocation.NEW_PRODUCTS_ONLY
            else users
        )
        divisor = sum((u.annual_count for u in bearers), Decimal(0))
        per_part = mold.annual_amount / divisor if divisor != 0 else None
    shares = tuple(
        ProductShare(
            product=u.product,
            annual=u.annual,
            quantity_per_unit=u.quantity_per_unit,
            per_unit=(
                Decimal(0)
                if u not in bearers
                else (None if per_part is None else u.quantity_per_unit * per_part)
            ),
        )
        for u in users
    )
    return MoldAllocation(
        mold.part, allocation, mold.annual_amount, annual_count, per_part, shares, unknown
    )


def reuse_impact(
    version: Version,
    target: ReuseTarget,
    mold_allocation: MoldAllocation | None = None,
    head: CostHead = CostHead.MATERIAL,
    policy: ScopePolicy | None = None,
) -> ReuseImpact:
    """流用先の既存品の差額 ＝ 使用数 ×（新しい部品の原価 − 古い部品の原価）＋ 型費の分。

    既存品の今の原価は原価の表の製品の行から引く。古い部品が無い流用は古い部品の原価を 0 とする。
    """
    if mold_allocation is None:
        mold_share: Decimal | None = Decimal(0)
    else:
        share = mold_allocation.share_for(target.product)
        mold_share = None if share is None else share.per_unit
    new_cost = calculate_cost(version, target.part, head, policy).total
    old_cost = (
        Decimal(0)
        if target.replaced_part is None
        else calculate_cost(version, target.replaced_part, head, policy).total
    )
    return ReuseImpact(
        product=target.product,
        current_cost=version.cost_value(target.product, head),
        quantity_per_unit=target.quantity_per_unit,
        new_part_cost=new_cost,
        old_part_cost=old_cost,
        mold_share=mold_share,
    )
