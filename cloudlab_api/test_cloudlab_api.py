"""Offline tests: python3 -m unittest discover -s cloudlab_api"""
import json
import os
import subprocess
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import client
import ops

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG = {"project": "proj", "profile_name": "prof", "duration": 4, "max_duration": 8,
          "refspec": "refs/heads/fully_agentic",
          "parameters": {"num_testers": 1, "name_server_software": "bind",
                         "name_server_hardware": "any", "tester_hardware": "any",
                         "allow_interswitch_links": False}}

MANIFEST = """<rspec xmlns="http://www.geni.net/resources/rspec/3">
 <node client_id="NS_Local">
  <hardware_type name="c6525-25g"/>
  <host name="NS_Local.exp.proj.cloudlab.us"/>
  <services><login authentication="ssh-keys" hostname="c1.cloudlab.us" port="22" username="bob"/>
  <login authentication="ssh-keys" hostname="c1.cloudlab.us" port="22" username="alice"/></services>
 </node>
 <node client_id="test_host_0">
  <services><login authentication="ssh-keys" hostname="c2.cloudlab.us" port="2222" username="alice"/></services>
 </node>
</rspec>"""


class LaunchBody(unittest.TestCase):
    def test_defaults_and_overrides(self):
        body = ops.build_launch_body(CONFIG, "e1", num_testers=3, name_server_software="nsd")
        self.assertEqual(body["bindings"]["num_testers"], "3")
        self.assertEqual(body["bindings"]["name_server_software"], "nsd")
        self.assertEqual(body["profile_project"], "proj")
        self.assertEqual(body["refspec"], "refs/heads/fully_agentic")

    def test_none_overrides_ignored(self):
        body = ops.build_launch_body(CONFIG, "e1", num_testers=None, duration=None)
        self.assertEqual(body["bindings"]["num_testers"], "1")
        self.assertEqual(body["bindings"]["allow_interswitch_links"], "False")
        self.assertEqual(body["duration"], 4)

    def test_rejects(self):
        for kw in ({"duration": 9}, {"num_testers": 0}, {"name_server_software": "x"}, {"project": ""}):
            cfg = {**CONFIG, "project": ""} if "project" in kw else CONFIG
            with self.assertRaises(client.CloudLabError):
                ops.build_launch_body(cfg, "e1", **kw)


class Manifest(unittest.TestCase):
    def test_nodes_found_in_nested_response(self):
        nodes = client.parse_nodes({"manifests": [{"xml": MANIFEST}]})
        self.assertEqual([n["client_id"] for n in nodes], ["NS_Local", "test_host_0"])
        self.assertEqual(nodes[0]["hardware_type"], "c6525-25g")
        self.assertEqual(client.ssh_command(nodes[0], "alice"), "ssh alice@c1.cloudlab.us")
        self.assertEqual(client.ssh_command(nodes[0], "nobody"), "ssh bob@c1.cloudlab.us")
        self.assertEqual(client.ssh_command(nodes[1]), "ssh -p 2222 alice@c2.cloudlab.us")


class Http(unittest.TestCase):
    def _resp(self, payload):
        r = mock.MagicMock()
        r.__enter__.return_value.read.return_value = json.dumps(payload).encode()
        return r

    def test_request_shape(self):
        with mock.patch.object(client._opener, "open", return_value=self._resp({"ok": 1})) as op:
            out = client.PortalClient(base_url="http://x:1/", token="TOK").create_experiment({"name": "e"})
        req = op.call_args[0][0]
        self.assertEqual(out, {"ok": 1})
        self.assertEqual((req.get_method(), req.full_url), ("POST", "http://x:1/experiments"))
        self.assertEqual(req.get_header("X-api-token"), "TOK")
        self.assertEqual(json.loads(req.data), {"name": "e"})

    def test_401_message(self):
        import io
        import urllib.error
        err = urllib.error.HTTPError("u", 401, "no", {}, io.BytesIO(b"bad"))
        with mock.patch.object(client._opener, "open", side_effect=err):
            with self.assertRaisesRegex(client.CloudLabError, "HTTP 401.*refresh-token"):
                client.PortalClient(token="T").list_experiments()

    def test_missing_token(self):
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(client, "TOKEN_FILE", "/nonexistent"):
            with self.assertRaisesRegex(client.CloudLabError, "No API token"):
                client.load_token()


class Redirect(unittest.TestCase):
    def _serve(self, location_for):
        import http.server
        import threading
        seen = []

        class H(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                seen.append((self.path, self.headers.get("X-Api-Token"), body))
                if self.path == "/experiments":
                    self.send_response(307)
                    self.send_header("Location", location_for(self.server.server_port))
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                else:
                    payload = b'{"id": "abc"}'
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)

            def log_message(self, *a):
                pass

        srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.shutdown)
        return srv, seen

    def test_same_host_307_keeps_post_body_and_token(self):
        srv, seen = self._serve(lambda port: f"http://127.0.0.1:{port}/v2/experiments")
        out = client.PortalClient(base_url=f"http://127.0.0.1:{srv.server_port}", token="TOK").create_experiment({"a": 1})
        self.assertEqual(out, {"id": "abc"})
        self.assertEqual(seen[1], ("/v2/experiments", "TOK", b'{"a": 1}'))

    def test_cross_host_redirect_not_followed(self):
        srv, seen = self._serve(lambda port: "http://evil.example/experiments")
        with self.assertRaisesRegex(client.CloudLabError, "another host"):
            client.PortalClient(base_url=f"http://127.0.0.1:{srv.server_port}", token="TOK").create_experiment({})
        self.assertEqual(len(seen), 1)


