"""Checks on k8s_config/, run inside the Home Assistant image the cluster runs
(scripts/test), so its own YAML loader, templates and check_config are the
ones under test.
"""
import asyncio
import glob
import os
import shutil
import subprocess
import sys
import tempfile
import types
import unittest

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
K8S = os.path.join(HERE, "..", "k8s_config")


class _Loader(yaml.SafeLoader):
    """Kubernetes manifests are plain YAML; Home Assistant's own files use
    tags like !include_dir_named, kept here as strings."""


_Loader.add_multi_constructor("!", lambda loader, tag, node: f"{tag} {node.value}")


def kustomization():
    with open(os.path.join(K8S, "kustomization.yaml")) as f:
        return yaml.safe_load(f)


def manifests():
    docs = []
    for path in sorted(glob.glob(os.path.join(K8S, "*.yaml"))):
        with open(path) as f:
            docs += [d for d in yaml.safe_load_all(f) if d and d["kind"] != "Kustomization"]
    return docs


def find(kind, name):
    return next(d for d in manifests() if d["kind"] == kind and d["metadata"]["name"] == name)


def configmap():
    """The generated ConfigMap's data: its keys and the ha/ files behind them."""
    gen = next(g for g in kustomization()["configMapGenerator"] if g["name"] == "home-assistant-config")
    data = {}
    for item in gen["files"]:
        key, path = item.split("=", 1)
        with open(os.path.join(K8S, path)) as f:
            data[key] = f.read()
    return data


def write_config_dir(path):
    """/config as the pod sees it: configuration.yaml, packages/."""
    data = configmap()
    os.makedirs(os.path.join(path, "packages"))
    with open(os.path.join(path, "configuration.yaml"), "w") as f:
        f.write(data["configuration.yaml"])
    for item in find("Deployment", "home-assistant")["spec"]["template"]["spec"]["volumes"]:
        if item["name"] == "packages":
            for key in item["configMap"]["items"]:
                with open(os.path.join(path, "packages", key["path"]), "w") as f:
                    f.write(data[key["key"]])


class _Now:
    """now() for render(): only its timestamp is used."""

    def __init__(self, ts):
        self.ts = ts


def render(template, **variables):
    from jinja2.sandbox import ImmutableSandboxedEnvironment

    env = ImmutableSandboxedEnvironment()
    env.tests["number"] = lambda v: isinstance(v, (int, float)) and not isinstance(v, bool)
    return env.from_string(template).render(**variables)


