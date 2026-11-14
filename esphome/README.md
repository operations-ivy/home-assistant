# The lamp proxy

An M5Stack Atom Lite (ESP32-PICO-D4) running ESPHome as a Bluetooth proxy
beside the lamp, powered from one of brick9000's USB ports
(home-assistant#20). Home Assistant reaches `light.lamp` (Hue BLE) through
it over Wi-Fi instead of through brick2000's onboard radio, which shares
the Pi's chip and antenna with its Wi-Fi. brick2000's adapter stays as a
fallback; HA picks the closest connectable scanner by itself.

- `lamp-proxy.yaml`: the config. Bluetooth proxy (active, 3 connection
  slots), the IR LED on GPIO12 (no commands yet), the button on GPIO39,
  and the RGB LED as a status light: dark while all is well, red while
  `light.lamp` is unavailable, dim amber while HA isn't connected.
- `esphome`: ESPHome pinned (2026.9.1) in Docker; usage in its header.
- `secrets.yaml` (gitignored, mode 600): Wi-Fi, the API encryption key and
  the OTA password; `secrets.example.yaml` lists the keys. Generate the API
  key with `openssl rand -base64 32`.

## Flashing

The first flash is over USB-C from the laptop: the Atom shows up as an
FT232 (`0403:6001`, `/dev/serial/by-id/usb-Hades2001_M5stack_*`) on
`/dev/ttyUSB0`. A charge-only cable lights the LED but shows no serial
port. ESPHome's 460800 baud upload fails on this board; it retries at
115200 by itself (about 75 s).

    esphome/esphome run lamp-proxy.yaml --device /dev/ttyUSB0

After that, over Wi-Fi (`lamp-proxy.local`):

    esphome/esphome run lamp-proxy.yaml --device lamp-proxy.local
    esphome/esphome logs lamp-proxy.yaml --device lamp-proxy.local

## In Home Assistant

Settings > Devices & services: HA discovers `lamp-proxy` (zeroconf; the pod
uses the host network). Configure it with the `api_key` from
`secrets.yaml`. The Bluetooth integration then lists it as a scanner, and
the lamp's connection shows which adapter it goes through.
