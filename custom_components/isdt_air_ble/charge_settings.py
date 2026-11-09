"""Charge settings of round-cell charger slots.

Pure helpers without Home Assistant imports, so they can be unit tested.

The charger takes all charge parameters of a slot in one WorkTasksReq
(0x13 0xEA) on AF01 and answers with WorkTasksResp (0x31 0xEB channel
error_code). The stored settings come back in the slot's WorkStateResp:
battery_type (byte 17), cut-off in mV (bytes 20-21), work current in mA
(bytes 22-25) and capacity limit in mAh (bytes 32-35). While a slot runs
in automatic mode the charger reports work current 0 and the chemistry it
detected (or 6 = Auto for a slot that never saw a battery).

Because one command carries every parameter, a change to one of them must
resend the others as the slot has them, or they fall back to defaults.

Protocol reference: PROTOCOL.md of DittelHome/ISDT-Charge-Utility (MIT),
"WorkTasksReq (0x13 0xEA) -- Set Charging Parameters" and its battery
validation table. Every WorkTasksReq resets the slot's energy (mWh) counter.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

CMD_WORKTASKS_REQ = bytearray([0x13, 0xEA])
RESP_WORKTASKS = 0xEB

TASK_TYPE_CHARGE = 0
BATTERY_TYPE_AUTO = 6

# Models that take the standard WorkTasksReq for charge settings. The A8 Air
# uses a separate task format and the balance/camera chargers use other
# chemistry enums, so they are left out until someone can test them.
CHARGE_SETTINGS_MODELS = frozenset({"C4 Air"})

# Charge current limits per model, in mA.
CHARGE_CURRENT_LIMITS_MA = {
    "C4 Air": (100, 2000),
}
CHARGE_CURRENT_STEP_MA = 100
DEFAULT_CHARGE_CURRENT_MA = 500

# Widest ranges over all chemistries, for the number entities' bounds.
CUTOFF_RANGE_MV = (3, 4450)
CAPACITY_LIMIT_MAX_MAH = 7000
CAPACITY_LIMIT_STEP_MAH = 100


@dataclass(frozen=True)
class Chemistry:
    """One selectable battery type and its limits."""

    option: str                    # select option / translation key
    battery_type: int              # protocol value
    cutoff_default_mv: int         # 0 = fixed by the charger
    cutoff_range_mv: tuple[int, int] | None  # None = not adjustable
    capacity_range_mah: tuple[int, int]      # allowed non-zero limit

    @property
    def cutoff_is_delta(self) -> bool:
        """NiMH/NiCd terminate on a -dV drop, not on an absolute voltage."""
        return self.option == "nimh"


CHEMISTRIES: tuple[Chemistry, ...] = (
    Chemistry("lihv", 0, 4350, (4250, 4450), (2000, 7000)),
    Chemistry("liion", 1, 4200, (4100, 4300), (2000, 7000)),
    Chemistry("life", 2, 3650, (3550, 3750), (2000, 7000)),
    Chemistry("nizn", 3, 1900, (1800, 2000), (2000, 7000)),
    Chemistry("nimh", 4, 4, (3, 12), (1000, 4000)),
    Chemistry("liion_1v5", 5, 0, None, (1000, 4000)),
)

OPTION_AUTO = "auto"
BATTERY_TYPE_OPTIONS: list[str] = [OPTION_AUTO] + [c.option for c in CHEMISTRIES]

_BY_OPTION = {c.option: c for c in CHEMISTRIES}
_BY_TYPE = {c.battery_type: c for c in CHEMISTRIES}


class ChargeSettingsError(ValueError):
    """A requested setting is not valid for the slot."""


@dataclass(frozen=True)
class SlotSettings:
    """The full parameter set of one slot's WorkTasksReq."""

    chemistry: Chemistry | None    # None = auto
    current_ma: int = 0
    cutoff_mv: int = 0
    capacity_limit_mah: int = 0


def supports_charge_settings(model: str) -> bool:
    """Return True if the model takes charge settings via WorkTasksReq."""
    return model in CHARGE_SETTINGS_MODELS


def current_limits_ma(model: str) -> tuple[int, int]:
    """Return (min, max) charge current in mA for a model."""
    return CHARGE_CURRENT_LIMITS_MA.get(model, (100, 1000))


def chemistry_for_option(option: str) -> Chemistry | None:
    """Return the chemistry for a select option (None for auto/unknown)."""
    return _BY_OPTION.get(option)


def chemistry_for_type(battery_type: int | None) -> Chemistry | None:
    """Return the chemistry for a protocol battery type (None for auto/unknown)."""
    if battery_type is None:
        return None
    return _BY_TYPE.get(battery_type)


def option_from_slot(slot: dict | None) -> str | None:
    """Derive the configured battery-type option from a slot's WorkState.

    A slot in automatic mode reports work_current 0 (and either 6 or the
    chemistry it detected), so a set charge current is what tells a manual
    setting apart from automatic detection.
    """
    if not slot or "battery_type" not in slot:
        return None
    battery_type = slot.get("battery_type")
    work_current = slot.get("work_current") or 0
    if battery_type == BATTERY_TYPE_AUTO or work_current <= 0:
        return OPTION_AUTO
    chem = chemistry_for_type(battery_type)
    return chem.option if chem else None


