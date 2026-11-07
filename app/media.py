"""Comment attachments; retain native payload separately in comments.raw."""
import hashlib
import json
from pathlib import Path
from urllib.parse import urlparse


def extract_attachments(raw):
    result = []

    def urls(node):
        if isinstance(node, str):
            return [node] if node.startswith(('https://', 'http://')) else []
        if isinstance(node, list):
            return [u for item in node for u in urls(item)]
        if isinstance(node, dict):
            # Prefer originals and animated/high-resolution representations.
            keys = sorted(node, key=lambda k: (0 if k in ('origin_url', 'animated_url', 'high_resolution_url') else 1))
            return [u for k in keys if k not in ('uri', 'url_prefix') for u in urls(node[k])]
        return []

    for field, value in raw.items():
        if not value or not any(word in field.lower() for word in ('image', 'photo', 'sticker', 'emoji', 'gif', 'audio', 'voice', 'video', 'attachment')):
            continue
        kind = 'sticker' if any(w in field for w in ('sticker', 'emoji', 'gif')) else 'audio' if any(w in field for w in ('audio', 'voice')) else 'video' if 'video' in field else 'image'
        for index, item in enumerate(value if isinstance(value, list) else [value]):
            candidates = list(dict.fromkeys(urls(item)))
            if not candidates:
                continue
            result.append({'kind': kind, 'source_field': field, 'index': index,
                           'url': candidates[0], 'urls': candidates, 'status': 'pending'})
    return result


async def download_attachments(attachments, directory, request=None, referer=''):
    """Try CDN alternatives, cache bytes, and retain failures without dropping comments."""
    import httpx
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    async with httpx.AsyncClient(timeout=25, follow_redirects=False) as client:
        for item in attachments:
            if item.get('local_path') and (directory / Path(item['local_path']).name).is_file():
                continue
            last_error = 'Không có URL tải tệp'
            for url in item.get('urls') or [item.get('url', '')]:
                host = (urlparse(url).hostname or '').lower()
                if urlparse(url).scheme != 'https' or not any(host == d or host.endswith('.' + d) for d in ('tiktokcdn.com', 'ibyteimg.com', 'byteoversea.com', 'tiktok.com')):
                    last_error = 'Chưa hỗ trợ tải từ host này; đã giữ URL gốc'
                    continue
                name = hashlib.sha256(url.split('?')[0].encode()).hexdigest()
                cached = list(directory.glob(name + '.*'))
                if cached:
                    item.update(local_path=cached[0].name, status='downloaded')
                    break
                response = None
                try:
                    headers = {'Referer': referer or 'https://www.tiktok.com/'}
                    if request:
                        response = await request.get(url, headers=headers, timeout=25000)
                        status, content_type = response.status, response.headers.get('content-type', '').split(';')[0]
                        length = int(response.headers.get('content-length') or 0)
                        if status != 200 or length > 30 * 1024 * 1024:
                            raise ValueError(f'HTTP {status} hoặc tệp quá 30 MB')
                        body = await response.body()
                    else:
                        async with client.stream('GET', url, headers=headers) as response:
                            status, content_type = response.status_code, response.headers.get('content-type', '').split(';')[0]
                            if status != 200:
                                raise ValueError(f'HTTP {status}')
                            parts, size = [], 0
                            async for part in response.aiter_bytes():
                                size += len(part)
                                if size > 30 * 1024 * 1024:
                                    raise ValueError('Tệp quá 30 MB')
                                parts.append(part)
                            body = b''.join(parts)
                    extensions = {'image/jpeg': '.jpg', 'image/png': '.png', 'image/webp': '.webp', 'image/gif': '.gif', 'image/avif': '.avif', 'video/mp4': '.mp4', 'video/webm': '.webm', 'audio/mpeg': '.mp3', 'audio/mp4': '.m4a', 'audio/ogg': '.ogg', 'audio/wav': '.wav'}
                    ext = extensions.get(content_type)
                    if not ext or not body or len(body) > 30 * 1024 * 1024:
                        raise ValueError('Định dạng tệp không hỗ trợ hoặc dữ liệu rỗng/quá 30 MB')
                    (directory / (name + ext)).write_bytes(body)
                    item.update(local_path=name + ext, content_type=content_type, size_bytes=len(body), status='downloaded')
                    item.pop('error', None)
                    break
                except Exception as exc:
                    last_error = type(exc).__name__ + ': ' + str(exc).split('https://')[0][:100]
                finally:
                    if request and response:
                        await response.dispose()
            else:
                item.update(status='failed', error=last_error)
    return attachments


def decode_attachments(row):
    row['attachments'] = json.loads(row.get('attachments') or '[]')
    return row
