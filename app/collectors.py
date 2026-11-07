from __future__ import annotations

import asyncio
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
import time
from urllib.parse import parse_qs, quote, urlparse

import httpx

from .db import DATA
from .media import extract_attachments, download_attachments


class NeedsAttention(Exception):
    pass


class SessionBlocked(NeedsAttention):
    """A session-wide login/challenge must stop the batch, not fail every video."""


COMMENT_TEXT_SELECTOR = '[data-e2e="comment-level-1"], [data-e2e="comment-level-2"]'


async def tiktok_block_reason(page):
    for selector in ('[id*="captcha"]', '[class*="captcha-verify"]', 'iframe[src*="captcha"]'):
        for element in (await page.locator(selector).all())[:5]:
            if await element.is_visible():
                return 'TikTok yêu cầu xác minh CAPTCHA. Mở profile đăng nhập để xử lý rồi chạy tiếp.'
    text = (await page.locator('body').inner_text())[:20000]
    if re.search(r'(verify to continue|complete the puzzle|xác minh để tiếp tục)', text, re.I):
        return 'TikTok yêu cầu xác minh. Hoàn tất xác minh trong profile đăng nhập rồi chạy tiếp.'
    if re.search(r'(log in to (view|see) comments|đăng nhập để xem (các )?bình luận)', text, re.I):
        return 'TikTok yêu cầu đăng nhập để xem bình luận. Đăng nhập trong profile của ứng dụng rồi chạy tiếp.'
    return ''


async def wait_tiktok_access(page, config, event, reason=None, timeout=300):
    """Let the user verify in the current page without reopening the session."""
    reason = reason or await tiktok_block_reason(page)
    if not reason:
        return
    if not config['visible_browser']:
        raise SessionBlocked(reason)
    await event('log', 'Đang chờ tối đa 5 phút: hoàn tất CAPTCHA/đăng nhập ngay trong cửa sổ TikTok đang mở; giữ cửa sổ mở, tác vụ sẽ tự tiếp tục.')
    deadline = time.monotonic() + timeout
    while reason:
        await event('check')
        if page.is_closed() or time.monotonic() >= deadline:
            raise SessionBlocked(reason)
        await asyncio.sleep(1)
        reason = await tiktok_block_reason(page)
    await event('log', 'Đã hoàn tất xác minh; tiếp tục thu thập trong cùng cửa sổ.')


async def open_tiktok_comments(page, event, config=None):
    """The new TikTok layout defaults to recommendations, not the comment tab."""
    for attempt in range(40):
        await event('check')
        if await page.locator(COMMENT_TEXT_SELECTOR).count():
            return
        candidates = [
            page.get_by_role('button', name=re.compile(r'^(Comments|Bình luận)(\s*\(.*\))?$', re.I)),
            page.get_by_role('tab', name=re.compile(r'^(Comments|Bình luận)', re.I)),
            page.get_by_role('button', name=re.compile(r'(Read or add comments|Đọc.*bình luận)', re.I)),
            page.locator('[data-e2e="comment-icon"]'),
        ]
        for candidate in candidates:
            for button in (await candidate.all())[:3]:
                if await button.is_visible():
                    try:
                        await button.click(timeout=3000)
                        await event('log', 'Đã mở tab Comments; đang chờ TikTok trả bình luận.')
                        return
                    except Exception:
                        continue
        if attempt % 5 == 0:
            reason = await tiktok_block_reason(page)
            if reason:
                await wait_tiktok_access(page, config or {'visible_browser': False}, event, reason)
        await asyncio.sleep(.5)
    raise NeedsAttention('Không tìm thấy nút mở Comments sau 20 giây. Giao diện TikTok có thể đã thay đổi hoặc video không truy cập được.')


