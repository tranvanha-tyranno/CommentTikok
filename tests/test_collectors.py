import asyncio

import httpx
from unittest.mock import AsyncMock

from app import collectors
from app.classifier import classify
from app import classifier, db
from app.media import extract_attachments, download_attachments


def test_photo_and_sticker_keep_original_urls_without_author_avatar():
    raw = {'image_list': [{'crop_url': {'url_list': ['https://cdn/crop']}, 'origin_url': {'url_list': ['https://cdn/original', 'https://cdn/fallback']}}],
           'cmt_sticker_struct': {'animated_url': {'high_resolution_url': {'url_list': ['https://cdn/animated.webp']}}},
           'user': {'avatar_thumb': {'url_list': ['https://cdn/avatar']}}}
    items = extract_attachments(raw)
    assert [a['kind'] for a in items] == ['image', 'sticker']
    assert items[0]['urls'] == ['https://cdn/original', 'https://cdn/fallback', 'https://cdn/crop']
    assert 'avatar' not in str(items)


def test_media_download_fallback_and_reuse(tmp_path, monkeypatch):
    requests = []
    def respond(request):
        requests.append(str(request.url))
        if request.url.path == '/bad':
            return httpx.Response(403)
        return httpx.Response(200, headers={'content-type': 'image/webp'}, content=b'RIFF-test-image')
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: original(transport=httpx.MockTransport(respond), **kwargs))
    items = [{'kind': 'sticker', 'urls': ['https://p16.tiktokcdn.com/bad', 'https://p16.tiktokcdn.com/ok']}]
    asyncio.run(download_attachments(items, tmp_path))
    assert items[0]['status'] == 'downloaded'
    assert (tmp_path / items[0]['local_path']).read_bytes() == b'RIFF-test-image'
    asyncio.run(download_attachments(items, tmp_path))
    assert len(requests) == 2


def test_media_download_preserves_failure_and_rejects_unknown_host(tmp_path):
    items = [{'kind': 'image', 'urls': ['http://127.0.0.1/private'], 'url': 'http://127.0.0.1/private'}]
    asyncio.run(download_attachments(items, tmp_path))
    assert items[0]['status'] == 'failed'
    assert items[0]['url'] == 'http://127.0.0.1/private'
    assert not list(tmp_path.iterdir())


def test_tiktok_waits_for_manual_verification_in_same_page(monkeypatch):
    reason = AsyncMock(side_effect=['CAPTCHA', ''])
    monkeypatch.setattr(collectors, 'tiktok_block_reason', reason)
    monkeypatch.setattr(collectors.asyncio, 'sleep', AsyncMock())
    from unittest.mock import Mock
    page = Mock()
    page.is_closed.return_value = False
    event = AsyncMock()
    asyncio.run(collectors.wait_tiktok_access(page, {'visible_browser': True}, event))
    assert reason.await_count == 2
    assert any('cùng cửa sổ' in str(call) for call in event.call_args_list)
    page.close.assert_not_called()


def test_tiktok_headless_challenge_stops_without_waiting(monkeypatch):
    import pytest
    monkeypatch.setattr(collectors, 'tiktok_block_reason', AsyncMock(return_value='CAPTCHA'))
    sleep = AsyncMock()
    monkeypatch.setattr(collectors.asyncio, 'sleep', sleep)
    with pytest.raises(collectors.SessionBlocked):
        asyncio.run(collectors.wait_tiktok_access(AsyncMock(), {'visible_browser': False}, AsyncMock()))
    sleep.assert_not_awaited()


def test_tiktok_manual_verification_timeout(monkeypatch):
    import pytest
    from unittest.mock import Mock
    page = Mock()
    page.is_closed.return_value = False
    with pytest.raises(collectors.SessionBlocked):
        asyncio.run(collectors.wait_tiktok_access(page, {'visible_browser': True}, AsyncMock(), 'CAPTCHA', timeout=0))


def test_tiktok_opens_comment_tab_before_scrolling():
    comments = AsyncMock()
    comments.count.return_value = 0
    button = AsyncMock()
    button.is_visible.return_value = True
    candidate = AsyncMock()
    candidate.all.return_value = [button]
    page = AsyncMock()
    page.locator = lambda selector: comments
    page.get_by_role = lambda *args, **kwargs: candidate
    event = AsyncMock()
    asyncio.run(collectors.open_tiktok_comments(page, event))
    button.click.assert_awaited_once()
    assert any('Comments' in str(call) for call in event.call_args_list)


def test_tiktok_filters_other_videos_and_retains_reply_parent():
    comment = {'cid': 'reply', 'aweme_id': '123', 'reply_id': '0', 'text': 'Reply', 'user': {'unique_id': 'sample'}}
    result = collectors.normalize_tiktok_comment(comment, 'https://www.tiktok.com/@sample/video/123', 'Title', '123', 'parent')
    assert result['video_id'] == '123'
    assert result['parent_id'] == 'parent'
    assert collectors.normalize_tiktok_comment({**comment, 'aweme_id': '456'}, '', '', '123') is None


def test_youtube_reply_pagination_and_total_limit(monkeypatch):
    requests = []
    def respond(request):
        requests.append(request)
        if request.url.path.endswith('commentThreads'):
            return httpx.Response(200, json={'items': [{'snippet': {'topLevelComment': {'id': 'top', 'snippet': {'textOriginal': 'Top'}}, 'totalReplyCount': 101}}]})
        if request.url.params.get('pageToken') == 'second':
            return httpx.Response(200, json={'items': [{'id': 'reply2', 'snippet': {'textOriginal': 'Reply 2'}}]})
        return httpx.Response(200, json={'items': [{'id': 'reply1', 'snippet': {'textOriginal': 'Reply 1'}}], 'nextPageToken': 'second'})
    original_client = httpx.AsyncClient
    monkeypatch.setattr(collectors.httpx, 'AsyncClient', lambda **kwargs: original_client(transport=httpx.MockTransport(respond)))
    async def run(limit):
        async def event(*args):
            pass
        return [c async for c in collectors.youtube_comments({'url': 'https://youtu.be/abcdefghijk'}, {'max_comments': limit, 'include_replies': True}, event, 'dummy')]
    comments = asyncio.run(run(10))
    assert [c['external_id'] for c in comments] == ['top', 'reply1', 'reply2']
    assert comments[-1]['parent_id'] == 'top'
    assert len(requests) == 3
    assert len(asyncio.run(run(2))) == 2


def test_local_rules_do_not_guess_safety_label(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'DATA', tmp_path)
    monkeypatch.delenv('LLM_MODEL', raising=False)
    db.init_db()
    result = asyncio.run(classify('camera điện thoại đẹp', 'Review điện thoại'))
    assert result['domain_id'] == 'technology'
    assert result['safety_label'] is None
    assert result['confidence'] is None


def test_llm_validates_label_and_does_not_invent_confidence(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'DATA', tmp_path)
    db.init_db()
    with db.connect() as c:
        c.execute("INSERT INTO settings(key,value) VALUES('llm_model','test-model')")
    def respond(request):
        return httpx.Response(200, json={'choices': [{'message': {'content': '{"safety_label":0,"domain_id":"made-up","safety_confidence":8,"confidence":true}'}}]})
    original_client = httpx.AsyncClient
    monkeypatch.setattr(classifier.httpx, 'AsyncClient', lambda **kwargs: original_client(transport=httpx.MockTransport(respond)))
    result = asyncio.run(classify('Nội dung bình thường'))
    assert result['safety_label'] == 0
    assert result['domain_id'] == 'unknown'
    assert result['safety_confidence'] is None
    assert result['confidence'] is None
