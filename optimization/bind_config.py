import logging
import os
import re
import subprocess
import tempfile

from max_sustainable_qps import scp_to, ssh_run

log = logging.getLogger(__name__)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE_OPTIONS_FILE = os.path.join(REPO_ROOT, "ns_software", "bind", "named.conf.options")

INSTALLED_OPTIONS_PATH = "/etc/bind/named.conf.options"
NAMED_CONF_PATH = "/etc/bind/named.conf"
STAGED_PATH = "/tmp/named.conf.options.candidate"

INDENT = "    "


class BindConfigError(Exception):
    pass


def _as_int(name, value):
    try:
        return int(value)
    except (TypeError, ValueError):
        raise BindConfigError(f"{name}: expected an integer, got {value!r}")


def render_udp_receive_buffer(value):
    size = _as_int("udp-receive-buffer", value)
    if size < 0:
        raise BindConfigError(f"udp-receive-buffer: must be >= 0, got {size}")
    return f"udp-receive-buffer {size};"


def render_udp_send_buffer(value):
    size = _as_int("udp-send-buffer", value)
    if size < 0:
        raise BindConfigError(f"udp-send-buffer: must be >= 0, got {size}")
    return f"udp-send-buffer {size};"


def render_max_udp_size(value):
    size = _as_int("max-udp-size", value)
    if not 512 <= size <= 4096:
        raise BindConfigError(
            f"max-udp-size: must be between 512 and 4096, got {size}"
        )
    return f"max-udp-size {size};"


RENDERERS = {
    "udp-receive-buffer": render_udp_receive_buffer,
    "udp-send-buffer": render_udp_send_buffer,
    "max-udp-size": render_max_udp_size,
}

FORBIDDEN = {
    "listen-on", "listen-on-v6", "allow-query", "recursion",
    "rate-limit", "directory",
}


def render_statement(name, value):
    if name in FORBIDDEN:
        raise BindConfigError(f"refusing to set protected statement: {name}")
    renderer = RENDERERS.get(name)
    if renderer is None:
        raise BindConfigError(
            f"no renderer registered for '{name}'. Known statements: "
            f"{', '.join(sorted(RENDERERS))}"
        )
    return renderer(value)


def load_base_options(path=BASE_OPTIONS_FILE):
    with open(path) as f:
        text = f.read()
    if "options" not in text:
        raise BindConfigError(f"{path} does not contain an options block")
    return text


def find_options_block(text):
    match = re.search(r"^[ \t]*options\b[^{]*\{", text, re.MULTILINE)
    if not match:
        raise BindConfigError("no 'options {' block found")

    body_start = match.end()
    depth = 1
    for i in range(body_start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return body_start, i
    raise BindConfigError("unbalanced braces in the options {} block")


def render_options(base_text, overrides):
    statements = {name: render_statement(name, value)
                  for name, value in overrides.items()}

    body_start, body_end = find_options_block(base_text)
    body = base_text[body_start:body_end]

    kept = []
    for line in body.splitlines():
        token = line.strip().split(" ")[0].rstrip(";").strip()
        if token in statements:
            continue
        kept.append(line)

    while kept and not kept[-1].strip():
        kept.pop()

    added = [f"{INDENT}{s}" for s in statements.values()]
    new_body = "\n".join(kept + added) + "\n"
    return base_text[:body_start] + new_body + base_text[body_end:]


def statement_keyword(statement):
    """The leading keyword of a named.conf.options statement."""
    match = re.match(r"\s*([A-Za-z0-9-]+)", statement)
    if not match:
        raise BindConfigError(f"not a named.conf.options statement: {statement!r}")
    return match.group(1)


def check_raw_statement(statement):
    """Return ``statement`` stripped, or raise if it is not one statement
    ending in ';' or sets a protected option. named-checkconf checks the
    rest when the file is installed."""
    text = statement.strip()
    if not text.endswith(";"):
        raise BindConfigError(f"statement must end with ';': {statement!r}")
    if text.count("{") != text.count("}"):
        raise BindConfigError(f"unbalanced braces in statement: {statement!r}")
    keyword = statement_keyword(text)
    if keyword in ("options", "zone", "include", "view"):
        raise BindConfigError(f"{keyword} is not an options-block statement")
    if keyword in FORBIDDEN:
        raise BindConfigError(f"refusing to set protected statement: {keyword}")
    return text


def render_raw_options(base_text, statements):
    """Like render_options, but with free-form statements. A base statement
    with the same keyword is replaced (single-line base statements only)."""
    checked = [check_raw_statement(s) for s in statements]
    keywords = {statement_keyword(s) for s in checked}

    body_start, body_end = find_options_block(base_text)
    body = base_text[body_start:body_end]
    kept = [line for line in body.splitlines()
            if not (line.strip() and line.strip().split()[0].rstrip(";") in keywords)]
    while kept and not kept[-1].strip():
        kept.pop()

    added = [INDENT + s.replace("\n", "\n" + INDENT) for s in checked]
    new_body = "\n".join(kept + added) + "\n"
    return base_text[:body_start] + new_body + base_text[body_end:]


def _sh(server, command, check=True, timeout=60):
    result = ssh_run(server, command, timeout=timeout, check=False)
    if check and result.returncode != 0:
        raise BindConfigError(
            f"command failed on {server} (rc={result.returncode}): {command}\n"
            f"{(result.stderr or '').strip()}"
        )
    return result.stdout


def install_options(server, rendered, options_path=INSTALLED_OPTIONS_PATH,
                    named_conf=NAMED_CONF_PATH, dry_run=False):
    if dry_run:
        print(rendered)
        return

    with tempfile.NamedTemporaryFile("w", suffix=".named.conf.options",
                                     delete=False) as fh:
        fh.write(rendered)
        local_path = fh.name

    try:
        scp_to(server, local_path, STAGED_PATH)
    except subprocess.CalledProcessError as e:
        raise BindConfigError(
            f"failed to copy candidate config to {server}: {e.stderr or e}"
        ) from e
    finally:
        os.unlink(local_path)

    _sh(server, f"sudo cp {STAGED_PATH} {options_path}")
    _sh(server, f"sudo named-checkconf {named_conf}", timeout=120)


def restore_base(server, base_text, options_path=INSTALLED_OPTIONS_PATH,
                 dry_run=False):
    log.info("Restoring base named.conf.options on %s", server)
    install_options(server, base_text, options_path=options_path,
                    dry_run=dry_run)


def read_installed(server, options_path=INSTALLED_OPTIONS_PATH):
    return _sh(server, f"cat {options_path}", check=False)
