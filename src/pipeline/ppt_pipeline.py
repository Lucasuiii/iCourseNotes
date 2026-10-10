"""Unified PPT fetch → dHash dedup → OCR → classify pipeline.

Two consumption patterns share the same four stages:

  PPTPipeline.submit(...)          → returns a ``PPTAsyncHandle`` that holds
                                     in-flight OCR ``Future`` s.  Caller (the
                                     LectureRunner) does ASR transcription in
                                     parallel, then calls ``handle.drain()`` to
                                     block for stats.  With ``defer_ocr=True``
                                     the OCR jobs are submitted by ``drain``
                                     itself, so ASR keeps the CPU to itself.
  PPTPipeline.prefetch_and_ocr(...) → same stages for the *next* lecture,
                                     kicked off while the current lecture
                                     waits on the LLM.  The next ``submit``
                                     call absorbs whatever it finished.

The four stages are identical in both paths (``_collect_survivors``):

  1. Fetch the PPT list from iCourse and ``INSERT OR IGNORE`` each row into
     ``ppt_pages`` with ``ocr_status='pending'``.  Idempotent — safe to call
     across resumed runs.  Items typically come from the prefetch cache the
     previous lecture warmed up; if absent, ``submit`` re-schedules.
  2. Collect image bytes for every still-pending page.  Pulls from
     ``Scheduler.image_cache`` (prefetch) first, then falls back to a sync
     ``fetch_ppt_image`` call for any pending row missing from the cache
     (typically a stale row from a prior interrupted run, or a download
     that failed in the prefetch pool).  Pages whose row is already
     done/invalid/failed/dedup_dropped are never re-collected, so their
     status can't be overwritten by a later pass.
  3. dHash dedup over the chronologically ordered pending pages — garbage-
     catalog matches and pairwise near-duplicates are stamped
     ``dedup_dropped`` and removed before OCR.
  4. For each survivor: submit an OCR job to ``Scheduler.submit_ocr`` (live
     concurrency is capped by a fixed BoundedSemaphore).  Workers classify
     the OCR'd text as ``invalid`` (matches one of the classroom-noise
     screens) or ``done``, and write the row in place.

``get_done_ppt_pages`` only surfaces rows with status='done', so dropped /
invalid / failed pages naturally drop out of the LLM prompt.
"""

from __future__ import annotations

import threading
from concurrent.futures import Future, as_completed
from dataclasses import dataclass
from typing import TYPE_CHECKING

from src.api import icourse
from src.ai.ocr import ocr_image_text
from src.ai.ppt_dedup import clean_ppt_text, compute_dhash, dedup_dhash, is_invalid_page, match_garbage

if TYPE_CHECKING:
    from src.data.database import Database
    from src.api.icourse import ICourseClient
    from src.runtime.reporter import Reporter
    from src.runtime.scheduler import Scheduler


@dataclass
class PPTStats:
    """Final accounting of one lecture's PPT pipeline run.

    ``done`` is the only count that feeds the LLM prompt; the other buckets
    are diagnostic only.
    """

    total: int       # total ``ppt_pages`` rows after registration
    inserted: int    # newly registered this run
    done: int        # OCR succeeded, text kept
    invalid: int     # matched a classroom-noise pattern
    dedupped: int    # dropped by garbage-catalog match or pairwise dHash dedup
    failed: int      # download or OCR error


