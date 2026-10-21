from __future__ import annotations

import contextlib
import shutil
import tempfile
import zipfile
from pathlib import Path

@contextlib.contextmanager
def materialize(path):
    p = Path(path)
    if p.suffix.lower() == ".zip":
        tmp = tempfile.mkdtemp(prefix="coach_zip_")
        try:
            with zipfile.ZipFile(p) as z:
                z.extractall(tmp)
            yield Path(tmp)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    else:
        yield p
