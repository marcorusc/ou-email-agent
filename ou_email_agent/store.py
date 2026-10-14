from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.directory / "email.sqlite3", timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS messages (
          id INTEGER PRIMARY KEY, source_key TEXT UNIQUE NOT NULL,
          message_id TEXT, references_json TEXT NOT NULL, subject TEXT, sender TEXT,
          recipients TEXT, date TEXT, received_at TEXT NOT NULL, body TEXT,
          attachments_json TEXT, content_hash TEXT NOT NULL, historical INTEGER NOT NULL,
          analysis_json TEXT, analysis_attempts INTEGER NOT NULL DEFAULT 0,
          analysis_error TEXT, notified INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS locations (
          folder TEXT, validity TEXT, uid INTEGER, message INTEGER REFERENCES messages(id),
          PRIMARY KEY(folder,validity,uid)
        );
        CREATE TABLE IF NOT EXISTS checkpoints (
          folder TEXT PRIMARY KEY, validity TEXT, last_uid INTEGER, initial_done INTEGER
        );
        CREATE TABLE IF NOT EXISTS agenda (
          id TEXT PRIMARY KEY, message INTEGER REFERENCES messages(id), details_json TEXT,
          calendar_status TEXT NOT NULL DEFAULT 'pending', calendar_ref TEXT,
          obsidian_status TEXT NOT NULL DEFAULT 'pending', obsidian_ref TEXT,
          updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS chunks (
          message INTEGER REFERENCES messages(id), part INTEGER, text TEXT,
          model TEXT, vector_json TEXT, PRIMARY KEY(message,part,model)
        );
        CREATE TABLE IF NOT EXISTS embedding_jobs (
          message INTEGER REFERENCES messages(id), model TEXT, status TEXT NOT NULL,
          error_code TEXT, http_status INTEGER, attempts INTEGER NOT NULL DEFAULT 0,
          updated_at TEXT NOT NULL, PRIMARY KEY(message,model)
        );
        INSERT OR IGNORE INTO embedding_jobs(message,model,status,updated_at)
          SELECT DISTINCT message,model,'complete',datetime('now') FROM chunks WHERE vector_json IS NOT NULL;
        CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT);
        CREATE VIRTUAL TABLE IF NOT EXISTS message_fts USING fts5(
          subject,sender,body,content='messages',content_rowid='id',tokenize='unicode61'
        );
        CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
          INSERT INTO message_fts(rowid,subject,sender,body)
          VALUES(new.id,new.subject,new.sender,new.body);
        END;
        ''')

    def close(self):
        self.db.close()

    def state(self, key, value=None):
        if value is not None:
            with self.db:
                self.db.execute("INSERT OR REPLACE INTO state VALUES (?,?)", (key, str(value)))
        row = self.db.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def insert(self, mail: dict, folder: str, validity: str, uid: int, historical: bool) -> bool:
        digest = hashlib.sha256((mail['subject'] + mail['sender'] + mail['date'] + mail['body']).encode()).hexdigest()
        key = hashlib.sha256((mail['message_id'] + ':' + digest).encode()).hexdigest()
        with self.db:
            cur = self.db.execute('''INSERT OR IGNORE INTO messages
              (source_key,message_id,references_json,subject,sender,recipients,date,received_at,
               body,attachments_json,content_hash,historical)
              VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''',
              (key, mail['message_id'], json.dumps(mail['references']), mail['subject'],
               mail['sender'], mail['recipients'], mail['date'], now(), mail['body'],
               json.dumps(mail['attachments']), digest, int(historical)))
            new = cur.rowcount == 1
            mid = self.db.execute("SELECT id FROM messages WHERE source_key=?", (key,)).fetchone()[0]
            self.db.execute("INSERT OR REPLACE INTO locations VALUES (?,?,?,?)", (folder, validity, uid, mid))
        return new

    def get(self, mid: int) -> dict:
        row = self.db.execute("SELECT * FROM messages WHERE id=?", (mid,)).fetchone()
        if row is None:
            raise ValueError("Unknown local email ID")
        item = dict(row)
        for key in ['references', 'attachments', 'analysis']:
            value = item.pop(key + '_json')
            item[key] = json.loads(value) if value else None
        item['locations'] = [dict(x) for x in self.db.execute(
            "SELECT folder,validity,uid FROM locations WHERE message=?", (mid,))]
        item['source'] = f"ou-email://message/{mid}"
        return item

    def thread(self, mid: int, limit: int = 30) -> list[dict]:
        seed = self.get(mid)
        ids = set(seed['references'] or []) | {seed['message_id']}
        ids.discard('')
        found = {mid}
        # Follow References/In-Reply-To rather than joining unrelated matching subjects.
        rows = list(self.db.execute("SELECT id,message_id,references_json FROM messages"))
        changed = True
        while changed and len(found) < limit:
            changed = False
            for row in rows:
                links = set(json.loads(row['references_json'])) | {row['message_id']}
                links.discard('')
                if row['id'] not in found and links & ids:
                    found.add(row['id']); ids |= links; changed = True
                    if len(found) >= limit:
                        break
        return sorted((self.get(i) for i in found), key=lambda m: m['date'])

    def search(self, query: str, sender: str = '', after: str = '', before: str = '',
               limit: int = 10, vector=None, model='', recipient: str = '') -> list[dict]:
        limit = max(1, min(limit, 50))
        if not query.strip() and not (sender or recipient or after or before):
            raise ValueError("Provide a topic, sender, recipient, or date filter.")
        filters, args = [], []
        if sender:
            filters.append("instr(lower(m.sender),lower(?))>0"); args.append(sender)
        if recipient:
            filters.append("instr(lower(m.recipients),lower(?))>0"); args.append(recipient)
        for value, op in [(after, '>='), (before, '<=')]:
            if value:
                datetime.strptime(value, '%Y-%m-%d')
                filters.append(f"substr(m.date,1,10){op}?"); args.append(value)
        where = ' AND '.join(filters) or '1=1'
        terms = re.findall(r'\w+', query, re.UNICODE)
        scores = {}
        if terms:
            match = ' OR '.join('"' + t.replace('"', '""') + '"' for t in terms)
            rows = self.db.execute(f'''SELECT m.id FROM message_fts f JOIN messages m ON m.id=f.rowid
                WHERE message_fts MATCH ? AND {where} ORDER BY bm25(message_fts) LIMIT 100''',
                [match] + args).fetchall()
            for rank, row in enumerate(rows):
                scores[row[0]] = 1 / (60 + rank)
            # Substring fallback also supports unsegmented Japanese and partial identifiers.
            rows = self.db.execute(f'''SELECT m.id FROM messages m WHERE {where}
                AND (instr(lower(m.body),lower(?))>0 OR instr(lower(m.subject),lower(?))>0)
                ORDER BY m.date DESC LIMIT 100''', args + [query, query]).fetchall()
            for rank, row in enumerate(rows):
                scores[row[0]] = scores.get(row[0], 0) + 1 / (60 + rank)
        elif not query.strip():
            rows = self.db.execute(f'SELECT m.id FROM messages m WHERE {where} ORDER BY m.date DESC LIMIT ?',
                                   args + [limit]).fetchall()
            scores = {r[0]: 1 / (60 + n) for n, r in enumerate(rows)}
        if vector is not None:
            best = {}
            for row in self.db.execute(f'''SELECT c.message,c.vector_json FROM chunks c JOIN messages m
              ON m.id=c.message WHERE c.model=? AND c.vector_json IS NOT NULL AND {where}''', [model] + args):
                other = json.loads(row['vector_json'])
                if len(vector) != len(other):
                    continue
                denom = math.sqrt(sum(v*v for v in vector) * sum(v*v for v in other))
                similarity = sum(a*b for a,b in zip(vector,other)) / denom if denom else 0
                best[row['message']] = max(best.get(row['message'], -1), similarity)
            for rank, (mid, similarity) in enumerate(sorted(best.items(), key=lambda x: -x[1])[:100]):
                if similarity > 0:
                    scores[mid] = scores.get(mid, 0) + 1 / (60 + rank)
        result = []
        for mid in sorted(scores, key=scores.get, reverse=True)[:limit]:
            item = self.get(mid)
            result.append({k: item[k] for k in ['id','source','subject','sender','recipients','date','analysis']} |
                          {'excerpt': item['body'][:1200], 'rank_score': round(scores[mid], 5)})
        return result

    def save_analysis(self, mid: int, analysis: dict):
        with self.db:
            self.db.execute("UPDATE messages SET analysis_json=?,analysis_error=NULL WHERE id=?",
                            (json.dumps(analysis, ensure_ascii=False), mid))
            for i, event in enumerate(analysis['agenda_items']):
                key = hashlib.sha256(f'{mid}:{i}'.encode()).hexdigest()[:24]
                self.db.execute('''INSERT OR IGNORE INTO agenda(id,message,details_json,updated_at)
                  VALUES (?,?,?,?)''', (key, mid, json.dumps(event, ensure_ascii=False), now()))

    def pending(self, limit: int = 50):
        result = []
        for row in self.db.execute('''SELECT a.*,m.subject,m.sender,m.date,m.historical FROM agenda a
          JOIN messages m ON a.message=m.id WHERE calendar_status='pending' OR obsidian_status='pending'
          ORDER BY a.updated_at LIMIT ?''', (max(1,min(limit,100)),)):
            item = dict(row)
            item['details'] = json.loads(item.pop('details_json'))
            item['source'] = f"ou-email://message/{item['message']}"
            result.append(item)
        return result

    def acknowledge(self, item_id: str, destination: str, status: str, reference: str):
        if destination not in {'calendar', 'obsidian'} or status not in {'done', 'dismissed'}:
            raise ValueError("Use calendar/obsidian and done/dismissed.")
        if not reference.strip():
            raise ValueError("Provide the event ID, note path, or dismissal reason.")
        with self.db:
            row = self.db.execute(f'SELECT {destination}_status,{destination}_ref FROM agenda WHERE id=?',
                                  (item_id,)).fetchone()
            if row is None:
                raise ValueError("Unknown agenda item")
            if row[0] != 'pending' and (row[0] != status or row[1] != reference):
                raise ValueError("Item already acknowledged differently; inspect before retrying.")
            self.db.execute(f'UPDATE agenda SET {destination}_status=?,{destination}_ref=?,updated_at=? WHERE id=?',
                            (status, reference, now(), item_id))
        return {'id': item_id, 'destination': destination, 'status': status}

    def status(self):
        counts = {name: self.db.execute(f'SELECT count(*) FROM {name}').fetchone()[0]
                  for name in ['messages','chunks','agenda']}
        counts['chunks'] = self.db.execute('SELECT count(*) FROM chunks WHERE vector_json IS NOT NULL').fetchone()[0]
        counts['embedding_jobs'] = [dict(row) for row in self.db.execute(
            'SELECT model,status,error_code,COUNT(*) AS count FROM embedding_jobs GROUP BY model,status,error_code')]
        counts['unanalyzed'] = self.db.execute('SELECT count(*) FROM messages WHERE analysis_json IS NULL').fetchone()[0]
        counts['failed_analysis'] = self.db.execute('SELECT count(*) FROM messages WHERE analysis_error IS NOT NULL').fetchone()[0]
        counts['pending_agenda'] = self.db.execute("SELECT count(*) FROM agenda WHERE calendar_status='pending' OR obsidian_status='pending'").fetchone()[0]
        counts['state'] = dict(self.db.execute('SELECT key,value FROM state'))
        return counts
