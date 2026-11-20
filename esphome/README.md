# ESPHome devices

Two devices: the lamp proxy and the CYD panel (below). Both build with
`esphome` here, ESPHome pinned in Docker, and share `secrets.yaml`.

## The lamp proxy

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
- `secrets.yaml` (gitignored, mode 600): Wi-Fi, and each device's API
  encryption key and OTA password, sealed in brick-k8s-secrets as
  `home-assistant/lamp-proxy-esphome-sealedsecret.yaml` (the name predates
  the panel);
  `secrets.example.yaml` lists the keys. Generate the API
  key with `openssl rand -base64 32`.

### Flashing

The first flash is over USB-C from the laptop: the Atom shows up as an
FT232 (`0403:6001`, `/dev/serial/by-id/usb-Hades2001_M5stack_*`) on
`/dev/ttyUSB0`. A charge-only cable powers it but shows no serial
port. ESPHome's 460800 baud upload fails on this board; it retries at
115200 by itself (about 75 s).

    esphome/esphome run lamp-proxy.yaml --device /dev/ttyUSB0

After that, over Wi-Fi (`lamp-proxy.local`):

    esphome/esphome run lamp-proxy.yaml --device lamp-proxy.local
    esphome/esphome logs lamp-proxy.yaml --device lamp-proxy.local

### In Home Assistant

Settings > Devices & services: HA discovers `lamp-proxy` (zeroconf; the pod
uses the host network). Configure it with the value of `api_key` from
`secrets.yaml`, without the `api_key:` prefix or quotes (pasting the whole
line fails with `Malformed PSK (length=55)`). The Bluetooth integration
then lists it as a scanner, and each lamp's connection shows which adapter it goes through. On brick2000,
`bluetoothctl devices Connected` lists only the bulbs on its own radio.

lamp2 is in a bedroom: never switch it on to test the proxy. Check the
link by reading (HA's Bluetooth panel, the proxy's logs, `bluetoothctl`).

## The CYD panel

An ESP32-2432S028R "Cheap Yellow Display" (2.8" 320x240 ILI9341, XPT2046
resistive touch, no PSRAM) as a touch panel for Home Assistant,
`cyd-panel.yaml`. A green terminal look: VT323 and Silkscreen pixel fonts
at 1 bit per pixel, square corners. Three pages, from the bar at the
bottom:

- **STATUS** (at boot): the clock and each calendar's next event, soonest
  first.
- **CLIMATE**: inside (the ecobee), bedroom and outside temperature and
  humidity, the weather, and `climate.thermostat`'s heat and cool
  setpoints and mode. Changes stay inside the guardrails
  (`input_number.thermostat_setpoint_min` / `_max`), keep heat and cool
  5 °F apart in heat_cool, go out 1.5 s after the last tap, and are refused
  while `input_select.lcars_panel_lock` is `hard_locked` or Home Assistant
  isn't connected. No PIN, unlike the LCARS panel.
- **LIGHTS**: on/off and brightness for `light.lamp`, `light.lamp2`
  (bedroom) and `light.living_room_lamp3`.

The screen dims to 15% after 2 minutes untouched; the first tap after that
only wakes it. The backlight is also `light.cyd_panel_backlight` in HA.

### Flashing

The first flash is over USB (a CH340 on `/dev/ttyUSB0`); after that, over
Wi-Fi:

    esphome/esphome run cyd-panel.yaml --device /dev/ttyUSB0
    esphome/esphome upload cyd-panel.yaml --device cyd-panel.local
    esphome/esphome logs cyd-panel.yaml --device cyd-panel.local

Opening the USB serial port resets the board. Within 60 s of an OTA update
that counts as a failed boot, and ESPHome rolls back to the previous
firmware: read logs over Wi-Fi after an update, not USB.

### Touch

LVGL rotates the screen (`lvgl: rotation: 90`) and rotates touches with it,
so the touchscreen is calibrated in the panel's native portrait frame: no
`swap_xy`, `mirror_x` because the panel mirrors x. The calibration came
from four corner taps (2026-11-20). The touch IRQ (GPIO36) never fired, so
touch is polled every 50 ms.

### In Home Assistant

Added through the ESPHome integration with `cyd_api_key` from
`secrets.yaml`, then its options: **Allow the device to perform Home
Assistant actions** on. Without that, taps change nothing.
