"""Fetch the pinned embedding model into a local folder and verify it (Stage 13).

Run by compose's one-off ``model-init`` service:

    docker compose --profile setup run --rm model-init

or directly (any machine with internet access):

    python -m scripts.fetch_embedding_model --dest /models/all-MiniLM-L6-v2
    python -m scripts.fetch_embedding_model --dest /models/all-MiniLM-L6-v2 --verify-only

Steps:

1. Ask the Hugging Face Hub for the file list of ``--repo`` at ``--revision`` (a full
   40-character commit, so the files can never change underneath), with each file's
   checksum: SHA-256 for files stored in Git LFS (the weights), the Git object ID for the
   others.
2. Keep only the files the backend reads (``app.services.model_files.NEEDED``): about
   92 MB of the repository's 977 MB. ONNX, OpenVINO, TensorFlow, Rust and legacy PyTorch
   copies of the weights are not downloaded.
3. Download each file that is missing or does not match its checksum (a re-run repairs a
   corrupt or interrupted copy and skips intact files), and check it after download.
4. Remove anything else from the folder (``--keep-extra`` to keep it): for example the
   full repository an earlier model-init downloaded.
5. Write ``model-manifest.json`` (repository, revision, size and SHA-256 per file). The
   backend checks the folder against it before every model load.

``--verify-only`` makes no network call: it checks the folder against its manifest.

Exit status: 0 on success, 1 if a file cannot be fetched or verified. Output names files
and sizes only.
"""

import argparse
import logging
import os
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:  # allow `python scripts/fetch_embedding_model.py`
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.config import (  # noqa: E402
    DEFAULT_EMBEDDING_MODEL_NAME,
    PINNED_EMBEDDING_MODEL_REVISIONS,
)
from app.services.model_files import (  # noqa: E402
    MANIFEST_NAME,
    REQUIRED,
    ModelFilesError,
    git_blob_sha1_of,
    is_needed,
    sha256_of,
    verify_model_folder,
    write_manifest,
)

logger = logging.getLogger("fetch_embedding_model")

DEFAULT_DEST = "/models/all-MiniLM-L6-v2"
# Kept by --prune: the Hub library's own bookkeeping for a local_dir download.
HUB_METADATA = ".cache"


@dataclass(frozen=True)
class RemoteFile:
    path: str
    size: int
    sha256: str | None  # LFS files
    git_sha1: str  # every file

    def matches(self, local: Path) -> bool:
        if not local.is_file() or local.stat().st_size != self.size:
            return False
        if self.sha256 is not None:
            return sha256_of(local) == self.sha256
        return git_blob_sha1_of(local) == self.git_sha1


def remote_files(repo: str, revision: str, api: Any = None) -> list[RemoteFile]:
    """The repository's files at ``revision`` with their checksums (one Hub API call)."""
    if api is None:
        from huggingface_hub import HfApi

        api = HfApi()
    info = api.model_info(repo, revision=revision, files_metadata=True)
    if info.sha != revision:
        raise ModelFilesError(f"the Hub resolved revision {revision} to {info.sha}")
    return [
        RemoteFile(
            path=sibling.rfilename,
            size=int(sibling.size),
            sha256=sibling.lfs.sha256 if sibling.lfs else None,
            git_sha1=sibling.blob_id,
        )
        for sibling in info.siblings
    ]


def fetch(
    repo: str,
    revision: str,
    dest: Path,
    *,
    api: Any = None,
    download: Callable[..., str] | None = None,
    prune: bool = True,
) -> None:
    """Make ``dest`` hold exactly the needed files of ``repo`` at ``revision``, verified.

    Raises:
        ModelFilesError: a required file is absent from the repository, or a file does not
            match its checksum after download.
    """
    if download is None:
        from huggingface_hub import hf_hub_download

        download = hf_hub_download
    wanted = [file for file in remote_files(repo, revision, api) if is_needed(file.path)]
    names = {file.path for file in wanted}
    if missing := [name for name in REQUIRED if name not in names]:
        raise ModelFilesError(f"{repo} at {revision} has no {', '.join(missing)}")
    dest.mkdir(parents=True, exist_ok=True)
    (dest / MANIFEST_NAME).unlink(missing_ok=True)  # invalid until every file is checked
    fetched = kept = 0
    for file in wanted:
        local = dest / file.path
        if file.matches(local):
            kept += 1
            continue
        logger.info("Downloading %s (%.1f MB)", file.path, file.size / 1e6)
        download(
            repo_id=repo,
            filename=file.path,
            revision=revision,
            local_dir=str(dest),
            force_download=local.exists(),  # a present file failed its check: replace it
        )
        if not file.matches(local):
            raise ModelFilesError(f"{file.path} does not match its checksum after download")
        fetched += 1
    if prune:
        remove_extra(dest, names)
    manifest = write_manifest(dest, repo, revision, sorted(names))
    logger.info(
        "Model ready in %s: %s at %s, %d files (%d downloaded, %d already intact), %.1f MB",
        dest,
        repo,
        revision,
        len(names),
        fetched,
        kept,
        manifest.total_bytes / 1e6,
    )


def remove_extra(dest: Path, keep: set[str]) -> None:
    """Delete files and folders under ``dest`` that are not in ``keep`` (nor the Hub's
    bookkeeping or the manifest)."""
    removed = 0
    for path in sorted(dest.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        relative = path.relative_to(dest).as_posix()
        if relative.split("/")[0] == HUB_METADATA or relative == MANIFEST_NAME:
            continue
        if path.is_file() and relative not in keep:
            removed += path.stat().st_size
            path.unlink()
        elif path.is_dir() and not any(path.iterdir()):
            path.rmdir()
    if removed:
        logger.info("Removed %.1f MB of files the backend does not use", removed / 1e6)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--repo", default=os.environ.get("EMBEDDING_MODEL_REPO", DEFAULT_EMBEDDING_MODEL_NAME)
    )
    parser.add_argument("--revision", default=os.environ.get("EMBEDDING_MODEL_REVISION"))
    parser.add_argument("--dest", type=Path, default=Path(DEFAULT_DEST))
    parser.add_argument("--verify-only", action="store_true", help="no network: check only")
    parser.add_argument("--keep-extra", action="store_true", help="do not remove other files")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # one line per HTTP request otherwise

    if args.verify_only:
        try:
            manifest = verify_model_folder(args.dest)
        except ModelFilesError as exc:
            logger.error("%s", exc)
            return 1
        if manifest is None:
            logger.error("%s has no %s: fetch the model first", args.dest, MANIFEST_NAME)
            return 1
        logger.info("Verified %s: %s at %s, %d files", args.dest, manifest.repo,
                    manifest.revision, len(manifest.files))  # fmt: skip
        return 0

    revision = args.revision or PINNED_EMBEDDING_MODEL_REVISIONS.get(args.repo)
    if not revision or len(revision) != 40 or not all(c in "0123456789abcdef" for c in revision):
        parser.error("--revision must be a full 40-character commit (pinning is required)")
    try:
        fetch(args.repo, revision, args.dest, prune=not args.keep_extra)
    except ModelFilesError as exc:
        logger.error("%s", exc)
        return 1
    except Exception as exc:  # noqa: BLE001 - network, Hub or disk: report, do not dump
        offline = os.environ.get("HF_HUB_OFFLINE", "0") not in ("", "0")
        logger.error(
            "Could not fetch %s at %s (%s)%s",
            args.repo,
            revision,
            type(exc).__name__,
            ": HF_HUB_OFFLINE is set, so the Hub cannot be reached" if offline else "",
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
