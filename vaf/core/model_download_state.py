# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Process-wide signal for an in-progress local model download (a GGUF, or a vLLM model).

The tray (activity thread) and the uvicorn web worker (`headless_runner`) run in the SAME process on
different threads, so they otherwise race when the first prompt triggers a download: the worker would
start a server against a not-yet-downloaded file. This tiny thread-safe singleton lets any in-process
thread tell that a download is underway (and read its progress) instead of racing it.

Cross-PROCESS integrity (two VAF processes, e.g. tray + `vaf run`) is handled separately by a
``filelock`` around the actual download in ``vaf.core.backend.ensure_model_available``; this object is
only the in-process coordination/progress signal.
"""

from __future__ import annotations

import threading


class DownloadCancelled(InterruptedError):
    """A download ended by ``MODEL_DOWNLOAD.request_cancel`` (the banner's Cancel)."""


class _ModelDownloadState:
    """Thread-safe snapshot of the current model download (at most one at a time, serialized by the
    download filelock). All access goes through the internal lock."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active = False
        self._repo = ""
        self._filename = ""
        self._bytes_done = 0
        self._bytes_total = 0
        self._cancel = False
        self._cancels = 0

    def start(self, repo: str, filename: str) -> None:
        with self._lock:
            self._active = True
            self._repo = repo or ""
            self._filename = filename or ""
            self._bytes_done = 0
            self._bytes_total = 0
            self._cancel = False

    def update(self, bytes_done: int, bytes_total: int) -> None:
        with self._lock:
            self._bytes_done = int(bytes_done or 0)
            if bytes_total:
                self._bytes_total = int(bytes_total)

    def finish(self) -> None:
        with self._lock:
            self._active = False
            # A request belongs to the download it was made for; none outlives it.
            self._cancel = False

    @property
    def active(self) -> bool:
        with self._lock:
            return self._active

    def request_cancel(self) -> bool:
        """Ask the running download to end (its progress hook raises ``DownloadCancelled`` at
        the next tick). False, and nothing remembered, when none runs: a request must not end
        the next download. A caller that wants to know whether ITS download was cancelled
        compares ``cancels`` before and after it, so a cancel of an earlier download never
        answers for a later failure."""
        with self._lock:
            if not self._active:
                return False
            self._cancel = True
            return True

    @property
    def cancel_requested(self) -> bool:
        with self._lock:
            return self._cancel

    def note_cancelled(self) -> None:
        """Count a cancel the progress hook honoured (it raises right after)."""
        with self._lock:
            self._cancels += 1

    @property
    def cancels(self) -> int:
        """How many downloads a cancel has ended in this process."""
        with self._lock:
            return self._cancels

    def snapshot(self) -> dict:
        """A consistent copy for readers (the worker status push, load_model's wait loop, the WS layer)."""
        with self._lock:
            total = self._bytes_total
            done = self._bytes_done
            pct = round(done / total * 100, 1) if total else 0.0
            return {
                "active": self._active,
                "repo": self._repo,
                "filename": self._filename,
                "bytes_done": done,
                "bytes_total": total,
                "pct": pct,
            }


# The single shared instance.
MODEL_DOWNLOAD = _ModelDownloadState()


def make_state_tqdm(should_abort=None):
    """Return a ``tqdm`` subclass usable as ``hf_hub_download(tqdm_class=...)`` that mirrors byte
    progress into :data:`MODEL_DOWNLOAD`. Mirrors the pattern of ``make_progress_tqdm`` in
    ``web_server.py`` but writes to the shared state instead of a per-websocket queue.
    ``should_abort`` is called on every tick and ends the download by raising
    (``inference_profile.download_guard``); a ``request_cancel`` ends it the same way."""
    from tqdm import tqdm as _tqdm

    state = MODEL_DOWNLOAD

    class _StateTqdm(_tqdm):  # type: ignore[misc]
        def __init__(self, *args, **kwargs):
            # huggingface_hub below 1.10 hands every progress class a `name` its own tqdm
            # subclass understands and plain tqdm refuses (TqdmKeyError), which failed the
            # download before its first byte; 1.10+ leaves it out for foreign classes.
            kwargs.pop("name", None)
            super().__init__(*args, **kwargs)

        def update(self, n=1):
            if should_abort is not None:
                should_abort()
            if state.cancel_requested:
                state.note_cancelled()
                raise DownloadCancelled("The download was cancelled.")
            res = super().update(n)
            try:
                state.update(self.n, self.total or 0)
            except Exception:
                pass
            return res

    return _StateTqdm
