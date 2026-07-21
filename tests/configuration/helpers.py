from pathlib import Path


def write_config(path: Path, text: str) -> Path:
    path.write_text(text)
    return path
