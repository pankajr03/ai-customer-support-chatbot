"""Embedding model files: fetching only what is needed, verifying them, and refusing a
corrupt copy before loading it (Stage 13, Task 18). Offline: the Hub is faked."""

import hashlib
import json
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from dotenv import dotenv_values

from app.core.config import DEFAULT_EMBEDDING_MODEL_NAME, PINNED_EMBEDDING_MODEL_REVISIONS
from app.services.embedding_service import (
    EmbeddingModelLoadError,
    EmbeddingService,
    load_sentence_transformer,
)
from app.services.model_files import (
    MANIFEST_NAME,
    ModelFilesError,
    is_needed,
    verify_model_folder,
    write_manifest,
)
from scripts import fetch_embedding_model
from scripts.fetch_embedding_model import fetch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
REPO = "org/model"
REVISION = "a" * 40

# The pinned all-MiniLM-L6-v2 commit's file list (Hub API, 2026-10-20).
HUB_FILES = [
    ".gitattributes", "1_Pooling/config.json", "README.md", "config.json",
    "config_sentence_transformers.json", "data_config.json", "model.safetensors",
    "modules.json", "onnx/model.onnx", "onnx/model_O1.onnx", "onnx/model_qint8_arm64.onnx",
    "openvino/openvino_model.bin", "openvino/openvino_model.xml", "pytorch_model.bin",
    "rust_model.ot", "sentence_bert_config.json", "special_tokens_map.json", "tf_model.h5",
    "tokenizer.json", "tokenizer_config.json", "train_script.py", "vocab.txt",
]  # fmt: skip


def test_only_the_files_the_backend_reads_are_needed() -> None:
    needed = sorted(name for name in HUB_FILES if is_needed(name))

    assert needed == [
        "1_Pooling/config.json", "README.md", "config.json", "config_sentence_transformers.json",
        "data_config.json", "model.safetensors", "modules.json", "sentence_bert_config.json",
        "special_tokens_map.json", "tokenizer.json", "tokenizer_config.json", "vocab.txt",
    ]  # fmt: skip
    assert not is_needed(MANIFEST_NAME)


# --- Manifest and verification ---------------------------------------------------------


@pytest.fixture
def folder(tmp_path: Path) -> Path:
    model = tmp_path / "model"
    (model / "1_Pooling").mkdir(parents=True)
    files = {
        "config.json": b'{"hidden_size": 384}',
        "modules.json": b"[]",
        "tokenizer.json": b"{}",
        "model.safetensors": b"weights" * 1000,
        "1_Pooling/config.json": b"{}",
    }
    for name, content in files.items():
        (model / name).write_bytes(content)
    write_manifest(model, REPO, REVISION, list(files))
    return model


def test_an_intact_folder_verifies_and_names_its_revision(folder: Path) -> None:
    manifest = verify_model_folder(folder)

    assert manifest is not None and (manifest.repo, manifest.revision) == (REPO, REVISION)
    assert len(manifest.files) == 5 and manifest.total_bytes > 7000


@pytest.mark.parametrize(
    ("damage", "reported"),
    [
        (lambda f: (f / "model.safetensors").unlink(), "model.safetensors (missing)"),
        (lambda f: (f / "model.safetensors").write_bytes(b"trunc"), "model.safetensors (size 5"),
        # Same size, different bytes: only the checksum can tell.
        (lambda f: (f / "config.json").write_bytes(b'{"hidden_size": 768}'), "config.json (che"),
    ],
)  # fmt: skip
def test_missing_truncated_or_altered_files_are_named_with_the_fix(
    folder: Path, damage: Any, reported: str
) -> None:
    damage(folder)

    with pytest.raises(ModelFilesError) as raised:
        verify_model_folder(folder)

    assert reported in str(raised.value)
    assert "model-init" in str(raised.value)
    assert "weights" not in str(raised.value)  # never file contents


def test_presence_and_sizes_can_be_checked_without_reading(folder: Path) -> None:
    (folder / "config.json").write_bytes(b'{"hidden_size": 768}')  # same size

    assert verify_model_folder(folder, hash_files=False) is not None


def test_an_unreadable_manifest_is_corruption_too(folder: Path) -> None:
    (folder / MANIFEST_NAME).write_text("{not json", encoding="utf-8")

    with pytest.raises(ModelFilesError, match="unreadable"):
        verify_model_folder(folder)


def test_a_folder_without_a_manifest_is_not_verified(tmp_path: Path) -> None:
    assert verify_model_folder(tmp_path) is None


