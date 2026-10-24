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
  volume. A YAML change is a merge (Argo CD applies `k8s_config/` from
  `main`) plus `kubectl -n home-assistant rollout restart deploy/home-assistant`.
- **Through the front door**: Caddy on brick9000, then Traefik
  (`ingress.yaml`). Both are trusted proxies, so HA logs the real client.

## The panel package

`lcars_panel.yaml` in `config.yaml`:

| Entity | What |
| --- | --- |
| `input_number.thermostat_setpoint_min` / `_max` | The guardrails, 64 and 76 °F, reset from git on every restart. |
| `input_select.lcars_panel_lock` | `normal`: the panel asks for its code. `unlocked`: no code, for 10 minutes (a trusted phone). `hard_locked`: no changes from the panel at all. |
| automation `thermostat_guardrails` | Pulls any setpoint outside the guardrails back to the nearest one, whoever set it. **Paused** (`initial_state: false`) while the LCARS panel is read-only, so HA never changes the ecobee; remove that line to turn it back on. |
| `sensor.bedroom_temperature` / `_humidity` | The bedroom's SHT35 on brick1982, polled from the panel's `/room.json` every minute. Read-only: nothing acts on them. |
| automation `lcars_panel_unlock_expires` | `unlocked` returns to `normal` after 10 minutes. |

The thermostat must be `climate.thermostat` (rename its entity after pairing,
below).

## First setup

By hand, once:

1. Argo CD creates everything in `k8s_config/` (brick-k8s-config's
   `argocd/apps/home-assistant.yaml`); wait for the pod to be Ready.
2. Open `http://brick2000:8123` and create the owner account. Pick the
   **US customary** unit system: the guardrails are in °F. It also discovers
   brick2000's Bluetooth adapter: leave it. Nothing here uses Bluetooth,
   and without `NET_ADMIN` it only logs errors, so `scripts/setup` disables
   it if it was added (step 8).
3. On the ecobee: Main menu → Settings → HomeKit, which shows a pairing code.
   In HA, Settings → Devices → the discovered ecobee (HomeKit Device) →
   enter the code.
4. Make a long-lived access token on the owner's profile (Security tab) and
   save it, alone, in `~/.config/home-assistant/setup-token` on the laptop
   (mode 600). Delete it from HA after step 5 if you like; nothing keeps it.

Then `scripts/setup` (safe to run again; each step skips what's done):

5. **Trusted proxies.** Since 2026.10 HA keeps its HTTP config in storage
   (the YAML is migrated once, then ignored) and reverts a new one unless
   it's confirmed within 5 minutes. The script stages Caddy, Traefik's nodes
   and the pod network as trusted proxies, waits for the restart, checks the
   site through Caddy, and only then confirms them.
6. Renames the ecobee's climate entity to `climate.thermostat`.
7. Creates two users, **Panel** and **Monitoring** (not administrators,
   local only, passwords never kept), each with a 10-year token. Panel's
   goes into brick1982's `~/.config/lcars/env` (the panel's changes show
   under that name in the logbook); Monitoring's is sealed into
   brick-k8s-secrets (`home-assistant/`, commit it there) and applied for
   the PodMonitor. Neither is printed. To replace one, delete the user in HA
   and run the script again.
8. Disables the Bluetooth integration's config entry, if
   `default_config` added one for the node's adapter (it logged a traceback
   every few seconds, 2026-10-23). Nothing here uses Bluetooth, so the pod
   gets no `NET_ADMIN` for it.

## Metrics

Prometheus scrapes `/api/prometheus` as the **Monitoring** user
(`podmonitor.yaml`; the token is the `home-assistant-prometheus`
SealedSecret). HA exports temperatures in °C whatever the unit system; the
dashboard converts. Its Internet row comes from the router through HA's UPnP
integration (WAN status and the whole house's download and upload rates);
the router's external IP stays off it. The Grafana dashboard **Home Assistant: the ecobee and the internet**
(`k8s_config/metrics-dashboard.yaml`) is generated: edit
`tools/dashboard.py`, run `python3 tools/dashboard.py
k8s_config/metrics-dashboard.yaml` (`--exprs` lists its queries, for checking
against live Prometheus), and apply.

## Changes

1. Branch, change, `scripts/test`: the tests run inside the pinned Home
   Assistant image (needs Docker): HA's own `check_config` on the YAML, the
   guardrail templates' arithmetic, and `scripts/setup_ha.py` against a
   throwaway HA in the container (onboarded through its API).
2. Merge: Argo CD deploys `k8s_config/` from `main` within about 3 minutes and
   puts back changes made by hand. A YAML change also needs the rollout restart.
   To try the branch first: `scripts/trial-branch home-assistant <branch>` in brick-k8s-config (and `scripts/trial-branch home-assistant main` after the merge).
3. Upgrades: bump the image tag in `deployment.yaml` (an exact version), run
   `scripts/test` against it, merge.
