"""BOM（部品構成）から製品の原価を計算する中心の層。取り込み（CSV／Excel）と SQLite への保存も持つ。"""

from .doublecount import (
    DEFAULT_NEAR_TOLERANCE,
    DEFAULT_SMALL_RATIO,
    Judgement,
    Verdict,
    judge_double_counting,
    tally,
)
from .importer import (
    TABLE_FIELDS,
    ColumnMap,
    FieldSpec,
    ImportWarning,
    MissingColumnsError,
    Table,
    VersionImport,
    read_csv,
    read_table,
    read_xlsx,
)
from .model import (
    BASIC_HEADS,
    MANUAL_VERSION_ID,
    BomConflict,
    BomLine,
    CostHead,
    CostRow,
    Item,
    ManualOverlap,
    Source,
    Version,
    Volume,
    VolumeKind,
    base_code,
    mbom_to_ebom,
    overlay_manual,
)
from .modelchange import MatchKind, StructureRow, compare_structures
from .mold import (
    DEFAULT_AMORTIZATION_YEARS,
    Allocation,
    MoldAllocation,
    MoldCost,
    PartUser,
    ProductShare,
    ReuseImpact,
    ReuseTarget,
    allocate_mold_cost,
    part_users,
    reuse_impact,
    usage_per_unit,
)
from .normalize import to_code, to_decimal, to_flag, to_optional_code, to_optional_decimal, to_symbol
from .rollup import CostCalculator, CostResult, Scope, ScopePolicy, calculate_cost
from .scenario import (
    Change,
    ChangeKind,
    ChangeRecord,
    LineComparison,
    Scenario,
    VersionComparison,
)
from .store import PartMapRow, ScenarioInfo, Store, VersionInfo, VersionInUseError

__all__ = [
    "Allocation", "BASIC_HEADS", "BomConflict", "BomLine", "Change", "ChangeKind", "ChangeRecord",
    "ColumnMap", "CostCalculator", "CostHead", "CostResult", "CostRow", "DEFAULT_AMORTIZATION_YEARS",
    "DEFAULT_NEAR_TOLERANCE", "DEFAULT_SMALL_RATIO", "FieldSpec", "ImportWarning", "Item", "Judgement",
    "LineComparison", "MANUAL_VERSION_ID", "ManualOverlap", "MatchKind", "MissingColumnsError",
    "MoldAllocation", "MoldCost", "PartMapRow", "PartUser", "ProductShare", "ReuseImpact", "ReuseTarget", "Scenario",
    "ScenarioInfo", "Scope", "ScopePolicy", "Source", "Store", "StructureRow", "TABLE_FIELDS", "Table",
    "Verdict", "Version", "VersionComparison", "VersionImport", "VersionInUseError", "VersionInfo", "Volume",
    "VolumeKind", "allocate_mold_cost", "base_code", "calculate_cost", "compare_structures",
    "judge_double_counting", "mbom_to_ebom", "overlay_manual", "part_users", "read_csv", "read_table",
    "read_xlsx", "reuse_impact", "tally", "to_code", "to_decimal", "to_flag", "to_optional_code",
    "to_optional_decimal", "to_symbol", "usage_per_unit",
]
