"""Thin client for the CloudLab Portal API (https://gitlab.flux.utah.edu/emulab/portal-api).

Standard library only. The token is read from CLOUDLAB_API_TOKEN or from
~/.config/cloudlab/token (which must not be readable by other users) and is
never logged.
"""
import json
import os
import ssl
import stat
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

DEFAULT_BASE_URL = "https://boss.emulab.net:43794"
TOKEN_ENVS = ("CLOUDLAB_API_TOKEN", "PORTAL_TOKEN")  # PORTAL_TOKEN is the portal-api client's name
URL_ENVS = ("CLOUDLAB_API_URL", "PORTAL_HTTP")
TOKEN_FILE = os.path.expanduser("~/.config/cloudlab/token")


class CloudLabError(Exception):
    """Any failure talking to the Portal API, with a message fit to show a user."""


def load_token():
    for env in TOKEN_ENVS:
        token = os.environ.get(env, "").strip()
        if token:
            return token
    try:
        mode = os.stat(TOKEN_FILE).st_mode
        if mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise CloudLabError(f"{TOKEN_FILE} is accessible by other users; run: chmod 600 {TOKEN_FILE}")
        with open(TOKEN_FILE) as f:
            token = f.read().strip()
    except FileNotFoundError:
        pass
    if not token:
        raise CloudLabError(f"No API token. Set {TOKEN_ENVS[0]} or write the token to {TOKEN_FILE} (chmod 600). "
                            "See cloudlab_api/README.md.")
    return token


class _SameHostRedirect(urllib.request.HTTPRedirectHandler):
    """Follow 307/308 for any method (urllib only does so for GET/HEAD), but only
    within the same host so the API token is never sent elsewhere."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if code in (307, 308):
            if urllib.parse.urlparse(newurl).hostname != urllib.parse.urlparse(req.full_url).hostname:
                raise CloudLabError(f"{req.get_method()} {req.full_url} was redirected to another host "
                                    f"({newurl}); not following it with the API token")
            return urllib.request.Request(newurl, data=req.data, headers=dict(req.header_items()),
                                          method=req.get_method())
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_opener = urllib.request.build_opener(_SameHostRedirect)


class PortalClient:
    def __init__(self, base_url=None, token=None, timeout=60):
        env_url = next((os.environ[e] for e in URL_ENVS if os.environ.get(e)), None)
        self.base_url = (base_url or env_url or DEFAULT_BASE_URL).rstrip("/")
        self._token = token
        self.timeout = timeout

    @property
    def token(self):
        if self._token is None:
            self._token = load_token()
        return self._token

    def request(self, method, path, params=None, body=None):
        url = self.base_url + path
        if params:
            url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers={
            "X-Api-Token": self.token, "Accept": "application/json",
            **({"Content-Type": "application/json"} if data is not None else {})})
        try:
            raw = self._open_with_tls_retry(req)
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:1000]
            if e.headers.get("Location"):
                detail = f"Location: {e.headers['Location']} {detail}"
            hint = {401: " (token missing, expired or invalid; try `launch.py refresh-token`)",
                    403: " (not permitted for this project/profile)"}.get(e.code, "")
            raise CloudLabError(f"{method} {path} -> HTTP {e.code}{hint}: {detail}") from e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise CloudLabError(f"Could not reach {self.base_url}: {e}") from e
        if not raw.strip():
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return raw

    def _open_with_tls_retry(self, req, attempts=6, delay=2):
        """boss.emulab.net intermittently fails certificate verification ("certificate has
        expired") on a minority of connections. The handshake fails before any request is sent,
        so retrying is safe for every method. Verification stays on; only that error is retried."""
        for attempt in range(1, attempts + 1):
            try:
                with _opener.open(req, timeout=self.timeout) as resp:
                    return resp.read().decode()
            except urllib.error.URLError as e:
                if not isinstance(e.reason, ssl.SSLCertVerificationError) or attempt == attempts:
                    raise
                time.sleep(delay)

    # --- endpoints -------------------------------------------------------
    def version(self):
        return self.request("GET", "/version")

    def list_profiles(self, **filters):
        return self.request("GET", "/profiles", params=filters)

    def list_experiments(self, **filters):
        return self.request("GET", "/experiments", params=filters)

    def get_experiment(self, experiment_id):
        return self.request("GET", f"/experiments/{urllib.parse.quote(str(experiment_id), safe='')}")

    def manifests(self, experiment_id):
        return self.request("GET", f"/experiments/{urllib.parse.quote(str(experiment_id), safe='')}/manifests")

    def create_experiment(self, body):
        return self.request("POST", "/experiments", body=body)

    def extend(self, experiment_id, extend_by, reason):
        return self.request("PUT", f"/experiments/{urllib.parse.quote(str(experiment_id), safe='')}",
                            body={"extend_by": extend_by, "reason": reason})

    def terminate(self, experiment_id):
        return self.request("DELETE", f"/experiments/{urllib.parse.quote(str(experiment_id), safe='')}")

    def refresh_token(self):
        return self.request("PUT", "/tokens/this/refresh")


# --- manifest parsing ----------------------------------------------------

def _strip(tag):
    return tag.rsplit("}", 1)[-1]


def _manifest_strings(obj):
    """Yield every RSpec XML string found anywhere in a manifests response."""
    if isinstance(obj, str):
        if "<rspec" in obj:
            yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _manifest_strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _manifest_strings(v)


def parse_nodes(manifests):
    """Return [{client_id, hostname, hardware_type, ssh_user, ssh_port, ips}] from manifest XML."""
    nodes = []
    for xml in _manifest_strings(manifests):
        try:
            root = ET.fromstring(xml)
        except ET.ParseError:
            continue
        for node in root.iter():
            if _strip(node.tag) != "node":
                continue
            info = {"client_id": node.get("client_id"), "hostname": None, "hardware_type": None,
                    "ssh_user": None, "ssh_users": [], "ssh_port": 22, "ips": []}
            for child in node.iter():
                tag = _strip(child.tag)
                if tag == "host" and child.get("name") and not info["hostname"]:
                    info["hostname"] = child.get("name")
                elif tag == "hardware_type" and child.get("name"):
                    info["hardware_type"] = child.get("name")
                elif tag == "login" and child.get("authentication") == "ssh-keys":
                    info["hostname"] = child.get("hostname") or info["hostname"]
                    # a login is listed for every project member
                    info["ssh_users"].append(child.get("username"))
                    info["ssh_user"] = info["ssh_user"] or child.get("username")
                    info["ssh_port"] = int(child.get("port") or 22)
                elif tag == "ip" and child.get("address"):
                    info["ips"].append(child.get("address"))
            nodes.append(info)
    return nodes


def ssh_command(node, prefer_user=None):
    if not node.get("hostname"):
        return None
    chosen = prefer_user if prefer_user in node.get("ssh_users", []) else node.get("ssh_user")
    user = f"{chosen}@" if chosen else ""
    port = f" -p {node['ssh_port']}" if node.get("ssh_port", 22) != 22 else ""
    return f"ssh{port} {user}{node['hostname']}"
