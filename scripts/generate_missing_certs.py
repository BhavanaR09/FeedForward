import sys
import sqlite3, os
from pathlib import Path

# Ensure project root is on sys.path so imports like `config` and `utils` work
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from config import Config
    from utils import generate_delivery_certificate
except Exception as e:
    print('Failed importing project modules. Make sure you run this from the project or activate the venv.')
    print('Error:', e)
    raise

conn=sqlite3.connect(Config.DATABASE)
conn.row_factory=sqlite3.Row
cursor=conn.cursor()
rows=conn.execute("""
 SELECT d.id as donation_id, d.food_name, d.quantity, d.address, d.donor_id, fd.ngo_id, fd.volunteer_id
 FROM food_donations d
 JOIN food_distribution fd ON d.id = fd.donation_id
 WHERE d.status='Delivered' AND NOT EXISTS (SELECT 1 FROM certificates c WHERE c.donation_id = d.id)
""").fetchall()
print('to process:', len(rows))
for r in rows:
    donation_id=r['donation_id']
    donation_details=dict(r)
    donor = conn.execute("SELECT company_name FROM donors WHERE id = ?", (r['donor_id'],)).fetchone()
    ngo = conn.execute("SELECT ngo_name FROM ngos WHERE id = ?", (r['ngo_id'],)).fetchone()
    vol = conn.execute("SELECT name FROM volunteers WHERE id = ?", (r['volunteer_id'],)).fetchone()
    donor_name = donor['company_name'] if donor else ''
    ngo_name = ngo['ngo_name'] if ngo else ''
    volunteer_name = vol['name'] if vol else ''
    try:
        cert_path = generate_delivery_certificate(donation_id, donation_details, donor_name, ngo_name, volunteer_name)
        cursor.execute("INSERT INTO certificates (donation_id, volunteer_id, donor_id, ngo_id, certificate_path) VALUES (?, ?, ?, ?, ?)",
                       (donation_id, r['volunteer_id'], r['donor_id'], r['ngo_id'], cert_path))
        conn.commit()
        print('created:', donation_id, cert_path)
    except Exception as e:
        print('error creating for', donation_id, e)
conn.close()
