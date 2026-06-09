import sqlite3
from pathlib import Path
BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / 'dgx_access.sqlite'
print('DB', DB_PATH.exists(), DB_PATH)
conn = sqlite3.connect(str(DB_PATH))
conn.row_factory = sqlite3.Row
cur = conn.cursor()
label_like = '%TEST-GPU%'
rows = cur.execute("SELECT id,label,status,created_at FROM inventory_items WHERE label LIKE ?", (label_like,)).fetchall()
print('Found', len(rows))
for r in rows:
    print(dict(r))
if rows:
    ids = [r['id'] for r in rows]
    print('Deleting', ids)
    cur.executemany('DELETE FROM inventory_items WHERE id = ?', [(i,) for i in ids])
    conn.commit()
    print('Deleted')
rows2 = cur.execute("SELECT id,label,status,created_at FROM inventory_items WHERE label LIKE ?", (label_like,)).fetchall()
print('Remaining', len(rows2))
conn.close()
