"""Create (or verify) a Telethon session interactively — phone+code+2FA or QR scan.

The user brings their own api_id/api_hash from https://my.telegram.org (API
development tools → Create application); tg-qa only provides the login machinery.
An EXISTING session from any Telethon tool works too — drop the .session file into
the sessions dir (or point the registry role at its path) and skip login entirely;
run this with the same --session ref to verify it.

Usage:
  tg-qa-login --session main --api-id 123 --api-hash abc           # phone + code
  tg-qa-login --session main --project telebook --qr               # QR scan
  tg-qa-login --session /path/to/existing.session --project telebook   # verify

Interactive by design (code arrives in Telegram; 2FA password may be needed) —
run it in a terminal, not through an agent.
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import os
import sys

from registry import find_project, resolve_api, session_path, sessions_dir


def parse_args():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--session", default="main",
                    help="session name (stored in %s) or a path to a .session file" % sessions_dir())
    ap.add_argument("--project", help="registry alias to take api_id/api_hash from")
    ap.add_argument("--api-id", type=int, help="override api_id (else project registry / env)")
    ap.add_argument("--api-hash", help="override api_hash")
    ap.add_argument("--qr", action="store_true",
                    help="QR login: scan with Telegram → Settings → Devices → Link Desktop Device")
    return ap.parse_args()


def show_qr(url: str):
    import qrcode
    q = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_L)
    q.add_data(url)
    q.make(fit=True)
    buf = io.StringIO()
    q.print_ascii(out=buf, invert=True)
    print(buf.getvalue(), file=sys.stderr)


async def qr_flow(client) -> bool:
    qr = await client.qr_login()
    print("Scan with Telegram → Settings → Devices → Link Desktop Device\n", file=sys.stderr)
    show_qr(qr.url)
    for attempt in range(4):
        try:
            await qr.wait(timeout=30)
            return True
        except asyncio.TimeoutError:
            if attempt == 3:
                return False
            print("\nQR expired, new one:\n", file=sys.stderr)
            await qr.recreate()
            show_qr(qr.url)
    return False


async def main():
    args = parse_args()
    api_id, api_hash = args.api_id, args.api_hash
    if not (api_id and api_hash):
        proj = find_project(args.project) if args.project else {}
        api_id, api_hash = resolve_api(proj)

    path = session_path(args.session)
    path.parent.mkdir(parents=True, exist_ok=True)

    from telethon import TelegramClient
    client = TelegramClient(str(path), api_id, api_hash)
    await client.connect()
    try:
        if not await client.is_user_authorized():
            if args.qr:
                ok = await qr_flow(client)
                if not ok:
                    print(json.dumps({"error": "QR not scanned in time"}))
                    return 1
                # QR login may still require the 2FA password
                from telethon.errors import SessionPasswordNeededError
                try:
                    await client.get_me()
                except SessionPasswordNeededError:
                    import getpass
                    await client.sign_in(password=getpass.getpass("2FA password: "))
            else:
                phone = input("Phone (+…): ").strip()
                await client.start(phone=lambda: phone)  # asks code / 2FA itself
        me = await client.get_me()
        os.chmod(f"{path}", 0o600)
        print(json.dumps({"status": "ok", "session": str(path),
                          "user": me.first_name, "username": me.username, "id": me.id},
                         ensure_ascii=False))
        return 0
    finally:
        await client.disconnect()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
