import os
import json
import base64
from getpass import getpass
from pathlib import Path

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.backends import default_backend


SECRET_FILE = Path("secret.enc")


def derive_key(passphrase: str, salt: bytes) -> bytes:
    """Derive a Fernet key from the passphrase + salt."""
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=390000,
        backend=default_backend(),
    )
    return base64.urlsafe_b64encode(kdf.derive(passphrase.encode("utf-8")))


def main():
    print("=== Book Worm API Key Encryption ===")
    api_key = getpass("Enter your OpenAI API key (input hidden): ").strip()
    if not api_key:
        print("No API key entered. Aborting.")
        return

    passphrase = getpass("Create a passphrase for decrypting this key: ").strip()
    confirm = getpass("Re-enter the passphrase: ").strip()

    if passphrase != confirm:
        print("Passphrases did not match. Aborting.")
        return

    salt = os.urandom(16)
    key = derive_key(passphrase, salt)
    f = Fernet(key)
    token = f.encrypt(api_key.encode("utf-8"))

    data = {
        "salt": base64.b64encode(salt).decode("utf-8"),
        "token": token.decode("utf-8"),
    }

    SECRET_FILE.write_text(json.dumps(data), encoding="utf-8")
    print(f"Encrypted API key saved to {SECRET_FILE.resolve()}")
    print("You can now remove the plain API key from .env if you want.")

if __name__ == "__main__":
    main()
