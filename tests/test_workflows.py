import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from app import db, engine
from app.main import app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'DATA', tmp_path)
    monkeypatch.delenv('SIGNALDESK_TOKEN', raising=False)
    monkeypatch.delenv('LLM_MODEL', raising=False)
    with TestClient(app) as client:
        for task in app.state.tasks:
            task.cancel()
        yield client


def seed(client, text='Sản phẩm này chưa tốt, giá cao.'):
    result = client.post('/api/import', json={'items': [{'text': text, 'platform': 'import', 'external_id': 'one'}]})
    assert result.status_code == 200
    return client.get('/api/comments').json()['items'][0]


def test_media_only_comment_export_download_and_review_protection(client):
    comment = {'platform': 'tiktok', 'external_id': 'photo-only', 'text': '',
               'raw': {'cid': 'photo-only', 'text': '', 'image_list': [{'origin_url': {'url_list': ['https://p16.tiktokcdn.com/photo']}}], 'text_extra': [{'user_id': 'mentioned'}]}}
    assert db.insert_comment(None, None, comment)
    row = client.get('/api/comments?media=yes').json()['items'][0]
    assert row['text'] == '' and len(row['attachments']) == 1
    assert client.get('/api/comments?media=no').json()['total'] == 0
    assert client.get('/api/media/%s/0' % row['id']).status_code == 404
    assert client.post('/api/review', json={'ids': [row['id']], 'safety_label': 1}).status_code == 200
    folder = db.DATA / 'media'
    folder.mkdir()
    (folder / 'test.webp').write_bytes(b'RIFF-test')
    comment['attachments'] = [{**row['attachments'][0], 'status': 'downloaded', 'local_path': 'test.webp', 'content_type': 'image/webp'}]
    assert not db.insert_comment(None, None, comment)
    detail = client.get('/api/comments/%s' % row['id']).json()
    assert detail['safety_label'] == 1 and detail['review_status'] == 'reviewed'
    assert client.get('/api/media/%s/0' % row['id']).content == b'RIFF-test'
    assert client.get('/api/media/%s/-1' % row['id']).status_code == 404
    export = json.loads(client.get('/api/export?format=jsonl&media=yes').text)
    assert export['raw']['text_extra'] == [{'user_id': 'mentioned'}]
    assert export['attachments'][0]['local_path'] == 'test.webp'
    # Repeated collection must retain a previously saved file on download failure.
    comment['attachments'][0].update(status='failed')
    db.insert_comment(None, None, comment)
    assert client.get('/api/comments/%s' % row['id']).json()['attachments'][0]['status'] == 'downloaded'


def test_taxonomy_exact_codes_and_clean_filter(client):
    assert [x['id'] for x in client.get('/api/labels').json()] == [0, 1, 3]
    row = seed(client)
    assert client.post('/api/review', json={'ids': [row['id']], 'safety_label': 2}).status_code == 422
    assert client.post('/api/review', json={'ids': [row['id']], 'safety_label': 0}).status_code == 200
    assert client.get('/api/comments?label=0').json()['total'] == 1
    assert client.get('/api/comments?label=1').json()['total'] == 0


def test_review_conflict_audit_and_atomic_bulk(client):
    row = seed(client)
    payload = {'ids': [row['id']], 'safety_label': 1, 'versions': {str(row['id']): row['version']}}
    assert client.post('/api/review', json=payload).status_code == 200
    assert client.post('/api/review', json=payload).status_code == 409
    detail = client.get(f"/api/comments/{row['id']}").json()
    assert len(detail['audit']) == 1
    assert json.loads(detail['audit'][0]['new_domain'])['safety_label'] == 1
    assert client.post('/api/review', json={'ids': [row['id'], 9999], 'safety_label': 3}).status_code == 404
    assert client.get(f"/api/comments/{row['id']}").json()['safety_label'] == 1


def test_unlabeled_cannot_be_approved_and_import_dedup(client):
    row = seed(client)
    assert client.post('/api/review', json={'ids': [row['id']]}).status_code == 422
    result = client.post('/api/import', json={'items': [{'text': 'Duplicate', 'external_id': 'one'}]}).json()
    assert result == {'inserted': 0, 'duplicates': 1}


def test_model_does_not_overwrite_human_review(client):
    row = seed(client)
    client.post('/api/review', json={'ids': [row['id']], 'safety_label': 0})
    job_id = engine.enqueue(kind='classify')
    asyncio.run(engine.label_pending(job_id))
    result = client.get(f"/api/comments/{row['id']}").json()
    assert result['safety_label'] == 0
    assert result['label_method'] == 'human'


def test_export_filter_formula_safety_and_jsonl_raw(client):
    row = seed(client, '=SUM(1,2)')
    client.post('/api/review', json={'ids': [row['id']], 'safety_label': 0})
    csv = client.get('/api/export?format=csv&label=0').text
    assert "'=SUM(1,2)" in csv
    raw = client.get('/api/export?format=jsonl&label=0').text
    assert json.loads(raw)['text'] == '=SUM(1,2)'
    assert client.get('/api/export?format=jsonl&label=3').text == ''


