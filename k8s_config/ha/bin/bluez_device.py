"""Whether BlueZ (the host's bluetoothd, over /run/dbus) has a device connected.

    python3 /config/bin/bluez_device.py <name>   ->  on | off | missing

Looked up by its Bluetooth name, so no address is in this repo. Used by
lamp.yaml's watchdog: Home Assistant can't reach the lamp while bluetoothd
says it's connected (a phantom link, 2026-11-11), and only a restart of
bluetooth.service clears that.
"""

import asyncio
import sys


def connected(objects, name):
    """GetManagedObjects' answer -> "on", "off" or "missing" for the device called name."""
    for interfaces in objects.values():
        device = interfaces.get("org.bluez.Device1")
        if device is None:
            continue
        value = lambda key: getattr(device.get(key), "value", device.get(key))  # noqa: E731
        if value("Name") == name or value("Alias") == name:
            return "on" if value("Connected") else "off"
    return "missing"


async def managed_objects():
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


def main(argv):
    if len(argv) != 2:
        print(__doc__.strip().splitlines()[2].strip(), file=sys.stderr)
        return 2
    print(connected(asyncio.run(managed_objects()), argv[1]))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
