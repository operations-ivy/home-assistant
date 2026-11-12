"""Whether BlueZ (the host's bluetoothd, over /run/dbus) has a device connected.

    python3 /config/bin/bluez_device.py <name>   ->  on | off | missing

Looked up by its Bluetooth name, so no address is in this repo. Used by
lamp.yaml's watchdog: Home Assistant can't reach the lamp while bluetoothd
says it's connected (a phantom link, 2026-11-11), and only a restart of
bluetooth.service clears that.
"""

import asyncio
import sys

USAGE = "usage: bluez_device.py <name>  ->  on | off | missing"


def _unwrap(variant):
    """A dbus_fast Variant -> its value; plain values pass through."""
    return getattr(variant, "value", variant)


def connected(objects: dict[str, dict[str, dict]], name: str) -> str:
    """GetManagedObjects' answer -> "on", "off" or "missing" for the device called name."""
    for interfaces in objects.values():
        if (device := interfaces.get("org.bluez.Device1")) is None:
            continue
        props = {key: _unwrap(value) for key, value in device.items()}
        if name in (props.get("Name"), props.get("Alias")):
            return "on" if props.get("Connected") else "off"
    return "missing"


async def managed_objects() -> dict:
    # Imported here so the tests can load connected() without dbus_fast.
    from dbus_fast import BusType, Message, MessageType
    from dbus_fast.aio import MessageBus

    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    try:
        reply = await bus.call(
            Message(
                destination="org.bluez",
                path="/",
                interface="org.freedesktop.DBus.ObjectManager",
                member="GetManagedObjects",
            )
        )
    finally:
        bus.disconnect()
    if reply.message_type != MessageType.METHOD_RETURN:
        raise RuntimeError(reply.error_name)
    return reply.body[0]


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(USAGE, file=sys.stderr)
        return 2
    try:
        objects = asyncio.run(managed_objects())
    except (OSError, RuntimeError) as e:  # no bluetoothd, no bus: the sensor goes unavailable
        print(f"bluez_device: {e!r}", file=sys.stderr)
        return 1
    print(connected(objects, argv[1]))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
