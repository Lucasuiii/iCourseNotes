"""Concurrency primitives: thread pools, prefetch caches, audio downloader.

Owns three resource pools and coordinates work across them so the
LectureRunner can focus on per-lecture business logic.

  image_pool       20 workers   IO bound  (per-image HTTP)
  ocr_pool          8 workers   CPU bound (RapidOCR), gated by BoundedSemaphore(2)
  audio_downloader  2 slots     IO bound  (ffmpeg URL → audio.raw to disk)

The audio downloader is special: each "slot" hosts a running ffmpeg process
that writes f32le mono 16 kHz audio to a per-sub_id scratch file.  Transcriber
reads that file with tail-f semantics while ffmpeg is still writing — so the
network download isn't bottlenecked by ASR speed and the ASR isn't blocked
on download completion.  See ``AudioDownloader`` below.
"""

from __future__ import annotations

import os
import copy
import subprocess
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable, Optional

from src.runtime import config
from src.runtime.media_transport import SignedRangeRelay
from src.runtime.aac_ranges import AACRangeTransport, Limits, MediaTransportError
from src.runtime.aac_audio import AACDecodeProcess, FALLBACK_CODES, prepare_aac, duration_header
from src.runtime.audio_preparation import DecodeErrorScanner, record_decode_errors, startup_diagnostics
from src.api import icourse


# ── PrefetchCache (per-sub_id image bytes) ─────────────────────────────────

class PrefetchCache:
    """Per-lecture image-bytes pre-fetcher driven by the global image pool.

    schedule(client, course_id, sub_id) — fetch the PPT list (synchronous,
                                          a few paginated GETs), fire all
                                          image downloads into the pool,
                                          then return.  Idempotent.
    wait(sub_id) -> (items, images)   — block until every download for sub_id
                                          resolves.
    discard(sub_id)                    — drop the cached entry (release bytes).
    in_flight(sub_id)                  — number of unfinished futures.

    The reporter (passed at __init__) is called for per-image ticks so
    progress logging is throttled in one place.  ``reporter`` may be ``None``
    in tests that don't care about output.
    """

    def __init__(self, image_pool: ThreadPoolExecutor, reporter=None):
        self._image_pool = image_pool
        self._reporter = reporter
        self._lock = threading.Lock()
        # sub_id -> {"items": list[dict]|None, "futures": dict[int, Future]}
        self._cache: dict[str, dict] = {}

    def schedule(self, client, course_id: str, sub_id: str):
        sub_id = str(sub_id)
        with self._lock:
            if sub_id in self._cache:
                return
            self._cache[sub_id] = {"items": None, "futures": {}}

        try:
            ppt_items = client.get_ppt_list(course_id, sub_id)
        except Exception as e:
            if self._reporter:
                self._reporter.ppt_list_failed(type(e).__name__, str(e))
            ppt_items = []
        for idx, item in enumerate(ppt_items, start=1):
            item["page_num"] = idx

        if self._reporter and ppt_items:
            self._reporter.image_progress_start(sub_id, len(ppt_items))

        futures: dict[int, Future] = {}
        for item in ppt_items:
            futures[item["page_num"]] = self._image_pool.submit(
                self._download_one, client, item, sub_id,
            )

        with self._lock:
            self._cache[sub_id]["items"] = ppt_items
            self._cache[sub_id]["futures"] = futures

    def _download_one(self, client, item: dict, sub_id: str) -> bytes | None:
        """Image-pool worker body. Goes through the module-level
        ``icourse.fetch_ppt_image`` so tests can monkey-patch it."""
        try:
            return icourse.fetch_ppt_image(client, item)
        finally:
            if self._reporter:
                self._reporter.image_progress_tick(sub_id)

    def wait(self, sub_id: str) -> tuple[list[dict], dict[int, bytes]]:
        sub_id = str(sub_id)
        with self._lock:
            entry = self._cache.get(sub_id)
        if entry is None:
            return [], {}
        items = entry.get("items") or []
        images: dict[int, bytes] = {}
        for page_num, fut in entry.get("futures", {}).items():
            try:
                img = fut.result()
            except Exception as e:
                print(
                    f"    [Prefetch] page {page_num} download failed: "
                    f"{type(e).__name__}"
                )
                img = None
            if img is not None:
                images[page_num] = img
        return items, images

    def discard(self, sub_id: str) -> None:
        sub_id = str(sub_id)
        with self._lock:
            self._cache.pop(sub_id, None)
        if self._reporter:
            self._reporter.image_progress_abort(sub_id)