class PPTAsyncHandle:
    """Caller-owned handle returned by ``PPTPipeline.submit``.

    Holds the list of in-flight OCR ``Future`` s plus the counts already
    known at submit-time (dedup losers, sync-fallback download failures).
    ``drain()`` blocks until every OCR future resolves and returns the
    aggregated ``PPTStats``.

    The handle is one-shot — calling ``drain()`` twice returns cached stats.
    """

    def __init__(self, pipeline: "PPTPipeline", sub_id: str,
                 *, total: int, inserted: int,
                 futures: list[Future], dedupped: int, presubmit_failed: int,
                 images: dict[int, bytes] | None = None):
        self._pipeline = pipeline
        self._sub_id = sub_id
        self._total = total
        self._inserted = inserted
        self._futures = futures
        self._dedupped = dedupped
        self._presubmit_failed = presubmit_failed
        self._images = images  # non-None when OCR was deferred
        self._ocr_submitted = False
        self._drained: PPTStats | None = None

    def drain(self) -> PPTStats:
        """Block until every OCR future resolves; return aggregate stats."""
        if self._drained is not None:
            return self._drained
        # If OCR was deferred (submit with defer_ocr=True), submit it now
        # so that drain blocks for the actual OCR work, not an empty list.
        if self._images and not self._ocr_submitted:
            self._ocr_submitted = True
            s = self._pipeline._scheduler
            if self._pipeline._reporter and self._images:
                self._pipeline._reporter.ocr_progress_start(
                    self._sub_id, len(self._images),
                )
            for page_num, img in self._images.items():
                self._futures.append(
                    s.submit_ocr(
                        self._pipeline._ocr_worker,
                        self._sub_id, page_num, img,
                    )
                )
            self._images = None  # release memory
        done = invalid = 0
        failed = self._presubmit_failed
        for fut in as_completed(self._futures):
            try:
                _page_num, status = fut.result()
            except Exception as e:
                print(f"      OCR worker exception: {type(e).__name__}",
                      flush=True)
                failed += 1
                continue
            if status == "done":
                done += 1
            elif status == "invalid":
                invalid += 1
            elif status == "failed":
                failed += 1
        stats = PPTStats(
            total=self._total, inserted=self._inserted,
            done=done, invalid=invalid,
            dedupped=self._dedupped, failed=failed,
        )
        self._drained = stats
        reporter = self._pipeline._reporter
        if reporter and (done or invalid or self._dedupped or failed):
            reporter.ppt_pipeline_summary(done, self._dedupped, invalid, failed)
        return stats