async def scroll_tiktok_comments(page):
    # The list itself has overflow:visible. Its ancestor owns the scroll bar.
    return await page.evaluate('''() => {
      const item = document.querySelector('[data-e2e="comment-level-1"], [data-e2e="comment-level-2"]');
      const fallback = document.querySelector('[class*="DivCommentMain"], [class*="DivCommentListContainer"], [data-e2e="comment-list"]');
      for (let n = item || fallback; n && n !== document.body; n = n.parentElement) {
        if (n.scrollHeight > n.clientHeight + 10 && /auto|scroll/.test(getComputedStyle(n).overflowY)) {
          n.scrollBy(0, Math.max(400, n.clientHeight * .8));
          return true;
        }
      }
      return false;
    }''')


def normalize_tiktok_comment(c, video_url, title, target_id, parent_hint=''):
    if not c.get('cid') or (c.get('aweme_id') and str(c['aweme_id']) != target_id):
        return None
    user = c.get('user') or {}
    parent = str(c.get('reply_id') or parent_hint or '')
    if parent == '0':
        parent = parent_hint
    return {'platform': 'tiktok', 'external_id': str(c['cid']), 'video_id': target_id,
            'video_url': video_url, 'context': title, 'parent_id': parent,
            'text': c.get('text', ''), 'author': user.get('unique_id', user.get('nickname', '')),
            'likes': c.get('digg_count', 0),
            'published_at': datetime.fromtimestamp(c['create_time'], timezone.utc).isoformat() if c.get('create_time') else None,
            'attachments': extract_attachments(c), 'raw': c}


async def save_tiktok_diagnostic(page, video_url, stats):
    directory = DATA / 'diagnostics'
    directory.mkdir(parents=True, exist_ok=True)
    name = f'tiktok-{int(time.time()*1000)}'
    # No cookies, request headers or signed URLs are saved.
    details = {'video_url': video_url, 'page_url': page.url.split('?')[0], 'title': await page.title(), **stats}
    (directory / (name + '.json')).write_text(json.dumps(details, ensure_ascii=False, indent=2), encoding='utf-8')
    try:
        await page.screenshot(path=str(directory / (name + '.png')), timeout=5000)
    except Exception:
        pass
    return str(Path('data/diagnostics') / (name + '.json'))


def youtube_id(value):
    u = urlparse(value)
    if u.hostname in ('youtu.be', 'www.youtu.be'):
        return u.path.strip('/').split('/')[0]
    query = parse_qs(u.query)
    if 'v' in query:
        return query['v'][0]
    match = re.search(r'/(?:shorts|embed|live)/([\w-]+)', u.path)
    if match:
        return match[1]
    if re.fullmatch(r'[\w-]{11}', value):
        return value
    raise ValueError('Cần URL video YouTube hoặc video ID hợp lệ')


async def discover(source, config, event):
    platform, query = source['platform'], source['query']
    if source['mode'] == 'urls':
        return [{'id': x.strip(), 'url': x.strip(), 'title': ''} for x in query.splitlines() if x.strip()][:config['max_videos']]
    if platform == 'youtube':
        def search():
            import yt_dlp
            with yt_dlp.YoutubeDL({'quiet': True, 'extract_flat': True, 'skip_download': True, 'socket_timeout': 30}) as ydl:
                result = ydl.extract_info(f"ytsearch{config['max_videos']}:{query}", download=False)
                return [{'id': e['id'], 'url': f"https://www.youtube.com/watch?v={e['id']}", 'title': e.get('title', '')}
                        for e in result.get('entries', []) if e]
        return await asyncio.to_thread(search)
    if platform == 'tiktok':
        from playwright.async_api import async_playwright
        async with async_playwright() as p:
            browser = await p.chromium.launch_persistent_context(str(DATA / 'browser-profile'), headless=not config['visible_browser'])
            try:
                page = browser.pages[0] if browser.pages else await browser.new_page()
                await page.goto('https://www.tiktok.com/search/video?q=' + quote(query), wait_until='domcontentloaded', timeout=45000)
                found = {}
                for _ in range(12):
                    await event('check')
                    for link in await page.locator('a[href*="/video/"]').evaluate_all('(nodes) => nodes.map(n => ({url:n.href,title:n.innerText}))'):
                        found[link['url']] = {**link, 'id': link['url']}
                    if len(found) >= config['max_videos']:
                        break
                    await page.mouse.wheel(0, 1400)
                    await asyncio.sleep(2)
                if not found:
                    raise NeedsAttention('TikTok chưa trả video. Mở trình duyệt đăng nhập trong Cài đặt rồi thử lại; có thể gặp CAPTCHA hoặc giới hạn khu vực.')
                return list(found.values())[:config['max_videos']]
            finally:
                await browser.close()
    raise ValueError('Website tùy chỉnh cần danh sách URL')