# ── AudioDownloader (per-sub_id ffmpeg → disk audio file) ──────────────────

@dataclass
class AudioHandle:
    """Reference to an audio-extraction job."""

    sub_id: str
    path: str          # disk file ffmpeg writes f32le mono 16 kHz to
    process: subprocess.Popen | AACDecodeProcess
    stderr_chunks: list[bytes]
    timeline_preserved: bool = False
    decode_error_counts: dict[str, int] = field(default_factory=dict)
    stderr_done: Optional[threading.Event] = None
    media_transport: Optional[SignedRangeRelay] = None


class _PendingSpawn:
    """Per-schedule placeholder stored in ``_active`` while the background
    spawn is still working.  A unique instance per ``schedule()`` call lets
    the spawn thread detect that its entry was ``release()``-d (or replaced)
    in the meantime and abort instead of resurrecting a zombie entry."""
    def __init__(self):
        self.cancelled = threading.Event()
        self.media_transport = None
        self.file_token = uuid.uuid4().hex


class _AudioSpawnCancelled(Exception):
    pass


class AudioDownloader:
    """Spawn-and-track concurrent, timestamp-preserving audio extractions.

    Supported AAC sources use verified bounded batches on FFmpeg stdin;
    unsupported sources use the existing signed MP4 range relay. FFmpeg
    writes decoded mono float32 audio straight to disk, independently of ASR
    consumption. Transcriber reads that file with tail-f semantics as chunks
    arrive. The AAC aggregate process also validates producer completion.

    Concurrency is bounded by ``max_concurrent`` (default 2: current lecture
    being transcribed + one pre-decoded for the next lecture).  ``schedule()``
    returns immediately; if all slots are taken the background spawn waits.
    """

    def __init__(self, audio_dir: str, max_concurrent: int = None,
                 reporter=None, *, audio_mode=None):
        self._dir = audio_dir
        self.max_concurrent = max_concurrent or config.VIDEO_DOWNLOAD_CONCURRENCY
        self._sem = threading.BoundedSemaphore(self.max_concurrent)
        # sub_id -> AudioHandle (ready) | _PendingSpawn (spawn in flight)
        self._active: dict[str, "AudioHandle | _PendingSpawn"] = {}
        self._lock = threading.Lock()
        self._reporter = reporter
        self.audio_mode = config.AUDIO_ACQUISITION if audio_mode is None else audio_mode
        if self.audio_mode not in ('aac_auto', 'mp4'):
            raise ValueError('Invalid audio acquisition mode')
        self._startup_failures = {}
        os.makedirs(self._dir, exist_ok=True)

    @property
    def active_count(self) -> int:
        with self._lock:
            return sum(
                1 for h in self._active.values()
                if isinstance(h, AudioHandle)
            )

    def schedule(self, client, course_id: str, sub_id: str, *, preserve_timestamps=False) -> None:
        """Reserve a slot for sub_id and spawn ffmpeg in the background.

        Returns immediately. If all slots are taken the spawn blocks in its
        background thread until a slot frees.  Idempotent — second call for
        the same sub_id is a no-op.
        """
        sub_id = str(sub_id)
        pending = _PendingSpawn()
        with self._lock:
            if sub_id in self._active:
                return
            self._startup_failures.pop(sub_id, None)
            self._active[sub_id] = pending

        threading.Thread(
            target=self._spawn_when_ready,
            args=(client, course_id, sub_id, pending, preserve_timestamps),
            name=f"audio-spawn-{sub_id}",
            daemon=True,
        ).start()

    def _pop_if_mine(self, sub_id: str, pending: _PendingSpawn) -> None:
        """Remove our pending entry — but only if it is still ours."""
        with self._lock:
            if self._active.get(sub_id) is pending:
                self._active.pop(sub_id, None)

    def _record_startup_failure(self, sub_id, pending, diagnostics):
        with self._lock:
            if self._active.get(sub_id) is not pending: return
            self._startup_failures[sub_id] = diagnostics
            while len(self._startup_failures) > 128:
                self._startup_failures.pop(next(iter(self._startup_failures)))

    def startup_failure(self, sub_id):
        with self._lock:
            return copy.deepcopy(self._startup_failures.get(str(sub_id), {}))

    def _spawn_when_ready(self, client, course_id: str, sub_id: str,
                          pending: _PendingSpawn, preserve_timestamps=False):
        transport = None
        def check_pending():
            with self._lock:
                if pending.cancelled.is_set() or self._active.get(sub_id) is not pending:
                    raise _AudioSpawnCancelled()
        try:
            while not self._sem.acquire(timeout=.05):
                check_pending()
            try:
                phase = 'media_lookup'
                check_pending()
                url = client.get_video_url(course_id, sub_id)
                check_pending()
                if not url:
                    from src.api.playback_diagnostics import attach_lookup
                    self._record_startup_failure(sub_id, pending,
                        attach_lookup(startup_diagnostics(phase), client, course_id, sub_id))
                    self._pop_if_mine(sub_id, pending)
                    self._sem.release()
                    return
                prepared_aac = None
                if preserve_timestamps and self.audio_mode == 'aac_auto':
                    phase = 'media_transport_start'
                    transport = AACRangeTransport(client, url, allow_session_refresh=True,
                        limits=Limits(seconds=5400, network_bytes=1_000_000_000, requests=10000),
                        timeout=(10, 15))
                    with self._lock:
                        if self._active.get(sub_id) is not pending: raise _AudioSpawnCancelled()
                        pending.media_transport = transport
                    check_pending()
                    try:
                        prepared_aac = prepare_aac(transport)
                    except MediaTransportError as error:
                        if error.code not in FALLBACK_CODES: raise
                        check_pending()
                        transport.start_mp4_fallback(error.code)
                    check_pending()
                    vpn_url, headers = transport.url, ''
                    network_options = ['-rw_timeout', '180000000']
                elif preserve_timestamps:
                    phase = 'media_transport_start'
                    transport = SignedRangeRelay(client,url,allow_session_refresh=True,
                                                 cache_bytes=16*1024*1024)
                    with self._lock:
                        if self._active.get(sub_id) is not pending: raise _AudioSpawnCancelled()
                        pending.media_transport = transport
                    transport.start()
                    check_pending()
                    vpn_url, headers = transport.url, ''
                    # Bounded range recovery, old-session probes and one 75s
                    # fresh authentication fit within the 180s network window.
                    # The existing 5-minute PCM-stall gate still applies.
                    network_options = ['-rw_timeout','180000000']
                else:
                    vpn_url, headers = client.get_stream_params(url)
                    check_pending()
                    network_options = ['-reconnect','1','-reconnect_streamed','1','-reconnect_delay_max','5']
                # A cancelled generation's cleanup cannot delete a replacement.
                path = os.path.join(self._dir, f"{pending.file_token}.raw")
                if os.path.exists(path):
                    os.remove(path)

                cmd = [
                    "ffmpeg", "-y",
                    "-headers", headers,
                    *network_options,
                    "-i", vpn_url,
                    "-vn",
                    # Raw PCM has no timestamps. Fill actual source timestamp
                    # gaps before discarding them so later ASR/visual offsets
                    # stay on the playback timeline. Do not invent a video tail.
                    *(["-af", "aresample=async=1:first_pts=0"] if preserve_timestamps else []),
                    "-ar", "16000",
                    "-ac", "1",
                    "-f", "f32le",
                    path,
                ]
                phase = 'decoder_spawn'
                check_pending()
                if prepared_aac is not None:
                    proc = AACDecodeProcess.spawn(transport, prepared_aac, path)
                else:
                    proc = subprocess.Popen(
                        cmd, stdout=subprocess.DEVNULL,
                        stderr=subprocess.PIPE,
                    )

                # Drain stderr so the pipe never deadlocks.  Keep last few KB
                # for diagnostics if ffmpeg dies.
                stderr_chunks: list[bytes] = ([duration_header(prepared_aac[0])]
                                              if prepared_aac is not None else [])
                decode_error_counts: dict[str, int] = {}
                error_scanner = DecodeErrorScanner(decode_error_counts)
                stderr_done = threading.Event()

                def _drain():
                    try:
                        for chunk in proc.stderr:
                            error_scanner.feed(chunk)
                            stderr_chunks.append(chunk)
                            if len(stderr_chunks) > 2048:
                                # Preserve the input header (Duration) and a
                                # bounded tail; errors have separate counters.
                                del stderr_chunks[64: -1024]
                    except Exception:
                        decode_error_counts['stderr_read_error'] = 1
                    finally:
                        try:
                            close = getattr(proc.stderr, 'close', None)
                            if callable(close): close()
                        finally:
                            stderr_done.set()

                threading.Thread(
                    target=_drain, name=f"audio-stderr-{sub_id}",
                    daemon=True,
                ).start()

                handle = AudioHandle(
                    sub_id=sub_id, path=path,
                    process=proc, stderr_chunks=stderr_chunks,
                    timeline_preserved=preserve_timestamps,
                    decode_error_counts=decode_error_counts,
                    stderr_done=stderr_done,
                    media_transport=transport,
                )

                # Install the handle — unless release() already removed our
                # pending entry (e.g. get() timed out and the caller gave
                # up).  In that case nobody will ever release this handle,
                # so kill the orphan ffmpeg here instead of letting it hold
                # a download slot for the rest of the run.
                with self._lock:
                    installed = self._active.get(sub_id) is pending
                    if installed:
                        self._active[sub_id] = handle
                if not installed:
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait()
                    if transport is not None: transport.close()
                    self._sem.release()
                    if os.path.exists(path):
                        try:
                            os.remove(path)
                        except OSError:
                            pass
                    return

                # Background monitor: release the semaphore slot when
                # ffmpeg exits.  We do NOT pop from _active here — that's
                # the caller's job (via release()).
                threading.Thread(
                    target=self._monitor, args=(handle,),
                    name=f"audio-monitor-{sub_id}", daemon=True,
                ).start()
                if self._reporter:
                    try: self._reporter.audio_prefetch_start(sub_id)
                    except Exception: pass  # Display must not orphan a decoder.
            except Exception as error:
                from src.api.playback_diagnostics import attach_lookup
                self._record_startup_failure(sub_id, pending,
                    attach_lookup(startup_diagnostics(phase, error, transport), client, course_id, sub_id))
                if transport is not None:
                    try: transport.close()
                    except Exception: pass  # Preserve the original spawn error.
                self._pop_if_mine(sub_id, pending)
                self._sem.release()
                raise
        except Exception as e:
            if self._reporter and not isinstance(e, _AudioSpawnCancelled) and not pending.cancelled.is_set():
                self._reporter.audio_prefetch_failed(sub_id, e)

    def _monitor(self, handle: AudioHandle):
        try:
            handle.process.wait()
        finally:
            try:
                if handle.media_transport is not None: handle.media_transport.close()
            finally:
                self._sem.release()

    def get(self, sub_id: str, timeout: float = 120.0) -> AudioHandle | None:
        """Block until ffmpeg has been spawned for sub_id; return its handle.

        Returns None if sub_id was never scheduled (or already released).
        Raises TimeoutError if the spawn never happens within ``timeout``.
        """
        sub_id = str(sub_id)
        deadline = time.time() + timeout
        while True:
            with self._lock:
                entry = self._active.get(sub_id)
                if entry is None:
                    return None
                if isinstance(entry, AudioHandle):
                    return entry
            if time.time() > deadline:
                raise TimeoutError(
                    f"audio download for {sub_id} did not start within "
                    f"{timeout}s — likely WebVPN session expired or "
                    f"get_video_url failed"
                )
            time.sleep(0.05)

    def release(self, sub_id: str) -> None:
        """Kill ffmpeg (if still alive) and delete the audio file.

        Called from LectureRunner Phase H once the lecture has been
        transcribed and summarized.  If the spawn is still pending, the
        entry is removed and the spawn thread aborts itself when it
        notices its token is gone.
        """
        sub_id = str(sub_id)
        with self._lock:
            self._startup_failures.pop(sub_id, None)
            handle = self._active.pop(sub_id, None)
            if isinstance(handle, _PendingSpawn): handle.cancelled.set()
        if isinstance(handle, _PendingSpawn):
            if handle.media_transport is not None: handle.media_transport.close()
            return
        if not isinstance(handle, AudioHandle):
            return
        proc = handle.process
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        if handle.media_transport is not None: handle.media_transport.close()
        # The monitor thread releases the semaphore on its own.
        if os.path.exists(handle.path):
            try:
                os.remove(handle.path)
            except OSError:
                pass

    def shutdown(self) -> None:
        """Kill every in-flight ffmpeg and wipe the scratch directory."""
        with self._lock:
            sub_ids = list(self._active.keys())
        for sub_id in sub_ids:
            self.release(sub_id)
        with self._lock: self._startup_failures.clear()


