"""Resumable vector work, never an active index or source of truth."""
import sqlite3
import time
from array import array

class VectorProgress:
    def __init__(self,root):
        root.mkdir(parents=True,exist_ok=True)
        self.db=sqlite3.connect(root/'vector-progress.sqlite',timeout=5)
        self.db.execute('CREATE TABLE IF NOT EXISTS vectors(key TEXT PRIMARY KEY,value BLOB,touched REAL)')
    def get(self,key):
        row=self.db.execute('SELECT value FROM vectors WHERE key=?',(key,)).fetchone()
        if not row:return None
        value=array('f');value.frombytes(row[0]);return value.tolist()
    def put_batch(self,rows):
        with self.db:
            self.db.executemany('INSERT OR REPLACE INTO vectors VALUES(?,?,?)',[(r['vector_cache_key'],array('f',r['vector']).tobytes(),time.time()) for r in rows])
    def trim_published(self,limit=20000):
        # Only published vectors are disposable. Never evict unfinished progress.
        with self.db:self.db.execute('DELETE FROM vectors WHERE key IN (SELECT key FROM vectors ORDER BY touched DESC LIMIT -1 OFFSET ?)',(limit,))
    def close(self):self.db.close()
