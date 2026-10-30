"""Checks on k8s_config/, run inside the Home Assistant image the cluster runs
(scripts/test), so its own YAML loader, templates and check_config are the
ones under test.
"""
import glob
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
K8S = os.path.join(HERE, "..", "k8s_config")


class _Loader(yaml.SafeLoader):
    """Kubernetes manifests are plain YAML; Home Assistant's own files use
    tags like !include_dir_named, kept here as strings."""


_Loader.add_multi_constructor("!", lambda loader, tag, node: f"{tag} {node.value}")


def manifests():
    docs = []
    for path in sorted(glob.glob(os.path.join(K8S, "*.yaml"))):
        with open(path) as f:
            docs += [d for d in yaml.safe_load_all(f) if d]
    return docs


def find(kind, name):
    return next(d for d in manifests() if d["kind"] == kind and d["metadata"]["name"] == name)


def configmap():
    return find("ConfigMap", "home-assistant-config")["data"]


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

        def allowed(room, calendar):
            stamps = {"input_datetime.lcars_room_watchdog_last_restart": room,
                      "input_datetime.lcars_calendar_watchdog_last_restart": calendar}
            out = render(rate, now=lambda: _Now(now), states=lambda e: stamps[e],
                         as_timestamp=lambda v, d=None: (v.ts if isinstance(v, _Now) else
                                                         d if v in ("unknown", None) else float(v)))
            return out.strip() == "True"

        self.assertTrue(allowed("unknown", "unknown"))  # never restarted
        self.assertFalse(allowed(now - 3600, "unknown"))
        self.assertFalse(allowed("unknown", now - 3600))
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


if __name__ == "__main__":
    unittest.main()
