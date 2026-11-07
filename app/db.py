from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from .media import extract_attachments

load_dotenv()
ROOT = Path(__file__).resolve().parent.parent
DATA = Path(os.getenv('SIGNALDESK_DATA_DIR', str(ROOT / 'data'))).resolve()


def now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


@contextmanager
def connect():
    DATA.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DATA / 'signaldesk.db', timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys=ON')
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


DEFAULT_DOMAINS = [
    ('beauty', 'Làm đẹp', '#ca6b99', 'mỹ phẩm,son môi,kem chống nắng,skincare,trang điểm,dưỡng da'),
    ('technology', 'Công nghệ', '#6e85d9', 'điện thoại,laptop,iphone,android,máy tính,phần mềm,camera'),
    ('food', 'Ẩm thực', '#d39b49', 'món ăn,nấu ăn,ngon,quán ăn,công thức,bánh,trà sữa'),
    ('fashion', 'Thời trang', '#9576c8', 'quần áo,áo,giày,váy,túi xách,thời trang'),
    ('education', 'Giáo dục', '#48a39c', 'học,giáo viên,sinh viên,tiếng anh,đại học,bài giảng'),
    ('entertainment', 'Giải trí', '#e28061', 'phim,ca sĩ,âm nhạc,diễn viên,hài,bài hát'),
    ('lifestyle', 'Đời sống', '#6a9c66', 'gia đình,du lịch,nhà cửa,thú cưng,vườn,chăm sóc'),
    ('unknown', 'Chưa rõ domain', '#92979d', ''),
]


def init_db():
    with connect() as c:
        c.execute('PRAGMA journal_mode=WAL')
        c.executescript('''
        CREATE TABLE IF NOT EXISTS domains (
          id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE, color TEXT NOT NULL,
          keywords TEXT NOT NULL DEFAULT '', active INTEGER NOT NULL DEFAULT 1);
        CREATE TABLE IF NOT EXISTS sources (
          id INTEGER PRIMARY KEY, name TEXT NOT NULL, platform TEXT NOT NULL,
          mode TEXT NOT NULL, query TEXT NOT NULL, config TEXT NOT NULL DEFAULT '{}',
          schedule_minutes INTEGER NOT NULL DEFAULT 0, enabled INTEGER NOT NULL DEFAULT 1,
          created_at TEXT NOT NULL, last_enqueued TEXT);
        CREATE TABLE IF NOT EXISTS jobs (
          id INTEGER PRIMARY KEY, source_id INTEGER REFERENCES sources(id), kind TEXT NOT NULL DEFAULT 'crawl',
          status TEXT NOT NULL DEFAULT 'queued', created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT,
          collected INTEGER NOT NULL DEFAULT 0, seen INTEGER NOT NULL DEFAULT 0,
          videos_done INTEGER NOT NULL DEFAULT 0, checkpoint TEXT NOT NULL DEFAULT '[]',
          message TEXT NOT NULL DEFAULT '', cancel_requested INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS comments (
          id INTEGER PRIMARY KEY, platform TEXT NOT NULL, external_id TEXT NOT NULL,
          source_id INTEGER REFERENCES sources(id), job_id INTEGER REFERENCES jobs(id),
          video_id TEXT NOT NULL DEFAULT '', video_url TEXT NOT NULL DEFAULT '',
          context TEXT NOT NULL DEFAULT '', parent_id TEXT NOT NULL DEFAULT '',
          author TEXT NOT NULL DEFAULT '', text TEXT NOT NULL, likes INTEGER NOT NULL DEFAULT 0,
          published_at TEXT, collected_at TEXT NOT NULL, raw TEXT NOT NULL DEFAULT '{}',
          domain_id TEXT REFERENCES domains(id), confidence REAL,
          label_method TEXT NOT NULL DEFAULT 'unlabeled', review_status TEXT NOT NULL DEFAULT 'pending',
          safety_label INTEGER REFERENCES labels(id), safety_confidence REAL,
          version INTEGER NOT NULL DEFAULT 1, UNIQUE(platform, external_id));
        CREATE TABLE IF NOT EXISTS audit (
          id INTEGER PRIMARY KEY, comment_id INTEGER NOT NULL REFERENCES comments(id),
          old_domain TEXT, new_domain TEXT, action TEXT NOT NULL, actor TEXT NOT NULL,
          created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS logs (
          id INTEGER PRIMARY KEY, job_id INTEGER NOT NULL REFERENCES jobs(id),
          level TEXT NOT NULL, message TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS labels (id INTEGER PRIMARY KEY, name TEXT NOT NULL, color TEXT NOT NULL, definition TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS comments_filter ON comments(review_status,domain_id,platform,id);
        CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status,id);
        CREATE INDEX IF NOT EXISTS logs_job ON logs(job_id,id);
        ''')
        c.executemany('INSERT OR IGNORE INTO domains(id,name,color,keywords) VALUES(?,?,?,?)', DEFAULT_DOMAINS)
        c.executemany('INSERT OR IGNORE INTO labels(id,name,color,definition) VALUES(?,?,?,?)', [
            (0, 'CLEAN', '#38947d', 'Bình luận bình thường, trung tính, tích cực hoặc tiêu cực nhưng không mang tính xúc phạm, miệt thị hay thù ghét. Có thể chê bai ý kiến/sản phẩm/màn trình diễn nếu không công kích con người bằng ngôn từ hạ nhục.'),
            (1, 'OFFENSIVE', '#d78b35', 'Bình luận có chửi bới, xúc phạm, chế giễu, hạ nhục, công kích cá nhân hoặc nhóm, dùng từ tục/từ miệt thị, nhưng không dựa trên một đặc điểm nhóm được bảo vệ.'),
            (3, 'HATE', '#ce5d68', 'Bình luận có nội dung thù ghét, hạ nhục, loại trừ, đe dọa hoặc công kích một cá nhân/nhóm vì họ thuộc một nhóm có đặc điểm được bảo vệ, ví dụ giới tính, dân tộc/chủng tộc, quốc tịch, tôn giáo, xu hướng tính dục, bản dạng giới, khuyết tật.'),
        ])
        columns = {r['name'] for r in c.execute('PRAGMA table_info(comments)')}
        for name, kind in [('safety_label', 'INTEGER REFERENCES labels(id)'), ('safety_confidence', 'REAL'), ('attachments', "TEXT NOT NULL DEFAULT '[]'")]:
            if name not in columns:
                c.execute(f'ALTER TABLE comments ADD COLUMN {name} {kind}')
                if name == 'attachments':
                    for row in c.execute("SELECT id,raw FROM comments WHERE platform='tiktok'").fetchall():
                        attachments = extract_attachments(json.loads(row['raw']))
                        if attachments:
                            c.execute('UPDATE comments SET attachments=? WHERE id=?', (json.dumps(attachments, ensure_ascii=False), row['id']))
        # A process crash cannot leave jobs permanently marked as running.
        c.execute("UPDATE jobs SET status='queued',message='Tiếp tục sau khi khởi động lại',cancel_requested=0 WHERE status='running'")