async def youtube_comments(video, config, event, api_key=''):
    vid = youtube_id(video['url'])
    if api_key:
        async with httpx.AsyncClient(timeout=30) as client:
            token, count = '', 0
            async def request(endpoint, params):
                for attempt in range(3):
                    r = await client.get('https://www.googleapis.com/youtube/v3/' + endpoint, params={**params, 'key': api_key})
                    if r.status_code not in (429, 500, 502, 503) or attempt == 2:
                        if r.is_error:
                            raise RuntimeError('YouTube API từ chối yêu cầu; kiểm tra key, quota hoặc trạng thái bình luận.')
                        return r.json()
                    await asyncio.sleep(2 ** attempt)
            def normalize(item, parent=''):
                s = item['snippet']
                return {'platform': 'youtube', 'external_id': item['id'], 'video_id': vid, 'video_url': video['url'],
                        'context': video.get('title', ''), 'parent_id': parent, 'text': s.get('textOriginal', s.get('textDisplay', '')),
                        'author': s.get('authorDisplayName', ''), 'likes': s.get('likeCount', 0), 'published_at': s.get('publishedAt'), 'raw': item}
            while count < config['max_comments']:
                await event('check')
                data = await request('commentThreads', {'part': 'snippet', 'videoId': vid, 'maxResults': 100, 'pageToken': token, 'textFormat': 'plainText'})
                for thread in data.get('items', []):
                    top = thread['snippet']['topLevelComment']
                    yield normalize(top)
                    count += 1
                    if config['include_replies'] and thread['snippet'].get('totalReplyCount') and count < config['max_comments']:
                        rt = ''
                        while True:
                            await event('check')
                            replies = await request('comments', {'part': 'snippet', 'parentId': top['id'], 'maxResults': 100, 'pageToken': rt, 'textFormat': 'plainText'})
                            for reply in replies.get('items', []):
                                yield normalize(reply, top['id'])
                                count += 1
                                if count >= config['max_comments']:
                                    break
                            rt = replies.get('nextPageToken', '')
                            if not rt or count >= config['max_comments']:
                                break
                    if count >= config['max_comments']:
                        break
                token = data.get('nextPageToken', '')
                if not token:
                    break
        return
    # Isolate yt-dlp in a subprocess so cancelling a job can terminate the extraction.
    import sys
    args = [sys.executable, '-m', 'yt_dlp', '--skip-download', '--write-comments', '--dump-single-json', '--no-warnings', '--no-progress',
            '--socket-timeout', '25', '--retries', '2', '--extractor-args',
            f"youtube:max_comments={config['max_comments']};comment_sort=new", f'https://www.youtube.com/watch?v={vid}']
    process = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    task = asyncio.create_task(process.communicate())
    try:
        while not task.done():
            await asyncio.wait({task}, timeout=1)
            await event('check')
        stdout, stderr = await task
        if process.returncode:
            raise NeedsAttention('YouTube chưa thu thập được: ' + stderr.decode(errors='replace')[-350:])
        info = json.loads(stdout)
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
        if not task.done():
            task.cancel()
    count = 0
    for c in info.get('comments') or []:
        parent = c.get('parent', 'root')
        if not config['include_replies'] and parent != 'root':
            continue
        yield {'platform': 'youtube', 'external_id': str(c['id']), 'video_id': vid, 'video_url': video['url'],
               'context': info.get('title', ''), 'parent_id': '' if parent == 'root' else str(parent), 'author': c.get('author') or '',
               'text': c.get('text', ''), 'likes': c.get('like_count') or 0,
               'published_at': datetime.fromtimestamp(c['timestamp'], timezone.utc).isoformat() if c.get('timestamp') else None, 'raw': c}
        count += 1
        if count >= config['max_comments']:
            break


