# The lamp proxy

An M5Stack Atom Lite (ESP32-PICO-D4) running ESPHome as a Bluetooth proxy
(home-assistant#20). It sits in the bedroom beside `light.lamp2` (Philips
Hue BLE), the bulb farthest from brick2000, powered by a USB wall charger.
Home Assistant reaches lamp2 through it over Wi-Fi instead of through
brick2000's onboard radio, which shares the Pi's chip and antenna with its
Wi-Fi. `light.lamp`, in the room with the brick nodes, stays on
brick2000's adapter; HA picks the closest connectable scanner for each
bulb by itself, and either adapter is a fallback for the other.

- `lamp-proxy.yaml`: the config. Bluetooth proxy (active, 3 connection
  slots), the IR LED on GPIO12 (no commands yet) and the button on GPIO39.
  The RGB LED (GPIO27) is deliberately left out of the firmware, so it
  never lights: the proxy sits in a bedroom where someone may be asleep.
- `esphome`: ESPHome pinned (2026.9.1) in Docker; usage in its header.
- `secrets.yaml` (gitignored, mode 600): Wi-Fi, the API encryption key and
  the OTA password, sealed in brick-k8s-secrets as
  `home-assistant/lamp-proxy-esphome-sealedsecret.yaml`;
  `secrets.example.yaml` lists the keys. Generate the API
  key with `openssl rand -base64 32`.

## Flashing

The first flash is over USB-C from the laptop: the Atom shows up as an
FT232 (`0403:6001`, `/dev/serial/by-id/usb-Hades2001_M5stack_*`) on
`/dev/ttyUSB0`. A charge-only cable powers it but shows no serial
port. ESPHome's 460800 baud upload fails on this board; it retries at
115200 by itself (about 75 s).

    esphome/esphome run lamp-proxy.yaml --device /dev/ttyUSB0

After that, over Wi-Fi (`lamp-proxy.local`):

    esphome/esphome run lamp-proxy.yaml --device lamp-proxy.local
    esphome/esphome logs lamp-proxy.yaml --device lamp-proxy.local

## In Home Assistant

Settings > Devices & services: HA discovers `lamp-proxy` (zeroconf; the pod
uses the host network). Configure it with the value of `api_key` from
`secrets.yaml`, without the `api_key:` prefix or quotes (pasting the whole
line fails with `Malformed PSK (length=55)`). The Bluetooth integration
then lists it as a scanner, and each lamp's connection shows which adapter it goes through. On brick2000,
`bluetoothctl devices Connected` lists only the bulbs on its own radio.

lamp2 is in a bedroom: never switch it on to test the proxy. Check the
link by reading (HA's Bluetooth panel, the proxy's logs, `bluetoothctl`).
