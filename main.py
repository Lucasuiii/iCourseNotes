"""iCourse Subscriber — top-level orchestration.

The runtime is split across cooperating components — Scheduler (pools +
resource monitor), Reporter (centralised logging), PPTPipeline, LectureRunner,
AudioDownloader.  This file does only orchestration:

  1. Build all components.
  2. Login + enumerate.
  3. Drive LectureRunner across the queued lectures.
  4. Email + bookkeeping.
  5. Shutdown.

Anything more interesting belongs in one of ``src/*`` modules.
"""

import sys
import time
import os
from collections import OrderedDict

from src.runtime import config
from src.runtime.enumeration import EnumerationResult
from src.runtime.session_rules import lecture_is_selected
from src.data.database import Database
from src.api.emailer import Emailer
from src.api.icourse import ICourseClient
from src.pipeline.lecture_runner import LectureRunner
from src.runtime.reporter import Reporter
from src.runtime.scheduler import Scheduler
from src.ai.summarizer import Summarizer
from src.ai.transcriber import Transcriber
from src.api.webvpn import WebVPNSession


def login_with_retry(max_attempts: int = 3) -> WebVPNSession:
    """Verify iCourse using bounded fresh sessions and outage probes."""
    from src.api.auth_recovery import initial_authenticated_session
    return initial_authenticated_session(max_attempts=max_attempts, factory=WebVPNSession,
                                         sleep=time.sleep)


def _check_session(client: ICourseClient) -> None:
    """Verify WebVPN session; re-login in place if expired.

    Mutates ``client`` so background workers holding the same instance
    automatically pick up refreshed cookies.
    """
    if client.check_alive():
        return
    print("[Session] WebVPN session expired, re-logging in...")
    client.vpn = login_with_retry()
    client._userinfo = None


def _in_run_scope(course_id: str, lecture: dict) -> bool:
    """Keep date reruns isolated from routine allowlists and unsent recovery."""
    if os.environ.get('PARALLEL_COURSE_SCOPE') == 'true' and str(course_id) not in config.COURSE_IDS:
        return False
    if not lecture_is_selected(course_id, lecture, {}, exclusions=config.COURSE_SESSION_EXCLUSIONS):
        return False
    if config.RERUN_TARGET_IDS:
        return str(lecture["sub_id"]) in config.RERUN_TARGET_IDS
    return lecture_is_selected(
        course_id, lecture, config.COURSE_SESSION_RULES,
        config.COURSE_SESSION_OVERRIDE_DATES,
        exclusions=config.COURSE_SESSION_EXCLUSIONS,
    )


