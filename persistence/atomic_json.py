from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any


def atomic_write_json(path: Path, payload: Any, *, indent: int = 2, ensure_ascii: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fp:
            json.dump(payload, fp, indent=indent, ensure_ascii=ensure_ascii)
            fp.flush()
            os.fsync(fp.fileno())
        os.replace(str(tmp_path), str(path))
    finally:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass


def load_json_or_quarantine(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        quarantine_dir = path.parent / "corrupt"
        quarantine_dir.mkdir(parents=True, exist_ok=True)
        stamp = int(time.time() * 1000)
        backup = quarantine_dir / f"{path.stem}.corrupt.{stamp}{path.suffix}"
        try:
            shutil.move(str(path), str(backup))
        except Exception:
            pass
        return default
