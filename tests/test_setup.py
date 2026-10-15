"""scripts/setup_ha.py against a throwaway Home Assistant started in the test
container (scripts/test): onboarded through its API, then the users and
entities steps run as they would in the pod. The HTTP steps restart HA, so
they're left to the real setup, which checks the site before promoting.
"""
import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
SETUP = os.path.join(HERE, "..", "scripts", "setup_ha.py")
BASE = "http://127.0.0.1:8123"
CLIENT_ID = BASE + "/"


def free(port):
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) != 0


@unittest.skipUnless(shutil.which("hass") and free(8123), "needs the Home Assistant image (scripts/test)")
class Setup(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp()
        with open(os.path.join(cls.dir, "configuration.yaml"), "w") as f:
            f.write("api:\nwebsocket_api:\nauth:\nconfig:\nonboarding:\nfrontend:\n")
        cls.hass = subprocess.Popen([sys.executable, "-m", "homeassistant", "-c", cls.dir],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.time() + 300
        while time.time() < deadline:
            try:
                urllib.request.urlopen(BASE + "/api/onboarding", timeout=2)
                break
            except OSError:
                time.sleep(2)
        else:
            raise RuntimeError("Home Assistant didn't start")
        cls.admin = asyncio.run(cls.onboard())

    @classmethod
    def tearDownClass(cls):
        cls.hass.terminate()
        cls.hass.wait(60)
        shutil.rmtree(cls.dir, ignore_errors=True)

    @classmethod
    async def onboard(cls):
        import aiohttp
        sys.path.insert(0, os.path.dirname(SETUP))
        from setup_ha import WS

        async with aiohttp.ClientSession() as s:
            async with s.post(BASE + "/api/onboarding/users", json={
                    "client_id": CLIENT_ID, "name": "Owner", "username": "owner",
                    "password": "owner-password", "language": "en"}) as r:
                code = (await r.json())["auth_code"]
            async with s.post(BASE + "/auth/token", data={
                    "grant_type": "authorization_code", "code": code, "client_id": CLIENT_ID}) as r:
                access = (await r.json())["access_token"]
            async with WS(s, access) as ws:
                return await ws.call("auth/long_lived_access_token", client_name="test", lifespan=1)

    def step(self, name):
        return subprocess.run([sys.executable, SETUP, name], input=self.admin + "\n",
                              capture_output=True, text=True, timeout=120)

    async def is_admin(self, token):
        """Whether an administrator-only command (listing users) works."""
        import aiohttp
        from setup_ha import WS

        async with aiohttp.ClientSession() as s, WS(s, token) as ws:
            try:
                await ws.call("config/auth/list")
                return True
            except SystemExit as e:
                self.assertIn("Unauthorized", str(e))
                return False

    def api(self, token, path):
        req = urllib.request.Request(BASE + path, headers={"Authorization": f"Bearer {token}"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status
        except urllib.error.HTTPError as e:
            e.close()
            return e.code

    def test_users_get_working_tokens_once_and_are_not_admins(self):
        r = self.step("users")
        self.assertEqual(r.returncode, 0, r.stderr)
        tokens = json.loads(r.stdout)
        self.assertEqual(set(tokens), {"Panel", "Monitoring"})
        for token in tokens.values():
            self.assertEqual(self.api(token, "/api/"), 200)
            self.assertEqual(self.api(token, "/api/states/sun.sun"), 404)  # a user's read, not a 401
            self.assertFalse(asyncio.run(self.is_admin(token)))
        self.assertTrue(asyncio.run(self.is_admin(self.admin)))
        again = self.step("users")
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertEqual(json.loads(again.stdout), {})
        self.assertIn("exists", again.stderr)

    def test_entities_wants_a_paired_thermostat(self):
        r = self.step("entities")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("pair the ecobee first", r.stderr)

    def test_a_bad_token_is_refused(self):
        r = subprocess.run([sys.executable, SETUP, "users"], input="not-a-token\n",
                           capture_output=True, text=True, timeout=60)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("auth failed", r.stderr)


if __name__ == "__main__":
    unittest.main()
