"""Optional live smoke checks. No user data/database writes; prints counts only.

Run: .venv/Scripts/python scripts/smoke_collectors.py [--youtube]
"""
import asyncio
import functools
import http.server
from pathlib import Path
import sys
import tempfile
import threading

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import collectors


async def main():
    async def event(*args):
        pass
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        (root / 'index.html').write_text('<title>Fixture</title><p class="comment">First comment</p><p class="comment">Second comment</p>', encoding='utf-8')
        handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=folder)
        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        collectors.DATA = root
        try:
            config = {'visible_browser': False, 'max_comments': 2, 'scroll_rounds': 1, 'delay_seconds': 1, 'comment_selector': '.comment'}
            comments = [c async for c in collectors.browser_comments({'url': f'http://127.0.0.1:{server.server_port}/index.html'}, {'platform': 'website'}, config, event)]
            assert len(comments) == 2
            assert comments[0]['context'] == 'Fixture'
            print('Website browser collection: PASS (2 comments)')
        finally:
            server.shutdown()
            server.server_close()
    if '--youtube' in sys.argv:
        try:
            config = {'max_comments': 3, 'include_replies': True}
            async def collect():
                return [c async for c in collectors.youtube_comments({'url': 'https://www.youtube.com/watch?v=jNQXAC9IVRw'}, config, event)]
            comments = await asyncio.wait_for(collect(), timeout=90)
            print(f'YouTube live collection: {len(comments)} comments')
        except Exception as exc:
            print('YouTube live collection: UNVERIFIED / ' + type(exc).__name__ + ': ' + str(exc)[:350])


if __name__ == '__main__':
    asyncio.run(main())
