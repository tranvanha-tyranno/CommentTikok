"""Recover attachments from existing native TikTok payloads without recrawling."""
import asyncio
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import db
from app.media import extract_attachments, download_attachments


async def main():
    # Migrations run at server startup; avoid resetting jobs in a running server.
    total = saved = failed = 0
    for row in db.rows("SELECT id,raw,video_url,attachments FROM comments WHERE platform='tiktok'"):
        attachments = json.loads(row['attachments']) or extract_attachments(json.loads(row['raw']))
        if not attachments:
            continue
        await download_attachments(attachments, db.DATA / 'media', referer=row['video_url'])
        with db.connect() as c:
            c.execute('UPDATE comments SET attachments=?,version=version+1 WHERE id=?', (json.dumps(attachments, ensure_ascii=False), row['id']))
        total += len(attachments)
        saved += sum(a['status'] == 'downloaded' for a in attachments)
        failed += sum(a['status'] == 'failed' for a in attachments)
        print(f"Comment #{row['id']}: {len(attachments)} attachments", flush=True)
    print(json.dumps({'attachments': total, 'downloaded': saved, 'failed': failed}), flush=True)


if __name__ == '__main__':
    asyncio.run(main())
