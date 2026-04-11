"""Reorder RANKED.csv by full 127-name application order."""
import csv

RANKED = '/Users/nickita/cv-rank/results/run_20260304_144508/RANKED.csv'
OUTPUT = '/Users/nickita/cv-rank/results/run_20260304_144508/OPENENV_R2_ORDERED.csv'

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
    "Gwendalynn", "ayush", "Antje", "Dioulo Rubinel", "Zala", "Kaniska",
    "Jagdish", "YuXuan", "Atharva", "Humair", "Eric", "rakshith", "Farseen",
    "Samee ur", "Harsh", "Osama", "Christian", "Praneet", "Guilherme",
    "RackSavant", "Samuel", "Arno", "Md Mujeeb", "Steven", "Paul",
]

with open(RANKED, 'r', encoding='utf-8') as f:
    rows = list(csv.DictReader(f))

# Build multiple lookup strategies
by_first_lower = {}
for r in rows:
    fn = r['First_Name'].strip().lower()
    by_first_lower.setdefault(fn, []).append(r)

by_full = {}
for r in rows:
    fn = r['First_Name'].strip().lower()
    ln = r['Last_Name'].strip().lower()
    by_full[f"{fn} {ln}"] = r

ordered = []
used_emails = set()
unmatched = []

for name in ORDER:
    nl = name.lower().strip()
    found = None

    # 1. Try full "first last" match
    for key, r in by_full.items():
        if key.startswith(nl) and r['Email'] not in used_emails:
            found = r
            break

    # 2. Try exact first name
    if not found and nl in by_first_lower:
        for r in by_first_lower[nl]:
            if r['Email'] not in used_emails:
                found = r
                break

    # 3. Try prefix match (3+ chars)
    if not found:
        for r in rows:
            fn = r['First_Name'].strip().lower()
            if len(nl) >= 3 and fn.startswith(nl[:3]) and nl[:3] == fn[:3] and r['Email'] not in used_emails:
                # Extra check: make sure it's a reasonable match
                if fn.startswith(nl) or nl.startswith(fn):
                    found = r
                    break

    # 4. Try Name column (for "RackSavant" etc)
    if not found:
        for r in rows:
            if nl in r['Name'].lower() and r['Email'] not in used_emails:
                found = r
                break

    if found:
        used_emails.add(found['Email'])
        status = found.get('openenv_r2_Status', '')
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
        unmatched.append(name)
        ordered.append({'Name': name, 'WHY': 'NOT FOUND', 'Status': '?'})

with open(OUTPUT, 'w', newline='', encoding='utf-8') as f:
    writer = csv.DictWriter(f, fieldnames=['Name', 'WHY', 'Status'])
    writer.writeheader()
    writer.writerows(ordered)

ac = sum(1 for r in ordered if r['Status'] == 'Approved')
pe = sum(1 for r in ordered if r['Status'] == 'Pending')
print(f"Wrote {len(ordered)} rows → {OUTPUT}")
print(f"  Approved: {ac}, Pending: {pe}")
if unmatched:
    print(f"  UNMATCHED ({len(unmatched)}): {unmatched}")

# Check if any ranked people were NOT placed
placed = {r['Name'] for r in ordered if r['Status'] != '?'}
missed = [r['Name'] for r in rows if r['Name'] not in placed]
if missed:
    print(f"  NOT IN ORDER LIST ({len(missed)}): {missed}")
