import os
import json
import base64
import platform
import getpass
import boto3
from botocore.config import Config as BotoConfig
from Crypto.Cipher import AES
from Crypto.Protocol.KDF import PBKDF2
from Crypto.Random import get_random_bytes


CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".dobro_loader")
CONFIG_FILE = os.path.join(CONFIG_DIR, "s3_config.enc")

# Соль для деривации ключа из машинных данных
_APP_SALT = b"dobro_loader_s3_v1"


def _derive_key():
    """Генерирует AES-ключ на основе машинных данных (имя хоста + пользователь)."""
    machine_id = f"{platform.node()}:{getpass.getuser()}".encode("utf-8")
    return PBKDF2(machine_id, _APP_SALT, dkLen=32, count=100_000)


def save_config(config: dict):
    """Шифрует и сохраняет конфиг S3 на диск."""
    os.makedirs(CONFIG_DIR, exist_ok=True)

    key = _derive_key()
    nonce = get_random_bytes(12)
    cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)

    plaintext = json.dumps(config, ensure_ascii=False).encode("utf-8")
    ciphertext, tag = cipher.encrypt_and_digest(plaintext)

    payload = {
        "nonce": base64.b64encode(nonce).decode(),
        "tag": base64.b64encode(tag).decode(),
        "data": base64.b64encode(ciphertext).decode(),
    }

    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f)


def load_config() -> dict | None:
    """Загружает и расшифровывает конфиг S3. Возвращает None если файла нет."""
    if not os.path.exists(CONFIG_FILE):
        return None

    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            payload = json.load(f)

        key = _derive_key()
        cipher = AES.new(
            key,
            AES.MODE_GCM,
            nonce=base64.b64decode(payload["nonce"]),
        )
        plaintext = cipher.decrypt_and_verify(
            base64.b64decode(payload["data"]),
            base64.b64decode(payload["tag"]),
        )
        return json.loads(plaintext.decode("utf-8"))
    except Exception:
        return None


def upload_file(config: dict, file_path: str, s3_key: str, log_fn=None):
    """Загружает файл в S3/R2. Возвращает True при успехе."""
    if log_fn is None:
        log_fn = lambda msg: None

    try:
        s3 = boto3.client(
            "s3",
            endpoint_url=config["endpoint"],
            aws_access_key_id=config["access_key"],
            aws_secret_access_key=config["secret_key"],
            config=BotoConfig(signature_version="s3v4"),
            region_name="auto",
        )

        file_size = os.path.getsize(file_path)
        uploaded = 0

        def progress_callback(bytes_transferred):
            nonlocal uploaded
            uploaded += bytes_transferred
            pct = min(int(uploaded / file_size * 100), 100)
            log_fn(f"☁️ S3 загрузка: {pct}%")

        log_fn(f"☁️ Начало загрузки в S3: {s3_key}")
        s3.upload_file(
            file_path,
            config["bucket"],
            s3_key,
            Callback=progress_callback,
        )
        log_fn(f"☁️ S3 загрузка завершена: {s3_key}")
        return True

    except Exception as e:
        log_fn(f"❌ S3 ошибка: {str(e)}")
        return False