class Manifests(unittest.TestCase):
    def test_every_manifest_is_in_the_namespace(self):
        for d in manifests():
            if d["kind"] != "Namespace":
                self.assertEqual(d["metadata"]["namespace"], "home-assistant", d["metadata"]["name"])

    def test_kustomization_lists_every_manifest(self):
        # Argo CD prunes: a manifest left out would be deleted from the cluster.
        on_disk = sorted(os.path.basename(p) for p in glob.glob(os.path.join(K8S, "*.yaml")))
        on_disk.remove("kustomization.yaml")
        self.assertEqual(sorted(kustomization()["resources"]), on_disk)

    def test_every_ha_file_is_in_the_configmap(self):
        # ha/ and its bin/ (scripts the pod runs), each under its own name
        ha = os.path.join(K8S, "ha")
        files = sorted(os.path.basename(p) for p in glob.glob(os.path.join(ha, "**", "*"), recursive=True)
                       if os.path.isfile(p))
        self.assertEqual(sorted(configmap()), files)
        gen = kustomization()["configMapGenerator"][0]
        self.assertEqual(gen["namespace"], "home-assistant")
        self.assertNotIn("options", gen)  # the hash suffix is what rolls HA

    def test_the_deployment_mounts_the_generated_configmap(self):
        # Kustomize rewrites these to the hashed name; they must start as the
        # generator's name or the rewrite misses them.
        volumes = find("Deployment", "home-assistant")["spec"]["template"]["spec"]["volumes"]
        names = {v["name"]: v["configMap"]["name"] for v in volumes if "configMap" in v}
        self.assertEqual(names, {"yaml": "home-assistant-config", "packages": "home-assistant-config",
                                 "bin": "home-assistant-config"})

    def test_image_is_pinned(self):
        image = find("Deployment", "home-assistant")["spec"]["template"]["spec"]["containers"][0]["image"]
        tag = image.rsplit(":", 1)[1]
        self.assertRegex(tag, r"^\d{4}\.\d+\.\d+$")

    def test_runs_on_the_storage_node_one_at_a_time(self):
        spec = find("Deployment", "home-assistant")["spec"]
        self.assertEqual(spec["strategy"]["type"], "Recreate")
        self.assertEqual(spec["replicas"], 1)
        pod = spec["template"]["spec"]
        self.assertEqual(pod["nodeSelector"], {"chuck.io/storage-node": "true"})
        self.assertTrue(pod["hostNetwork"])
        self.assertEqual(pod["dnsPolicy"], "ClusterFirstWithHostNet")

    def test_reaches_the_hosts_bluetooth(self):
        pod = find("Deployment", "home-assistant")["spec"]["template"]["spec"]
        container = pod["containers"][0]
        dbus = next(v for v in pod["volumes"] if v["name"] == "dbus")
        self.assertEqual(dbus["hostPath"]["path"], "/run/dbus")
        mount = next(m for m in container["volumeMounts"] if m["name"] == "dbus")
        self.assertEqual(mount["mountPath"], "/run/dbus")
        self.assertTrue(mount["readOnly"])
        caps = container["securityContext"]["capabilities"]["add"]
        self.assertEqual(set(caps), {"NET_ADMIN", "NET_RAW"})

    def test_mounted_keys_exist(self):
        data = configmap()
        for v in find("Deployment", "home-assistant")["spec"]["template"]["spec"]["volumes"]:
            for item in v.get("configMap", {}).get("items", []):
                self.assertIn(item["key"], data)