class PPTPipeline:
    """Drives the PPT pipeline against a ``Scheduler`` and a ``Database``."""

    def __init__(self, db: "Database", scheduler: "Scheduler",
                 reporter: "Reporter | None" = None):
        self._db = db
        self._scheduler = scheduler
        self._reporter = reporter
        # OCR futures submitted by prefetch_and_ocr (runs during LLM wait).
        # Keyed by sub_id; submit() drains them before starting ASR if the
        # pre-submitted OCR hasn't completed yet.
        self._prefetched_ocr: dict[str, list[Future]] = {}
        self._figure_cache: dict[str, list[dict]] = {}
        # The background threads driving prefetch_and_ocr, keyed by sub_id.
        # submit() joins the thread before reading _prefetched_ocr, which
        # is also what makes the dict handoff thread-safe.
        self._prefetch_threads: dict[str, threading.Thread] = {}

    def figure_frames(self, sub_id):
        return self._figure_cache.pop(str(sub_id), [])

    # ── Public entry points ─────────────────────────────────────────────

    def submit(self, client: "ICourseClient", course_id: str,
               sub_id: str, *, defer_ocr: bool = False) -> PPTAsyncHandle:
        """Stages 1-3 run inline; stage 4 (OCR) goes to the pool — either
        immediately, or at ``drain()`` time when ``defer_ocr=True``.

        Returns a handle so the caller can do ASR (or any other long
        parallel work) before draining.  After this call returns the
        prefetch cache entry has been ``discard``-ed; the OCR workers hold
        the image bytes they need via closure capture.
        """
        sub_id = str(sub_id)

        # If prefetch_and_ocr ran during the previous lecture's LLM phase,
        # wait for whatever it managed to start.  Pages it already OCR'd
        # are no longer 'pending'; anything it didn't get to still is, and
        # _collect_survivors picks those up below.
        self._join_prefetch(sub_id)

        images, c = self._collect_survivors(client, course_id, sub_id)
        if self._reporter and (c["inserted"] or c["total"]):
            self._reporter.ppt_pages_registered(c["total"], c["inserted"])

        # Stage 4 — optionally submit OCR.
        futures: list[Future] = []
        if not defer_ocr and images:
            if self._reporter:
                self._reporter.ocr_progress_start(sub_id, len(images))
            for page_num, img in images.items():
                futures.append(
                    self._scheduler.submit_ocr(
                        self._ocr_worker, sub_id, page_num, img,
                    )
                )
            images = {}  # release memory; workers hold closures

        self._scheduler.image_cache.discard(sub_id)

        return PPTAsyncHandle(
            self, sub_id,
            total=c["total"], inserted=c["inserted"], futures=futures,
            dedupped=c["dedupped"],
            presubmit_failed=c["failed"],
            images=images or None,
        )

    def prefetch_and_ocr(self, client: "ICourseClient", course_id: str,
                          sub_id: str) -> None:
        """Kick off download + dedup + OCR for a lecture in a background
        thread, without draining or discarding the prefetch cache.

        Designed to be called right before the LLM wait of the *previous*
        lecture: image collection, dHash and OCR all overlap with the API
        call instead of blocking the main thread.  Whatever hasn't finished
        by the time the LLM returns is simply absorbed by this lecture's
        own ``submit()`` — it joins the thread, drains the OCR futures,
        and processes any rows that are still pending.

        The prefetch cache is intentionally NOT discarded here — the
        real ``submit()`` call in Phase B of the next lecture handles that.
        """
        sub_id = str(sub_id)
        if sub_id in self._prefetch_threads:
            return
        t = threading.Thread(
            target=self._prefetch_worker,
            args=(client, course_id, sub_id),
            name=f"ppt-prefetch-{sub_id}",
            daemon=True,
        )
        self._prefetch_threads[sub_id] = t
        t.start()

    def _prefetch_worker(self, client: "ICourseClient", course_id: str,
                         sub_id: str) -> None:
        try:
            images, _ = self._collect_survivors(client, course_id, sub_id)
            if not images:
                return
            if self._reporter:
                self._reporter.ocr_progress_start(sub_id, len(images))
            self._prefetched_ocr[sub_id] = [
                self._scheduler.submit_ocr(self._ocr_worker, sub_id, pn, img)
                for pn, img in images.items()
            ]
        except Exception as e:
            if self._reporter:
                self._reporter.info(
                    f"    [Prefetch OCR] failed: {type(e).__name__}"
                )

    # ── Shared stages 1-3 ───────────────────────────────────────────────

    def _join_prefetch(self, sub_id: str) -> None:
        """Wait out a prior ``prefetch_and_ocr`` for sub_id: join its
        worker thread, then block for the OCR futures it submitted."""
        t = self._prefetch_threads.pop(sub_id, None)
        if t is not None:
            t.join()
        futs = self._prefetched_ocr.pop(sub_id, None)
        if not futs:
            return
        for fut in as_completed(futs):
            try:
                fut.result()
            except Exception:
                pass  # worker already persisted 'failed' and logged

    def _collect_survivors(
        self, client: "ICourseClient", course_id: str, sub_id: str,
    ) -> tuple[dict[int, bytes], dict]:
        """Stages 1-3: register pages, gather images for *pending* rows
        only, then garbage-catalog + pairwise dHash dedup.

        Returns ``(images, counters)`` where ``images`` maps page_num →
        bytes for exactly the pages that should be OCR'd next.  Pages whose
        row is already done/invalid/failed/dedup_dropped — from a previous
        run or a prefetch_and_ocr pass — never enter ``images``, so they
        can't be re-OCR'd and their status can't be overwritten (a dropped
        page must stay dropped).
        """
        sub_id = str(sub_id)

        # Stage 1 — register pending rows from the current PPT list.
        # ``schedule`` is idempotent; usually the previous lecture's
        # prefetch already warmed the cache.
        self._scheduler.image_cache.schedule(client, course_id, sub_id)
        ppt_items, cached = self._scheduler.image_cache.wait(sub_id)
        inserted = 0
        if ppt_items:
            inserted = self._db.insert_ppt_pages_pending(sub_id, ppt_items)
        total = self._db.count_total_ppt_pages(sub_id)

        # Stage 2 — image bytes for every still-pending row.  Cache first,
        # sync re-download for stale rows from a prior interrupted run.
        pending = self._db.get_pending_ppt_pages(sub_id)
        images: dict[int, bytes] = {}
        failed = 0
        for p in pending:
            pn = p["page_num"]
            img = cached.get(pn)
            if img is None:
                img = icourse.fetch_ppt_image(client, p)
            if img is None:
                self._db.update_ppt_page(sub_id, pn, None, "failed")
                failed += 1
            else:
                images[pn] = img

        # Stage 3 — dHash, then garbage catalog, then pairwise dedup on
        # the survivors.
        dhashes: list[str | None] = []
        page_at: list[int] = []
        for p in pending:
            pn = p["page_num"]
            img = images.get(pn)
            if img is None:
                continue
            dh = compute_dhash(img)
            self._db.update_ppt_page_dhash(sub_id, pn, dh)
            dhashes.append(dh)
            page_at.append(pn)

        garbage_idx = set(match_garbage(dhashes))
        survivor_items = [
            dh for i, dh in enumerate(dhashes) if i not in garbage_idx
        ]
        survivor_pages = [
            page_at[i] for i in range(len(page_at)) if i not in garbage_idx
        ]
        dropped_pages = {survivor_pages[i] for i in dedup_dhash(survivor_items)}
        dropped_pages |= {page_at[i] for i in garbage_idx}
        from src.pipeline.summary_figures import spread, _jpeg
        rows = [dict(p, seconds=p['created_sec']) for p in pending if p['page_num'] in images and p['page_num'] not in {page_at[i] for i in garbage_idx}]
        retained = []
        for page in spread(rows, 12):
            try:
                data = _jpeg(images[page['page_num']])
                if data:
                    retained.append({'image': data, 'seconds': page['created_sec'], 'source': 'platform_screenshot'})
            except Exception:
                pass
        if retained:
            self._figure_cache[sub_id] = retained
        for pn in dropped_pages:
            self._db.update_ppt_page(sub_id, pn, None, "dedup_dropped")
            images.pop(pn, None)


        return images, {
            "total": total, "inserted": inserted,
            "failed": failed, "dedupped": len(dropped_pages),
        }

    # ── Worker ──────────────────────────────────────────────────────────

    def _ocr_worker(self, sub_id: str, page_num: int,
                    image_bytes: bytes) -> tuple[int, str]:
        """OCR one image, classify, persist. Returns (page_num, status).

        Runs in the OCR pool (gated by a fixed BoundedSemaphore).  Database
        writes go through ``Database._lock`` so concurrent workers don't
        race on the same row.

        The reporter tick fires for every outcome (done/invalid/failed) so
        the printed page/s reflects total OCR throughput, not just
        successful pages — otherwise a run with many "invalid" classroom
        screens would look artificially slow.
        """
        try:
            text = ocr_image_text(image_bytes)
        except Exception as e:
            print(f"      page {page_num}: OCR error "
                  f"{type(e).__name__}", flush=True)
            self._db.update_ppt_page(sub_id, page_num, None, "failed")
            if self._reporter:
                self._reporter.ocr_progress_tick(sub_id)
            return page_num, "failed"
        # Strip UI chrome lines before storing so (a) the database holds
        # cleaned text and (b) is_invalid_page judges content, not chrome.
        cleaned = clean_ppt_text(text)
        status = "invalid" if is_invalid_page(cleaned) else "done"
        self._db.update_ppt_page(sub_id, page_num, cleaned, status)
        if self._reporter:
            self._reporter.ocr_progress_tick(sub_id)
        return page_num, status
