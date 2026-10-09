"""Per-lecture state machine: prefetch → ASR → OCR drain → summarize → release.

One ``LectureRunner`` instance drives one lecture from "started" to either
"summary saved" or "deliberately skipped". Instances may be reused; all
lecture-specific evidence and budgets are reset before each run.

Phases (named like the original ``main.process_lecture`` for diff-friendly
log greps):

  A  short-circuit ``summary already exists`` → mark processed, return.
  B  ``PPTPipeline.submit`` with ``defer_ocr=True``: stages 1-3 (fetch,
     register, dedup) run inline; the OCR jobs are held on the returned
     ``PPTAsyncHandle`` and only enter the pool at drain time, so ASR in
     Phase D gets the CPU to itself.
  C  schedule **next** lecture's prefetch (images always; audio only when
     the next lecture will actually be ASR-transcribed) so its download
     overlaps with the current lecture's ASR.
  D  transcript: cached → local ASR → bounded Seed-ASR rescue. Official iCourse
     subtitles only warn about timeline differences and supplement substantial
     ASR gaps.
     For ASR, ``Scheduler.audio_downloader
     .get`` blocks for the ffmpeg spawn scheduled earlier, then
     ``Transcriber.transcribe_tail`` reads PCM from the disk file with
     tail-f semantics while ffmpeg keeps writing.
  E  ``handle.drain()`` submits the deferred OCR jobs and blocks for them.
  E1 reject consistently empty audio + visual material before the LLM.
  E2 ``PPTPipeline.prefetch_and_ocr`` spawns a background thread that
     collects + dedups + OCRs the next lecture's pages, overlapping with
     this lecture's LLM wait; leftovers are absorbed by the next
     ``submit``.
  F  ``bucketer.assemble`` builds the prompt; ``Summarizer.summarize``
     calls the LLM round-robin until one succeeds.
  G  persist ``update_summary``, ``mark_processed``, ``clear_error``.
  H  ``audio_downloader.release`` kills ffmpeg + deletes scratch file.

The runner never owns the WebVPN session check; the orchestrator
(LectureRunner's caller) calls ``_check_session`` between lectures so all
threads pick up refreshed cookies through the shared ``ICourseClient``.
"""

from __future__ import annotations

import time
import os
import json
from pathlib import Path
import subprocess
import tempfile
from typing import TYPE_CHECKING, Optional

from src.ai import bucketer
from src.ai import doubao_asr
from src.ai.segment_rescue import (
    MAX_CLOUD_CLIPS,
    MAX_CLOUD_SECONDS,
    merge_rescued_segments,
    select_weak_windows,
)
from src.pipeline.ppt_pipeline import PPTPipeline
from src.ai.transcriber import IncompleteAudioError, NoAudioStreamError
from src.runtime import config
from src.runtime.content_quality import assess_content_quality
from src.runtime.transcript_policy import (
    assess_official_transcript,
    supplement_asr_gaps,
)

if TYPE_CHECKING:
    from src.data.database import Database
    from src.api.icourse import ICourseClient
    from src.runtime.reporter import Reporter
    from src.runtime.scheduler import Scheduler
    from src.ai.summarizer import Summarizer
    from src.ai.transcriber import Transcriber