class Wait(unittest.TestCase):
    def test_polls_until_ready(self):
        c = mock.Mock()
        c.get_experiment.side_effect = [{"status": "provisioning"}, {"status": "ready"}]
        c.manifests.return_value = {"m": MANIFEST}
        info = ops.wait_ready(c, "id", interval=0, sleep=lambda s: None)
        self.assertEqual(len(info["nodes"]), 2)

    def test_failure_raises(self):
        c = mock.Mock()
        c.get_experiment.return_value = {"status": "failed"}
        with self.assertRaises(client.CloudLabError):
            ops.wait_ready(c, "id", interval=0, sleep=lambda s: None)


class McpProtocol(unittest.TestCase):
    def test_initialize_list_and_dry_run(self):
        lines = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                 {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                 {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                  "params": {"name": "cloudlab_launch", "arguments": {"name": "e1", "project": "p"}}}]
        out = subprocess.run([sys.executable, os.path.join(HERE, "mcp_server.py")], cwd=HERE,
                             input="\n".join(map(json.dumps, lines)) + "\n",
                             capture_output=True, text=True, timeout=30, env={**os.environ, "CLOUDLAB_API_TOKEN": "x"})
        replies = {r["id"]: r for r in map(json.loads, out.stdout.splitlines())}
        self.assertEqual(replies[1]["result"]["serverInfo"]["name"], "cloudlab")
        self.assertIn("cloudlab_terminate", [t["name"] for t in replies[2]["result"]["tools"]])
        text = json.loads(replies[3]["result"]["content"][0]["text"])
        self.assertTrue(text["dry_run"])
        self.assertFalse(replies[3]["result"]["isError"])


class SetupSshKeys(unittest.TestCase):
    PUB = "ssh-ed25519 AAAAC3Nza test_host_0@h0"

    def _client(self):
        c = mock.Mock()
        c.get_experiment.return_value = {"name": "e", "status": "ready", "creator": "alice"}
        c.manifests.return_value = {"m": MANIFEST.replace('client_id="NS_Local"', 'client_id="NS_Local"')
                                    .replace("</rspec>", "")
                                    + '<node client_id="test_host_1"><host name="h1"/></node></rspec>'}
        return c

    def test_flow_and_verification(self):
        import setup_ssh_keys as sk
        calls = []

        def runner(host, user, script, identity=None):
            calls.append((host, script))
            if "ssh-keygen -q" in script:
                return 0, self.PUB, "created"
            if "authorized_keys" in script and "grep -qxF" in script:
                self.assertIn(self.PUB, script)
                return 0, "added", ""
            return 0, "\n".join(f"{ip} OK x" for ip in ("10.10.1.1", "10.10.1.2")), ""

        c = self._client()
        # give nodes internal ips as parse_nodes would from the manifest
        with mock.patch.object(sk.ops, "describe", return_value={
                "experiment": {"name": "e", "creator": "alice"}, "status": "ready",
                "nodes": [{"client_id": "NS_Local", "hostname": "n", "ips": ["10.10.1.1"]},
                          {"client_id": "test_host_0", "hostname": "t0", "ips": ["10.10.1.2"]},
                          {"client_id": "test_host_1", "hostname": "t1", "ips": ["10.10.1.3"]}]}):
            out = []
            ok = sk.setup(c, "id", runner=runner, out=out.append)
        self.assertTrue(ok)
        self.assertEqual(calls[0][0], "t0")                       # key made on test_host_0 first
        self.assertEqual([h for h, _ in calls[1:4]], ["n", "t0", "t1"])  # then authorized on all nodes
        self.assertIn("10.10.1.1", calls[-1][1])                    # verification targets exclude test_host_0
        self.assertNotIn("10.10.1.2 ", calls[-1][1].split("ssh-keyscan")[-1])

    def test_verification_failure_reported(self):
        import setup_ssh_keys as sk
        runner = lambda h, u, s, i=None: ((0, self.PUB, "") if "ssh-keygen -q" in s else
                                          (0, "added", "") if "grep -qxF" in s else (0, "10.10.1.1 FAIL", ""))
        with mock.patch.object(sk.ops, "describe", return_value={
                "experiment": {"name": "e", "creator": "a"}, "status": "ready",
                "nodes": [{"client_id": "NS_Local", "hostname": "n", "ips": ["10.10.1.1"]},
                          {"client_id": "test_host_0", "hostname": "t0", "ips": ["10.10.1.2"]}]}):
            self.assertFalse(sk.setup(mock.Mock(), "id", runner=runner, out=lambda *_: None))

    def test_requires_source_node(self):
        import setup_ssh_keys as sk
        with mock.patch.object(sk.ops, "describe", return_value={
                "experiment": {"name": "e"}, "status": "ready", "nodes": [{"client_id": "NS_Local", "hostname": "n", "ips": []}]}):
            with self.assertRaisesRegex(client.CloudLabError, "no node named test_host_0"):
                sk.setup(mock.Mock(), "id", runner=None)


if __name__ == "__main__":
    unittest.main()