def _enumerate_lectures(client: ICourseClient, db: Database,
                        reporter: Reporter) -> EnumerationResult:
    """Return tasks plus explicit successful/failed course scan outcomes.

    Sync, fast: list every (course_id, course_title, lecture) we'll
    process this run.  Done up-front so the prefetch loop can see across
    course boundaries when picking the "next" lecture."""
    result = EnumerationResult()
    for course_id in config.COURSE_IDS:
        try:
            _check_session(client)
            detail = client.get_course_detail(course_id)
            course_title = detail["title"]
            teacher = detail["teacher"]
            lectures = detail["lectures"]
            playback_count = sum(1 for l in lectures if l.get("has_playback"))
            reporter.course_header(
                course_id, course_title, teacher,
                total=len(lectures), playback=playback_count,
            )
            db.upsert_course(course_id, course_title, teacher)

            # School system sometimes lists duplicate lectures; dedup the
            # raw list so the same logic produces the same outcome each run.
            # When a sub_title appears more than once, keep the first one
            # that has playback; if none have playback, keep the first.
            seen_sub_titles: dict[str, dict] = {}
            deduped = []
            for lec in lectures:
                title = lec.get("sub_title", "")
                if not title:
                    deduped.append(lec)
                    continue
                existing = seen_sub_titles.get(title)
                if existing is None:
                    seen_sub_titles[title] = lec
                    deduped.append(lec)
                elif not existing.get("has_playback") and lec.get("has_playback"):
                    # replace the earlier no-playback entry in place so the
                    # processing order stays chronological
                    deduped[deduped.index(existing)] = lec
                    seen_sub_titles[title] = lec
                    reporter.course_dedup_skip(title, existing["sub_id"])
                else:
                    reporter.course_dedup_skip(title, lec["sub_id"])
            lectures = deduped

            selected_lectures = []
            filtered_count = 0
            for lecture in lectures:
                if not lecture.get("has_playback"):
                    continue
                if _in_run_scope(course_id, lecture):
                    selected_lectures.append(lecture)
                else:
                    filtered_count += 1
            if filtered_count:
                reporter.course_filter_skip(filtered_count)

            known_processed = db.get_processed_sub_ids(course_id)
            new_lectures = []
            for lec in selected_lectures:
                sub_id = str(lec["sub_id"])
                if sub_id in known_processed:
                    continue
                stored = db.get_lecture(sub_id)
                if stored and (stored.get("error_count") or 0) >= 3:
                    continue
                new_lectures.append(lec)
            unprocessed = db.get_unprocessed_lectures(course_id)
            new_ids = {str(lec["sub_id"]) for lec in new_lectures}
            retry_only = [
                {"sub_id": u["sub_id"], "sub_title": u["sub_title"],
                 "date": u["date"]}
                for u in unprocessed
                if u["sub_id"] not in new_ids
                and _in_run_scope(course_id, u)
            ]
            new_lectures.extend(retry_only)
            reporter.course_new_count(len(new_lectures))
            if not new_lectures:
                result.successful_courses.append(str(course_id))
                continue

            course_tasks = []
            for lecture in new_lectures:
                sub_id = str(lecture["sub_id"])
                db.insert_lecture(
                    sub_id, course_id,
                    lecture.get("sub_title", ""),
                    lecture.get("date", ""),
                )
                course_tasks.append((course_id, course_title, lecture))
            result.lectures.extend(course_tasks)
            result.successful_courses.append(str(course_id))
        except Exception as e:
            result.failed_courses.append(str(course_id))
            reporter.course_enumeration_error(course_id)
            reporter.info(f"  Error type: {type(e).__name__}")
    return result


def _drive_lectures(client: ICourseClient, db: Database,
                    scheduler: Scheduler, transcriber: Transcriber,
                    summarizer: Summarizer, reporter: Reporter,
                    all_lectures: list[tuple[str, str, dict]],
                    email_items: list) -> None:
    """Phase 2: run each lecture through LectureRunner.

    Pre-schedules the first lecture's prefetch (audio + images) before
    entering the loop; subsequent prefetches are kicked off from inside
    each LectureRunner.run via ``next_info``.
    """
    if not all_lectures:
        return

    runner = LectureRunner(
        client, db, scheduler, transcriber, summarizer, reporter,
    )

    first_course, _, first_lec = all_lectures[0]
    runner.prefetch_first(first_course, str(first_lec["sub_id"]))

    for i, (course_id, course_title, lecture) in enumerate(all_lectures):
        sub_id = str(lecture["sub_id"])
        next_info: tuple[str, str] | None = None
        if i + 1 < len(all_lectures):
            next_course, _, next_lec = all_lectures[i + 1]
            next_info = (next_course, str(next_lec["sub_id"]))

        _check_session(client)
        try:
            summary = runner.run(
                course_id, course_title, lecture, next_info=next_info,
            )
            if summary:
                email_items.append({
                    "sub_id": sub_id,
                    "course_id": course_id,
                    "course_title": course_title,
                    "sub_title": lecture.get("sub_title", sub_id),
                    "date": lecture.get("date", ""),
                    "summary": summary,
                })
        except Exception as e:
            reporter.lecture_error(sub_id)
            reporter.info(f"    Error type: {type(e).__name__}")
            db.update_error(sub_id, "pipeline", type(e).__name__)
        finally:
            # Belt-and-braces: drop any lingering prefetch entry for this
            # lecture so we don't leak bytes if the runner crashed before
            # PPTPipeline.submit released the cache.
            scheduler.image_cache.discard(sub_id)
            scheduler.audio_downloader.release(sub_id)


