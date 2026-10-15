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