def test_source_validation_and_jobs_resume(client):
    source = {'name': 'Example', 'platform': 'website', 'mode': 'urls', 'query': 'https://example.com'}
    assert client.post('/api/sources', json=source).status_code == 422
    source['config'] = {'comment_selector': '.comment'}
    sid = client.post('/api/sources', json=source).json()['id']
    job_id = client.post(f'/api/sources/{sid}/run').json()['id']
    assert client.post(f'/api/sources/{sid}/run').json()['id'] is None
    client.post(f'/api/jobs/{job_id}/cancel')
    assert db.one('SELECT status FROM jobs WHERE id=?', (job_id,))['status'] == 'cancelled'
    assert client.post(f'/api/jobs/{job_id}/retry').status_code == 200
    with db.connect() as c:
        c.execute("UPDATE jobs SET status='running',checkpoint='[\"https://example.com\"]' WHERE id=?", (job_id,))
    db.init_db()
    job = db.one('SELECT * FROM jobs WHERE id=?', (job_id,))
    assert job['status'] == 'queued'
    assert json.loads(job['checkpoint']) == ['https://example.com']


def test_job_partial_failure_and_resume_only_unfinished(client, monkeypatch):
    source = {'name': 'YouTube', 'platform': 'youtube', 'mode': 'urls', 'query': 'https://youtube.com/watch?v=aaaaaaaaaaa\nhttps://youtube.com/watch?v=bbbbbbbbbbb', 'config': {'auto_classify': False}}
    sid = client.post('/api/sources', json=source).json()['id']
    job_id = engine.enqueue(sid)
    seen = []
    async def fake_comments(video, config, event, api_key):
        seen.append(video['url'])
        if 'bbbbbbbbbbb' in video['url'] and len(seen) == 2:
            raise RuntimeError('temporary failure')
        yield {'platform': 'youtube', 'external_id': video['url'], 'text': 'Nội dung'}
    monkeypatch.setattr(engine, 'youtube_comments', fake_comments)
    asyncio.run(engine.run_job(db.one('SELECT * FROM jobs WHERE id=?', (job_id,))))
    job = db.one('SELECT * FROM jobs WHERE id=?', (job_id,))
    assert job['status'] == 'attention'
    assert job['collected'] == 1
    asyncio.run(engine.run_job(job))
    assert seen.count('https://youtube.com/watch?v=aaaaaaaaaaa') == 1
    assert db.one('SELECT collected,status FROM jobs WHERE id=?', (job_id,)) == {'collected': 2, 'status': 'completed'}


def test_auth_and_cross_origin_mutation(client, monkeypatch):
    assert client.post('/api/import', json={'items': [{'text': 'abc'}]}, headers={'Origin': 'https://foreign.example'}).status_code == 403
    monkeypatch.setenv('SIGNALDESK_TOKEN', 'test-secret')
    assert client.get('/api/comments').status_code == 401
    assert client.get('/api/comments', headers={'Authorization': 'Bearer test-secret'}).status_code == 200


def test_tiktok_stops_after_three_consecutive_failures(client, monkeypatch):
    source = {'name': 'TikTok fail', 'platform': 'tiktok', 'mode': 'urls', 'query': '\n'.join(f'https://www.tiktok.com/@sample/video/{i}' for i in range(5)), 'config': {'auto_classify': False}}
    sid = client.post('/api/sources', json=source).json()['id']
    job_id = engine.enqueue(sid)
    calls = []
    async def failed(video, *args):
        calls.append(video['url'])
        raise engine.NeedsAttention('test failure')
        yield
    monkeypatch.setattr(engine, 'browser_comments', failed)
    asyncio.run(engine.run_job(db.one('SELECT * FROM jobs WHERE id=?', (job_id,))))
    assert len(calls) == 3
    assert db.one('SELECT status FROM jobs WHERE id=?', (job_id,))['status'] == 'attention'


def test_browser_login_only_accepts_tiktok_urls(client, monkeypatch):
    from unittest.mock import AsyncMock
    opener = AsyncMock()
    monkeypatch.setattr(engine, 'open_login', opener)
    monkeypatch.setitem(engine.browser_state, 'open', False)
    assert client.post('/api/browser/login', json={'url': 'https://foreign.example/'}).status_code == 422
    assert client.post('/api/browser/login', json={'url': 'https://tiktok.com.evil.example/'}).status_code == 422
    url = 'https://www.tiktok.com/@sample/video/123'
    assert client.post('/api/browser/login', json={'url': url}).status_code == 200
    # Flush the event loop so the background task receives the exact failed URL.
    client.get('/api/settings')
    opener.assert_awaited_once_with(url)
