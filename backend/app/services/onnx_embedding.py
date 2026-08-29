"""Lightweight ONNX Runtime embedding inference."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import ssl
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Sequence

import numpy as np
import onnxruntime as ort
import certifi
from filelock import FileLock
from tokenizers import Tokenizer


MODEL_NAME = "paraphrase-multilingual-MiniLM-L12-v2"
MODEL_ENV_VAR = "ONNX_EMBEDDING_MODEL_DIR"
MODEL_OFFLINE_ENV_VAR = "ONNX_EMBEDDING_OFFLINE"
MODEL_BASE_URL = (
    "https://huggingface.co/sentence-transformers/"
    "paraphrase-multilingual-MiniLM-L12-v2/resolve/main/onnx"
)
MODEL_FILES = {
    "model.onnx": {
        "bytes": 470301610,
        "sha256": "10f7a088420252b26caf819236ca2c9d2987afd0fc06fec7553b542a5655a05a",
    },
    "tokenizer.json": {
        "bytes": 9081518,
        "sha256": "2c3387be76557bd40970cec13153b3bbf80407865484b209e655e5e4729076b8",
    },
}
# Hugging Face 仓库中 tokenizer.json 位于仓库根目录，model.onnx 位于 onnx/ 子目录
MODEL_FILE_URLS = {
    "model.onnx": f"{MODEL_BASE_URL}/model.onnx",
    "tokenizer.json": (
        "https://huggingface.co/sentence-transformers/"
        "paraphrase-multilingual-MiniLM-L12-v2/resolve/main/tokenizer.json"
    ),
}

logger = logging.getLogger(__name__)
SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())


def _is_model_dir(path: Path) -> bool:
    return all((path / filename).is_file() for filename in MODEL_FILES)


def _is_offline() -> bool:
    return os.getenv(MODEL_OFFLINE_ENV_VAR, "").lower() in {"1", "true", "yes", "on"}


def _default_download_dir() -> Path:
    configured = os.getenv(MODEL_ENV_VAR)
    if configured:
        return Path(configured).expanduser().resolve()

    if getattr(sys, "frozen", False):
        local_app_data = os.getenv("LOCALAPPDATA")
        cache_root = Path(local_app_data) if local_app_data else Path.home() / ".cache"
        return cache_root / "AIFictionForge" / "embedding-onnx" / MODEL_NAME

    backend_dir = Path(__file__).resolve().parents[2]
    return backend_dir / "embedding" / "onnx" / MODEL_NAME


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _commit_partial(
    partial: Path,
    destination: Path,
    expected_size: int,
    expected_sha256: str,
) -> bool:
    if not partial.is_file() or partial.stat().st_size != expected_size:
        return False
    actual_sha256 = _sha256(partial)
    if actual_sha256 != expected_sha256:
        partial.unlink()
        raise OSError(
            f"{destination.name} SHA256 校验失败: {actual_sha256}，"
            f"预期 {expected_sha256}"
        )
    os.replace(partial, destination)
    logger.info("模型文件下载完成: %s", destination)
    return True


def _download_file(filename: str, target_dir: Path) -> None:
    metadata = MODEL_FILES[filename]
    expected_size = int(metadata["bytes"])
    expected_sha256 = str(metadata["sha256"])
    destination = target_dir / filename
    partial = target_dir / f"{filename}.part"

    if destination.is_file() and destination.stat().st_size == expected_size:
        return
    if destination.exists():
        destination.unlink()
    if partial.exists() and partial.stat().st_size > expected_size:
        partial.unlink()

    url = MODEL_FILE_URLS[filename]
    for attempt in range(1, 4):
        try:
            if _commit_partial(
                partial, destination, expected_size, expected_sha256
            ):
                return
            resume_at = partial.stat().st_size if partial.exists() else 0
            headers = {"User-Agent": "AIFictionForge/ONNX-model-downloader"}
            if resume_at:
                headers["Range"] = f"bytes={resume_at}-"

            request = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(
                request, timeout=120, context=SSL_CONTEXT
            ) as response:
                append = resume_at > 0 and response.status == 206
                if not append:
                    resume_at = 0
                mode = "ab" if append else "wb"
                downloaded = resume_at
                next_log_at = downloaded + 64 * 1024 * 1024
                with partial.open(mode) as file:
                    while chunk := response.read(4 * 1024 * 1024):
                        file.write(chunk)
                        downloaded += len(chunk)
                        if downloaded >= next_log_at:
                            logger.info(
                                "模型下载进度 %s: %.1f%%",
                                filename,
                                downloaded * 100 / expected_size,
                            )
                            next_log_at += 64 * 1024 * 1024

            actual_size = partial.stat().st_size
            if actual_size != expected_size:
                raise OSError(
                    f"{filename} 大小校验失败: {actual_size}，预期 {expected_size}"
                )
            if _commit_partial(
                partial, destination, expected_size, expected_sha256
            ):
                return
        except (OSError, urllib.error.URLError) as error:
            if attempt == 3:
                raise RuntimeError(f"从 Hugging Face 下载 {filename} 失败") from error
            logger.warning("下载 %s 失败，第 %s/3 次重试: %s", filename, attempt, error)
            time.sleep(2**attempt)


def download_model(target_dir: Path | str | None = None) -> Path:
    """Download and verify the deployment model from Hugging Face."""
    target = Path(target_dir).resolve() if target_dir else _default_download_dir()
    target.parent.mkdir(parents=True, exist_ok=True)
    lock = FileLock(str(target) + ".lock", timeout=3600)

    logger.info("本地未找到 ONNX Embedding 模型，将从 Hugging Face 下载到: %s", target)
    with lock:
        if _is_model_dir(target):
            return target
        target.mkdir(parents=True, exist_ok=True)
        for filename in MODEL_FILES:
            _download_file(filename, target)
        config_path = target / "embedding_config.json"
        if not config_path.exists():
            config_path.write_text(
                json.dumps({
                    "model_name": MODEL_NAME,
                    "max_seq_length": 128,
                    "embedding_dimension": 384,
                    "pooling": "mean",
                    "normalize": False,
                    "pad_token": "<pad>",
                }),
                encoding="utf-8",
            )

    if not _is_model_dir(target):
        raise RuntimeError(f"ONNX Embedding 模型下载不完整: {target}")
    return target


def resolve_model_dir() -> Path:
    """Resolve a bundled/external model, downloading it when necessary."""
    configured = os.getenv(MODEL_ENV_VAR)
    candidates: list[Path] = []
    if configured:
        candidates.append(Path(configured).expanduser())

    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).parent
        if hasattr(sys, "_MEIPASS"):
            candidates.append(
                Path(sys._MEIPASS) / "backend" / "embedding" / "onnx" / MODEL_NAME
            )
        candidates.extend(
            [
                exe_dir / "embedding-onnx" / MODEL_NAME,
                exe_dir / "embedding-onnx",
                exe_dir / "_internal" / "backend" / "embedding" / "onnx" / MODEL_NAME,
                _default_download_dir(),
            ]
        )
    else:
        backend_dir = Path(__file__).resolve().parents[2]
        candidates.append(backend_dir / "embedding" / "onnx" / MODEL_NAME)

    for candidate in candidates:
        resolved = candidate.resolve()
        if _is_model_dir(resolved):
            return resolved

    checked = "\n".join(f"  - {candidate}" for candidate in candidates)
    if _is_offline():
        raise FileNotFoundError(
            f"离线模式下未找到 ONNX Embedding 模型。已检查：\n{checked}"
        )
    return download_model()


class OnnxEmbeddingModel:
    """Sentence Transformer-compatible encode interface backed by ONNX Runtime."""

    def __init__(self, model_dir: Path | str | None = None) -> None:
        if model_dir:
            requested_dir = Path(model_dir).resolve()
            if not _is_model_dir(requested_dir):
                if _is_offline():
                    raise FileNotFoundError(f"ONNX Embedding 模型不完整: {requested_dir}")
                requested_dir = download_model(requested_dir)
            self.model_dir = requested_dir
        else:
            self.model_dir = resolve_model_dir()
        config_path = self.model_dir / "embedding_config.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))

        self.max_seq_length = int(config["max_seq_length"])
        self.embedding_dimension = int(config["embedding_dimension"])
        if config.get("pooling") != "mean" or config.get("normalize", False):
            raise ValueError("当前运行时仅支持不归一化的 Mean Pooling 模型")

        self.tokenizer = Tokenizer.from_file(str(self.model_dir / "tokenizer.json"))
        pad_token = str(config.get("pad_token", "<pad>"))
        pad_id = self.tokenizer.token_to_id(pad_token)
        if pad_id is None:
            raise ValueError(f"tokenizer 中不存在 padding token: {pad_token}")
        self.tokenizer.enable_truncation(max_length=self.max_seq_length)
        self.tokenizer.enable_padding(pad_id=pad_id, pad_token=pad_token)

        options = ort.SessionOptions()
        options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        self.session = ort.InferenceSession(
            str(self.model_dir / "model.onnx"),
            sess_options=options,
            providers=["CPUExecutionProvider"],
        )
        self.input_names = {item.name for item in self.session.get_inputs()}

    def encode(self, texts: str | Sequence[str]) -> np.ndarray:
        """Encode one string or a batch using the original model's mean pooling."""
        single_input = isinstance(texts, str)
        batch = [texts] if single_input else list(texts)
        if not batch:
            return np.empty((0, self.embedding_dimension), dtype=np.float32)

        encodings = self.tokenizer.encode_batch(batch)
        inputs: dict[str, np.ndarray] = {
            "input_ids": np.asarray([item.ids for item in encodings], dtype=np.int64),
            "attention_mask": np.asarray(
                [item.attention_mask for item in encodings], dtype=np.int64
            ),
        }
        if "token_type_ids" in self.input_names:
            inputs["token_type_ids"] = np.asarray(
                [item.type_ids for item in encodings], dtype=np.int64
            )

        unexpected = set(inputs) - self.input_names
        if unexpected:
            inputs = {name: value for name, value in inputs.items() if name in self.input_names}

        token_embeddings = self.session.run(None, inputs)[0]
        mask = inputs["attention_mask"].astype(np.float32)[..., None]
        embeddings = (token_embeddings * mask).sum(axis=1) / np.clip(
            mask.sum(axis=1), 1e-9, None
        )
        embeddings = embeddings.astype(np.float32, copy=False)

        if embeddings.shape[1] != self.embedding_dimension:
            raise RuntimeError(
                f"ONNX 模型输出维度异常: {embeddings.shape[1]}，预期 {self.embedding_dimension}"
            )
        return embeddings[0] if single_input else embeddings
