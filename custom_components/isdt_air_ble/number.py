"""Number platform for ISDT Air BLE integration (charge settings per slot)."""

import logging

from homeassistant.components.number import NumberDeviceClass, NumberEntity, NumberMode
from homeassistant.const import EntityCategory, UnitOfElectricCurrent, UnitOfElectricPotential
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .charge_settings import (
    CAPACITY_LIMIT_MAX_MAH,
    CAPACITY_LIMIT_STEP_MAH,
    CHARGE_CURRENT_STEP_MA,
    CUTOFF_RANGE_MV,
    current_limits_ma,
    settings_from_slot,
)
from .const import (
    CONF_ALLOW_CAPACITY_LIMIT,
    CONF_ALLOW_CHARGE_CURRENT,
    CONF_ALLOW_CUTOFF,
    DOMAIN,
    DeviceType,
    supports_charge_settings,
)
from .helpers import slot_device_info

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass, entry, async_add_entities):
    """Set up ISDT number entities from a config entry."""
    coordinator = hass.data[DOMAIN][entry.entry_id]

    entities = []
    if coordinator.device_type == DeviceType.CHARGER and supports_charge_settings(
        coordinator.model
    ):
        # Each writable setting is opt-in in the integration's options.
        enabled = [
            cls
            for option, cls in (
                (CONF_ALLOW_CHARGE_CURRENT, ISDTSlotChargeCurrentNumber),
                (CONF_ALLOW_CUTOFF, ISDTSlotCutoffNumber),
                (CONF_ALLOW_CAPACITY_LIMIT, ISDTSlotCapacityLimitNumber),
            )
            if entry.options.get(option, False)
        ]
        for ch in range(coordinator.channel_count):
            entities.extend(cls(coordinator, ch) for cls in enabled)

    async_add_entities(entities)


class ISDTSlotSettingNumber(CoordinatorEntity, NumberEntity):
    """Base for the per-slot charge setting numbers.

    The value is what the charger reports for the slot. In auto mode the
    charger chooses everything itself and the value is unknown.
    """

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG
    _attr_mode = NumberMode.BOX
    _key: str

    def __init__(self, coordinator, channel: int):
        super().__init__(coordinator)
        self._channel = channel
        address = coordinator.address
        self._attr_unique_id = f"{address}_ch{channel}_{self._key}"
        self._attr_translation_key = self._key
        self._attr_device_info = slot_device_info(address, channel + 1, coordinator.model)

    def _slot(self) -> dict | None:
        return self.coordinator.slot_view(self._channel)

    @property
    def available(self) -> bool:
        """Available while the slot has a fixed battery type.

        In auto mode the charger picks these values itself; a slot only
        takes them after a battery type is set.
        """
        return (
            self.coordinator.entities_available
            and settings_from_slot(self._slot()) is not None
        )


class ISDTSlotChargeCurrentNumber(ISDTSlotSettingNumber):
    """Charge current of one slot with a fixed battery type."""

    _key = "charge_current_setting"
    _attr_device_class = NumberDeviceClass.CURRENT
    _attr_native_unit_of_measurement = UnitOfElectricCurrent.AMPERE
    _attr_native_step = CHARGE_CURRENT_STEP_MA / 1000
    _attr_icon = "mdi:current-dc"

    def __init__(self, coordinator, channel: int):
        super().__init__(coordinator, channel)
        low, high = current_limits_ma(coordinator.model)
        self._attr_native_min_value = low / 1000
        self._attr_native_max_value = high / 1000

    @property
    def native_value(self) -> float | None:
        settings = settings_from_slot(self._slot())
        return settings.current_ma / 1000 if settings else None

    async def async_set_native_value(self, value: float) -> None:
        current_ma = round(value * 1000)
        await self.coordinator.async_apply_charge_settings(
            self._channel, current_ma=current_ma
        )
        # Reused when this slot goes back to auto and a type is picked again.
        self.coordinator.charge_current_setpoint[self._channel] = current_ma


class ISDTSlotCutoffNumber(ISDTSlotSettingNumber):
    """Full-charge cut-off of one slot, in mV.

    An absolute voltage for lithium and NiZn cells, the -dV delta for
    NiMH/NiCd. The chemistry's allowed range is checked on write.
    """

    _key = "cutoff_setting"
    _attr_native_unit_of_measurement = UnitOfElectricPotential.MILLIVOLT
    _attr_native_min_value = CUTOFF_RANGE_MV[0]
    _attr_native_max_value = CUTOFF_RANGE_MV[1]
    _attr_native_step = 1
    _attr_icon = "mdi:battery-arrow-up"

    @property
    def available(self) -> bool:
        settings = settings_from_slot(self._slot())
        return (
            super().available
            and settings is not None
            and settings.chemistry.cutoff_range_mv is not None
        )

    @property
    def native_value(self) -> int | None:
        settings = settings_from_slot(self._slot())
        return settings.cutoff_mv if settings else None

    @property
    def extra_state_attributes(self) -> dict | None:
        settings = settings_from_slot(self._slot())
        if not settings or settings.chemistry.cutoff_range_mv is None:
            return None
        low, high = settings.chemistry.cutoff_range_mv
        return {
            "allowed_min": low,
            "allowed_max": high,
            "delta_v": settings.chemistry.cutoff_is_delta,
        }

    async def async_set_native_value(self, value: float) -> None:
        await self.coordinator.async_apply_charge_settings(
            self._channel, cutoff_mv=round(value)
        )


class ISDTSlotCapacityLimitNumber(ISDTSlotSettingNumber):
    """Capacity limit of one slot in mAh; 0 switches it off."""

    _key = "capacity_limit_setting"
    _attr_native_unit_of_measurement = "mAh"
    _attr_native_min_value = 0
    _attr_native_max_value = CAPACITY_LIMIT_MAX_MAH
    _attr_native_step = CAPACITY_LIMIT_STEP_MAH
    _attr_icon = "mdi:battery-lock"

    @property
    def native_value(self) -> int | None:
        settings = settings_from_slot(self._slot())
        return settings.capacity_limit_mah if settings else None

    @property
    def extra_state_attributes(self) -> dict | None:
        settings = settings_from_slot(self._slot())
        if not settings:
            return None
        low, high = settings.chemistry.capacity_range_mah
        return {"allowed_min": low, "allowed_max": high}

    async def async_set_native_value(self, value: float) -> None:
        await self.coordinator.async_apply_charge_settings(
            self._channel, capacity_limit_mah=round(value)
        )
