# Validation — 2026-11-05

## Comment attachments — 2026-11-07

- Recovered **10 attachments from 10 existing TikTok comments**; **10/10 successfully downloaded** from the original CDN URLs to `data/media/`. Images and animated stickers were both present in the native payloads. Avatar images are retained in `raw.user` and are not incorrectly treated as comment attachments.
- Added attachment schema migration, photo-only comment persistence, duplicate enrichment that preserves human-reviewed labels, authenticated file delivery, previews/download controls, attachment filter, and complete native payload export in CSV/JSONL. Files are stored separately from exports.
- **22 tests passed**, JavaScript syntax passed, and Chromium comment pagination fixture passed. New tests cover original/animated URL extraction, CDN fallback/cache, blocked hosts, photo-only persistence, reviewed-label preservation, file serving, attachment filtering and native payload export. Live video/audio comment attachments were not encountered; their URL parsing and supported download formats are implemented but not live verified.

## Live TikTok collection — 2026-11-07

- App worker job **#5**, source **#3**, completed against `https://www.tiktok.com/@honganh.ne/video/7696513876510248198`: **32 real records persisted in SQLite (23 root comments + 9 replies)**. All records match the requested video ID; every reply has its parent in the dataset. This is a sample, not a guarantee of all comments displayed by TikTok.
- TikTok challenged the session again after reopening the browser, even after manual verification in the login window. The collector now waits up to five minutes for the user to verify in the active visible collector window and continues in that same page. The user completed the CAPTCHA manually; no automated challenge solving was used. Headless runs still stop immediately on a challenge. Cancellation remains checked during the wait.
- Tests: **18 passed**; local Chromium pagination fixture passed. New tests cover resuming after manual verification, immediate headless blocking, and verification timeout.
- CSV and JSONL exports saved under `output/exports/tiktok-comments-2026-11-07.*`; both reloaded and verified to contain 32 nonempty, unique TikTok comments. No demo records included. Counts and job status saved in `output/exports/tiktok-validation-2026-11-07.json`.

Validated on Windows, Python 3.11.9, local server http://127.0.0.1:8765.

- `python -m pytest -q`: **11 passed**. Includes exact label codes, CLEAN=0 filtering, invalid-label rejection, audit, stale-version conflicts, atomic bulk updates, reviewed-label protection, deduplication, filtered export, CSV formula protection, cancel/retry/recovery, partial-job recovery, token/origin checks, YouTube reply pagination, conservative local classification, and LLM output validation.
- `node --check app/static/app.js`, Python compile: passed.
- Chromium CLI UI: loaded demo dataset, opened comment, selected domain, approved CLEAN=0, verified saved label/domain and audit entry after reload. Taxonomy definitions displayed as supplied. No console errors after adding favicon.
- Browser collector against a local HTML fixture: **2 comments**, title/context correct.
- **Live YouTube** through installed yt-dlp: **3 comments retrieved**, no user data written to application database by smoke script. This small smoke check does not establish sustained throughput or completeness.
- Desktop 1440px and mobile 390px taxonomy screenshots visually checked; no document overflow at 390px. Screenshots in ignored `output/playwright/`.
- App has 8 explicitly marked `demo` records loaded during UI validation. One demo has been approved to demonstrate the audit workflow. They can be filtered separately; no real TikTok data is preloaded.

Not verified in the initial build: live TikTok collection/login, real LLM endpoint/model accuracy, YouTube API credentials/quota, Docker execution, scheduled runs over long durations, large-volume performance. See the TikTok repair verification below for subsequent live testing. Website-specific interactions still require source validation. This is a local first version, not a proven distributed large-scale collection service.

The installed FastAPI/Starlette test client emits one deprecation warning about a future HTTPX transport change; all tests pass with the pinned `requirements.lock` environment.

## TikTok collection repair — 2026-11-05

Reproduced on the user's video `https://www.tiktok.com/@honganh.ne/video/7696513876510248198` using the application's persistent Chromium profile. The current layout opens You may like; no comment request is made until Comments is clicked. The list container has `overflow: visible`; its ancestor `DivCommentMain` owns the scroll bar. The original collector did neither operation correctly.

The repaired collector explicitly opens Comments, scrolls the actual scrollable ancestor, filters unrelated video responses, preserves reply parents, reports endpoint response counts, and writes minimal diagnostics on failure. Session-wide challenges stop the batch, and three consecutive TikTok failures stop the remaining URLs. A valid empty response is distinguished from missing or invalid responses.

- Live collector before the final reply-button deduplication adjustment: **30 records: 22 root comments + 8 replies**, all matching the requested video ID. No login/CAPTCHA bypass was used.
- Local Chromium regression fixture (`python scripts/smoke_tiktok_fixture.py`): **PASS**, opens the comment tab, triggers second-page loading by scrolling an ancestor, and excludes an unrelated video's comment.
- Updated automated tests: **15 passed** including comment-tab activation, video identity, reply parent linkage, batch stop after repeated failures, and validation of the exact TikTok URL used for manual recovery.
- Live application worker after restart: created disabled/manual source #2 with two supplied video URLs, job #3. The first video (`user5986682005595`) displayed a real CAPTCHA overlay; the worker correctly stopped immediately with `attention` and saved `data/diagnostics/tiktok-1793863429405.json` plus screenshot. No subsequent URL was attempted. This verified challenge detection and batch stopping, not successful unattended collection for this challenged video. The UI now provides **Mở TikTok xử lý** on TikTok jobs needing attention; it opens the last attempted URL in the same profile. The user must complete the challenge, close that browser, and retry the job.