class LcarsPanel(unittest.TestCase):
    def setUp(self):
        self.pkg = yaml.load(configmap()["lcars_panel.yaml"], Loader=_Loader)

    def automation(self, id_):
        return next(a for a in self.pkg["automation"] if a["id"] == id_)

    def test_guardrails_default_to_64_76(self):
        n = self.pkg["input_number"]
        self.assertEqual(n["thermostat_setpoint_min"]["initial"], 64)
        self.assertEqual(n["thermostat_setpoint_max"]["initial"], 76)

    def test_guardrails_start_paused_while_the_panel_is_read_only(self):
        # The user, 2026-10-15: HA watches the ecobee and never changes it.
        # Turning the guardrails back on is a deliberate change to this test.
        self.assertIs(self.automation("thermostat_guardrails")["initial_state"], False)

    def test_lock_modes(self):
        sel = self.pkg["input_select"]["lcars_panel_lock"]
        self.assertEqual(sel["options"], ["normal", "unlocked", "hard_locked"])
        self.assertNotIn("initial", sel)  # a hard lock survives a restart

    def test_unlock_expires_after_10_minutes(self):
        a = self.automation("lcars_panel_unlock_expires")
        self.assertEqual(a["triggers"][0]["to"], "unlocked")
        self.assertEqual(a["triggers"][0]["for"], {"minutes": 10})
        self.assertEqual(a["actions"][0]["data"], {"option": "normal"})

    def test_bedroom_sensor_watchdog_restarts_at_most_every_2_hours(self):
        a = self.automation("lcars_bedroom_sensor_watchdog")
        self.assertEqual(a["mode"], "single")
        panel, stuck, rate = a["conditions"]
        self.assertEqual(panel["entity_id"], "binary_sensor.lcars_panel_room_reading")
        self.assertEqual(panel["for"], {"minutes": 15})
        self.assertIn("sensor.bedroom_temperature", stuck["value_template"])
        self.assertIn("2 * 3600", rate["value_template"])
        # The time is saved before the restart, or it would restart again.
        self.assertEqual([x["action"] for x in a["actions"]],
                         ["input_datetime.set_datetime", "system_log.write", "homeassistant.restart"])
        self.assertNotIn("initial", self.pkg["input_datetime"]["lcars_room_watchdog_last_restart"])

    def test_calendar_watchdog_restarts_at_most_every_2_hours(self):
        a = self.automation("lcars_calendar_watchdog")
        self.assertEqual(a["mode"], "single")
        google, stuck, rate = a["conditions"]
        self.assertEqual(google["entity_id"], "binary_sensor.google_calendar_api_reachable")
        self.assertEqual(google["for"], {"minutes": 15})
        self.assertEqual(stuck["entity_id"], "binary_sensor.calendars_available")
        self.assertEqual((stuck["state"], stuck["for"]), ("off", {"minutes": 45}))
        self.assertEqual(rate, self.automation("lcars_bedroom_sensor_watchdog")["conditions"][2])
        self.assertEqual([x["action"] for x in a["actions"]],
                         ["input_datetime.set_datetime", "system_log.write", "homeassistant.restart"])
        self.assertEqual(a["actions"][0]["target"]["entity_id"],
                         "input_datetime.lcars_calendar_watchdog_last_restart")
        self.assertNotIn("initial", self.pkg["input_datetime"]["lcars_calendar_watchdog_last_restart"])

    def test_watchdogs_share_the_2_hour_limit(self):
        rate = self.automation("lcars_calendar_watchdog")["conditions"][2]["value_template"]
        now = 1_800_000_000

        def allowed(room, calendar, lamp="unknown"):
            stamps = {"input_datetime.lcars_room_watchdog_last_restart": room,
                      "input_datetime.lcars_calendar_watchdog_last_restart": calendar,
                      "input_datetime.lamp_watchdog_last_restart": lamp}
            out = render(rate, now=lambda: _Now(now), states=lambda e: stamps[e],
                         as_timestamp=lambda v, d=None: (v.ts if isinstance(v, _Now) else
                                                         d if v in ("unknown", None) else float(v)))
            return out.strip() == "True"

        self.assertTrue(allowed("unknown", "unknown"))  # never restarted
        self.assertFalse(allowed(now - 3600, "unknown"))
        self.assertFalse(allowed("unknown", now - 3600))
        self.assertFalse(allowed("unknown", "unknown", now - 3600))  # lamp.yaml's restart counts too
        self.assertFalse(allowed(now - 3 * 3600, now - 600))
        self.assertTrue(allowed(now - 3 * 3600, now - 2.5 * 3600))

    def test_calendars_available_in_home_assistant(self):
        # HA's own template engine: states.calendar is HA's, not plain Jinja.
        import asyncio
        from homeassistant.core import HomeAssistant
        from homeassistant.helpers.template import Template

        state = self.pkg["template"][0]["binary_sensor"][0]["state"]

        async def available(**calendars):
            with tempfile.TemporaryDirectory() as d:
                hass = HomeAssistant(d)
                for name, value in calendars.items():
                    hass.states.async_set(f"calendar.{name}", value)
                hass.states.async_set("sensor.other", "on")
                out = Template(state, hass).async_render()
                await hass.async_stop(force=True)
                return out

        run = asyncio.run
        self.assertIs(run(available()), False)  # never set up
        self.assertIs(run(available(a="unavailable", b="unknown")), False)
        self.assertIs(run(available(a="unavailable", b="off")), True)
        self.assertIs(run(available(a="on")), True)

    def test_google_check_says_on_or_off(self):
        check = self.pkg["command_line"][1]["binary_sensor"]
        self.assertEqual(check["unique_id"], "google_calendar_api_reachable")
        self.assertTrue(check["command"].endswith("&& echo on || echo off"))
        self.assertIn("-m 10", check["command"])

    def test_panel_reading_needs_up(self):
        check = self.pkg["command_line"][0]["binary_sensor"]
        self.assertEqual(check["command"].split()[-1], self.pkg["rest"][0]["resource"])
        for doc, want in (({"up": True}, "on"), ({"up": False}, "off")):
            self.assertEqual(render(check["value_template"], value_json=doc), want)
        self.assertEqual(render(check["value_template"]), "off")  # not JSON

    def clamp(self, which, **v):
        choose = self.automation("thermostat_guardrails")["actions"][0]["choose"]
        branch = choose[0] if which == "temperature" else choose[1]
        if render(branch["conditions"], **v).strip() != "True":
            return None
        data = branch["sequence"][0]["data"]
        return {k: float(render(t, **v)) for k, t in data.items()}

    def test_single_setpoint_is_clamped(self):
        self.assertEqual(self.clamp("temperature", t=85, lo=64, hi=76), {"temperature": 76})
        self.assertEqual(self.clamp("temperature", t=55, lo=64, hi=76), {"temperature": 64})
        self.assertIsNone(self.clamp("temperature", t=70, lo=64, hi=76))
        self.assertIsNone(self.clamp("temperature", t=None, lo=64, hi=76))

    def test_range_setpoints_are_clamped(self):
        self.assertEqual(self.clamp("range", low=60, high=80, lo=64, hi=76),
                         {"target_temp_low": 64, "target_temp_high": 76})
        self.assertEqual(self.clamp("range", low=66, high=79, lo=64, hi=76),
                         {"target_temp_low": 66, "target_temp_high": 76})
        self.assertIsNone(self.clamp("range", low=66, high=74, lo=64, hi=76))


