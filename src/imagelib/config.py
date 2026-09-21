"""Load configuration from config/config.toml (falls back to example values)."""

from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # Python <3.11
    import tomli as tomllib  # type: ignore[no-redef]


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "config.example.toml"
USER_CONFIG_PATH = PROJECT_ROOT / "config" / "config.toml"


def load_config(path: Path | None = None) -> dict[str, Any]:
    path = path or USER_CONFIG_PATH
    if not path.exists():
        path = DEFAULT_CONFIG_PATH
    with path.open("rb") as f:
        return tomllib.load(f)


config = load_config()


def watched_directories() -> list[Path]:
    return [Path(value).expanduser().resolve() for value in config["scan"]["watched_dirs"]]


def thumbnail_directory() -> Path:
    value = Path(config["scan"]["thumbnail_dir"]).expanduser()
    return (value if value.is_absolute() else PROJECT_ROOT / value).resolve()