# ── Scheduler — single façade ──────────────────────────────────────────────

@dataclass
class ResourceSnapshot:
    cpu_pct: float
    ocr_busy: int
    ocr_target: int
    image_busy: int
    audio_busy: int


class Scheduler:
    """Single façade owning every concurrency primitive.

    LectureRunner gets one Scheduler.  Through it, every other layer talks
    to pools and prefetch caches by name — no module holds its own
    ThreadPoolExecutor.

    OCR concurrency is capped by a fixed BoundedSemaphore (2 permits).
    RapidOCR is single-threaded CPU-bound; more than 2 concurrent workers
    don't increase throughput on a 4-core runner and waste cycles on
    contention.  There is no dynamic adjustment — the simple fixed cap is
    both sufficient and easier to reason about.

    Lifecycle:
        scheduler = Scheduler(reporter=...)
        ... LectureRunner uses it ...
        scheduler.shutdown()        # drains pools, kills ffmpegs
    """

    def __init__(self, reporter):
        self._reporter = reporter
        self.image_pool = ThreadPoolExecutor(
            max_workers=config.IMAGE_WORKERS, thread_name_prefix="img",
        )
        self.ocr_pool = ThreadPoolExecutor(
            max_workers=config.OCR_MAX_WORKERS, thread_name_prefix="ocr",
        )
        self._ocr_sem = threading.BoundedSemaphore(
            config.OCR_MAX_TARGET
        )
        self.image_cache = PrefetchCache(self.image_pool, reporter=reporter)
        self.audio_downloader = AudioDownloader(
            audio_dir=config.AUDIO_DIR,
            max_concurrent=config.VIDEO_DOWNLOAD_CONCURRENCY,
            reporter=reporter,
        )

    def prefetch_lecture(self, client, course_id: str, sub_id: str,
                         *, audio: bool = True) -> None:
        """Schedule image (always) + audio (optional) prefetch for a future
        lecture.  Pass ``audio=False`` when the caller already knows
        transcription won't read the stream (cached or official
        transcript) — audio acquisition is a full pull of the lecture
        and would otherwise hold one of the two slots for nothing."""
        self.image_cache.schedule(client, course_id, sub_id)
        if audio:
            self.audio_downloader.schedule(client, course_id, sub_id, preserve_timestamps=True)

    def submit_ocr(self, fn: Callable, *args, **kwargs) -> Future:
        """Submit an OCR job.  Live concurrency is capped at
        OCR_MAX_TARGET (2) by a fixed BoundedSemaphore — no dynamic
        CPU-based adjustment since RapidOCR is single-threaded and
        never benefits from more than 2 concurrent workers on a 4-core
        runner."""
        def _wrapped():
            with self._ocr_sem:
                return fn(*args, **kwargs)
        return self.ocr_pool.submit(_wrapped)

    def shutdown(self) -> None:
        self.audio_downloader.shutdown()
        self.image_pool.shutdown(wait=True)
        self.ocr_pool.shutdown(wait=True)
