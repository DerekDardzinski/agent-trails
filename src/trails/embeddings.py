"""Local ONNX embeddings for the vector arm. Optional: this is the `search` extra.

VENDORED from `~/projects/synth-acp/src/synth_acp/embeddings.py` (157 lines, same
author) rather than depended on, because depending on `synth-acp` would pull
textual, ACP and mcp in for one module. Vendoring with a provenance note is already
the convention in that repo.

FOUR DELIBERATE CHANGES from the source, and nothing else:

* the model directory is `~/.trails/models/all-MiniLM-L6-v2` rather than
  `~/.synth/models/...`;
* `similarity()` is dropped — Dolt computes distance in SQL, so a numpy cosine
  helper would have no call site here;
* `model_id()` and `MODEL_ID` are added, because every vector row records the model
  that produced it: changing models means reprocessing the corpus, and a
  mixed-model column silently returns nonsense rather than failing;
* `token_count()` is added, for the truncation lint. See its docstring for why it
  needs a second tokenizer.

`self._session` and `self._tokenizer` carry `Any` annotations the source omits, so
the type checker accepts the lazy-init pattern without an assertion per use.

`embedding_available()` RETURNS FALSE RATHER THAN RAISING. That is load-bearing:
Every read and write must all keep working on a machine
without the extra, and an ImportError escaping from here would break all three.
"""

from __future__ import annotations

import shutil
import tempfile
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal
from urllib.error import URLError

_HF_BASE = "https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2/resolve/main"
_MODEL_FILES = {"model": "onnx/model_O4.onnx", "tokenizer": "tokenizer.json"}
_DEFAULT_MODELS_DIR = Path.home() / ".trails" / "models" / "all-MiniLM-L6-v2"

MODEL_ID = "all-MiniLM-L6-v2/model_O4"
# The schema declares VECTOR(384) because this model's output is 384-dim,
# mean-pooled and L2-normalised.
VECTOR_DIM = 384
# The tokenizer truncates here, so a longer text loses its tail. `vector.chunk_texts` packs
# body paragraphs into chunks of at most `vector.CHUNK_TOKENS`, below this limit.
TRUNCATION_LIMIT = 256


class ModelNotAvailableError(Exception):
    """Raised when onnxruntime or tokenizers are not installed."""


class ModelDownloadError(Exception):
    """Raised when model file download fails."""


def embedding_available() -> bool:
    """Return True if onnxruntime and tokenizers are importable."""
    try:
        import onnxruntime  # noqa: F401
        import tokenizers  # noqa: F401
    except ImportError:
        return False
    return True


def engine_status(engine: EmbeddingEngine) -> tuple[Literal["ready", "unavailable"], str]:
    """Whether `engine` can embed a query now without downloading anything.

    ("ready", <model id>) when the dependencies import and the model files are present;
    otherwise ("unavailable", <reason>). A reading operation embeds only on "ready", because
    `embed` downloads a missing model and a read must never do that.
    """
    if not engine.is_available():
        return "unavailable", "dependencies not installed"
    if not engine.model_present():
        return "unavailable", "model not downloaded; run embed"
    return "ready", engine.model_id()


