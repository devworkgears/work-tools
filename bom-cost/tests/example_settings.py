"""テストで使う架空の設定。計算するテストはこれを import して使う（率の既定は無いため）。"""

from decimal import Decimal

from bomcost import settings

# 原価の品番は 9桁 ＝ 基本6桁 ＋ 枝番2桁 ＋ 0
EXAMPLE = settings.Settings(adjustment_percent=Decimal(10), indirect_percent=Decimal(20), code_total=9, code_base=6,
                            code_branch=2, product_kind="S", part_kind="T", mold_years=Decimal(5))
settings.use(EXAMPLE)