@unittest.skipUnless(shutil.which("hass"), "needs the Home Assistant image (scripts/test)")
class CheckConfig(unittest.TestCase):
    def test_home_assistant_accepts_the_config(self):
        with tempfile.TemporaryDirectory() as d:
            write_config_dir(d)
            r = subprocess.run([sys.executable, "-m", "homeassistant", "--script", "check_config",
                                "-c", d], capture_output=True, text=True, timeout=600)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            # check_config exits 0 on an invalid integration's config too
            # (2026.10): only its report says so.
            self.assertNotIn("Incorrect config", r.stdout)
            self.assertNotIn("Invalid config", r.stdout)


class PrestoRoom(unittest.TestCase):
    """The Presto's /room.json (presto#8), read like the bedroom's."""

    def setUp(self):
        self.pkg = yaml.load(configmap()["presto.yaml"], Loader=_Loader)
        (self.rest,) = self.pkg["rest"]
        self.sensors = self.rest["sensor"]
        self.assertTrue(self.sensors)

    def test_reads_the_presto_on_the_lan_every_minute(self):
        self.assertEqual(self.rest["resource"], "http://presto:8767/room.json")
        self.assertEqual(self.rest["scan_interval"], 60)

    def test_unavailable_while_its_reading_is_stale(self):
        for sensor in self.sensors:
            with self.subTest(sensor=sensor["name"]):
                for doc, want in (({"up": True}, True), ({"up": False}, False), ({}, False)):
                    with self.subTest(doc=doc):
                        self.assertIs(ha_render(sensor["availability"], value_json=doc), want)
                # a failed fetch: no value_json at all, and no template error
                self.assertIs(ha_render(sensor["availability"]), False)

    def test_temperature_is_named_as_presto_feed_shows_it(self):
        # presto-feed strips " temperature": PRESTO, which the Presto leaves out
        # while it has its own reading (presto's uplink.indoor).
        self.assertIn("Presto temperature", [s["name"] for s in self.sensors])

    def test_only_sensors_no_automations_or_services(self):
        # Read-only: nothing in this package can change the thermostat or anything else.
        self.assertEqual(set(self.pkg), {"rest"})


def ha_render(template, home=None, states=None, **variables):
    """A template through Home Assistant's own engine (zone.home, states(), round)."""
    # Imported here, like the calendar test: the module still loads outside the HA image.
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.template import Template

    async def go():
        with tempfile.TemporaryDirectory() as d:
            hass = HomeAssistant(d)
            if home:
                hass.states.async_set("zone.home", "0", {"latitude": home[0], "longitude": home[1]})
            for entity, value in (states or {}).items():
                hass.states.async_set(entity, value)
            try:
                return Template(template, hass).async_render(variables)
            finally:
                await hass.async_stop(force=True)

    return asyncio.run(go())


def observation(temperature, humidity):
    return {"properties": {"temperature": {"value": temperature}, "relativeHumidity": {"value": humidity}}}


