"""Tests for the charge-settings helpers (battery type / charge current)."""

from isdt_air_ble.charge_settings import (
    BATTERY_TYPE_OPTIONS,
    build_auto_cmd,
    build_manual_cmd,
    build_worktask_cmd,
    chemistry_for_option,
    chemistry_for_type,
    current_limits_ma,
    option_from_slot,
    supports_charge_settings,
)
from isdt_air_ble.parser import parse_charger_responses


def test_worktask_layout_matches_protocol():
    """0x13 0xEA ch task type link current(4 LE) cells cutoff(2 LE) cap(4 LE)."""
    cmd = build_worktask_cmd(2, 1, 1500, 4200, 0)
    assert cmd == bytearray(
        [0x13, 0xEA, 0x02, 0x00, 0x01, 0x00,
         0xDC, 0x05, 0x00, 0x00,   # 1500 mA
         0x00,                     # cells: auto
         0x68, 0x10,               # 4200 mV
         0x00, 0x00, 0x00, 0x00]   # no capacity limit
    )
    assert len(cmd) == 17


def test_auto_cmd_clears_current_and_cutoff():
    cmd = build_auto_cmd(5)
    assert cmd[2] == 5
    assert cmd[4] == 6  # battery type Auto
    assert cmd[6:10] == bytearray(4)
    assert cmd[11:13] == bytearray(2)


def test_manual_cmd_uses_chemistry_default_cutoff():
    nimh = chemistry_for_option("nimh")
    cmd = build_manual_cmd(0, nimh, 800)
    assert cmd[4] == 4
    assert int.from_bytes(cmd[6:10], "little") == 800
    assert int.from_bytes(cmd[11:13], "little") == 4  # -dV in mV

    liion = chemistry_for_option("liion")
    assert int.from_bytes(build_manual_cmd(0, liion, 1000)[11:13], "little") == 4200


def test_options_cover_every_round_cell_type():
    assert BATTERY_TYPE_OPTIONS[0] == "auto"
    for battery_type in range(6):
        chem = chemistry_for_type(battery_type)
        assert chem is not None
        assert chem.option in BATTERY_TYPE_OPTIONS
    assert chemistry_for_type(6) is None
    assert chemistry_for_option("auto") is None


def test_option_from_slot():
    # Never-used slot: Auto
    assert option_from_slot({"battery_type": 6, "work_current": 0.0}) == "auto"
    # Auto mode with a detected NiMH cell reports no work current
    assert option_from_slot({"battery_type": 4, "work_current": 0.0}) == "auto"
    # Manual setting: chemistry plus a set current
    assert option_from_slot({"battery_type": 1, "work_current": 1.5}) == "liion"
    # No WorkState yet
    assert option_from_slot({}) is None
    assert option_from_slot(None) is None


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
        model="C4 Air",
    )
    assert parsed[1] == {}
    assert parsed[2] == {}
