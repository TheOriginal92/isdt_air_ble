"""Tests for the charge-settings helpers of round-cell chargers."""

import json
from pathlib import Path

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
    is_auto_slot,
    option_from_slot,
    overlay_settings,
    resolve_settings,
    settings_from_slot,
    slot_matches,
)
from isdt_air_ble.const import supports_charge_settings
from isdt_air_ble.parser import parse_charger_responses, parse_workstate

MODEL = "C4 Air"
FIXTURES = Path(__file__).parent / "fixtures" / "c4air"


def _capture(name: str) -> list[bytes]:
    frames = []
    for line in (FIXTURES / name).read_text().splitlines():
        event = json.loads(line) if line.strip() else {}
        if event.get("kind") == "notify" and event.get("char") == "AF01":
            frames.append(bytes.fromhex(event["hex"].replace(" ", "")))
    return frames


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
    # Auto mode after a detected NiMH cell: no current and no cut-off
    assert option_from_slot(
        {"battery_type": 4, "work_current": 0.0, "cutoff_mv": 0}
    ) == "auto"
    assert option_from_slot(
        {"battery_type": 1, "work_current": 1.5, "cutoff_mv": 4200}
    ) == "liion"
    assert option_from_slot({}) is None
    assert option_from_slot(None) is None


def test_fixed_type_with_a_cutoff_but_no_current_is_not_auto():
    """Only no current AND no cut-off means auto; one set value is a fixed type."""
    assert not is_auto_slot({"battery_type": 4, "work_current": 0.0, "cutoff_mv": 4})
    assert option_from_slot(
        {"battery_type": 4, "work_current": 0.0, "cutoff_mv": 4}
    ) == "nimh"
    # 1.5 V Li-ion has no cut-off of its own, but a current
    assert option_from_slot(
        {"battery_type": 5, "work_current": 0.5, "cutoff_mv": 0}
    ) == "liion_1v5"


def test_captured_auto_slot_charges_without_a_current():
    """In the maintainer's capture, a slot reporting NiMH with current and
    cut-off 0 is charging: the charger picks the current itself."""
    parsed, _ = parse_charger_responses(_capture("1cell_charging.jsonl"), 6, MODEL)
    slot0 = parsed[0]
    assert slot0["battery_type"] == 4
    assert slot0["work_state_str"] == "charging"
    assert is_auto_slot(slot0)


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


@pytest.mark.parametrize(
    "change", [{"current_ma": 600}, {"cutoff_mv": 4}, {"capacity_limit_mah": 2000}]
)
def test_auto_slot_with_battery_does_not_pin_the_detected_chemistry(change):
    """A current alone on an auto slot would fix the detected chemistry for
    every later cell; the slot needs an explicit battery type first."""
    slot = _slot(battery_type=4, current=0.0, cutoff=0, state="charging")
    with pytest.raises(ChargeSettingsError):
        resolve_settings(slot, MODEL, **change)


def test_reported_values_are_resent_unjudged():
    """A cut-off the charger reports outside the table (here 0) does not
    block a current change; it is sent back as the charger has it."""
    slot = _slot(battery_type=1, current=0.5, cutoff=0, cap=0)
    result = resolve_settings(slot, MODEL, current_ma=700)
    assert result == SlotSettings(chemistry_for_option("liion"), 700, 0, 0)
    # Only the value that changes is checked
    with pytest.raises(ChargeSettingsError):
        resolve_settings(slot, MODEL, cutoff_mv=0)


def test_second_change_builds_on_the_first():
    """After a type change the next change must use the new type, not the
    one the last poll still reports."""
    slot = _slot(battery_type=1, current=0.5, cutoff=4200)
    first = resolve_settings(slot, MODEL, option="nimh")
    view = overlay_settings(slot, first)
    second = resolve_settings(view, MODEL, current_ma=800)
    assert second == SlotSettings(chemistry_for_option("nimh"), 800, 4, 0)
    assert slot_matches(overlay_settings(slot, second), second)
    assert not slot_matches(slot, second)


def test_overlay_for_auto():
    slot = _slot(battery_type=1, current=0.5, cutoff=4200, cap=2000)
    auto = SlotSettings(chemistry=None)
    view = overlay_settings(slot, auto)
    assert option_from_slot(view) == "auto"
    assert slot_matches(view, auto)
    # The charger reporting the last chemistry with nothing set also counts
    assert slot_matches(_slot(battery_type=1, current=0.0, cutoff=0), auto)


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