def settings_from_slot(slot: dict | None) -> SlotSettings | None:
    """Return the slot's current settings, or None while it is in auto mode."""
    option = option_from_slot(slot)
    if option in (None, OPTION_AUTO):
        return None
    return SlotSettings(
        chemistry=chemistry_for_option(option),
        current_ma=round((slot.get("work_current") or 0) * 1000),
        cutoff_mv=slot.get("cutoff_mv") or 0,
        capacity_limit_mah=slot.get("capacity_limit_mah") or 0,
    )


def _valid_cutoff(chem: Chemistry, cutoff_mv: int) -> bool:
    if chem.cutoff_range_mv is None:
        return cutoff_mv == 0
    low, high = chem.cutoff_range_mv
    return low <= cutoff_mv <= high


def _valid_capacity(chem: Chemistry, capacity_mah: int) -> bool:
    if capacity_mah == 0:
        return True
    low, high = chem.capacity_range_mah
    return low <= capacity_mah <= high


def resolve_settings(
    slot: dict | None,
    model: str,
    *,
    option: str | None = None,
    current_ma: int | None = None,
    cutoff_mv: int | None = None,
    capacity_limit_mah: int | None = None,
    current_fallback_ma: int | None = None,
) -> SlotSettings:
    """Merge one change into the slot's current settings.

    Unchanged parameters keep the values the slot reports. A new chemistry
    takes its default cut-off and keeps the capacity limit if it fits.
    Raises ChargeSettingsError for values the chemistry does not accept.
    """
    slot = slot or {}
    current = settings_from_slot(slot)

    if option == OPTION_AUTO:
        return SlotSettings(chemistry=None)

    if option is not None:
        chem = chemistry_for_option(option)
        if chem is None:
            raise ChargeSettingsError(f"Unknown battery type: {option}")
    elif current is not None:
        chem = current.chemistry
    elif slot.get("work_state_str") in ("charging", "done"):
        # Auto mode with a battery inserted: keep the detected chemistry.
        chem = chemistry_for_type(slot.get("battery_type"))
    else:
        chem = None
    if chem is None:
        raise ChargeSettingsError(
            "The slot is in auto mode without a detected battery. "
            "Pick a battery type first."
        )

    if current is not None and current.chemistry == chem:
        result = current
    else:
        previous_cap = current.capacity_limit_mah if current else 0
        result = SlotSettings(
            chemistry=chem,
            current_ma=(current.current_ma if current else 0)
            or current_fallback_ma
            or DEFAULT_CHARGE_CURRENT_MA,
            cutoff_mv=chem.cutoff_default_mv,
            capacity_limit_mah=previous_cap if _valid_capacity(chem, previous_cap) else 0,
        )

    if current_ma is not None:
        result = replace(result, current_ma=current_ma)
    if cutoff_mv is not None:
        result = replace(result, cutoff_mv=cutoff_mv)
    if capacity_limit_mah is not None:
        result = replace(result, capacity_limit_mah=capacity_limit_mah)

    low, high = current_limits_ma(model)
    if not low <= result.current_ma <= high:
        raise ChargeSettingsError(
            f"Charge current must be between {low} and {high} mA."
        )
    if not _valid_cutoff(chem, result.cutoff_mv):
        if chem.cutoff_range_mv is None:
            raise ChargeSettingsError(f"{chem.option} has a fixed cut-off voltage.")
        lo, hi = chem.cutoff_range_mv
        raise ChargeSettingsError(f"Cut-off for {chem.option} must be {lo}–{hi} mV.")
    if not _valid_capacity(chem, result.capacity_limit_mah):
        lo, hi = chem.capacity_range_mah
        raise ChargeSettingsError(
            f"Capacity limit for {chem.option} must be 0 (off) or {lo}–{hi} mAh."
        )
    return result


def build_worktask_cmd(
    channel: int,
    battery_type: int,
    current_ma: int,
    cutoff_mv: int = 0,
    capacity_limit_mah: int = 0,
    cells: int = 0,
) -> bytearray:
    """Build a WorkTasksReq (charge) for one slot.

    Layout: 0x13 0xEA channel task_type battery_type linking_type
            work_current(4 LE) cells full_charged_volt(2 LE) capacity_limit(4 LE)
    """
    cmd = bytearray(CMD_WORKTASKS_REQ)
    cmd.append(channel & 0xFF)
    cmd.append(TASK_TYPE_CHARGE)
    cmd.append(battery_type & 0xFF)
    cmd.append(0)  # linking type: default
    cmd.extend(int(current_ma).to_bytes(4, "little"))
    cmd.append(cells & 0xFF)
    cmd.extend(int(cutoff_mv).to_bytes(2, "little"))
    cmd.extend(int(capacity_limit_mah).to_bytes(4, "little"))
    return cmd


def build_settings_cmd(channel: int, settings: SlotSettings) -> bytearray:
    """Build the WorkTasksReq for a resolved settings set."""
    if settings.chemistry is None:
        return build_worktask_cmd(channel, BATTERY_TYPE_AUTO, 0, 0, 0)
    return build_worktask_cmd(
        channel,
        settings.chemistry.battery_type,
        settings.current_ma,
        settings.cutoff_mv,
        settings.capacity_limit_mah,
    )
