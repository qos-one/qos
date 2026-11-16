#!/usr/bin/env python3
"""Scan tracked files for personal data before it reaches a public repository.

Generic rules (no secrets needed, anyone can run them):
  - Thai text
  - personal home paths (/Users/<name>, /home/<name>)
  - IBKR account numbers (the D-U-digits form)
  - public IPv4 addresses
  - e-mail addresses outside .md and NOTICE files (those may carry the public contact address)

Optional private rules: set QOS_PERSONAL_PATTERN to a regular expression of personal values.
CI sets it only on `main`, from a protected environment. Without it, that part is skipped.
Set QOS_PUBLIC_CONTACT to an address that is allowed in .md and NOTICE files for the private rules.

Matched values are never printed, only file and line.

    python3 scripts/privacy_check.py
"""
from __future__ import annotations

import ipaddress
import os
import re
import subprocess
import sys

SKIP_EXT = (
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".icns", ".bmp", ".tiff", ".pdf",
    ".woff", ".woff2", ".ttf", ".otf", ".eot",
    ".zip", ".gz", ".tgz", ".bz2", ".xz", ".7z", ".tar",
    ".joblib", ".pkl", ".npy", ".npz", ".parquet", ".feather", ".wasm", ".rgba",
)

THAI = re.compile("[%s-%s]" % (chr(0x0E00), chr(0x0E7F)))
HOME = re.compile(r"(?<![\w.\-])/(?:Users|home)/([A-Za-z0-9._\-]+)")
HOME_OK = {"qos", "runner", "ubuntu", "user", "you", "trader", "name", "username", "example"}
ACCOUNT = re.compile(r"\bD[UF][A-Z]?\d{5,}\b")
IPV4 = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")
EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@([A-Za-z0-9.\-]+\.[A-Za-z]{2,})")
EMAIL_OK_DOMAINS = {
    "example.com", "example.org", "example.net", "evil.com", "evil.example",
    "anthropic.com", "cursor.com", "users.noreply.github.com", "qos.one", "qos.bot",
}


def tracked_files() -> list[str]:
    out = subprocess.run(["git", "ls-files", "-z"], capture_output=True, check=True).stdout
    return [p for p in out.decode("utf-8", "surrogateescape").split("\0") if p]


def is_doc(path: str) -> bool:
    return path.endswith(".md") or os.path.basename(path) == "NOTICE"


def public_ip(text: str) -> bool:
    try:
        ip = ipaddress.ip_address(text)
    except ValueError:
        return False
    return ip.is_global


def generic_problems(path: str, line: str) -> list[str]:
    found = []
    if THAI.search(line):
        found.append("Thai text")
    for m in HOME.finditer(line):
        name = m.group(1)
        if name.lower() not in HOME_OK and not name.startswith(("$", "<", "{")):
            found.append("personal home path")
            break
    if ACCOUNT.search(line):
        found.append("account number")
    if any(public_ip(m.group(0)) for m in IPV4.finditer(line)):
        found.append("public IP address")
    if not is_doc(path):
        for m in EMAIL.finditer(line):
            domain = m.group(1).lower()
            if domain not in EMAIL_OK_DOMAINS and not domain.endswith(".example"):
                found.append("e-mail address")
                break
    return found


def main() -> int:
    pattern = os.environ.get("QOS_PERSONAL_PATTERN", "")
    personal = re.compile(pattern) if pattern else None
    contact = os.environ.get("QOS_PUBLIC_CONTACT", "")
    files = tracked_files()
    hits = []
    for path in files:
        if path.lower().endswith(SKIP_EXT):
            continue
        try:
            with open(path, "rb") as f:
                raw = f.read()
        except OSError:
            continue
        if b"\0" in raw[:8192]:
            continue
        text = raw.decode("utf-8", "replace")
        for n, line in enumerate(text.splitlines(), 1):
            for problem in generic_problems(path, line):
                hits.append(f"{path}:{n}: {problem}")
            if personal:
                checked = line.replace(contact, "") if (contact and is_doc(path)) else line
                if personal.search(checked):
                    hits.append(f"{path}:{n}: personal value")
    for h in hits:
        print(h)
    if not personal:
        print("Note: QOS_PERSONAL_PATTERN is not set, so only the generic rules ran.")
    if hits:
        print(f"{len(hits)} problem(s) found. The matched values are not printed on purpose.")
        return 1
    print(f"OK, {len(files)} files scanned.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
