from __future__ import annotations

import asyncio
import csv
import io
import json
import os
import re
import secrets
import uuid
from contextlib import asynccontextmanager
from urllib.parse import urlparse
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, model_validator

from . import db, engine
from .media import decode_attachments


@asynccontextmanager
async def lifespan(app):
    db.init_db()
    tasks = [asyncio.create_task(engine.worker()) for _ in range(max(1, min(8, int(os.getenv('SIGNALDESK_WORKERS', '2')))))]
    tasks.append(asyncio.create_task(engine.scheduler()))
    app.state.tasks = tasks
    yield
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


app = FastAPI(title='SignalDesk', lifespan=lifespan)
app.mount('/static', StaticFiles(directory=db.ROOT / 'app' / 'static'), name='static')


@app.middleware('http')
async def local_guard(request: Request, call_next):
    if request.url.path.startswith('/api/'):
        token = os.getenv('SIGNALDESK_TOKEN', '')
        provided = request.headers.get('authorization', '').removeprefix('Bearer ')
        if token and not secrets.compare_digest(provided, token):
            return JSONResponse({'detail': 'Cần access token'}, status_code=401)
        # Reject cross-origin mutations, including local-network browser requests.
        origin = request.headers.get('origin')
        if request.method not in ('GET', 'HEAD') and origin:
            if urlparse(origin).netloc != request.headers.get('host'):
                return JSONResponse({'detail': 'Origin không hợp lệ'}, status_code=403)
    response = await call_next(request)
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Referrer-Policy'] = 'no-referrer'
    response.headers['X-Frame-Options'] = 'DENY'
    return response


@app.get('/')
def index():
    return FileResponse(db.ROOT / 'app' / 'static' / 'index.html')


@app.get('/api/overview')
def overview():
    return {
        'total': db.one('SELECT count(*) n FROM comments')['n'],
        'pending': db.one("SELECT count(*) n FROM comments WHERE review_status='pending'")['n'],
        'reviewed': db.one("SELECT count(*) n FROM comments WHERE review_status='reviewed'")['n'],
        'running': db.one("SELECT count(*) n FROM jobs WHERE status IN ('running','queued')")['n'],
        'labels': db.rows('SELECT l.*,count(c.id) count FROM labels l LEFT JOIN comments c ON c.safety_label=l.id GROUP BY l.id ORDER BY l.id'),
        'unlabeled': db.one('SELECT count(*) n FROM comments WHERE safety_label IS NULL')['n'],
        'platforms': db.rows('SELECT platform,count(*) count FROM comments GROUP BY platform'),
    }


@app.get('/api/labels')
def labels():
    return db.rows('SELECT * FROM labels ORDER BY id')


class LabelEdit(BaseModel):
    definition: str = Field(min_length=20, max_length=3000)


@app.patch('/api/labels/{label_id}')
def edit_label(label_id: int, body: LabelEdit):
    with db.connect() as c:
        if not c.execute('UPDATE labels SET definition=? WHERE id=?', (body.definition, label_id)).rowcount:
            raise HTTPException(404, 'Không có nhãn này')
    return {'ok': True}


@app.get('/api/domains')
def domains():
    return db.rows('SELECT * FROM domains ORDER BY id')