async def browser_comments(video, source, config, event):
    from playwright.async_api import async_playwright
    platform = source['platform']
    seen, count, tasks = set(), 0, set()
    queue = asyncio.Queue()
    target_match = re.search(r'/(?:video|photo)/(\d+)', video['url'])
    target_id = target_match[1] if target_match else ''
    stats = {'responses': 0, 'empty_bodies': 0, 'http_errors': [], 'api_statuses': [], 'root_has_more': None}
    expanded = set()
    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(str(DATA / 'browser-profile'), headless=not config['visible_browser'])
        try:
            page = context.pages[0] if context.pages else await context.new_page()
            async def receive(response):
                if '/api/comment/list' in response.url:
                    try:
                        params = parse_qs(urlparse(response.url).query)
                        requested_id = (params.get('aweme_id') or params.get('item_id') or [''])[0]
                        if target_id and requested_id and requested_id != target_id:
                            return
                        stats['responses'] += 1
                        if not response.ok:
                            stats['http_errors'].append(response.status)
                            return
                        data = await response.json()
                        code = data.get('status_code', 0)
                        if code:
                            stats['api_statuses'].append(code)
                            return
                        is_reply = '/reply/' in urlparse(response.url).path
                        parent_hint = (params.get('comment_id') or [''])[0] if is_reply else ''
                        if not is_reply:
                            stats['root_has_more'] = bool(data.get('has_more'))
                        await event('log', f"TikTok {'replies' if is_reply else 'bình luận'}: HTTP {response.status}, nhận {len(data.get('comments') or [])} mục.")
                        for c in data.get('comments') or []:
                            await queue.put((c, parent_hint))
                    except Exception:
                        stats['empty_bodies'] += 1
            if platform == 'tiktok':
                def on_response(response):
                    t = asyncio.create_task(receive(response))
                    tasks.add(t)
                    t.add_done_callback(tasks.discard)
                page.on('response', on_response)
            await page.goto(video['url'], wait_until='domcontentloaded', timeout=45000)
            if platform == 'tiktok':
                # Short links redirect to a canonical video URL.
                if not target_id:
                    match = re.search(r'/(?:video|photo)/(\d+)', page.url)
                    target_id = match[1] if match else ''
                try:
                    await wait_tiktok_access(page, config, event)
                    await open_tiktok_comments(page, event, config)
                except NeedsAttention as exc:
                    path = await save_tiktok_diagnostic(page, video['url'], stats)
                    await event('log', 'Chẩn đoán đã lưu: ' + path)
                    raise
            title = await page.title()
            idle = 0
            for _ in range(config['scroll_rounds']):
                await event('check')
                await asyncio.sleep(config['delay_seconds'])
                before = count
                if platform == 'tiktok':
                    reason = await tiktok_block_reason(page)
                    if reason:
                        try:
                            await wait_tiktok_access(page, config, event, reason)
                        except SessionBlocked:
                            path = await save_tiktok_diagnostic(page, video['url'], stats)
                            await event('log', 'Chẩn đoán đã lưu: ' + path)
                            raise
                    while not queue.empty():
                        c, parent_hint = queue.get_nowait()
                        normalized = normalize_tiktok_comment(c, video['url'], title, target_id, parent_hint)
                        if not normalized:
                            continue
                        cid, parent = normalized['external_id'], normalized['parent_id']
                        if cid in seen or (parent and not config['include_replies']):
                            continue
                        seen.add(cid)
                        if normalized['attachments']:
                            await event('check')
                            await download_attachments(normalized['attachments'], DATA / 'media', context.request, video['url'])
                            saved = sum(a['status'] == 'downloaded' for a in normalized['attachments'])
                            await event('log', f'Tệp bình luận {cid}: lưu {saved}/{len(normalized["attachments"])} tệp; giữ URL và dữ liệu gốc cho mọi tệp.')
                        yield normalized
                        count += 1
                        if count >= config['max_comments']:
                            return
                    if config['include_replies']:
                        buttons = page.get_by_text(re.compile(r'^(View.*repl|Xem.*(trả lời|câu trả lời))', re.I))
                        clicked = 0
                        for button in await buttons.all():
                            if await button.is_visible():
                                # is_visible() also includes offscreen nodes; clicking those
                                # scrolls the panel backwards and repeats empty reply requests.
                                signature = await button.evaluate('''n => {
                                  const r=n.getBoundingClientRect();
                                  const hit=document.elementFromPoint(r.left+r.width/2,r.top+r.height/2);
                                  if(!hit || !(n.contains(hit)||hit.contains(n))) return null;
                                  const group=n.closest('[class*="DivCommentObjectWrapper"]') || n.parentElement;
                                  return group.innerText;
                                }''')
                                if not signature or signature in expanded:
                                    continue
                                try:
                                    await button.click(timeout=2000)
                                    expanded.add(signature)
                                    clicked += 1
                                except Exception:
                                    continue
                            if clicked >= 5:
                                break
                    await scroll_tiktok_comments(page)
                else:
                    selector = config.get('comment_selector')
                    if not selector:
                        raise ValueError('Website cần CSS selector cho từng bình luận')
                    for text in await page.locator(selector).all_inner_texts():
                        text = text.strip()
                        cid = hashlib.sha256((video['url'] + '\n' + text).encode()).hexdigest()
                        if cid in seen or not text:
                            continue
                        seen.add(cid)
                        yield {'platform': 'website', 'external_id': cid, 'video_url': video['url'], 'context': title, 'text': text}
                        count += 1
                        if count >= config['max_comments']:
                            return
                    await page.mouse.wheel(0, 1200)
                idle = idle + 1 if before == count else 0
                if idle >= 8:
                    break
            if not count:
                if platform == 'tiktok':
                    if stats['responses'] and stats['root_has_more'] is False and not stats['http_errors'] and not stats['api_statuses'] and not stats['empty_bodies']:
                        await event('log', 'TikTok đã trả danh sách bình luận rỗng (HTTP 200, status 0).')
                        return
                    path = await save_tiktok_diagnostic(page, video['url'], stats)
                    await event('log', 'Chẩn đoán đã lưu: ' + path)
                    raise NeedsAttention(f"Đã mở Comments nhưng chưa đọc được bình luận: {stats['responses']} phản hồi, {stats['empty_bodies']} body không đọc được, HTTP lỗi {stats['http_errors']}, mã API {stats['api_statuses']}. Xem tệp chẩn đoán.")
                raise NeedsAttention('Không nhận được bình luận. Kiểm tra CSS selector hoặc trạng thái trang.')
            if platform == 'tiktok' and stats['root_has_more']:
                raise NeedsAttention(f'Đã đọc {count} bình luận nhưng TikTok vẫn báo còn trang. Giữ dữ liệu và chạy tiếp để thử phần còn lại.')
            await event('log', f'Đã đọc {count} bình luận; không bảo đảm đủ toàn bộ bình luận của nền tảng.')
        finally:
            for t in tasks:
                t.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            await context.close()
