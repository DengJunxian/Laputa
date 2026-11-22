"""Source and dependency identity for v4 runs."""
import hashlib
import importlib.metadata
from pathlib import Path
import platform
import subprocess

import civitas
from core.experiments import identity, strict_types

from civitas.contracts.base import content_hash


def implementation_identity() -> dict:
    package = Path(civitas.__file__).resolve().parent
    sources = {"civitas/" + path.relative_to(package).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
               for path in sorted(package.rglob("*.py"))}
    for module in (identity, strict_types):
        sources[module.__name__.replace(".", "/") + ".py"] = hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest()
    commit = None
    dirty = None
    try:
        result = subprocess.run(["git", "-C", str(package), "rev-parse", "HEAD"], text=True,
                                capture_output=True, check=True, timeout=5)
        commit = result.stdout.strip()
        status = subprocess.run(["git", "-C", str(package), "status", "--porcelain"], text=True,
                                capture_output=True, check=True, timeout=5)
        dirty = bool(status.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        pass  # Installed wheels retain the exact source hash without assuming a Git checkout.
    dependencies = {name: importlib.metadata.version(name) for name in ("pydantic", "pydantic_core", "PyYAML")}
    dependencies.update(python=platform.python_version(), platform=platform.platform(), machine=platform.machine())
    return {"code_commit": commit, "code_dirty": dirty, "source_hash": content_hash(sources), "source_files": sources,
            "dependencies": dependencies, "dependency_hash": content_hash(dependencies)}