class Outside(unittest.TestCase):
    """The National Weather Service's nearest station, for the dashboard's Outside."""

    def setUp(self):
        self.pkg = yaml.load(configmap()["weather.yaml"], Loader=_Loader)
        self.stations, self.obs = self.pkg["rest"]
        self.sensors = {s["name"]: s for s in self.obs["sensor"]}

    def test_station_comes_from_home_assistants_home_not_this_repo(self):
        url = ha_render(self.stations["resource_template"], home=(38.897712345, -77.036512345))
        self.assertEqual(url, "https://api.weather.gov/points/38.8977,-77.0365/stations")
        station = self.stations["sensor"][0]
        doc = {"features": [{"properties": {"stationIdentifier": "KDCA"}}]}
        self.assertEqual(ha_render(station["value_template"], value_json=doc), "KDCA")
        self.assertIs(ha_render(station["availability"], value_json={"features": []}), False)
        self.assertEqual(self.stations["scan_interval"], 3600)  # a failed lookup heals within the hour

    def test_observations_from_the_station(self):
        url = ha_render(self.obs["resource_template"], states={"sensor.nws_station": "KDCA"})
        self.assertEqual(url, "https://api.weather.gov/stations/KDCA/observations?limit=5")

    def test_newest_observation_with_a_value(self):
        doc = {"features": [observation(None, None), observation(13.3, 86.53), observation(12.0, 93.6)]}
        t, rh = self.sensors["Outside temperature"], self.sensors["Outside humidity"]
        self.assertEqual(ha_render(t["value_template"], value_json=doc), 13.3)
        self.assertEqual(ha_render(rh["value_template"], value_json=doc), 87)
        self.assertIs(ha_render(t["availability"], value_json=doc), True)

    def test_unavailable_without_any_value(self):
        station = self.stations["sensor"][0]
        for doc in ({"features": [observation(None, None)]}, {"features": []}, {}, None):
            variables = {} if doc is None else {"value_json": doc}
            sensors = list(self.sensors.values())
            if not (doc and doc.get("features")):  # the station is only about having features
                sensors.append(station)
            for sensor in sensors:
                with self.subTest(doc=doc, sensor=sensor["name"]):
                    self.assertIs(ha_render(sensor["availability"], **variables), False)

    def test_observation_is_fetched_again_once_the_station_is_known(self):
        (auto,) = self.pkg["automation"]
        # The station usually arrives during setup, before automations run: start too.
        self.assertEqual([tr["trigger"] for tr in auto["triggers"]], ["homeassistant", "state"])
        self.assertEqual(auto["triggers"][1]["entity_id"], "sensor.nws_station")
        cond = auto["conditions"][0]["value_template"]
        self.assertIs(ha_render(cond, states={"sensor.nws_station": "KDCA"}), True)
        self.assertIs(ha_render(cond, states={"sensor.nws_station": "unknown"}), False)
        self.assertEqual(auto["actions"], [{"action": "homeassistant.update_entity",
                                            "target": {"entity_id": "sensor.outside_temperature"}}])

    def test_named_so_presto_feed_keeps_it_off_the_rooms(self):
        # presto-feed's OUTDOOR pattern (outdoor|outside|exterior|weather) skips it.
        self.assertIn("Outside temperature", self.sensors)
        self.assertEqual(self.sensors["Outside temperature"]["unit_of_measurement"], "°C")

    def test_names_itself_to_the_nws(self):
        for rest in self.pkg["rest"]:
            self.assertIn("User-Agent", rest["headers"])

    def test_only_refreshes_its_own_sensor(self):
        self.assertEqual(set(self.pkg), {"rest", "automation"})
        actions = [a["action"] for auto in self.pkg["automation"] for a in auto["actions"]]
        self.assertEqual(actions, ["homeassistant.update_entity"])


def bluez_device():
    """ha/bin/bluez_device.py, imported from its file (it isn't a package)."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("bluez_device", os.path.join(K8S, "ha", "bin", "bluez_device.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Variant:
    """dbus_fast's Variant: the value is in .value."""

    def __init__(self, value):
        self.value = value