# --- Loading ---------------------------------------------------------------------------


class Factory:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, name: str, **kwargs: Any) -> str:
        self.calls.append((name, kwargs))
        return "model"


def test_a_corrupt_folder_is_refused_before_loading(folder: Path) -> None:
    (folder / "model.safetensors").write_bytes(b"x")
    factory = Factory()

    with pytest.raises(ModelFilesError, match=r"model\.safetensors"):
        load_sentence_transformer(str(folder), factory=factory)  # type: ignore[arg-type]

    assert factory.calls == []  # never handed to PyTorch, never "downloaded" instead


def test_an_intact_folder_is_loaded_from_disk_only(
    folder: Path, caplog: pytest.LogCaptureFixture
) -> None:
    factory = Factory()

    with caplog.at_level(logging.INFO):
        load_sentence_transformer(str(folder), factory=factory, revision="b" * 40)  # type: ignore[arg-type]

    assert factory.calls == [(str(folder), {"local_files_only": True})]  # no revision
    assert f"Embedding model files verified: {REPO} at revision {REVISION}" in caplog.text


def test_a_folder_without_a_manifest_loads_with_a_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    factory = Factory()

    load_sentence_transformer(str(tmp_path), factory=factory)  # type: ignore[arg-type]

    assert len(factory.calls) == 1 and "loading it unverified" in caplog.text


def test_the_default_hub_model_is_loaded_at_the_pinned_revision() -> None:
    factory = Factory()

    load_sentence_transformer(DEFAULT_EMBEDDING_MODEL_NAME, factory=factory)  # type: ignore[arg-type]
    load_sentence_transformer("other/model", factory=factory)  # type: ignore[arg-type]
    load_sentence_transformer("other/model", factory=factory, revision="c" * 40)  # type: ignore[arg-type]

    pinned = PINNED_EMBEDDING_MODEL_REVISIONS[DEFAULT_EMBEDDING_MODEL_NAME]
    assert [kwargs for _, kwargs in factory.calls] == [
        {"local_files_only": True, "revision": pinned},
        {"local_files_only": True},  # unknown model: no pin is invented
        {"local_files_only": True, "revision": "c" * 40},
    ]


def test_a_corrupt_model_is_a_load_failure_with_a_cooldown(folder: Path) -> None:
    (folder / "model.safetensors").write_bytes(b"x")
    service = EmbeddingService(str(folder), load_retry_seconds=30)

    with pytest.raises(EmbeddingModelLoadError, match="ModelFilesError"):
        service.load()
    assert service.load_failed_recently  # readiness: semantic_search=unavailable
    with pytest.raises(EmbeddingModelLoadError, match="failed to load recently"):
        service.load()  # not re-hashed on every request


def test_the_app_and_the_deployment_pin_the_same_revision() -> None:
    deployment = dotenv_values(PROJECT_ROOT / "deploy" / ".env.example")

    assert (
        PINNED_EMBEDDING_MODEL_REVISIONS[DEFAULT_EMBEDDING_MODEL_NAME]
        == deployment["EMBEDDING_MODEL_REVISION"]
    )
    assert deployment["EMBEDDING_MODEL_REPO"] == DEFAULT_EMBEDDING_MODEL_NAME


# --- Fetching --------------------------------------------------------------------------


class FakeHub:
    """The Hub's file list with checksums, and downloads that write the expected bytes
    (or wrong ones, to simulate a corrupt transfer)."""

    def __init__(self, files: dict[str, bytes], revision: str = REVISION) -> None:
        self.files, self.revision = files, revision
        self.downloads: list[tuple[str, bool]] = []
        self.corrupt: set[str] = set()

    def model_info(self, repo: str, *, revision: str, files_metadata: bool) -> Any:
        siblings = []
        for name, content in self.files.items():
            lfs = name.endswith((".safetensors", ".onnx", ".bin"))
            siblings.append(
                SimpleNamespace(
                    rfilename=name,
                    size=len(content),
                    lfs=SimpleNamespace(sha256=hashlib.sha256(content).hexdigest())
                    if lfs
                    else None,
                    blob_id=hashlib.sha1(b"blob %d\0" % len(content) + content).hexdigest(),  # noqa: S324
                )
            )
        return SimpleNamespace(sha=self.revision, siblings=siblings)

    def download(self, *, repo_id: str, filename: str, revision: str, local_dir: str,
                 force_download: bool) -> str:  # fmt: skip
        self.downloads.append((filename, force_download))
        path = Path(local_dir) / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"garbage" if filename in self.corrupt else self.files[filename])
        return str(path)


