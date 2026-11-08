"""Select platform for ISDT Air BLE integration."""

import logging

from homeassistant.components.select import SelectEntity
from homeassistant.const import EntityCategory
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .charge_settings import (
    BATTERY_TYPE_OPTIONS,
    DEFAULT_CHARGE_CURRENT_MA,
    OPTION_AUTO,
    build_auto_cmd,
    build_manual_cmd,
    chemistry_for_option,
    current_limits_ma,
    option_from_slot,
    supports_charge_settings,
)
from .const import DOMAIN, AdapterProtocol, DeviceType
from .helpers import main_device_info, slot_device_info

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass, entry, async_add_entities):
    """Set up ISDT select entities from a config entry."""
    coordinator = hass.data[DOMAIN][entry.entry_id]

    entities = []
    # Volume control is a MASS2 settings feature; PS200 adapters only
    # have the plain alarm-tone switch.
    if coordinator.adapter_protocol == AdapterProtocol.MASS2:
        entities.append(ISDTMASS2VolumeSelect(coordinator))

    if coordinator.device_type == DeviceType.CHARGER and supports_charge_settings(
        coordinator.model
    ):
        entities.extend(
            ISDTSlotBatteryTypeSelect(coordinator, ch)
            for ch in range(coordinator.channel_count)
        )

    async_add_entities(entities)


class ISDTMASS2VolumeSelect(CoordinatorEntity, SelectEntity):
    """Select entity for the MASS2 buzzer volume."""

    _attr_has_entity_name = True
    _attr_translation_key = "volume"
    _attr_options = ["low", "medium", "high"]
    _attr_icon = "mdi:volume-medium"

    _VALUE_TO_OPTION = {0: "low", 1: "medium", 2: "high"}
    _OPTION_TO_VALUE = {v: k for k, v in _VALUE_TO_OPTION.items()}

    def __init__(self, coordinator):
        super().__init__(coordinator)
        address = coordinator.address
        model = coordinator.model

        self._attr_unique_id = f"{address}_volume"
        self._attr_device_info = main_device_info(address, model)

    @property
    def current_option(self) -> str | None:
        settings = self.coordinator.mass2_settings
        if settings is None:
            return None
        return self._VALUE_TO_OPTION.get(settings.get("volume", 0))

    @property
    def available(self) -> bool:
        return self.coordinator.connected and self.coordinator.mass2_settings is not None

    async def async_select_option(self, option: str) -> None:
        value = self._OPTION_TO_VALUE.get(option)
        if value is None:
            return
        try:
            await self.coordinator.async_set_mass2_volume(value)
            self.async_write_ha_state()
        except Exception as err:
            _LOGGER.error("Failed to set MASS2 volume: %s", err)


class ISDTSlotBatteryTypeSelect(CoordinatorEntity, SelectEntity):
    """Battery type setting of one charger slot (auto or a fixed chemistry).

    Picking a chemistry sends it together with the slot's charge current
    (the one the charger reports, else the last one set in HA, else a safe
    default). "Auto" hands the slot back to automatic detection.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "battery_type_setting"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:battery-sync"
    _attr_options = BATTERY_TYPE_OPTIONS

    def __init__(self, coordinator, channel: int):
        super().__init__(coordinator)
        self._channel = channel
        address = coordinator.address
        self._attr_unique_id = f"{address}_ch{channel}_battery_type_setting"
        self._attr_device_info = slot_device_info(address, channel + 1, coordinator.model)

    def _slot(self) -> dict | None:
        data = self.coordinator.data
        if data and self._channel in data:
            return data[self._channel]
        return None

    @property
    def available(self) -> bool:
        return self.coordinator.entities_available and option_from_slot(self._slot()) is not None

    @property
    def current_option(self) -> str | None:
        return option_from_slot(self._slot())

    async def async_select_option(self, option: str) -> None:
        if option == OPTION_AUTO:
            cmd = build_auto_cmd(self._channel)
        else:
            chem = chemistry_for_option(option)
            if chem is None:
                raise HomeAssistantError(f"Unknown battery type: {option}")
            slot = self._slot() or {}
            reported_ma = round((slot.get("work_current") or 0) * 1000)
            current_ma = (
                reported_ma
                or self.coordinator.charge_current_setpoint.get(self._channel)
                or DEFAULT_CHARGE_CURRENT_MA
            )
            low, high = current_limits_ma(self.coordinator.model)
            current_ma = max(low, min(high, current_ma))
            cmd = build_manual_cmd(self._channel, chem, current_ma)
        try:
            await self.coordinator.async_send_charge_task(self._channel, cmd)
        except Exception as err:
            raise HomeAssistantError(
                f"Could not set the battery type for slot {self._channel + 1}: {err}"
            ) from err