class DomainBody(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    color: str = Field(default='#6e85d9', pattern=r'^#[0-9a-fA-F]{6}$')
    keywords: str = Field(default='', max_length=3000)
    active: bool = True


@app.post('/api/domains')
def add_domain(body: DomainBody):
    domain_id = uuid.uuid4().hex[:12]
    with db.connect() as c:
        if c.execute('SELECT 1 FROM domains WHERE name=?', (body.name,)).fetchone():
            raise HTTPException(409, 'Tên domain đã tồn tại')
        c.execute('INSERT INTO domains(id,name,color,keywords,active) VALUES(?,?,?,?,?)', (domain_id, body.name, body.color, body.keywords, body.active))
    return {'id': domain_id}


@app.patch('/api/domains/{domain_id}')
def edit_domain(domain_id: str, body: DomainBody):
    with db.connect() as c:
        duplicate = c.execute('SELECT 1 FROM domains WHERE name=? AND id!=?', (body.name, domain_id)).fetchone()
        if duplicate:
            raise HTTPException(409, 'Tên domain đã tồn tại')
        if not c.execute('UPDATE domains SET name=?,color=?,keywords=?,active=? WHERE id=?', (body.name, body.color, body.keywords, body.active, domain_id)).rowcount:
            raise HTTPException(404, 'Không tìm thấy domain')
    return {'ok': True}


class CrawlConfig(BaseModel):
    max_videos: int = Field(default=10, ge=1, le=500)
    max_comments: int = Field(default=1000, ge=1, le=100000)
    include_replies: bool = True
    auto_classify: bool = True
    visible_browser: bool = True
    scroll_rounds: int = Field(default=100, ge=1, le=10000)
    delay_seconds: float = Field(default=2, ge=1, le=60)
    comment_selector: str = Field(default='', max_length=500)


class SourceBody(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    platform: str = Field(pattern='^(tiktok|youtube|website)$')
    mode: str = Field(default='urls', pattern='^(urls|search)$')
    query: str = Field(min_length=1, max_length=100000)
    config: CrawlConfig = Field(default_factory=CrawlConfig)
    schedule_minutes: int = Field(default=0, ge=0, le=525600)
    enabled: bool = True

    @model_validator(mode='after')
    def validate_source(self):
        if self.platform == 'website' and (self.mode != 'urls' or not self.config.comment_selector):
            raise ValueError('Website cần URL và CSS selector bình luận')
        if self.mode == 'urls':
            for value in self.query.splitlines():
                if not value.strip():
                    continue
                u = urlparse(value.strip())
                if u.scheme not in ('https', 'http') or not u.hostname or u.username:
                    raise ValueError('Mỗi dòng cần một URL http(s) hợp lệ')
                host = u.hostname.lower()
                if self.platform == 'tiktok' and host != 'tiktok.com' and not host.endswith('.tiktok.com'):
                    raise ValueError('Nguồn TikTok chỉ nhận URL tiktok.com')
                if self.platform == 'youtube' and host not in ('youtu.be', 'youtube.com', 'www.youtube.com', 'm.youtube.com'):
                    raise ValueError('Nguồn YouTube chỉ nhận URL YouTube')
        return self


@app.get('/api/sources')
def sources():
    result = db.rows('SELECT * FROM sources ORDER BY id DESC')
    for row in result:
        row['config'] = json.loads(row['config'])
    return result


@app.post('/api/sources')
def create_source(body: SourceBody):
    with db.connect() as c:
        source_id = c.execute('INSERT INTO sources(name,platform,mode,query,config,schedule_minutes,enabled,created_at) VALUES(?,?,?,?,?,?,?,?)',
                             (body.name, body.platform, body.mode, body.query.strip(), body.config.model_dump_json(), body.schedule_minutes, body.enabled, db.now())).lastrowid
    return {'id': source_id}


@app.put('/api/sources/{source_id}')
def update_source(source_id: int, body: SourceBody):
    with db.connect() as c:
        if not c.execute('UPDATE sources SET name=?,platform=?,mode=?,query=?,config=?,schedule_minutes=?,enabled=? WHERE id=?',
                         (body.name, body.platform, body.mode, body.query.strip(), body.config.model_dump_json(), body.schedule_minutes, body.enabled, source_id)).rowcount:
            raise HTTPException(404, 'Không có nguồn này')
    return {'ok': True}


@app.post('/api/sources/{source_id}/run')
def start_source(source_id: int):
    if not db.one('SELECT id FROM sources WHERE id=?', (source_id,)):
        raise HTTPException(404, 'Không có nguồn này')
    job_id = engine.enqueue(source_id)
    return {'id': job_id, 'message': 'Đã xếp hàng' if job_id else 'Nguồn đang chạy hoặc đã xếp hàng'}


@app.get('/api/jobs')
def jobs():
    return db.rows('SELECT j.*,s.name source_name,s.platform FROM jobs j LEFT JOIN sources s ON s.id=j.source_id ORDER BY j.id DESC LIMIT 100')


@app.get('/api/jobs/{job_id}/logs')
def logs(job_id: int):
    return db.rows('SELECT * FROM (SELECT * FROM logs WHERE job_id=? ORDER BY id DESC LIMIT 200) ORDER BY id', (job_id,))


@app.post('/api/jobs/{job_id}/cancel')
def cancel_job(job_id: int):
    with db.connect() as c:
        c.execute("UPDATE jobs SET cancel_requested=1,status=CASE WHEN status='queued' THEN 'cancelled' ELSE status END WHERE id=? AND status IN ('running','queued')", (job_id,))
    return {'ok': True}


@app.post('/api/jobs/{job_id}/retry')
def retry_job(job_id: int):
    with db.connect() as c:
        job = c.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
        if not job or job['status'] in ('running', 'queued'):
            raise HTTPException(409, 'Tác vụ không thể chạy lại lúc này')
        if c.execute("SELECT 1 FROM jobs WHERE source_id IS ? AND kind=? AND status IN ('running','queued')", (job['source_id'], job['kind'])).fetchone():
            raise HTTPException(409, 'Nguồn này đang có tác vụ khác chạy')
        c.execute("UPDATE jobs SET status='queued',cancel_requested=0,message='',finished_at=NULL WHERE id=?", (job_id,))
    return {'ok': True}


def comment_filter(q='', platform='', label='', review='', domain='', source_id=None, media=''):
    clauses, params = ['1=1'], []
    if q:
        clauses.append('(text LIKE ? OR author LIKE ? OR context LIKE ?)')
        params.extend(['%' + q + '%'] * 3)
    if media:
        if media not in ('yes', 'no'):
            raise HTTPException(422, 'Bộ lọc tệp không hợp lệ')
        clauses.append('json_array_length(attachments)' + ('>0' if media == 'yes' else '=0'))
    for column, value in [('platform', platform), ('review_status', review), ('domain_id', domain), ('source_id', source_id)]:
        if value is not None and value != '':
            clauses.append(f'{column}=?')
            params.append(value)
    if label == 'none':
        clauses.append('safety_label IS NULL')
    elif label != '':
        if label not in ('0', '1', '3'):
            raise HTTPException(422, 'Nhãn không hợp lệ')
        clauses.append('safety_label=?')
        params.append(int(label))
    return ' AND '.join(clauses), params


@app.get('/api/comments')
def comments(q: str = '', platform: str = '', label: str = '', review: str = '', domain: str = '', source_id: int | None = None, media: str = '',
             page: int = Query(1, ge=1), page_size: int = Query(30, ge=1, le=100)):
    where, params = comment_filter(q, platform, label, review, domain, source_id, media)
    total = db.one(f'SELECT count(*) n FROM comments WHERE {where}', params)['n']
    result = db.rows(f'SELECT id,platform,external_id,video_url,author,text,context,parent_id,likes,published_at,domain_id,confidence,safety_label,safety_confidence,label_method,review_status,version,attachments FROM comments WHERE {where} ORDER BY id DESC LIMIT ? OFFSET ?', (*params, page_size, (page - 1) * page_size))
    for row in result:
        decode_attachments(row)
    return {'items': result, 'total': total, 'page': page, 'page_size': page_size}


@app.get('/api/comments/{comment_id}')
def comment_detail(comment_id: int):
    row = db.one('SELECT * FROM comments WHERE id=?', (comment_id,))
    if not row:
        raise HTTPException(404, 'Không có bình luận này')
    decode_attachments(row)
    row['raw'] = json.loads(row['raw'])
    row['audit'] = db.rows('SELECT * FROM audit WHERE comment_id=? ORDER BY id DESC', (comment_id,))
    row['parent'] = db.one('SELECT text,author FROM comments WHERE platform=? AND external_id=?', (row['platform'], row['parent_id'])) if row['parent_id'] else None
    return row


@app.get('/api/media/{comment_id}/{attachment_index}')
def comment_attachment(comment_id: int, attachment_index: int):
    row = db.one('SELECT attachments FROM comments WHERE id=?', (comment_id,))
    items = json.loads(row['attachments']) if row else []
    if attachment_index < 0 or attachment_index >= len(items):
        raise HTTPException(404, 'Không có tệp đính kèm')
    item = items[attachment_index]
    name = item.get('local_path', '')
    if not name or Path(name).name != name:
        raise HTTPException(404, 'Tệp chưa tải về')
    path = (db.DATA / 'media' / name).resolve()
    if path.parent != (db.DATA / 'media').resolve() or not path.is_file():
        raise HTTPException(404, 'Tệp không còn trên máy')
    return FileResponse(path, media_type=item.get('content_type'), headers={'Content-Disposition': f'inline; filename="{name}"'})


class ReviewBody(BaseModel):
    ids: list[int] = Field(min_length=1, max_length=1000)
    safety_label: int | None = None
    domain_id: str | None = None
    review_status: str = Field(default='reviewed', pattern='^(pending|reviewed|discarded)$')
    versions: dict[str, int] = Field(default_factory=dict)


@app.post('/api/review')
def review_comments(body: ReviewBody):
    if 'safety_label' in body.model_fields_set and body.safety_label not in (0, 1, 3, None):
        raise HTTPException(422, 'Chỉ dùng nhãn 0, 1, 3')
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        if body.domain_id and not c.execute('SELECT 1 FROM domains WHERE id=?', (body.domain_id,)).fetchone():
            raise HTTPException(422, 'Domain không tồn tại')
        selected = c.execute('SELECT * FROM comments WHERE id IN (' + ','.join('?' for _ in body.ids) + ')', body.ids).fetchall()
        if len(selected) != len(set(body.ids)):
            raise HTTPException(404, 'Có bình luận không còn tồn tại')
        for row in selected:
            expected = body.versions.get(str(row['id']))
            if expected is not None and expected != row['version']:
                raise HTTPException(409, 'Dữ liệu vừa được sửa ở nơi khác. Tải lại trước khi lưu.')
            label = body.safety_label if 'safety_label' in body.model_fields_set else row['safety_label']
            domain = body.domain_id if 'domain_id' in body.model_fields_set else row['domain_id']
            if body.review_status == 'reviewed' and label is None:
                raise HTTPException(422, 'Chọn CLEAN, OFFENSIVE hoặc HATE trước khi duyệt')
            old = json.dumps({'safety_label': row['safety_label'], 'domain_id': row['domain_id'], 'review_status': row['review_status']})
            new = json.dumps({'safety_label': label, 'domain_id': domain, 'review_status': body.review_status})
            c.execute('INSERT INTO audit(comment_id,old_domain,new_domain,action,actor,created_at) VALUES(?,?,?,?,?,?)', (row['id'], old, new, 'review', 'local-reviewer', db.now()))
            c.execute("UPDATE comments SET safety_label=?,domain_id=?,review_status=?,label_method='human',safety_confidence=NULL,confidence=NULL,version=version+1 WHERE id=?", (label, domain, body.review_status, row['id']))
    return {'updated': len(selected)}


@app.post('/api/classify')
def classify_all():
    return {'id': engine.enqueue(kind='classify')}


class ImportRow(BaseModel):
    text: str = Field(min_length=1, max_length=50000)
    platform: str = Field(default='import', pattern='^[a-z0-9_-]{1,30}$')
    external_id: str = Field(default='', max_length=200)
    context: str = Field(default='', max_length=50000)
    author: str = Field(default='', max_length=500)
    video_url: str = Field(default='', max_length=3000)
    safety_label: int | None = None


class ImportBody(BaseModel):
    items: list[ImportRow] = Field(min_length=1, max_length=5000)


@app.post('/api/import')
def import_data(body: ImportBody):
    if any(x.safety_label not in (0, 1, 3, None) for x in body.items):
        raise HTTPException(422, 'Nhãn import chỉ nhận 0, 1, 3 hoặc null')
    count = 0
    with db.connect() as c:
        for row in body.items:
            data = row.model_dump()
            result = c.execute('INSERT OR IGNORE INTO comments(platform,external_id,text,context,author,video_url,safety_label,collected_at,raw) VALUES(?,?,?,?,?,?,?,?,?)',
                               (row.platform, row.external_id or uuid.uuid4().hex, row.text, row.context, row.author, row.video_url, row.safety_label, db.now(), json.dumps(data, ensure_ascii=False)))
            count += result.rowcount
    return {'inserted': count, 'duplicates': len(body.items) - count}


@app.get('/api/export')
def export_data(format: str = 'csv', q: str = '', platform: str = '', label: str = '', review: str = '', domain: str = '', media: str = ''):
    if format not in ('csv', 'jsonl'):
        raise HTTPException(422, 'Dùng csv hoặc jsonl')
    where, params = comment_filter(q, platform, label, review, domain, media=media)
    fields = ['id','platform','external_id','video_url','context','parent_id','author','text','likes','published_at','collected_at','safety_label','domain_id','review_status','label_method','safety_confidence','confidence','attachments','raw']
    def stream():
        last = 0
        if format == 'csv':
            yield '\ufeff'
            buf = io.StringIO()
            writer = csv.writer(buf)
            writer.writerow(fields)
            yield buf.getvalue()
        while True:
            batch = db.rows(f"SELECT {','.join(fields)} FROM comments WHERE {where} AND id>? ORDER BY id LIMIT 500", (*params, last))
            if not batch:
                break
            for row in batch:
                if format == 'jsonl':
                    decode_attachments(row)
                    row['raw'] = json.loads(row['raw'])
                    yield json.dumps(row, ensure_ascii=False) + '\n'
                else:
                    buf = io.StringIO()
                    # Neutralize spreadsheet formula injection; JSONL preserves raw text.
                    values = [('\'' + v if isinstance(v, str) and v.lstrip().startswith(('=', '+', '-', '@')) else v) for v in (row[k] for k in fields)]
                    csv.writer(buf).writerow(values)
                    yield buf.getvalue()
                last = row['id']
    return StreamingResponse(stream(), media_type='text/csv' if format == 'csv' else 'application/x-ndjson', headers={'Content-Disposition': f'attachment; filename="signaldesk.{format}"'})


class SettingsBody(BaseModel):
    llm_base_url: str = Field(max_length=500)
    llm_model: str = Field(max_length=200)


@app.get('/api/settings')
def settings():
    return {'llm_base_url': db.setting('llm_base_url', os.getenv('LLM_BASE_URL', 'http://localhost:11434/v1')),
            'llm_model': db.setting('llm_model', os.getenv('LLM_MODEL', '')),
            'youtube_api': bool(os.getenv('YOUTUBE_API_KEY')), 'browser': engine.browser_state}


@app.put('/api/settings')
def update_settings(body: SettingsBody):
    if urlparse(body.llm_base_url).scheme not in ('http', 'https'):
        raise HTTPException(422, 'LLM URL cần http(s)')
    with db.connect() as c:
        for key, value in body.model_dump().items():
            c.execute('INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)', (key, value))
    return {'ok': True}


class BrowserLoginBody(BaseModel):
    url: str = Field(default='https://www.tiktok.com/', max_length=3000)

    @model_validator(mode='after')
    def validate_url(self):
        parsed = urlparse(self.url)
        host = (parsed.hostname or '').lower()
        if parsed.scheme != 'https' or parsed.username or parsed.port not in (None, 443) or not (host == 'tiktok.com' or host.endswith('.tiktok.com')):
            raise ValueError('Chỉ mở URL HTTPS của TikTok trong profile đăng nhập')
        return self


@app.post('/api/browser/login')
async def browser_login(body: BrowserLoginBody | None = None):
    if engine.browser_state['open']:
        return {'message': 'Trình duyệt đăng nhập đã mở hoặc đang chờ'}
    engine.browser_state['open'] = True
    task = asyncio.create_task(engine.open_login(body.url if body else 'https://www.tiktok.com/'))
    app.state.tasks.append(task)
    return {'message': 'Đang mở đúng trang TikTok trong profile riêng. Hoàn tất xác minh/đăng nhập, đóng cửa sổ rồi bấm Tiếp tục.'}
