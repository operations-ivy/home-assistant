# home-assistant

Home Assistant on the brick k3s cluster, at
`https://homeassistant.brick.nozdormu.cloud`. It reaches the ecobee
thermostat locally over HomeKit (ecobee issues no new cloud API keys since
2024-03-28) and is the backend of the LCARS panel on brick1982
(`operations-ivy/rpi_lcars`).

## How it runs

`k8s_config/`, namespace `home-assistant`:

- **One pod on brick2000** (`chuck.io/storage-node`), `Recreate`, with a 5Gi
  local-path volume for `/config`: users, integrations (the HomeKit pairing),
  the unit system, dashboards and the recorder's history.
- **Host networking**: HomeKit accessories are found over mDNS on the LAN, and
  the controller connects to them directly. The UI is also on
  `brick2000:8123`.
- **YAML from git** (`config.yaml`, a ConfigMap): `configuration.yaml` is
  mounted over the volume's, and packages go read-only into
  `/config/packages`. Everything else is set in the UI and lives on the
  volume. A YAML change is `kubectl apply` plus
  `kubectl -n home-assistant rollout restart deploy/home-assistant`.
- **Through the front door**: Caddy on brick9000, then Traefik
  (`ingress.yaml`). Both are trusted proxies, so HA logs the real client.

## The panel package

`lcars_panel.yaml` in `config.yaml`:

| Entity | What |
| --- | --- |
| `input_number.thermostat_setpoint_min` / `_max` | The guardrails, 64 and 76 °F, reset from git on every restart. |
| `input_select.lcars_panel_lock` | `normal`: the panel asks for its code. `unlocked`: no code, for 10 minutes (a trusted phone). `hard_locked`: no changes from the panel at all. |
| automation `thermostat_guardrails` | Pulls any setpoint outside the guardrails back to the nearest one, whoever set it. |
| automation `lcars_panel_unlock_expires` | `unlocked` returns to `normal` after 10 minutes. |

The thermostat must be `climate.thermostat` (rename its entity after pairing,
below).

## First setup (by hand, once)

1. `kubectl apply -f k8s_config/` and wait for the pod to be Ready.
2. Open the UI and create the owner account. Pick the **US customary**
   unit system: the guardrails are in °F.
   It also discovers brick2000's Bluetooth adapter: ignore it. Nothing here
   uses Bluetooth, and without `NET_ADMIN` it only logs errors.
3. On the ecobee: Main menu → Settings → HomeKit, which shows a pairing code.
   In HA, Settings → Devices → the discovered ecobee (HomeKit Device) →
   enter the code.
4. Rename its climate entity to `climate.thermostat`.
5. Create a user **Panel** (not an administrator), sign in as it, and make a
   long-lived access token under its profile. The panel uses it, so its
   changes show under that name in the logbook. The token goes in
   brick1982's `~/.config/lcars/env`, never in git.

## Changes

1. Branch, change, `scripts/test`: the tests run inside the pinned Home
   Assistant image (needs Docker), including HA's own `check_config` on the
   YAML, and check the guardrail templates' arithmetic.
2. `kubectl diff -f k8s_config/`, then `kubectl apply -f k8s_config/`, and a
   rollout restart for YAML changes.
3. Upgrades: bump the image tag in `deployment.yaml` (an exact version), run
   `scripts/test` against it, apply.