def _send_email(emailer: Emailer | None, db: Database, reporter: Reporter,
                email_items: list) -> None:
    """Append any previously-processed-but-unsent lectures, then send."""
    unsent = db.get_unsent_lectures()
    if unsent:
        seen_sub_ids = {item["sub_id"] for item in email_items}
        recovered_count = 0
        for row in unsent:
            if (
                row["sub_id"] not in seen_sub_ids
                and _in_run_scope(row["course_id"], row)
            ):
                email_items.append({
                    "sub_id": row["sub_id"],
                    "course_id": row["course_id"],
                    "course_title": row["course_title"],
                    "sub_title": row["sub_title"],
                    "date": row["date"],
                    "summary": row["summary"],
                })
                recovered_count += 1
        if recovered_count:
            reporter.email_recovered_unsent(recovered_count)

    if not (emailer and email_items):
        return

    # Send one message per course.  Use course_id as the grouping key so two
    # different courses with the same display title are never combined.
    courses: OrderedDict[str, list[dict]] = OrderedDict()
    for item in email_items:
        course_key = str(item.get("course_id") or item["course_title"])
        courses.setdefault(course_key, []).append(item)

    for course_items in courses.values():
        reporter.email_summary(len(course_items))
        try:
            if emailer.send(course_items):
                db.mark_emailed_batch(
                    [item["sub_id"] for item in course_items]
                )
            else:
                reporter.email_failed()
        except Exception as e:
            reporter.info(f"[Email] Failed to send ({type(e).__name__}).")


def _selected_attention_lectures(
    db: Database, only_unnotified: bool = True
) -> list[dict]:
    """Return paused failures still covered by the private course rules."""
    if not config.COURSE_IDS:
        return []
    rows = db.get_attention_lectures(
        list(config.COURSE_IDS), only_unnotified=only_unnotified
    )
    return [
        row for row in rows
        if lecture_is_selected(
            row["course_id"], row, config.COURSE_SESSION_RULES,
            exclusions=config.COURSE_SESSION_EXCLUSIONS,
        )
    ]


def _send_failure_notices(emailer: Emailer | None, db: Database,
                          reporter: Reporter) -> None:
    """Send one notice for newly-paused failures and mark only on success."""
    items = _selected_attention_lectures(db)
    if not items:
        return
    if emailer is None:
        reporter.info(
            f"[Attention] {len(items)} paused lecture(s); email is not configured."
        )
        return
    reporter.info(f"[Attention] Sending notice for {len(items)} lecture(s).")
    try:
        if emailer.send_failure_notice(items):
            db.mark_failure_notified_batch([row["sub_id"] for row in items])
        else:
            reporter.info("[Attention] Notice delivery failed; will retry next run.")
    except Exception as e:
        reporter.info(
            f"[Attention] Notice failed ({type(e).__name__}); will retry next run."
        )