class LampWatchdog(unittest.TestCase):
    """Restart bluetoothd, then HA, when it holds a phantom link to the lamp (2026-11-11)."""

    def setUp(self):
        self.pkg = yaml.load(configmap()["lamp.yaml"], Loader=_Loader)
        (self.auto,) = self.pkg["automation"]

    def test_bluez_device_by_name(self):
        connected = bluez_device().connected
        objects = {
            "/org/bluez/hci0": {"org.bluez.Adapter1": {"Powered": _Variant(True)}},
            "/org/bluez/hci0/dev_1": {"org.bluez.Device1": {"Name": _Variant("Other"), "Connected": _Variant(True)}},
            "/org/bluez/hci0/dev_2": {"org.bluez.Device1": {"Name": _Variant("Lamp"), "Connected": _Variant(True)}},
        }
        self.assertEqual(connected(objects, "Lamp"), "on")
        objects["/org/bluez/hci0/dev_2"]["org.bluez.Device1"]["Connected"] = _Variant(False)
        self.assertEqual(connected(objects, "Lamp"), "off")
        self.assertEqual(connected(objects, "Nothing"), "missing")
        alias_only = {"/d": {"org.bluez.Device1": {"Alias": _Variant("Lamp"), "Connected": _Variant(True)}}}
        self.assertEqual(connected(alias_only, "Lamp"), "on")

    def test_the_sensor_runs_the_mounted_script(self):
        (sensor,) = self.pkg["command_line"]
        self.assertEqual(sensor["sensor"]["command"], "python3 /config/bin/bluez_device.py Lamp")
        pod = find("Deployment", "home-assistant")["spec"]["template"]["spec"]
        mount = next(m for m in pod["containers"][0]["volumeMounts"] if m["name"] == "bin")
        self.assertEqual(mount["mountPath"], "/config/bin")
        self.assertTrue(mount["readOnly"])

    def lamp_down(self, state, minutes):
        import datetime

        now = datetime.datetime(2026, 11, 12, 12, 0, tzinfo=datetime.timezone.utc)
        lamp = None if state is None else types.SimpleNamespace(
            state=state, last_changed=now - datetime.timedelta(minutes=minutes))
        states = types.SimpleNamespace(light=types.SimpleNamespace(lamp=lamp))
        out = render(self.auto["conditions"][0]["value_template"], states=states, now=lambda: now,
                     timedelta=datetime.timedelta)
        return out.strip() == "True"

    def test_only_after_10_minutes_unavailable(self):
        self.assertTrue(self.lamp_down("unavailable", 11))
        self.assertTrue(self.lamp_down(None, 0))  # never set up
        self.assertFalse(self.lamp_down("unavailable", 5))
        self.assertFalse(self.lamp_down("on", 60))
        self.assertFalse(self.lamp_down("off", 60))

    def test_only_with_a_phantom_link(self):
        # A lamp switched off at the wall isn't connected: no restart for that.
        self.assertIn({"condition": "state", "entity_id": "sensor.lamp_bluetooth_link", "state": "on"},
                      self.auto["conditions"])

    def test_restarts_bluetooth_then_home_assistant(self):
        actions = [a.get("action", "delay") for a in self.auto["actions"]]
        self.assertEqual(actions, ["input_datetime.set_datetime", "system_log.write",
                                   "shell_command.restart_bluetooth", "delay", "homeassistant.restart"])
        command = self.pkg["shell_command"]["restart_bluetooth"]
        self.assertIn("org.freedesktop.systemd1.Manager.RestartUnit string:bluetooth.service", command)
        self.assertEqual(set(self.pkg["shell_command"]), {"restart_bluetooth"})

    def test_every_watchdog_shares_the_2_hour_limit(self):
        stamps = ["input_datetime.lcars_room_watchdog_last_restart",
                  "input_datetime.lcars_calendar_watchdog_last_restart",
                  "input_datetime.lamp_watchdog_last_restart"]
        panel = yaml.load(configmap()["lcars_panel.yaml"], Loader=_Loader)
        autos = [a for a in panel["automation"] if a["id"].endswith("_watchdog")] + [self.auto]
        self.assertEqual(len(autos), 3)
        for auto in autos:
            with self.subTest(auto=auto["id"]):
                limit = next(c["value_template"] for c in auto["conditions"]
                             if "2 * 3600" in c.get("value_template", ""))
                for stamp in stamps:
                    self.assertIn(stamp, limit)


if __name__ == "__main__":
    unittest.main()
