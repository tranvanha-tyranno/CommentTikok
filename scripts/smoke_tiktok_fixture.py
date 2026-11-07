"""Browser regression: comment tab activation and ancestor-scroller pagination.

No TikTok network traffic; a local fixture models the observed layout.
"""
import asyncio
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from threading import Thread

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import collectors


class Handler(SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith('/api/comment/list/'):
            second = 'cursor=2' in self.path
            comments = [{'cid': str(i), 'aweme_id': '123', 'text': f'Comment {i}', 'reply_id': '0'} for i in ([3] if second else [1, 2])]
            # A nearby autoplay video's response must not leak into the dataset.
            comments.append({'cid': 'wrong', 'aweme_id': '999', 'text': 'Other video'})
            data = json.dumps({'status_code': 0, 'comments': comments, 'has_more': not second}).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(data)
            return
        self.path = '/index.html'
        super().do_GET()

    def log_message(self, *args):
        pass


async def main():
    original_data = collectors.DATA
    with TemporaryDirectory() as folder:
        root = Path(folder)
        root.joinpath('index.html').write_text('''<!doctype html><title>Comment regression</title>
        <button id="open">Comments</button><p id="recommendations">You may like</p>
        <div id="panel" class="DivCommentMain" style="height:100px;overflow-y:scroll;display:none">
        <div id="list" class="DivCommentListContainer" style="height:900px;overflow:visible"></div></div>
        <script>
        let loaded=false;
        async function load(cursor='') {
          const data=await (await fetch('/api/comment/list/?aweme_id=123'+cursor)).json();
          for(const c of data.comments){const s=document.createElement('span');s.dataset.e2e='comment-level-1';s.textContent=c.text;list.appendChild(s);}
        }
        document.getElementById('open').onclick=async()=>{recommendations.hidden=true;panel.style.display='block';await load();};
        panel.onscroll=async()=>{if(!loaded&&panel.scrollTop>0){loaded=true;await load('&cursor=2');}};
        </script>''', encoding='utf-8')
        server = ThreadingHTTPServer(('127.0.0.1', 0), partial(Handler, directory=folder))
        Thread(target=server.serve_forever, daemon=True).start()
        collectors.DATA = root
        try:
            async def event(*args):
                pass
            config = {'visible_browser': False, 'max_comments': 3, 'scroll_rounds': 5, 'delay_seconds': 1, 'include_replies': False}
            comments = [c async for c in collectors.browser_comments({'url': f'http://127.0.0.1:{server.server_port}/video/123'}, {'platform': 'tiktok'}, config, event)]
            assert [c['external_id'] for c in comments] == ['1', '2', '3']
            print('PASS: opened Comments, paginated ancestor scroller, excluded other video (3 records).')
        finally:
            collectors.DATA = original_data
            server.shutdown()
            server.server_close()


if __name__ == '__main__':
    asyncio.run(main())