def _crawl_semester_catalog(client: ICourseClient, db: Database,
                            reporter: Reporter) -> None:
    """Discover semesters on every run and fetch catalogs for new terms.

    Compare API term names with the names stored in ``all_courses``.
    Only terms with a successfully fetched catalog are considered known,
    so empty or failed fetches are retried on the next run.
    """
    reporter.info("Discovering available semesters from API...")
    try:
        _check_session(client)
        terms = client.discover_terms()
    except Exception as e:
        reporter.crawl_courses_failed("discovery", e)
        return

    if not terms:
        reporter.info("No semesters found via API discovery.")
        return

    reporter.info(f"Found {len(terms)} semester(s): "
                  f"{', '.join(t['name'] for t in terms)}")

    known_terms = db.list_catalog_terms()
    new_terms = [term for term in terms if term["name"] not in known_terms]
    if not new_terms:
        reporter.info("Skipping catalog crawl (no new semesters).")
        return

    for term_info in new_terms:
        code = term_info["code"]
        name = term_info["name"]
        expected = term_info["count"]
        reporter.crawl_courses_start(name)
        t0 = time.time()
        try:
            _check_session(client)
            rows = client.list_semester_courses(code)
            if not rows:
                reporter.info(f"  Term {name}: API returned 0 courses, skipping.")
                continue
            if len(rows) < expected:
                reporter.info(
                    f"  Term {name}: fetched {len(rows)} of {expected} courses, "
                    "deferring catalog update until the next run."
                )
                continue
            # Pass the human-readable term name (not the API code) to the
            # DB so the frontend displays "2025-20262" instead of "25".
            deleted, upserted = db.upsert_all_courses_for_term(name, rows)
            reporter.crawl_courses_done(
                name, len(rows), deleted, upserted, time.time() - t0,
            )
        except Exception as e:
            reporter.crawl_courses_failed(name, e)
            continue
        reporter.info(f"  ({code}) → {expected} API courses, "
                      f"{len(rows)} fetched")

    reporter.info("Semester catalog crawl complete.")


def run():
    """Single execution of the full pipeline."""
    reporter = Reporter()
    reporter.run_header()

    if not config.COURSE_IDS and not config.CRAWL_TERM:
        reporter.info(
            "No COURSE_IDS configured. Set COURSE_IDS to process lectures "
            "or leave empty for crawl-only mode."
        )
        # Fall through — crawl-only mode is valid.

    db = Database()
    if config.RERUN_TARGET_IDS and config.RETRY_ALL_FAILED:
        raise ValueError("date rerun cannot also retry unrelated failures")
    if config.RETRY_ALL_FAILED:
        attention = _selected_attention_lectures(db, only_unnotified=False)
        retried = db.retry_attention_lectures(
            [row["sub_id"] for row in attention]
        )
        reporter.info(
            f"[Attention] Re-armed {retried} paused lecture(s) for manual retry."
        )
    corrected = db.sync_dates_from_sub()
    if corrected:
        print(f"  [Date] Synced {corrected} lecture date(s) from sub_title", flush=True)
    transcriber = Transcriber()
    summarizer = Summarizer() if config.COURSE_IDS else None
    emailer = Emailer() if (
        config.SMTP_EMAIL and config.SMTP_PASSWORD and config.RECEIVER_EMAILS
        and os.environ.get('PARALLEL_COURSE_WORKER') != 'true'
    ) else None

    vpn = login_with_retry()
    client = ICourseClient(vpn)
    email_items: list = []

    # Discover new semesters every run; only fetch catalogs not yet stored.
    if not config.RERUN_TARGET_IDS and os.environ.get('PARALLEL_COURSE_WORKER') != 'true':
        _crawl_semester_catalog(client, db, reporter)

    if not config.COURSE_IDS:
        # Crawl-only mode: nothing to process, just persist + exit.
        reporter.info("\n[Crawl-only mode] No COURSE_IDS — skipping lectures.")
        reporter.run_footer()
        return

    scheduler = Scheduler(reporter=reporter)

    try:
        enumeration = _enumerate_lectures(client, db, reporter)
        _drive_lectures(
            client, db, scheduler, transcriber, summarizer, reporter,
            enumeration.lectures, email_items,
        )

    finally:
        scheduler.shutdown()

    if config.RERUN_TARGET_IDS:
        completed = {item["sub_id"] for item in email_items}
        if completed != config.RERUN_TARGET_IDS:
            raise RuntimeError("date rerun incomplete; original data retained")
    _send_email(emailer, db, reporter, email_items)
    if not config.RERUN_TARGET_IDS:
        _send_failure_notices(emailer, db, reporter)
    reporter.run_footer()
    enumeration.require_success()


if __name__ == "__main__":
    try:
        run()
    except Exception as exc:
        # Actions logs are public in a public fork. Exception messages and
        # tracebacks may contain signed URLs or course metadata.
        print(f"[Fatal] Run failed: {type(exc).__name__}", flush=True)
        sys.exit(1)