def _download_file(
    url: str,
    dest: Path,
    progress_callback: Callable[[int, int], None] | None = None,
) -> None:
    """Download a single file via urllib with progress reporting.

    Written to a NamedTemporaryFile in the DESTINATION directory and then moved, so
    `dest` never exists in a partial state: a half-written model file is worse than
    an absent one, because the absent one is detected and refetched.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        # The URL is built from a fixed HuggingFace https base and a fixed file name.
        with urllib.request.urlopen(url) as resp:
            total = int(resp.headers.get("Content-Length", 0))
            with tempfile.NamedTemporaryFile(dir=dest.parent, delete=False) as fd:
                tmp_path = Path(fd.name)
                try:
                    downloaded = 0
                    while chunk := resp.read(65536):
                        fd.write(chunk)
                        downloaded += len(chunk)
                        if progress_callback:
                            progress_callback(downloaded, total)
                except BaseException:
                    tmp_path.unlink(missing_ok=True)
                    raise
            shutil.move(str(tmp_path), str(dest))
    except (URLError, OSError) as e:
        raise ModelDownloadError(f"Failed to download {url}: {e}") from e


def _mean_pool_and_normalize(token_embeddings: Any, attention_mask: Any) -> Any:
    """Apply mean pooling over token embeddings, then L2-normalize."""
    import numpy as np

    mask_expanded = attention_mask[:, :, None].astype(np.float32)
    summed = (token_embeddings * mask_expanded).sum(axis=1)
    counts = mask_expanded.sum(axis=1).clip(min=1e-9)
    pooled = summed / counts
    norm = np.linalg.norm(pooled, axis=1, keepdims=True).clip(min=1e-9)
    return (pooled / norm).squeeze(0)


class EmbeddingEngine:
    """Lazy-loading embedding engine. Thread-safe after initialization."""

    def __init__(self, models_dir: Path | None = None) -> None:
        """Initialize with model storage directory.

        Default: `~/.trails/models/all-MiniLM-L6-v2/`.
        """
        self._models_dir = models_dir or _DEFAULT_MODELS_DIR
        self._session: Any = None
        self._tokenizer: Any = None
        self._counting_tokenizer: Any = None

    def is_available(self) -> bool:
        """Return True if onnxruntime and tokenizers are importable."""
        return embedding_available()

    def model_id(self) -> str:
        """The identifier stored with every vector this engine produces.

        Recorded per row because changing models means reprocessing the corpus, and
        a query that mixed two models' vectors would rank on distances computed
        between incomparable spaces — returning nonsense rather than failing.
        """
        return MODEL_ID

    def model_present(self) -> bool:
        """True when the model and tokenizer files are both in the models directory.

        Checks the files only and never downloads, so a reading operation can ask it.
        """
        return all(
            (self._models_dir / Path(file_path).name).exists()
            for file_path in _MODEL_FILES.values()
        )

    def ensure_model(self, progress_callback: Callable[[int, int], None] | None = None) -> None:
        """Download model files if not present. Raises ModelDownloadError on failure."""
        self._models_dir.mkdir(parents=True, exist_ok=True)
        for file_path in _MODEL_FILES.values():
            dest = self._models_dir / Path(file_path).name
            if not dest.exists():
                url = f"{_HF_BASE}/{file_path}"
                _download_file(url, dest, progress_callback)

    def embed(self, text: str) -> Any:
        """Embed text into a pre-normalized 384-dim float32 vector.

        Loads model on first call. Truncates to 256 tokens.
        Returns: numpy array of shape (384,) with L2 norm = 1.0.
        Raises: ModelNotAvailableError if deps not installed.
        """
        if not self.is_available():
            raise ModelNotAvailableError("onnxruntime and tokenizers are required")

        import numpy as np

        if self._tokenizer is None:
            import tokenizers

            self._tokenizer = tokenizers.Tokenizer.from_file(str(self._tokenizer_path()))
            self._tokenizer.enable_truncation(max_length=TRUNCATION_LIMIT)
            self._tokenizer.no_padding()

        if self._session is None:
            import onnxruntime

            model_path = self._models_dir / "model_O4.onnx"
            if not model_path.exists():
                self.ensure_model()
            self._session = onnxruntime.InferenceSession(
                str(model_path), providers=["CPUExecutionProvider"]
            )

        encoding = self._tokenizer.encode(text)
        input_ids = np.array([encoding.ids], dtype=np.int64)
        attention_mask = np.array([encoding.attention_mask], dtype=np.int64)
        token_type_ids = np.zeros_like(input_ids)

        outputs = self._session.run(
            None,
            {
                "input_ids": input_ids,
                "attention_mask": attention_mask,
                "token_type_ids": token_type_ids,
            },
        )
        return _mean_pool_and_normalize(outputs[0], attention_mask)

    def token_count(self, text: str) -> int:
        """Tokens in `text` WITHOUT truncation, for the over-length lint.

        A SECOND tokenizer instance, from the same `tokenizer.json` and so the same
        vocabulary and the same special tokens, differing only in that truncation is
        switched OFF. That difference is the whole point: `embed`'s tokenizer caps at
        `TRUNCATION_LIMIT`, so its token count can never exceed the limit and an
        over-length field is undetectable through it.

        `no_truncation()` IS REQUIRED AND NOT MERELY TIDY. `tokenizer.json` ships its
        own truncation section — `max_length: 128` for this model — so a freshly
        loaded tokenizer truncates at 128 whether or not anyone asks it to, and every
        count comes back capped at 128. Measured: 300 repetitions of one word count as
        128 tokens as loaded and 302 with truncation off. Without this call the lint
        rule can never see a field over 256 tokens, so it would report nothing while
        appearing to work — and note that the cap is BELOW the limit being checked,
        which is why nothing else about the rule would look wrong.

        Counting with the real tokenizer rather than approximating from characters is
        required, not fastidious — an approximation flags the wrong fields, which
        would make the truncation cost look measured when it is not.

        Raises:
            ModelNotAvailableError: `tokenizers` is not installed.
        """
        if not self.is_available():
            raise ModelNotAvailableError("tokenizers is required to count tokens")
        if self._counting_tokenizer is None:
            import tokenizers

            counter = tokenizers.Tokenizer.from_file(str(self._tokenizer_path()))
            counter.no_truncation()
            counter.no_padding()
            self._counting_tokenizer = counter
        return len(self._counting_tokenizer.encode(text).ids)

    def _tokenizer_path(self) -> Path:
        path = self._models_dir / "tokenizer.json"
        if not path.exists():
            self.ensure_model()
        return path
