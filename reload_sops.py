"""Reload SOP markdown files from sops/ into the knowledge base.

Replaces any existing document with the same title, so it's safe to re-run
after editing an SOP. Run on the server:  ./.venv/bin/python reload_sops.py
"""

import pathlib

from app import db, ingest

# filename in sops/  ->  document title shown in the chatbot
TITLES = {
    "wifi-and-vpn.md": "WiFi and VPN Connectivity",
    "password-and-account-lockout.md": "Password Reset and Account Lockout",
    "general-it-troubleshooting.md": "General IT Troubleshooting - Common Fixes",
    "set-nitro-default-pdf.md": "Set Nitro PDF as Default PDF Viewer",
    "openvpn2-client-setup.md": "OpenVPN2 Client Setup Guide",
}

sop_dir = pathlib.Path(__file__).resolve().parent / "sops"

for fname, title in TITLES.items():
    f = sop_dir / fname
    if not f.exists():
        print(f"skip (missing): {fname}")
        continue
    content = f.read_text(encoding="utf-8")
    for d in db.list_documents():
        if d["title"] == title:
            db.delete_document(d["id"])
    chunks = ingest.chunk_text(content)
    db.add_document(title, None, "text", "jkeesler@capitalvacations.com", chunks, content=content)
    print(f"loaded '{title}': {len(chunks)} chunks")

print("--- knowledge base now ---")
for d in db.list_documents():
    print(" ", d["id"], d["title"])
