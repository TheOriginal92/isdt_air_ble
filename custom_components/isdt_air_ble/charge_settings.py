"""Charge settings (battery type, charge current) for round-cell chargers.

Pure helpers without Home Assistant imports, so they can be unit tested.

The charger takes all charge parameters of a slot in one WorkTasksReq
(0x13 0xEA) on AF01 and answers with WorkTasksResp (0x31 0xEB channel
error_code). The current settings come back in the slot's WorkStateResp:
battery_type at byte 17, work_current_mA at bytes 22-25. While a slot runs
in automatic mode the charger reports work_current 0 and the chemistry it
detected (or 6 = Auto for a slot that never saw a battery).

Protocol reference: PROTOCOL.md of DittelHome/ISDT-Charge-Utility (MIT),
"WorkTasksReq (0x13 0xEA) -- Set Charging Parameters".
"""

from __future__ import annotations

from dataclasses import dataclass

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


@dataclass(frozen=True)
class Chemistry:
    """One selectable battery type."""

    option: str           # select option / translation key
    battery_type: int     # protocol value
    cutoff_mv: int        # default full-charge (cut-off) voltage, 0 = device default


# Order follows the protocol values. NiMH/NiCd uses a -dV cut-off in mV
# rather than an absolute voltage; the 1.5 V Li-ion cells have a fixed one.
CHEMISTRIES: tuple[Chemistry, ...] = (
    Chemistry("lihv", 0, 4350),
    Chemistry("liion", 1, 4200),
    Chemistry("life", 2, 3650),
    Chemistry("nizn", 3, 1900),
    Chemistry("nimh", 4, 4),
    Chemistry("liion_1v5", 5, 0),
)

OPTION_AUTO = "auto"
BATTERY_TYPE_OPTIONS: list[str] = [OPTION_AUTO] + [c.option for c in CHEMISTRIES]

_BY_OPTION = {c.option: c for c in CHEMISTRIES}
_BY_TYPE = {c.battery_type: c for c in CHEMISTRIES}


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


def build_auto_cmd(channel: int) -> bytearray:
    """Build the WorkTasksReq that puts a slot back to automatic detection."""
    return build_worktask_cmd(channel, BATTERY_TYPE_AUTO, 0, 0, 0)


def build_manual_cmd(channel: int, chem: Chemistry, current_ma: int) -> bytearray:
    """Build the WorkTasksReq for a fixed chemistry and charge current."""
    return build_worktask_cmd(channel, chem.battery_type, current_ma, chem.cutoff_mv, 0)
