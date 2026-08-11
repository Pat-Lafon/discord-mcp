import os
import typing as tp
from pathlib import Path
from dotenv import load_dotenv


class DiscordConfig(tp.NamedTuple):
    email: str
    password: str
    headless: bool


def load_config() -> DiscordConfig:
    env_file = Path(".env")
    if env_file.exists():
        load_dotenv(env_file)

    email = os.getenv("DISCORD_EMAIL")
    if not email:
        raise ValueError("DISCORD_EMAIL environment variable is required")

    password = os.getenv("DISCORD_PASSWORD")
    if not password:
        raise ValueError("DISCORD_PASSWORD environment variable is required")

    headless = os.getenv("DISCORD_HEADLESS", "true").lower() == "true"

    return DiscordConfig(email=email, password=password, headless=headless)
