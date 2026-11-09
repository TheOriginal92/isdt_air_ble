"""Tests for the charge-settings helpers of round-cell chargers."""

import pytest

from isdt_air_ble.charge_settings import (
    BATTERY_TYPE_OPTIONS,
    ChargeSettingsError,
    SlotSettings,
    build_settings_cmd,
    build_worktask_cmd,
    chemistry_for_option,
    chemistry_for_type,
    current_limits_ma,
    option_from_slot,
    resolve_settings,
    settings_from_slot,
    supports_charge_settings,
)
from isdt_air_ble.parser import parse_charger_responses, parse_workstate

MODEL = "C4 Air"


def _slot(battery_type=1, current=0.5, cutoff=4200, cap=0, state="idle"):
    """A slot dict as parse_workstate produces it."""
    return {
        "battery_type": battery_type,
        "work_current": current,
        "cutoff_mv": cutoff,
        "capacity_limit_mah": cap,
        "work_state_str": state,
    }


def test_worktask_layout_matches_protocol():
    """0x13 0xEA ch task type link current(4 LE) cells cutoff(2 LE) cap(4 LE)."""
    cmd = build_worktask_cmd(2, 1, 1500, 4200, 3000)
    assert cmd == bytearray(
        [0x13, 0xEA, 0x02, 0x00, 0x01, 0x00,
         0xDC, 0x05, 0x00, 0x00,   # 1500 mA
         0x00,                     # cells: auto
         0x68, 0x10,               # 4200 mV
         0xB8, 0x0B, 0x00, 0x00]   # 3000 mAh
    )


def test_auto_cmd_clears_everything():
    cmd = build_settings_cmd(5, SlotSettings(chemistry=None))
    assert cmd[2] == 5
    assert cmd[4] == 6
    assert cmd[6:] == bytearray(11)


def test_workstate_exposes_raw_cutoff_and_capacity_limit():
    frame = bytearray(39)
    frame[0], frame[1], frame[2] = 0x31, 0xE7, 4
    frame[17] = 1                                   # Li-ion
    frame[20:22] = (4150).to_bytes(2, "little")     # cut-off
    frame[22:26] = (1000).to_bytes(4, "little")     # current
    frame[32:36] = (5000).to_bytes(4, "little")     # capacity limit
    parsed = parse_workstate(bytes(frame), model=MODEL)
    assert parsed["cutoff_mv"] == 4150
    assert parsed["capacity_limit_mah"] == 5000
    assert settings_from_slot(parsed) == SlotSettings(
        chemistry_for_option("liion"), 1000, 4150, 5000
    )


def test_options_cover_every_round_cell_type():
    assert BATTERY_TYPE_OPTIONS[0] == "auto"
    for battery_type in range(6):
        assert chemistry_for_type(battery_type).option in BATTERY_TYPE_OPTIONS
    assert chemistry_for_type(6) is None


def test_option_from_slot():
    assert option_from_slot({"battery_type": 6, "work_current": 0.0}) == "auto"
    # Auto mode with a detected NiMH cell reports no work current
    assert option_from_slot({"battery_type": 4, "work_current": 0.0}) == "auto"
    assert option_from_slot({"battery_type": 1, "work_current": 1.5}) == "liion"
    assert option_from_slot({}) is None
    assert option_from_slot(None) is None


def test_changing_current_keeps_cutoff_and_capacity_limit():
    """A custom cut-off and limit set in the ISDT app must survive."""
    slot = _slot(current=1.0, cutoff=4150, cap=5000)
    result = resolve_settings(slot, MODEL, current_ma=700)
    assert result == SlotSettings(chemistry_for_option("liion"), 700, 4150, 5000)


def test_changing_cutoff_or_limit_keeps_current():
    slot = _slot(current=0.8, cutoff=4200, cap=0)
    assert resolve_settings(slot, MODEL, cutoff_mv=4150).current_ma == 800
    assert resolve_settings(slot, MODEL, capacity_limit_mah=3000).capacity_limit_mah == 3000


def test_new_chemistry_uses_default_cutoff_and_fitting_limit():
    slot = _slot(current=0.8, cutoff=4150, cap=5000)
    nimh = resolve_settings(slot, MODEL, option="nimh")
    assert nimh.cutoff_mv == 4          # -dV default
    assert nimh.current_ma == 800       # current kept
    assert nimh.capacity_limit_mah == 0  # 5000 does not fit NiMH (1000–4000)

    life = resolve_settings(slot, MODEL, option="life")
    assert life.cutoff_mv == 3650
    assert life.capacity_limit_mah == 5000


def test_auto_slot_with_battery_fixes_detected_chemistry():
    slot = _slot(battery_type=4, current=0.0, cutoff=0, state="charging")
    result = resolve_settings(slot, MODEL, current_ma=600)
    assert result.chemistry.option == "nimh"
    assert result.cutoff_mv == 4


def test_auto_slot_without_battery_needs_a_type():
    slot = _slot(battery_type=6, current=0.0, cutoff=0)
    with pytest.raises(ChargeSettingsError):
        resolve_settings(slot, MODEL, current_ma=600)
    # Picking a type uses the remembered current
    result = resolve_settings(slot, MODEL, option="liion", current_fallback_ma=600)
    assert result.current_ma == 600


@pytest.mark.parametrize(
    "change",
    [
        {"current_ma": 2100},
        {"cutoff_mv": 4400},            # above Li-ion range
        {"capacity_limit_mah": 1500},   # below Li-ion range
    ],
)
def test_out_of_range_values_are_rejected(change):
    with pytest.raises(ChargeSettingsError):
        resolve_settings(_slot(), MODEL, **change)


def test_fixed_cutoff_chemistry_rejects_a_cutoff():
    slot = _slot(battery_type=5, current=0.5, cutoff=0)
    with pytest.raises(ChargeSettingsError):
        resolve_settings(slot, MODEL, cutoff_mv=1500)


def test_model_gate_and_limits():
    assert supports_charge_settings("C4 Air")
    assert not supports_charge_settings("A8 Air")
    assert not supports_charge_settings("NP2 Air")
    assert current_limits_ma("C4 Air") == (100, 2000)


def test_worktasks_response_is_consumed_quietly():
    """WorkTasksResp (0xEB) must not end up as slot data."""
    parsed, _ = parse_charger_responses(
        [bytes([0x31, 0xEB, 0x01, 0x00]), bytes([0x31, 0xEB, 0x02, 0xFF])],
        num_channels=6,
        model=MODEL,
    )
    assert parsed[1] == {}
    assert parsed[2] == {}