class LectureRunner:
    """Run one lecture end-to-end.  Construct once per orchestration session,
    call ``run`` for each lecture in order."""

    def __init__(self, client: "ICourseClient", db: "Database",
                 scheduler: "Scheduler", transcriber: "Transcriber",
                 summarizer: "Summarizer", reporter: "Reporter"):
        self._client = client
        self._db = db
        self._scheduler = scheduler
        self._transcriber = transcriber
        self._summarizer = summarizer
        self._reporter = reporter
        self._ppt = PPTPipeline(db, scheduler, reporter)
        self._reset_lecture_state()

    def _reset_lecture_state(self):
        """Keep evidence, terminology and cloud budgets within one lecture."""
        self._transcript_source = "unknown"
        self._asr_actual_duration = 0.0
        self._asr_expected_duration = 0.0
        self._cloud_seconds = 0.0
        self._cloud_windows = set()
        self._cloud_failed = False
        self._asr_audio_path = None
        self._automatic_glossary = None
        self._historical_terms = []
        self._cloud_term_sources = []
        self._qwen_review_material = {}

    # ── Public entry point ──────────────────────────────────────────────

    def run(self, course_id: str, course_title: str, lecture: dict,
            next_info: Optional[tuple[str, str]] = None, *,
            prepared_asr: dict | None = None, review_state: dict | None = None,
            checkpoint=None, prepared_ppt: bool = False) -> Optional[str]:
        """Process one lecture.  Returns the summary text or None.

        ``next_info``: ``(course_id, sub_id)`` of the next lecture, used to
        kick off its prefetch concurrently.  Pass ``None`` for the last
        lecture in the batch.
        """
        sub_id = str(lecture["sub_id"])
        self._reset_lecture_state()
        self._homework_course_id = str(course_id)
        self._homework_sub_id = sub_id
        self._prepared_asr = prepared_asr
        self._review_state = review_state
        self._checkpoint = checkpoint
        if prepared_asr is not None:
            from src.pipeline.prepared_lecture import validate_material
            validate_material(prepared_asr, course_id, sub_id)
        if review_state is not None:
            from src.ai.qwen_review_ledger import validate_ledger
            validate_ledger(review_state)
            self._qwen_review_material = review_state.get('material', {})
            self._cloud_term_sources = [v['cloud_text'] for v in self._qwen_review_material.get('variants', [])]
            self._cloud_seconds = review_state.get('seconds', 0)
            self._cloud_failed = review_state.get('failed', False)
            self._cloud_windows = {(a['interval']['start_ms'], a['interval']['end_ms'])
                                   for a in review_state.get('attempts', [])}
        self._transcriber.reset_lecture_state()
        if os.environ.get('AUTO_COURSE_TERMS','').lower() == 'true':
            from src.ai.automatic_glossary import AutomaticGlossary
            self._automatic_glossary = AutomaticGlossary(self._db,course_id)
            self._historical_terms = self._automatic_glossary.freeze(course_title, sub_id)['terms']
        if prepared_asr is not None:
            # The prepare job's immutable snapshot wins over later DB changes,
            # for Qwen, Doubao and suspicious-window selection alike.
            self._historical_terms = list(prepared_asr['recognition_terms'])
        from src.ai.course_glossary import course_terms
        self._transcriber.set_terms(self._historical_terms if prepared_asr is not None
                                   else self._historical_terms or course_terms(course_title))
        sub_title = lecture.get("sub_title", sub_id)
        date = lecture.get("date", "")
        t_start = time.time()
        self._reporter.lecture_start(course_title, sub_title, date)

        existing = self._db.get_lecture(sub_id)
        if existing and existing.get('deleted_at'):
            self._schedule_next(next_info)
            return None
        # ── Phase A — short-circuit if a summary already exists ─────────
        if self._has_summary(existing):
            self._reporter.lecture_skip_v2_done(
                sub_title, len(existing["summary"])
            )
            self._schedule_next(next_info)
            self._db.mark_processed(sub_id)
            self._db.clear_error(sub_id)
            # The return value feeds the email batch — suppress it when
            # this summary already went out so it isn't re-sent.
            if existing.get("emailed_at"):
                return None
            return existing["summary"]

        # ── Phase B — submit PPT pipeline (fetch + dedup, no OCR yet) ──
        # OCR is deferred (defer_ocr=True) so ASR in Phase D gets exclusive
        # CPU.  OCR will be submitted in Phase E (handle.drain()).
        if prepared_asr is not None or prepared_ppt:
            from types import SimpleNamespace
            ppt_handle = SimpleNamespace(drain=lambda: SimpleNamespace(failed=0))
        else:
            ppt_handle = self._ppt.submit(
                self._client, course_id, sub_id, defer_ocr=True,
            )

        # ── Phase C — schedule next lecture's prefetch ─────────────────
        # Done BEFORE ASR so the next audio download can start filling its
        # AudioDownloader slot while we transcribe.  Both audio + image
        # prefetches are idempotent so this is safe to call any time.
        self._schedule_next(next_info)

        # ── Phase D — ASR transcription ────────────────────────────────
        transcript, transcript_segments = self._get_transcript(
            existing, course_id, sub_id,
        )
        if transcript is None:
            # _get_transcript already logged + persisted the skip reason.
            # Still drain the PPT handle: with defer_ocr the OCR jobs are
            # only submitted at drain() time, so skipping it would leave
            # the pages 'pending' forever and force the retry run to redo
            # download + dedup from scratch.
            ppt_handle.drain()
            return None

        # ── Phase E — drain remaining OCR work ─────────────────────────
        ppt_stats = ppt_handle.drain()

        # ── Phase E1 — reject empty/uncertain material before the LLM ───
        # A complete long recording with almost no speech and no useful PPT
        # is terminal "No Content" (for example, the teacher never arrived).
        # Ambiguous technical cases are retried instead of being summarized.
        ppt_pages = self._db.get_done_ppt_pages(sub_id)
        transcript, transcript_segments = self._refine_unclear_transcript(
            transcript, transcript_segments, ppt_pages, course_title=course_title,
        )
        ppt_status_counts = self._db.get_ppt_status_counts(sub_id)
        ppt_uncertain = (
            ppt_status_counts.get("failed", 0)
            + ppt_status_counts.get("pending", 0)
        )
        quality = assess_content_quality(
            transcript=transcript,
            segments=transcript_segments,
            ppt_pages=ppt_pages,
            transcript_source=self._transcript_source,
            actual_audio_seconds=self._asr_actual_duration,
            expected_audio_seconds=self._asr_expected_duration,
            ppt_failed_count=max(ppt_stats.failed, ppt_uncertain),
        )
        if quality.action != "summarize":
            self._reporter.info(
                "    [Quality] Insufficient lecture material "
                f"(audio={self._asr_actual_duration / 60:.1f} min, "
                f"transcript={quality.transcript_chars} chars/"
                f"{quality.segment_count} segments, "
                f"PPT={quality.ppt_page_count} usable pages)."
            )
            self._release_audio(sub_id)
            if quality.action == "skip_no_content":
                self._reporter.info(
                    "    [SKIP] No effective lecture content detected; "
                    "summary and email suppressed."
                )
                self._db.update_transcript(sub_id, transcript)
                self._db.mark_processed(sub_id)
                self._db.clear_error(sub_id)
            else:
                self._reporter.info(
                    "    [RETRY] Material quality is uncertain; will retry."
                )
                self._db.clear_transcript(sub_id)
                self._db.update_error(
                    sub_id,
                    "content_quality",
                    "insufficient reliable lecture material",
                )
            return None

        # Persist only after the material has passed the gate. This preserves
        # ASR work across a later LLM failure without allowing a crash between
        # transcription and quality assessment to bypass the gate next run.
        self._db.update_transcript(sub_id, transcript)

        # ── Phase E2 — kick off next lecture's OCR (runs during LLM) ───
        # Images were prefetched in Phase C; prefetch_and_ocr spawns a
        # background thread that collects them, dedups and submits OCR,
        # so the whole thing genuinely overlaps with this lecture's LLM
        # wait.  Whatever isn't finished when the LLM returns is absorbed
        # by the next lecture's own PPTPipeline.submit().
        if next_info:
            next_course, next_sub = next_info
            self._ppt.prefetch_and_ocr(self._client, next_course, next_sub)

        # ── Phase F — bucketed-prompt LLM summary ──────────────────────
        if not transcript.strip():
            self._reporter.info("    Empty transcript, skipping summary.")
            self._release_audio(sub_id)
            self._db.mark_processed(sub_id)
            self._db.clear_error(sub_id)
            return None

        summary = self._summarize(
            sub_id, course_title, transcript, transcript_segments,
        )
        if summary is None:
            self._release_audio(sub_id)
            return None

        # ── Phase G — persist + clear errors ───────────────────────────
        self._db.mark_processed(sub_id)
        self._db.clear_error(sub_id)

        # ── Phase H — release audio resources (ffmpeg + file) ──────────
        self._release_audio(sub_id)

        elapsed = time.time() - t_start
        self._reporter.lecture_done(course_title, sub_title, elapsed)
        return summary

    # ── Internal helpers ────────────────────────────────────────────────

    @staticmethod
    def _has_summary(existing: dict | None) -> bool:
        return bool(
            existing
            and existing.get("summary")
        )

    def prefetch_first(self, course_id: str, sub_id: str) -> None:
        """Prefetch for the first lecture in the batch — same decision
        logic (skip the audio download when transcription won't need it)
        as the in-loop Phase C prefetch."""
        self._schedule_next((course_id, sub_id))

    def _schedule_next(self, next_info: Optional[tuple[str, str]]):
        if next_info is None:
            return
        next_course, next_sub = next_info
        # Image prefetch is always useful; the audio download — a full
        # ffmpeg pull of the lecture — only when the next lecture will
        # actually be ASR-transcribed.  Both are idempotent so repeated
        # invocations are no-ops; the audio side blocks on its semaphore
        # until a download slot frees.
        self._scheduler.prefetch_lecture(
            self._client, next_course, next_sub,
            audio=self._needs_audio(next_course, next_sub),
        )

    def _needs_audio(self, course_id: str, sub_id: str) -> bool:
        """Every uncached lecture needs audio, regardless of subtitle quality."""
        existing = self._db.get_lecture(sub_id)
        return not (existing and existing.get("transcript"))

    def _warn_official_tail(self, actual_seconds: float,
                            official: list[dict] | None) -> None:
        """Do not reject audio based solely on unreliable subtitle timing."""
        if not official or not actual_seconds:
            return
        official_tail = max(int(item.get("end_ms", 0)) for item in official) / 1000
        if official_tail - actual_seconds > max(120, actual_seconds * 0.05):
            self._reporter.info(
                "    [WARN] Official subtitle timeline exceeds audio; "
                "continuing with ASR."
            )

    def _warn_short_audio(self, actual: float, expected: float) -> None:
        """Keep a private-content-free warning for a moderate shortfall."""
        if expected > 0 and actual / expected < 0.90:
            self._reporter.info(
                "    [WARN] Audio shorter than media timeline "
                f"({actual / expected:.0%}); continuing with ASR."
            )

    def _get_transcript(self, existing: dict | None, course_id: str,
                        sub_id: str) -> tuple[Optional[str], Optional[list]]:
        """Return (transcript, segments) or (None, None) on skip.

        Local ASR takes priority; cloud rescue only reviews selected windows.
        Official subtitles are never used as the main transcript.
        """
        prepared = getattr(self, '_prepared_asr', None)
        if prepared is not None:
            self._transcript_source = ('hybrid_asr' if any(
                r.get('quality_state') == 'doubao_fallback' for r in prepared['full_chunks']) else 'local_asr')
            self._asr_actual_duration = prepared['audio_seconds']
            self._asr_expected_duration = prepared.get('media_seconds') or 0.0
            self._asr_audio_path = prepared.get('audio_path')
            self._transcriber.last_chunks = prepared['full_chunks']
            self._transcriber.last_vad_windows = prepared['vad_windows']
            self._transcriber.set_terms(prepared['recognition_terms'])
            return prepared['transcript'], prepared['segments']
        if existing and existing.get("transcript"):
            self._transcript_source = "cached"
            self._reporter.info(
                f"    Transcript exists "
                f"({len(existing['transcript'])} chars), "
                f"skipping transcription."
            )
            return existing["transcript"], None

        # Read official subtitles as lower-trust supporting evidence only.
        official: list[dict] | None = None
        if config.USE_OFFICIAL_TRANSCRIPT:
            try:
                official = self._client.get_transcript_segments(sub_id)
            except Exception as e:
                self._reporter.info(
                    f"    [Official transcript] unavailable: {type(e).__name__}"
                )

        # Pull the audio handle.  ``schedule`` is idempotent — usually the
        # previous lecture already kicked it off (Phase C), but for the
        # first lecture in the batch we still need to fire it ourselves.
        downloader = self._scheduler.audio_downloader
        downloader.schedule(self._client, course_id, sub_id, preserve_timestamps=True)
        try:
            handle = downloader.get(sub_id, timeout=120)
        except TimeoutError as e:
            self._reporter.info("    [SKIP] Audio download timed out.")
            self._db.update_error(sub_id, "transcribe", str(e))
            return None, None
        if handle is None:
            # AudioDownloader returns None when get_video_url() returned
            # None — i.e. the lecture has no playable video.  Record an
            # error so the lecture is retried (the video may appear later)
            # and then paused with a private notice after max_errors.
            # The "no_video" stage is a contract with the frontend, which
            # renders it as a gray "无视频" hint instead of a red failure.
            self._reporter.lecture_skip_no_video(
                existing.get("sub_title", sub_id) if existing else sub_id
            )
            self._db.update_error(sub_id, "no_video", "no playable video URL")
            return None, None

        try:
            transcript, segments = self._transcriber.transcribe_tail(
                handle.path, handle.process, handle.stderr_chunks,
            )
            self._transcript_source = "local_asr"
            self._asr_audio_path = handle.path
            self._asr_actual_duration = self._transcriber.last_audio_duration
            self._asr_expected_duration = (
                self._transcriber.last_media_duration or 0.0
            )
            self._warn_short_audio(
                self._asr_actual_duration, self._asr_expected_duration,
            )
            self._warn_official_tail(self._asr_actual_duration, official)
            if config.DOUBAO_ASR_API_KEY:
                weak = select_weak_windows(
                    self._transcriber.last_speech_windows,
                    self._asr_actual_duration,
                    # Leave clip slots for the context/PPT review when there
                    # is enough local text to make that review meaningful.
                    max_clips=(MAX_CLOUD_CLIPS // 2 if len(transcript) >= 200
                               else MAX_CLOUD_CLIPS),
                )
                if weak:
                    rescues, attempted, failed = doubao_asr.rescue_intervals_pcm(
                        handle.path, config.DOUBAO_ASR_API_KEY, weak,
                        **({'hotwords':self._historical_terms} if self._automatic_glossary else {}),
                    )
                    self._cloud_term_sources.extend(s['text'] for _,result in rescues for s in result)
                    self._cloud_seconds = attempted
                    self._cloud_failed = failed
                    self._cloud_windows.update(
                        (w["start_ms"], w["end_ms"]) for w in weak
                    )
                    segments = merge_rescued_segments(segments, rescues)
                    transcript = " ".join(s["text"] for s in segments)
                    if any(result for _, result in rescues):
                        self._transcript_source = "hybrid_asr"
                    self._reporter.info(
                        f"    [ASR] Local-first; cloud rescue attempted "
                        f"{attempted:.1f}s/{MAX_CLOUD_SECONDS}s, "
                        f"recovered {sum(bool(result) for _, result in rescues)} "
                        f"of {len(weak)} weak clips"
                        + ("; stopped after cloud error" if failed else "")
                        + "."
                    )
                else:
                    self._reporter.info(
                        "    [ASR] Local-first; no weak speech clips "
                        "eligible for cloud rescue."
                    )
        except NoAudioStreamError as e:
            self._reporter.info("    [SKIP] Video-only (no audio stream).")
            # Do not mark this as processed: retry it like other media
            # failures, then surface it through the attention notice.
            self._db.update_error(sub_id, "no_audio", str(e))
            self._release_audio(sub_id)
            return None, None
        except IncompleteAudioError as e:
            # Other audio paths may still detect a genuine truncated stream.
            # Do not persist the partial transcript; retry on the next run.
            self._reporter.info(
                "    [SKIP] Incomplete audio "
                f"({e.actual_duration:.0f}s/{e.expected_duration:.0f}s); "
                "will retry next run."
            )
            self._db.update_error(sub_id, "transcribe", str(e))
            self._release_audio(sub_id)
            return None, None
        except Exception as e:
            self._reporter.info(
                f"    [FAIL] Transcription error: {type(e).__name__}"
            )
            self._db.update_error(sub_id, "transcribe", str(e))
            self._release_audio(sub_id)
            raise

        if official:
            mode, _ = assess_official_transcript(
                official, self._asr_actual_duration,
            )
            self._reporter.info(
                f"    [Official transcript] completeness={mode}; "
                "ASR remains primary."
            )
            segments = supplement_asr_gaps(
                segments, official, self._asr_actual_duration,
            )
            transcript = " ".join(s["text"] for s in segments)
        return transcript, segments

    def _refine_unclear_transcript(self, transcript, segments, ppt_pages,
                                   *, course_title=""):
        """Let the LLM propose existing speech windows within remaining quota."""
        remaining = MAX_CLOUD_SECONDS - self._cloud_seconds
        clips_left = MAX_CLOUD_CLIPS - len(self._cloud_windows)
        if (getattr(self, '_prepared_asr', None) is not None
                and getattr(self, '_review_state', None) is not None):
            return self._refine_qwen(transcript, segments, ppt_pages, remaining, clips_left)
        if (not config.DOUBAO_ASR_API_KEY or self._cloud_failed
                or not self._asr_audio_path or remaining <= 0 or clips_left <= 0
                or self._transcript_source not in ("local_asr", "hybrid_asr")
                or len(transcript.strip()) < 200):
            return transcript, segments
        chunks=getattr(self._transcriber,'last_chunks',None)
        if isinstance(chunks,list) and chunks:
            return self._refine_qwen(transcript,segments,ppt_pages,remaining,clips_left)
        options = {'terms':self._historical_terms} if self._automatic_glossary else {}
        suspects = self._summarizer.find_unclear_windows(
            self._transcriber.last_speech_windows, ppt_pages, self._cloud_windows,
            course_title=course_title, **options,
        )
        if not suspects:
            return transcript, segments
        rescues, attempted, failed = doubao_asr.rescue_intervals_pcm(
            self._asr_audio_path, config.DOUBAO_ASR_API_KEY, suspects,
            max_seconds=remaining, max_clips=clips_left,
            **({'hotwords':self._historical_terms} if self._automatic_glossary else {}),
        )
        self._cloud_term_sources.extend(s['text'] for _,result in rescues for s in result)
        self._cloud_seconds += attempted
        self._cloud_failed = failed
        merged = merge_rescued_segments(segments or [], rescues)
        if any(result for _, result in rescues):
            self._transcript_source = "hybrid_asr"
        self._reporter.info(
            f"    [ASR] LLM review rescue: {attempted:.1f}s attempted; "
            f"lecture total={self._cloud_seconds:.1f}s/{MAX_CLOUD_SECONDS}s; "
            f"recovered {sum(bool(result) for _, result in rescues)} clips"
            + ("; stopped after cloud error" if failed else "") + "."
        )
        return " ".join(segment["text"] for segment in merged), merged

    def _refine_qwen(self, transcript, segments, ppt_pages, remaining, clips_left):
        """Exact quotes + whole-block alignment; uncertain variants stay separate."""
        from src.ai.qwen_quality import review_quality
        from src.ai.qwen_audio_alignment import align_suspects
        if getattr(self, '_review_state', None) is not None:
            from src.ai.qwen_review_ledger import review_prepared
            material = review_prepared(self._prepared_asr, ppt_pages, self._summarizer,
                                       self._review_state, self._checkpoint, homework_ocr=self._homework_visual)
            self._qwen_review_material = material
            self._cloud_term_sources = [v['cloud_text'] for v in material.get('variants', [])]
            self._cloud_seconds = self._review_state.get('seconds', 0)
            self._cloud_failed = self._review_state.get('failed', False)
            weak = material.get('weak_rescues', [])
            if weak:
                segments = merge_rescued_segments(segments or [], weak)
                transcript = ' '.join(s['text'] for s in segments)
                self._transcript_source = 'hybrid_asr'
                self._prepared_asr['transcript'] = transcript
                self._prepared_asr['segments'] = segments
                self._cloud_term_sources.extend(s['text'] for _, result in weak for s in result)
            return transcript, segments
        try:
            report={'full_chunks':self._transcriber.last_chunks,
                    'vad_windows':self._transcriber.last_vad_windows}
            provider=self._summarizer.providers[0]
            selected=review_quality(self._summarizer._clients[provider['name']],provider['models'][0],
                report,{'ppt':ppt_pages},max_suspects=min(MAX_CLOUD_CLIPS,clips_left),input_budget=96000)
            if not selected: return transcript,segments
            with tempfile.TemporaryDirectory(prefix='icourse-qwen-align-') as tmp:
                wav=Path(tmp)/'speech.wav'
                subprocess.run(['ffmpeg','-nostdin','-v','error','-f','f32le','-ar','16000','-ac','1',
                    '-i',self._asr_audio_path,'-y',str(wav)],stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,check=True,timeout=120)
                intervals,located,unresolved,_=align_suspects(report,selected,wav,lambda _:None,budget=remaining)
            rescues,attempted,failed=doubao_asr.rescue_intervals_pcm(self._asr_audio_path,
                config.DOUBAO_ASR_API_KEY,intervals,max_seconds=remaining,max_clips=clips_left,
                hotwords=self._historical_terms or self._transcriber._terms)
            self._cloud_seconds+=attempted;self._cloud_failed=failed
            variants=[]
            for interval,result in rescues:
                if not any(s['start_ms']<interval['quote_end_ms'] and interval['quote_start_ms']<s['end_ms'] for s in result):
                    continue
                cloud=' '.join(s['text'] for s in result)
                self._cloud_term_sources.append(cloud)
                variants.append({'original_quote':interval['text'],'cloud_text':cloud})
            self._qwen_review_material={'variants':variants,'unresolved':unresolved}
            self._reporter.info(f'    [Qwen review] located={len(located)}, unresolved={len(unresolved)}, cloud={attempted:.1f}s')
        except Exception as error:
            self._reporter.info(f'    [WARN] Qwen review unavailable: {type(error).__name__}; preserving local text')
        return transcript,segments

    def _homework_visual(self, candidates, intervals):
        from src.pipeline.homework_visual import collect_visual_evidence
        reader = None
        state, checkpoint = self._review_state, self._checkpoint
        if isinstance(state, dict) and callable(checkpoint):
            ledger = state.setdefault('homework', {}).setdefault('vision_calls', [])
            reader = self._summarizer.homework_image_reader(ledger, checkpoint)
        client = self._client
        if client is None:
            # The gather runner normally needs no login. Create a scoped
            # read-only session only when assignment evidence was detected.
            from main import login_with_retry
            from src.api.icourse import ICourseClient
            client = ICourseClient(login_with_retry())
        return collect_visual_evidence(client, self._homework_course_id, self._homework_sub_id,
                                       candidates, intervals,
                                       vision_reader=reader,
                                       audio_seconds=(getattr(self, '_prepared_asr', None) or {}).get('audio_seconds'))

    def _summarize(self, sub_id: str, course_title: str, transcript: str,
                   transcript_segments: list[dict] | None) -> Optional[str]:
        try:
            kept_pages = self._db.get_done_ppt_pages(sub_id)
            prompt_text, mode = bucketer.assemble(
                transcript, transcript_segments, kept_pages,
            )
            if getattr(self, '_prepared_asr', None) and self._prepared_asr.get('official_support'):
                prompt_text += ('\n\n官方字幕辅助材料（低可信度；不得覆盖 Qwen 转写，不得据此补写未识别的课堂内容）：\n'
                                +json.dumps(self._prepared_asr['official_support'], ensure_ascii=False))
            if self._qwen_review_material:
                general_review = {k: v for k, v in self._qwen_review_material.items() if k != 'homework'}
                prompt_text += ('\n\n局部云端复核版本（不保证正确，不得无条件替换原文；未解决疑点不得编造）：\n'
                                +json.dumps(general_review,ensure_ascii=False))
            from src.ai.homework_review import assignment_candidates, prioritize_candidates, homework_prompt, ensure_homework_notice
            homework = self._qwen_review_material.get('homework', {})
            if not homework.get('candidates'):
                chunks = (getattr(self, '_prepared_asr', None) or {}).get('full_chunks') or getattr(self._transcriber, 'last_chunks', [])
                if isinstance(chunks, list):
                    homework = {'candidates': prioritize_candidates(assignment_candidates(chunks)), 'review_unavailable': True}
            prompt_text += homework_prompt(homework)
            self._reporter.info(
                f"    [Time] Generating summary at "
                f"{time.strftime('%H:%M:%S')}"
                f" — mode={mode}, prompt={len(prompt_text)} chars"
            )
            keywords = []
            if self._automatic_glossary:
                sources={'asr':[transcript], 'ppt':[p['text'] for p in kept_pages if p.get('text')],
                         'cloud':self._cloud_term_sources}
                summary, model_used, keywords = self._summarizer.summarize_with_keywords(
                    course_title,prompt_text,sources,self._historical_terms)
            else:
                summary, model_used = self._summarizer.summarize(course_title, prompt_text)
            summary = ensure_homework_notice(summary, homework)
            self._reporter.info(
                f"    [OK] Summary by {model_used}: {len(summary)} chars"
            )
            self._db.update_summary(sub_id, summary, model_used)
            if self._automatic_glossary:
                try:
                    self._automatic_glossary.save(sub_id, keywords, sources=sources,
                                                  frozen_terms=self._historical_terms)
                except Exception as error:
                    self._reporter.info(f'    [WARN] Keyword metadata not saved: {type(error).__name__}')
            return summary
        except Exception as e:
            self._reporter.info(
                f"    [FAIL] Summarization error: {type(e).__name__}"
            )
            self._db.update_error(sub_id, "summarize", str(e))
            raise

    def _release_audio(self, sub_id: str):
        try:
            self._scheduler.audio_downloader.release(sub_id)
        except Exception as e:
            self._reporter.info(
                f"    [WARN] audio release failed: {type(e).__name__}"
            )
