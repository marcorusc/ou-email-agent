from __future__ import annotations

import imaplib
import re
import ssl
from datetime import datetime, timedelta, timezone
from email import policy
from email.parser import BytesParser
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser

from .store import now


class PlainHTML(HTMLParser):
    def __init__(self):
        super().__init__(); self.text = []; self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in {'script', 'style'}:
            self.hidden += 1
        if tag in {'p','br','div','li','tr'}:
            self.text.append('\n')

    def handle_endtag(self, tag):
        if tag in {'script','style'}:
            self.hidden = max(0, self.hidden - 1)

    def handle_data(self, data):
        if not self.hidden:
            self.text.append(data)


def parse_message(raw: bytes, internal_date: str = '') -> dict:
    msg = BytesParser(policy=policy.default).parsebytes(raw)
    attachments = [str(p.get_filename()) for p in msg.walk() if p.get_filename()]
    part = msg.get_body(preferencelist=('plain','html'))
    body = ''
    if part:
        try:
            body = part.get_content()
        except (LookupError, UnicodeError):
            body = (part.get_payload(decode=True) or b'').decode('utf-8', errors='replace')
        if part.get_content_type() == 'text/html':
            html = PlainHTML(); html.feed(body); body = ''.join(html.text)
    date = None
    for value in [str(msg.get('Date', '')), internal_date]:
        try:
            date = parsedate_to_datetime(value)
            if date:
                date = date.replace(tzinfo=timezone.utc) if date.tzinfo is None else date
                break
        except (ValueError, TypeError, OverflowError):
            pass
    # Preserve complete body text in the local store. Model calls use bounded chunks.
    return {
        'message_id': str(msg.get('Message-ID', '')).strip(),
        'references': re.findall(r'<[^<>]+>', str(msg.get('References', '')) + ' ' + str(msg.get('In-Reply-To', ''))),
        'subject': str(msg.get('Subject', '')), 'sender': str(msg.get('From', '')),
        'recipients': str(msg.get('To', '')) + '; ' + str(msg.get('Cc', '')),
        'date': date.astimezone(timezone.utc).isoformat() if date else now(),
        'body': body.replace('\x00', ''), 'attachments': attachments,
    }


def connect(config: dict, secret: str | None = None):
    client = imaplib.IMAP4_SSL(config['host'], config['port'],
                              ssl_context=ssl.create_default_context(), timeout=30)
    if secret is not None:
        try:
            # Credentials only travel over a certificate-verified TLS connection.
            client.login(config['username'], secret)
        except Exception:
            client.logout()
            raise
    return client


def check_connection(config, secret=None):
    with connect(config, secret) as client:
        result = {'host': config['host'], 'tls': client.sock.version(),
                  'authenticated': secret is not None,
                  'capabilities': [c.decode() if isinstance(c, bytes) else c for c in client.capabilities]}
        if secret is not None:
            typ, folders = client.list()
            if typ != 'OK':
                raise RuntimeError('Could not list mailboxes')
            result['folders'] = [x.decode(errors='replace') for x in folders if x]
            typ, count = client.select('INBOX', readonly=True)
            if typ != 'OK':
                raise RuntimeError('Could not open INBOX read-only')
            result['inbox_messages'] = int(count[0])
        return result


def sync(config, store, secret: str, client_factory=None):
    total = 0
    factory = client_factory or connect
    # Fix the historical cutoff at first start. Retries cannot move it forward.
    cutoff = store.state('initial_cutoff')
    if cutoff is None:
        cutoff = now(); store.state('initial_cutoff', cutoff)
    start = datetime.fromisoformat(cutoff) - timedelta(days=config['initial_days'])
    since = f"{start.day:02d}-{['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'][start.month-1]}-{start.year}"
    with factory(config, secret) as client:
        for folder in config['folders']:
            # Quote mailbox names, including spaces; reject protocol control characters.
            if any(c in folder for c in '\r\n\x00'):
                raise ValueError('Invalid mailbox name')
            quoted = '"' + folder.replace('\\','\\\\').replace('"','\\"') + '"'
            typ, _ = client.select(quoted, readonly=True)
            if typ != 'OK':
                raise RuntimeError(f'Cannot select folder {folder!r} read-only')
            _, values = client.response('UIDVALIDITY')
            if not values or not values[0]:
                raise RuntimeError('Server omitted UIDVALIDITY; refusing an unsafe checkpoint')
            validity = values[0].decode() if isinstance(values[0], bytes) else str(values[0])
            checkpoint = store.db.execute('SELECT * FROM checkpoints WHERE folder=?', (folder,)).fetchone()
            reset = checkpoint is not None and checkpoint['validity'] != validity
            last = checkpoint['last_uid'] if checkpoint and not reset else 0
            initial_done = bool(checkpoint and checkpoint['initial_done'])
            criteria = ('UID', f'{last+1}:*') if last else (('ALL',) if reset else ('SINCE', since))
            typ, data = client.uid('SEARCH', None, *criteria)
            if typ != 'OK':
                raise RuntimeError('IMAP UID search failed')
            # IMAP n:* can return the latest UID even if n exceeds it.
            uids = sorted(int(x) for x in (data[0] or b'').split() if int(x) > last)
            batch = uids[:config['batch_size']]
            for uid in batch:
                typ, meta = client.uid('FETCH', str(uid), '(RFC822.SIZE INTERNALDATE)')
                if typ != 'OK':
                    raise RuntimeError(f'IMAP metadata fetch failed for UID {uid}')
                meta_bytes = b' '.join(x for x in meta if isinstance(x, bytes))
                size = re.search(rb'RFC822.SIZE (\d+)', meta_bytes)
                if size and int(size[1]) > config['max_message_bytes']:
                    # Do not skip silently or fetch a huge attachment automatically.
                    store.state('oversize_message', f'{folder} UID {uid}: increase max_message_bytes to include it')
                    raise RuntimeError(f'Message UID {uid} exceeds configured download limit; sync paused for this folder')
                internal = re.search(rb'INTERNALDATE "([^"]+)"', meta_bytes)
                typ, payload = client.uid('FETCH', str(uid), '(BODY.PEEK[])')
                if typ != 'OK':
                    raise RuntimeError(f'IMAP body fetch failed for UID {uid}')
                literals = [x[1] for x in payload if isinstance(x, tuple) and isinstance(x[1], bytes)]
                if not literals:
                    # Expunged between SEARCH and FETCH: retry next cycle, never advance past a failure.
                    raise RuntimeError(f'Message UID {uid} disappeared during fetch; retry sync')
                raw = b''.join(literals)
                mail = parse_message(raw, internal[1].decode() if internal else '')
                received = mail['date']
                if internal:
                    try:
                        received = parsedate_to_datetime(internal[1].decode()).astimezone(timezone.utc).isoformat()
                    except (ValueError, TypeError):
                        pass
                historical = received <= cutoff
                total += int(store.insert(mail, folder, validity, uid, historical))
                last = uid
                with store.db:
                    store.db.execute('INSERT OR REPLACE INTO checkpoints VALUES (?,?,?,?)',
                                     (folder, validity, last, int(initial_done)))
            with store.db:
                store.db.execute('INSERT OR REPLACE INTO checkpoints VALUES (?,?,?,?)',
                                 (folder, validity, last, int(initial_done or len(uids) <= len(batch))))
    store.state('last_sync', now())
    store.state('sync_error', '')
    return {'new_messages': total}
