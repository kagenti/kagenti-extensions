#!/usr/bin/env python3
"""fleet-read.py — read and VALIDATE fleet.yaml, emit deploy rows.

Humans edit fleet.yaml only; the pipe-row output here is a PRIVATE interface
consumed by deploy-fleet.sh (12 fields, one app per line):

  name|role|image|entrypoint|self_id|port|svcport|env|overlay|ports_exclude|pvc_name|pvc_mount

Validation is part of the R-universal-adoption posture: the catalog itself is
schema-checked, so a typo'd key or malformed stanza refuses to deploy instead
of silently mis-adapting an app (unknown keys are errors, not ignored).

Usage:
  fleet-read.py <fleet.yaml>            emit deploy rows
  fleet-read.py --names <fleet.yaml>    emit app names, one per line
"""
import sys


def die(msg: str) -> None:
    print(f"fleet.yaml: {msg}", file=sys.stderr)
    sys.exit(2)


try:
    import yaml
except ImportError:  # pragma: no cover
    die("reader needs PyYAML (python3 -m pip install pyyaml)")

ROLES = {"agent", "tool", "raw"}
IMAGE_PREFIXES = ("ghcr:", "local:", "kit:", "pull:")
APP_KEYS = {"role", "image", "entrypoint", "self_id", "port", "svcport", "env",
            "attach", "overlay"}
ATTACH_KEYS = {"ports_exclude", "pvc"}
PVC_KEYS = {"name", "mount"}


def text(name: str, key: str, v: object) -> str:
    """Scalar -> str; refuse the characters the row format cannot carry."""
    if isinstance(v, (dict, list)):
        die(f"{name}.{key}: expected a scalar, got {type(v).__name__}")
    s = "" if v is None else str(v)
    if "|" in s or "\n" in s:
        die(f"{name}.{key}: value must not contain '|' or newlines")
    return s


def read_app(name: str, app: dict) -> str:
    if not isinstance(app, dict):
        die(f"{name}: expected a mapping of keys, got {type(app).__name__}")
    unknown = set(app) - APP_KEYS
    if unknown:
        die(f"{name}: unknown key(s) {sorted(unknown)} — allowed: {sorted(APP_KEYS)}")

    role = text(name, "role", app.get("role"))
    if role not in ROLES:
        die(f"{name}.role: '{role}' is not one of {sorted(ROLES)}")

    image = text(name, "image", app.get("image"))
    if not image.startswith(IMAGE_PREFIXES):
        die(f"{name}.image: '{image}' must start with one of {list(IMAGE_PREFIXES)}")

    port = text(name, "port", app.get("port"))
    if not port.isdigit():
        die(f"{name}.port: required, numeric (got '{port}')")
    svcport = text(name, "svcport", app.get("svcport",
                   8080 if role == "agent" else port))
    if not svcport.isdigit():
        die(f"{name}.svcport: numeric (got '{svcport}')")

    if role == "raw":
        for k in ("entrypoint", "self_id", "env", "attach", "overlay"):
            if k in app:
                die(f"{name}: raw stanzas take only image/port/svcport (got '{k}')")
        entrypoint, self_id = "-", "-"
    else:
        entrypoint = text(name, "entrypoint", app.get("entrypoint"))
        if not entrypoint:
            die(f"{name}.entrypoint: required for role '{role}'")
        self_id = text(name, "self_id", app.get("self_id", name))

    env = app.get("env", {}) or {}
    if not isinstance(env, dict):
        die(f"{name}.env: expected a mapping")
    pairs = []
    for k, v in env.items():
        val = text(name, f"env.{k}", v)
        if " " in val:
            die(f"{name}.env.{k}: values must not contain spaces "
                "(attach-lineage.sh's ENV_VARS is space-separated)")
        pairs.append(f"{k}={val}")
    env_str = " ".join(pairs)

    attach = app.get("attach", {}) or {}
    if not isinstance(attach, dict):
        die(f"{name}.attach: expected a mapping")
    unknown = set(attach) - ATTACH_KEYS
    if unknown:
        die(f"{name}.attach: unknown key(s) {sorted(unknown)} — allowed: {sorted(ATTACH_KEYS)}")
    ports_exclude = text(name, "attach.ports_exclude", attach.get("ports_exclude", ""))
    pvc = attach.get("pvc", {}) or {}
    if not isinstance(pvc, dict) or set(pvc) - PVC_KEYS or (pvc and set(pvc) != PVC_KEYS):
        die(f"{name}.attach.pvc: expected exactly {sorted(PVC_KEYS)}")
    pvc_name = text(name, "attach.pvc.name", pvc.get("name", ""))
    pvc_mount = text(name, "attach.pvc.mount", pvc.get("mount", ""))

    overlay = text(name, "overlay", app.get("overlay", "-")) or "-"

    return "|".join([name, role, image, entrypoint, self_id, port, svcport,
                     env_str, overlay, ports_exclude, pvc_name, pvc_mount])


def main() -> None:
    args = sys.argv[1:]
    names_only = "--names" in args
    args = [a for a in args if a != "--names"]
    if len(args) != 1:
        die("usage: fleet-read.py [--names] <fleet.yaml>")
    try:
        with open(args[0]) as f:
            doc = yaml.safe_load(f)
    except OSError as e:
        die(str(e))
    except yaml.YAMLError as e:
        die(f"not valid YAML: {e}")
    if not isinstance(doc, dict) or set(doc) != {"apps"} or not isinstance(doc["apps"], dict):
        die("top level must be exactly one mapping: apps:")
    for name, app in doc["apps"].items():
        name = str(name)
        if names_only:
            print(name)
        else:
            print(read_app(name, app))


if __name__ == "__main__":
    main()
