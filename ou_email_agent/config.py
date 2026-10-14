from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent


def load_config(path: str | Path | None = None) -> dict:
    path = Path(path or os.environ.get("OU_EMAIL_CONFIG", ROOT / "config.json")).resolve()
    config = json.loads(path.read_text(encoding="utf-8"))
    if "password" in config:
        raise ValueError("Remove password from config; use the local credentials command.")
    url = urlparse(config["ollama_url"])
    if url.scheme != "http" or url.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("Ollama must use a loopback HTTP endpoint to keep inference local.")
    if not config.get("folders") or not all(isinstance(x, str) for x in config["folders"]):
        raise ValueError("Configure at least one mailbox folder.")
    if int(config["poll_seconds"]) < 15 or int(config["batch_size"]) < 1:
        raise ValueError("poll_seconds must be >=15 and batch_size must be positive.")
    config.setdefault('background_analysis', False)
    config.setdefault('analysis_interval_seconds', 300)
    config.setdefault('model_keep_alive', 0)
    config.setdefault('embedding_max_bytes', 1800)
    config.setdefault('embedding_num_gpu', 0)
    config.setdefault('embedding_num_thread', 2)
    config.setdefault('embedding_request_budget', 8)
    if not 128 <= int(config['embedding_max_bytes']) <= 1800:
        raise ValueError('embedding_max_bytes must be between 128 and 1800.')
    if not 1 <= int(config['embedding_num_thread']) <= 4 or not 1 <= int(config['embedding_request_budget']) <= 32:
        raise ValueError('Use 1-4 embedding threads and 1-32 requests per manual batch.')
    if not isinstance(config['background_analysis'], bool):
        raise ValueError('background_analysis must be true or false.')
    if int(config['analysis_interval_seconds']) < 60:
        raise ValueError('analysis_interval_seconds must be at least 60.')
    config["data_dir"] = str(Path(os.environ.get("OU_EMAIL_DATA", path.parent / "data")).resolve())
    return config


def credential_store():
    if os.name != "nt":
        raise RuntimeError("Run credentials/sync/monitor using Windows Python for Windows Credential Manager.")
    from keyring.backends.Windows import WinVaultKeyring
    return WinVaultKeyring()


def credential_service(config: dict) -> str:
    return "ou-email-agent:" + config["host"]


def password(config: dict) -> str:
    value = credential_store().get_password(credential_service(config), config["username"])
    if not value:
        raise RuntimeError("No saved password. Run setup.cmd or: ou-email credentials")
    return value
