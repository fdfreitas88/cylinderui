import sqlite3,uuid
from datetime import datetime,timezone
from .config import DATA_DIR
DB=DATA_DIR/'agent.db'
def conn():
 c=sqlite3.connect(DB); c.row_factory=sqlite3.Row; return c
def init():
 with conn() as c:
  c.executescript('''CREATE TABLE IF NOT EXISTS conversations(id TEXT PRIMARY KEY,title TEXT,updated_at TEXT);CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY AUTOINCREMENT,conversation_id TEXT,role TEXT,content TEXT,created_at TEXT);''')
def now(): return datetime.now(timezone.utc).isoformat()
def ensure(cid,msg):
 cid=cid or str(uuid.uuid4()); t=msg.replace('\n',' ')[:80] or 'New conversation'
 with conn() as c:
  if c.execute('SELECT 1 FROM conversations WHERE id=?',(cid,)).fetchone(): c.execute('UPDATE conversations SET updated_at=? WHERE id=?',(now(),cid))
  else: c.execute('INSERT INTO conversations VALUES(?,?,?)',(cid,t,now()))
 return cid
def add(cid,role,content):
 with conn() as c:
  c.execute('INSERT INTO messages(conversation_id,role,content,created_at) VALUES(?,?,?,?)',(cid,role,content,now())); c.execute('UPDATE conversations SET updated_at=? WHERE id=?',(now(),cid))
def history(cid,limit=30):
 with conn() as c: rows=c.execute('SELECT role,content FROM messages WHERE conversation_id=? ORDER BY id DESC LIMIT ?',(cid,limit)).fetchall()
 return [dict(r) for r in reversed(rows)]
def conversations():
 with conn() as c: return [dict(r) for r in c.execute('SELECT id,title,updated_at FROM conversations ORDER BY updated_at DESC LIMIT 50')]
