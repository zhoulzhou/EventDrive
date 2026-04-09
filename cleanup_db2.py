import sqlite3
conn = sqlite3.connect('data/db.sqlite3')
cursor = conn.cursor()

cursor.execute("DELETE FROM news WHERE source != '财联社头条'")
print(f'删除了 {cursor.rowcount} 条新闻')

conn.commit()

cursor.execute('SELECT source, COUNT(*) FROM news GROUP BY source')
print('\n剩余新闻:')
for row in cursor.fetchall():
    print(f'  {row[0]}: {row[1]}条')

cursor.execute('SELECT title, source FROM news LIMIT 10')
print('\n新闻列表:')
for row in cursor.fetchall():
    print(f'  [{row[1]}] {row[0][:40]}...')

conn.close()
