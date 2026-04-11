"""Reorder RANKED.csv by the original application order and output Name, WHY, Status."""
import csv

RANKED = '/Users/nickita/cv-rank/results/run_20260304_144508/RANKED.csv'
OUTPUT = '/Users/nickita/cv-rank/results/run_20260304_144508/OPENENV_R2_ORDERED.csv'

# Original application order (first names as provided)
ORDER = [
    "amrit", "Naman", "Dev", "Soham Dinesh", "Mannan", "Nihal", "JungDae",
    "Brandon", "Mathew", "Vincent", "Aksh", "Sarthak", "hongwei",
    "King John", "Shun", "Tushar", "Jenish", "Mohd", "Jun", "Warren",
    "Jarrod", "Vijay", "Suryaprakash", "Jay", "Sujash", "Gavin",
    "Shravanthi", "Aamish", "Apratim", "Rishbha", "Ahmad", "Rishabh",
    "Skyler", "Joshua", "Ryu", "Heidi", "Strahinja", "Juan", "Ashutosh",
    "Vamsi", "MAHIMA", "Gwen", "Arnav", "Shaya", "Rahul", "Jidhnyasaa",
    "Devansh", "Ayush", "Jason", "Dominic", "Joshua", "Gleb", "Saloni",
    "Aman", "Neeraja", "Ms", "Varun", "Abhishek", "Eduardo", "Dhiraj Deelip",
    "Pavan Kumar", "Kevin", "Dedeepya", "Ba Thien", "Sumanth",
    "Ravichandran", "Joyce", "Yigit", "Yug", "William", "Sean", "Tergel",
    "Ricardo", "Jupalli", "Sarthak", "Maximilian", "Ankita", "Karthik",
    "Aaron", "Vibha", "Smriti", "Kevin", "Dharamendra", "Eid", "Vishnu",
    "Andrew", "Blake", "sun", "Zubin", "Ed", "hasanalam", "Krishna",
    "Subra", "Zhencheng", "Anup", "Chetan", "Vikhas", "Uppala", "Vivian",
    "Csaba", "Shubham", "Olivier",
]

with open(RANKED, 'r', encoding='utf-8') as f:
    reader = csv.DictReader(f)
    rows = list(reader)

# Build lookup by first name (case-insensitive)
by_first = {}
for r in rows:
    fn = r['First_Name'].strip()
    key = fn.lower()
    if key not in by_first:
        by_first[key] = []
    by_first[key].append(r)

# Also build lookup by "First Last" for multi-word first names
by_full_first = {}
for r in rows:
    fn = r['First_Name'].strip()
    ln = r['Last_Name'].strip()
    full = f"{fn} {ln}".lower()
    by_full_first[full] = r
    # Also just first name + first word of last
    by_full_first[fn.lower()] = r

ordered = []
used_emails = set()
joshua_idx = 0  # track which Joshua we're on
kevin_idx = 0
sarthak_idx = 0

for name in ORDER:
    name_lower = name.lower().strip()

    # Try exact full first name match first (for "Soham Dinesh", "King John", etc.)
    found = None

    # Check full first+last combo
    for r in rows:
        fn = r['First_Name'].strip().lower()
        ln = r['Last_Name'].strip().lower()
        full = f"{fn} {ln}"
        if full.startswith(name_lower) and r['Email'] not in used_emails:
            found = r
            break
        if fn == name_lower and r['Email'] not in used_emails:
            found = r
            break

    if not found:
        # Try partial match
        for r in rows:
            fn = r['First_Name'].strip().lower()
            if fn.startswith(name_lower[:3]) and r['Email'] not in used_emails:
                found = r
                break

    if found:
        used_emails.add(found['Email'])
        status = found.get('openenv_r2_Status', '')
        # Map WAITLIST -> Pending
        if status == 'WAITLIST':
            status = 'Pending'
        elif status == 'ACCEPT':
            status = 'Approved'
        ordered.append({
            'Name': found['Name'],
            'WHY': found.get('Specific_WHY', ''),
            'Status': status,
        })
    else:
        ordered.append({
            'Name': name,
            'WHY': f'NOT FOUND in ranked results',
            'Status': '?',
        })
        print(f"WARNING: Could not match '{name}'")

with open(OUTPUT, 'w', newline='', encoding='utf-8') as f:
    writer = csv.DictWriter(f, fieldnames=['Name', 'WHY', 'Status'])
    writer.writeheader()
    writer.writerows(ordered)

# Summary
accept_count = sum(1 for r in ordered if r['Status'] == 'Approved')
pending_count = sum(1 for r in ordered if r['Status'] == 'Pending')
unknown_count = sum(1 for r in ordered if r['Status'] == '?')
print(f"\nWrote {len(ordered)} rows to {OUTPUT}")
print(f"  Approved: {accept_count}")
print(f"  Pending:  {pending_count}")
print(f"  Unknown:  {unknown_count}")
