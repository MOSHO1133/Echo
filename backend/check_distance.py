from app import embeddings, db

conn = db.get_conn()
rows = conn.execute('SELECT id, title FROM papers WHERE in_library=1').fetchall()
conn.close()
paper_ids = [r['id'] for r in rows]
titles = {r['id']: r['title'] for r in rows}

q_emb = embeddings.encode_query('i want to make a boat')
for pid in paper_ids:
    chunks = embeddings.query_chunks_by_vector(q_emb, k=3, paper_ids=[pid], user_id='112175817815227736477')
    if chunks:
        best = min(c['distance'] for c in chunks)
        print(f'{titles[pid][:40]:40s} best distance: {best:.3f}')
    else:
        print(f'{titles[pid][:40]:40s} no chunks found')