import sqlite3
from pathlib import Path

db_path = Path('data/db.sqlite3')
conn = sqlite3.connect(db_path)
cursor = conn.cursor()

cursor.execute('SELECT source, COUNT(*) FROM news GROUP BY source')
print('删除前各来源新闻数量:')
for row in cursor.fetchall():
    print(f'  {row[0]}: {row[1]}条')

cursor.execute("DELETE FROM news WHERE source != '财联社头条'")
deleted = cursor.rowcount

conn.commit()
print(f'\n已删除 {deleted} 条非财联社头条的新闻')

cursor.execute('SELECT source, COUNT(*) FROM news GROUP BY source')
print('\n删除后各来源新闻数量:')
for row in cursor.fetchall():
    print(f'  {row[0]}: {row[1]}条')

cursor.execute('SELECT COUNT(*) FROM news')
print(f'\n数据库中剩余新闻总数: {cursor.fetchone()[0]}条')

conn.close()
