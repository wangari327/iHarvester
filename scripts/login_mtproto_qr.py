"""Authorize one human Telegram admin as a local MTProto recovery session.

Run over an interactive SSH terminal. It prints a short-lived QR code, so
phone numbers, login codes, and 2FA passwords never need to be placed in an
environment file or sent through the bot.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import os
from pathlib import Path

try:
    import qrcode
    from telethon import TelegramClient, errors
except ImportError as error:  # pragma: no cover - exercised by the operator
    raise SystemExit(
        "Run: python -m pip install -r requirements-mtproto-recovery.txt"
    ) from error


def _required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(f"{name} is required.")
    return value


def _print_qr(url: str) -> None:
    print("\nIn Telegram, open Settings > Devices > Link Desktop Device and scan this QR code:\n")
    code = qrcode.QRCode(border=2)
    code.add_data(url)
    code.make(fit=True)
    code.print_ascii(invert=True)
    print()


async def run(session_path: Path) -> int:
    client = TelegramClient(
        str(session_path),
        int(_required_env("TELEGRAM_API_ID")),
        _required_env("TELEGRAM_API_HASH"),
        receive_updates=False,
    )
    await client.connect()
    try:
        if not await client.is_user_authorized():
            while not await client.is_user_authorized():
                login = await client.qr_login()
                _print_qr(login.url)
                try:
                    await login.wait(timeout=120)
                except TimeoutError:
                    print("That QR code expired; a fresh one is shown below.")
                except errors.SessionPasswordNeededError:
                    password = getpass.getpass("Telegram 2FA password: ")
                    await client.sign_in(password=password)

        identity = await client.get_me()
        if not identity or identity.bot:
            raise SystemExit("This must be a human admin account, not a bot session.")
        label = identity.username or identity.id
        print(f"Authorized human admin @{label}. Session saved only at {session_path}.")
        return 0
    finally:
        await client.disconnect()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Create a local QR-authorized human MTProto recovery session.")
    parser.add_argument("--session", required=True, help="Sensitive local session path, for example /recovery/sessions/owner-1")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    path = Path(args.session).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    raise SystemExit(asyncio.run(run(path)))
