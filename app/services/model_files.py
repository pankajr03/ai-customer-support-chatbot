"""The embedding model's files on disk: which ones are needed, and are they intact (Stage 13).

The Docker deployment loads the model from a local folder filled once by
``scripts/fetch_embedding_model.py`` (compose's ``model-init``). That script checks every
file against the Hugging Face Hub's own checksums for the pinned revision and then writes
``model-manifest.json`` into the folder: the repository, the revision, and each file's
size and SHA-256.

Before loading, the backend checks the folder against that manifest
(``verify_model_folder``): a missing, truncated or altered file is reported by name, with
the fix, instead of surfacing as an obscure PyTorch or tokenizer error, or worse, loading
a different model than the one evaluated. Hashing the ~92 MB once per process takes well
under a second, next to the ~20 s the model takes to load.

A folder without a manifest (one copied by hand) is loaded unverified, with a warning. A
Hugging Face Hub ID (local development) is loaded from the Hub cache at the pinned
revision; that cache keeps no checksums of its own on Windows, so it is not verified here.

Only the files sentence-transformers reads for this model are fetched (``NEEDED``):
PyTorch weights in safetensors format, the tokenizer and the configuration. The
repository also holds ONNX, OpenVINO, TensorFlow, Rust and legacy PyTorch copies of the
weights: about 885 MB the backend never reads.
"""

import fnmatch
import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MANIFEST_NAME = "model-manifest.json"
MANIFEST_FORMAT = 1
# Files sentence-transformers reads: configuration, tokenizer, model card, safetensors
# weights. "*" also matches "/", so module folders such as 1_Pooling/ are included.
NEEDED = ("*.json", "*.txt", "*.md", "model.safetensors")
# Alternative runtimes' copies of the weights, never read by the backend.
NOT_NEEDED_FOLDERS = ("onnx/", "openvino/")
REQUIRED = ("config.json", "modules.json", "model.safetensors", "tokenizer.json")
CHUNK = 1024 * 1024


class ModelFilesError(Exception):
    """The model folder is incomplete or corrupt. The message names the files (never
    their contents) and how to fix it."""


@dataclass(frozen=True)
class ModelManifest:
    repo: str
    revision: str
    files: dict[str, dict[str, Any]]  # path -> {"size": int, "sha256": str}

    @property
    def total_bytes(self) -> int:
        return sum(int(entry["size"]) for entry in self.files.values())


def is_needed(path: str) -> bool:
    """Whether the backend needs this repository file (see ``NEEDED``)."""
    if path.startswith(NOT_NEEDED_FOLDERS) or path == MANIFEST_NAME:
        return False
    return any(fnmatch.fnmatch(path, pattern) for pattern in NEEDED)


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        while chunk := file.read(CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def git_blob_sha1_of(path: Path) -> str:
    """The Git object ID of a file: how the Hub identifies files not stored in LFS."""
    digest = hashlib.sha1(usedforsecurity=False)
    digest.update(f"blob {path.stat().st_size}\0".encode())
    with path.open("rb") as file:
        while chunk := file.read(CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def write_manifest(folder: Path, repo: str, revision: str, files: list[str]) -> ModelManifest:
    """Record each file's size and SHA-256 (atomically: a crash leaves no half manifest)."""
    entries = {
        name: {"size": (folder / name).stat().st_size, "sha256": sha256_of(folder / name)}
        for name in sorted(files)
    }
    manifest = ModelManifest(repo, revision, entries)
    body = {"format": MANIFEST_FORMAT, "repo": repo, "revision": revision, "files": entries}
    temporary = folder / f".{MANIFEST_NAME}.tmp"
    temporary.write_text(json.dumps(body, indent=2), encoding="utf-8")
    os.replace(temporary, folder / MANIFEST_NAME)
    return manifest


def read_manifest(folder: Path) -> ModelManifest | None:
    """The folder's manifest, or None if it has none.

    Raises:
        ModelFilesError: the manifest exists but cannot be read.
    """
    path = folder / MANIFEST_NAME
    if not path.is_file():
        return None
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
        if body.get("format") != MANIFEST_FORMAT:
            raise ValueError("unknown manifest format")
        files = {
            str(name): {"size": int(entry["size"]), "sha256": str(entry["sha256"])}
            for name, entry in body["files"].items()
        }
        return ModelManifest(str(body["repo"]), str(body["revision"]), files)
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise ModelFilesError(
            f"{MANIFEST_NAME} in {folder} is unreadable ({type(exc).__name__}); "
            "fetch the model again (docker compose --profile setup run --rm model-init)"
        ) from None


def verify_model_folder(folder: Path, *, hash_files: bool = True) -> ModelManifest | None:
    """Check a model folder against its manifest. None if it has no manifest.

    ``hash_files=False`` checks presence and sizes only (no reading).

    Raises:
        ModelFilesError: a file is missing, has another size or another SHA-256, or the
            manifest is unreadable.
    """
    manifest = read_manifest(folder)
    if manifest is None:
        return None
    problems = []
    for name, entry in manifest.files.items():
        path = folder / name
        if not path.is_file():
            problems.append(f"{name} (missing)")
        elif path.stat().st_size != entry["size"]:
            problems.append(f"{name} (size {path.stat().st_size}, expected {entry['size']})")
        elif hash_files and sha256_of(path) != entry["sha256"]:
            problems.append(f"{name} (checksum mismatch)")
    if problems:
        raise ModelFilesError(
            f"Embedding model files in {folder} are missing or corrupt: {', '.join(problems)}. "
            "Fetch the model again (docker compose --profile setup run --rm model-init)"
        )
    return manifest