REMOTE = {
    "config.json": b'{"hidden_size": 384}',
    "modules.json": b"[]",
    "tokenizer.json": b"{}",
    "model.safetensors": b"weights" * 1000,
    "1_Pooling/config.json": b"{}",
    "onnx/model.onnx": b"onnx" * 1000,
    "pytorch_model.bin": b"pickle" * 1000,
}


def test_fetch_downloads_only_needed_files_verifies_them_and_writes_the_manifest(
    tmp_path: Path,
) -> None:
    hub = FakeHub(REMOTE)

    fetch(REPO, REVISION, tmp_path, api=hub, download=hub.download)

    assert sorted(name for name, _ in hub.downloads) == [
        "1_Pooling/config.json", "config.json", "model.safetensors", "modules.json",
        "tokenizer.json",
    ]  # fmt: skip
    manifest = verify_model_folder(tmp_path)
    assert manifest is not None and manifest.revision == REVISION
    assert not (tmp_path / "onnx").exists() and not (tmp_path / "pytorch_model.bin").exists()


def test_a_rerun_keeps_intact_files_repairs_corrupt_ones_and_prunes_the_rest(
    tmp_path: Path,
) -> None:
    hub = FakeHub(REMOTE)
    fetch(REPO, REVISION, tmp_path, api=hub, download=hub.download)
    (tmp_path / "model.safetensors").write_bytes(b"bitrot" + REMOTE["model.safetensors"][6:])
    (tmp_path / "onnx").mkdir()
    (tmp_path / "onnx" / "model.onnx").write_bytes(b"left over from a full download")
    hub.downloads.clear()

    fetch(REPO, REVISION, tmp_path, api=hub, download=hub.download)

    assert hub.downloads == [("model.safetensors", True)]  # forced: the copy was wrong
    assert verify_model_folder(tmp_path) is not None
    assert not (tmp_path / "onnx").exists()


def test_a_download_that_does_not_match_its_checksum_fails_without_a_manifest(
    tmp_path: Path,
) -> None:
    hub = FakeHub(REMOTE)
    hub.corrupt.add("model.safetensors")

    with pytest.raises(ModelFilesError, match="model.safetensors does not match"):
        fetch(REPO, REVISION, tmp_path, api=hub, download=hub.download)

    assert not (tmp_path / MANIFEST_NAME).exists()  # the backend will not trust the folder


def test_fetch_refuses_a_moved_revision_or_a_repository_without_safetensors(
    tmp_path: Path,
) -> None:
    moved = FakeHub(REMOTE, revision="b" * 40)
    with pytest.raises(ModelFilesError, match="resolved revision"):
        fetch(REPO, REVISION, tmp_path, api=moved, download=moved.download)

    legacy = FakeHub({k: v for k, v in REMOTE.items() if k != "model.safetensors"})
    with pytest.raises(ModelFilesError, match="has no model.safetensors"):
        fetch(REPO, REVISION, tmp_path, api=legacy, download=legacy.download)


def test_verify_only_needs_no_network(tmp_path: Path, folder: Path) -> None:
    assert fetch_embedding_model.main(["--dest", str(folder), "--verify-only"]) == 0
    assert fetch_embedding_model.main(["--dest", str(tmp_path), "--verify-only"]) == 1
    (folder / "modules.json").unlink()
    assert fetch_embedding_model.main(["--dest", str(folder), "--verify-only"]) == 1


def test_a_revision_must_be_a_full_commit() -> None:
    with pytest.raises(SystemExit):
        fetch_embedding_model.main(["--repo", "other/model", "--revision", "main"])


def test_the_manifest_is_plain_json(folder: Path) -> None:
    body = json.loads((folder / MANIFEST_NAME).read_text(encoding="utf-8"))

    assert body["format"] == 1 and body["files"]["config.json"]["size"] == 20


def test_an_unreachable_hub_is_reported_without_a_traceback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    def offline(*args: Any, **kwargs: Any) -> None:
        raise ConnectionError("offline mode is enabled")

    monkeypatch.setattr(fetch_embedding_model, "fetch", offline)
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")

    assert fetch_embedding_model.main(["--dest", str(tmp_path)]) == 1
    assert "HF_HUB_OFFLINE is set, so the Hub cannot be reached" in caplog.text