def rows(sql, params=()):
    with connect() as c:
        return [dict(r) for r in c.execute(sql, params).fetchall()]


def one(sql, params=()):
    result = rows(sql, params)
    return result[0] if result else None


def setting(key, default=''):
    result = one('SELECT value FROM settings WHERE key=?', (key,))
    return result['value'] if result else default


def log(job_id, message, level='info'):
    with connect() as c:
        c.execute('INSERT INTO logs(job_id,level,message,created_at) VALUES(?,?,?,?)', (job_id, level, message, now()))


def insert_comment(job_id, source_id, comment):
    attachments = comment.get('attachments') or extract_attachments(comment.get('raw', {}))
    if not str(comment.get('text', '')).strip() and not attachments:
        return False
    keys = ['platform','external_id','video_id','video_url','context','parent_id','author','text','likes','published_at']
    values = [comment.get(k, '' if k != 'likes' else 0) for k in keys]
    with connect() as c:
        result = c.execute(f"INSERT OR IGNORE INTO comments({','.join(keys)},source_id,job_id,collected_at,raw,attachments) VALUES({','.join('?' for _ in range(15))})",
                           (*values, source_id, job_id, now(), json.dumps(comment.get('raw', {}), ensure_ascii=False), json.dumps(attachments, ensure_ascii=False)))
        inserted = result.rowcount > 0
        if not inserted and attachments:
            existing = c.execute('SELECT attachments FROM comments WHERE platform=? AND external_id=?', (comment['platform'], comment['external_id'])).fetchone()
            old = json.loads(existing['attachments'])
            for attachment in attachments:
                cached = next((a for a in old if a.get('source_field') == attachment.get('source_field') and a.get('index') == attachment.get('index') and a.get('status') == 'downloaded'), None)
                if cached and attachment.get('status') != 'downloaded':
                    attachment.update({k: cached[k] for k in ('local_path', 'content_type', 'size_bytes', 'status') if k in cached})
            c.execute('UPDATE comments SET attachments=?,raw=?,version=version+1 WHERE platform=? AND external_id=?',
                      (json.dumps(attachments, ensure_ascii=False), json.dumps(comment.get('raw', {}), ensure_ascii=False), comment['platform'], comment['external_id']))
        c.execute('UPDATE jobs SET seen=seen+1,collected=collected+? WHERE id=?', (int(inserted), job_id))
    return inserted
