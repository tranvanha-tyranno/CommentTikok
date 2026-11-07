import asyncio
import json
import os
from datetime import datetime, timezone

from . import db
from .classifier import classify
from .collectors import NeedsAttention, SessionBlocked, browser_comments, discover, youtube_comments


class CancelledJob(Exception):
    pass


browser_lock = asyncio.Lock()
browser_state = {'open': False, 'message': ''}


def enqueue(source_id=None, kind='crawl'):
    with db.connect() as c:
        if c.execute("SELECT 1 FROM jobs WHERE source_id IS ? AND kind=? AND status IN ('queued','running')", (source_id, kind)).fetchone():
            return None
        job_id = c.execute('INSERT INTO jobs(source_id,kind,created_at) VALUES(?,?,?)', (source_id, kind, db.now())).lastrowid
        if source_id:
            c.execute('UPDATE sources SET last_enqueued=? WHERE id=?', (db.now(), source_id))
        return job_id


async def label_pending(job_id, ids=None):
    last = 0
    while True:
        await check(job_id)
        params = [last]
        sql = "SELECT * FROM comments WHERE id>? AND review_status='pending' AND trim(text)<>''"
        if ids is not None:
            sql += ' AND id IN (' + ','.join('?' for _ in ids) + ')'
            params.extend(ids)
        batch = db.rows(sql + ' ORDER BY id LIMIT 50', params)
        if not batch:
            return
        for row in batch:
            await check(job_id)
            result = await classify(row['text'], row['context'])
            with db.connect() as c:
                # Optimistic condition protects a concurrent human edit.
                c.execute("UPDATE comments SET domain_id=?,confidence=?,safety_label=?,safety_confidence=?,label_method=?,version=version+1 WHERE id=? AND version=? AND review_status='pending'",
                          (result['domain_id'], result.get('confidence'), result.get('safety_label'), result.get('safety_confidence'), result['method'], row['id'], row['version']))
            last = row['id']


async def check(job_id):
    row = db.one('SELECT cancel_requested FROM jobs WHERE id=?', (job_id,))
    if row and row['cancel_requested']:
        raise CancelledJob()


async def run_job(job):
    job_id = job['id']
    try:
        if job['kind'] == 'classify':
            await label_pending(job_id)
        else:
            source = db.one('SELECT * FROM sources WHERE id=?', (job['source_id'],))
            config = json.loads(source['config'])
            async def event(kind, message=''):
                await check(job_id)
                if kind == 'log':
                    db.log(job_id, message)
            async def crawl():
                videos = await discover(source, config, event)
                completed = set(json.loads(job['checkpoint']))
                failures = 0
                consecutive_failures = 0
                for video in videos:
                    await check(job_id)
                    if video['url'] in completed:
                        continue
                    db.log(job_id, 'Đang đọc: ' + video['url'])
                    try:
                        collector = youtube_comments(video, config, event, os.getenv('YOUTUBE_API_KEY', '')) if source['platform'] == 'youtube' else browser_comments(video, source, config, event)
                        async for comment in collector:
                            await check(job_id)
                            inserted = db.insert_comment(job_id, source['id'], comment)
                            if inserted and config['auto_classify'] and comment.get('text', '').strip():
                                row = db.one('SELECT id FROM comments WHERE platform=? AND external_id=?', (comment['platform'], comment['external_id']))
                                await label_pending(job_id, [row['id']])
                        completed.add(video['url'])
                        consecutive_failures = 0
                        with db.connect() as c:
                            c.execute('UPDATE jobs SET checkpoint=?,videos_done=videos_done+1 WHERE id=?', (json.dumps(sorted(completed)), job_id))
                    except (CancelledJob, asyncio.CancelledError):
                        raise
                    except SessionBlocked:
                        raise
                    except Exception as exc:
                        failures += 1
                        consecutive_failures += 1
                        db.log(job_id, safe_error(exc), 'error')
                        if source['platform'] == 'tiktok' and consecutive_failures >= 3:
                            raise NeedsAttention('Đã tạm dừng sau 3 URL TikTok lỗi liên tiếp. Xem chẩn đoán trong nhật ký trước khi chạy tiếp; dữ liệu đã có được giữ lại.')
                if failures:
                    raise NeedsAttention(f'{failures} video/URL lỗi. Dữ liệu đã lấy vẫn được giữ; dùng Tiếp tục để thử các URL chưa hoàn tất.')
            if source['platform'] in ('tiktok', 'website'):
                db.log(job_id, 'Đang chờ lượt sử dụng profile trình duyệt')
                async with browser_lock:
                    await check(job_id)
                    await crawl()
            else:
                await crawl()
        status, message = 'completed', 'Hoàn tất'
    except CancelledJob:
        status, message = 'cancelled', 'Đã dừng; dữ liệu đã thu thập được giữ lại'
    except NeedsAttention as exc:
        status, message = 'attention', str(exc)
    except asyncio.CancelledError:
        # The startup recovery will requeue unfinished work.
        raise
    except Exception as exc:
        status, message = 'failed', safe_error(exc)
    with db.connect() as c:
        c.execute('UPDATE jobs SET status=?,message=?,finished_at=? WHERE id=?', (status, message, db.now(), job_id))
    db.log(job_id, message, 'info' if status == 'completed' else 'warning')


def safe_error(exc):
    text = str(exc)
    for name in ('LLM_API_KEY', 'YOUTUBE_API_KEY', 'SIGNALDESK_TOKEN'):
        secret = os.getenv(name)
        if secret:
            text = text.replace(secret, '[redacted]')
    return text[:800] or type(exc).__name__


async def worker():
    while True:
        with db.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            row = c.execute("SELECT * FROM jobs WHERE status='queued' ORDER BY id LIMIT 1").fetchone()
            job = dict(row) if row else None
            if job:
                c.execute("UPDATE jobs SET status='running',started_at=?,message='Đang xử lý' WHERE id=?", (db.now(), job['id']))
        if job:
            await run_job(job)
        else:
            await asyncio.sleep(1)


async def scheduler():
    while True:
        for source in db.rows('SELECT * FROM sources WHERE enabled=1 AND schedule_minutes>0'):
            last = datetime.fromisoformat(source['last_enqueued'] or source['created_at'])
            if (datetime.now(timezone.utc) - last).total_seconds() >= source['schedule_minutes'] * 60:
                enqueue(source['id'])
        await asyncio.sleep(15)


async def open_login(url='https://www.tiktok.com/'):
    from playwright.async_api import async_playwright
    browser_state.update(open=True, message='Đang chờ profile trình duyệt')
    try:
        async with browser_lock:
            async with async_playwright() as p:
                context = await p.chromium.launch_persistent_context(str(db.DATA / 'browser-profile'), headless=False)
                browser_state['message'] = 'Đăng nhập hoặc hoàn tất xác minh trong cửa sổ TikTok, đóng cửa sổ rồi bấm Tiếp tục ở tác vụ.'
                await context.pages[0].goto(url, wait_until='domcontentloaded', timeout=45000)
                try:
                    while context.pages:
                        await asyncio.sleep(1)
                finally:
                    await context.close()
    except Exception as exc:
        browser_state['message'] = safe_error(exc)
    finally:
        browser_state['open'] = False
