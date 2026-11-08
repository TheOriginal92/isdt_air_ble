"""Number platform for ISDT Air BLE integration (charge current per slot)."""

import logging

from homeassistant.components.number import NumberDeviceClass, NumberEntity, NumberMode
from homeassistant.const import EntityCategory, UnitOfElectricCurrent
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .charge_settings import (
    CHARGE_CURRENT_STEP_MA,
    OPTION_AUTO,
    build_manual_cmd,
    chemistry_for_option,
    chemistry_for_type,
    current_limits_ma,
    option_from_slot,
    supports_charge_settings,
)
from .const import DOMAIN, DeviceType
from .helpers import slot_device_info

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass, entry, async_add_entities):
    """Set up ISDT number entities from a config entry."""
    coordinator = hass.data[DOMAIN][entry.entry_id]

    entities = []
    if coordinator.device_type == DeviceType.CHARGER and supports_charge_settings(
        coordinator.model
    ):
        entities.extend(
            ISDTSlotChargeCurrentNumber(coordinator, ch)
            for ch in range(coordinator.channel_count)
        )

    async_add_entities(entities)


class ISDTSlotChargeCurrentNumber(CoordinatorEntity, NumberEntity):
    """Charge current setting of one charger slot.

    The charger only takes a current together with a fixed chemistry. In
    auto mode the chemistry it detected for the inserted battery is used;
    with no battery in an auto slot, a battery type has to be picked first.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "charge_current_setting"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_device_class = NumberDeviceClass.CURRENT
    _attr_native_unit_of_measurement = UnitOfElectricCurrent.AMPERE
    _attr_native_step = CHARGE_CURRENT_STEP_MA / 1000
    _attr_mode = NumberMode.BOX
    _attr_icon = "mdi:current-dc"

    def __init__(self, coordinator, channel: int):
        super().__init__(coordinator)
        self._channel = channel
        address = coordinator.address
        low, high = current_limits_ma(coordinator.model)
        self._attr_native_min_value = low / 1000
        self._attr_native_max_value = high / 1000
        self._attr_unique_id = f"{address}_ch{channel}_charge_current_setting"
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
    def native_value(self) -> float | None:
        """Return the charge current the charger reports for the slot.

        In auto mode the charger picks the current itself and reports 0,
        so the value is unknown there.
        """
        slot = self._slot() or {}
        reported = slot.get("work_current") or 0
        return round(reported, 2) if reported > 0 else None

    async def async_set_native_value(self, value: float) -> None:
        current_ma = round(value * 1000)
        slot = self._slot() or {}
        option = option_from_slot(slot)
        if option and option != OPTION_AUTO:
            chem = chemistry_for_option(option)
        elif slot.get("work_state_str") in ("charging", "done"):
            # Auto mode with a battery inserted: keep the detected chemistry.
            chem = chemistry_for_type(slot.get("battery_type"))
        else:
            chem = None

        # Remember the value either way, so a battery type picked next
        # uses it.
        self.coordinator.charge_current_setpoint[self._channel] = current_ma

        if chem is None:
            self.async_write_ha_state()
            raise HomeAssistantError(
                f"Slot {self._channel + 1} is in auto mode without a detected "
                "battery. Pick a battery type first; it will use this current."
            )

        cmd = build_manual_cmd(self._channel, chem, current_ma)
        try:
            await self.coordinator.async_send_charge_task(self._channel, cmd)
        except Exception as err:
            raise HomeAssistantError(
                f"Could not set the charge current for slot {self._channel + 1}: {err}"
            ) from err
        self.async_write_ha_state()
