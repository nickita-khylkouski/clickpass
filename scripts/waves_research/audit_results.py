"""Audit cv-rank results."""
import csv
import statistics

RESULTS = '/Users/nickita/cv-rank/results/run_20260304_144508/RANKED.csv'

with open(RESULTS, 'r', encoding='utf-8') as f:
    reader = csv.DictReader(f)
    rows = list(reader)

print(f"=== AUDIT: {len(rows)} total rows ===\n")

# Check for missing fields
missing = {'Name': 0, 'Combined_Score': 0, 'Verdict': 0, 'Email': 0}
for r in rows:
    if not r.get('Name', '').strip(): missing['Name'] += 1
    if not r.get('Combined_Score', '').strip(): missing['Combined_Score'] += 1
    if not r.get('Verdict', '').strip(): missing['Verdict'] += 1
    if not r.get('Email', '').strip(): missing['Email'] += 1
print(f"Missing fields: {missing}")

# Duplicate emails
emails = [r['Email'].strip().lower() for r in rows if r.get('Email')]
dupes = [e for e in set(emails) if emails.count(e) > 1]
print(f"Duplicate emails: {len(dupes)} → {dupes if dupes else 'none'}")

# Score distribution
scores = []
for r in rows:
    try:
        s = float(r.get('Combined_Score', 0))
        scores.append(s)
    except (ValueError, TypeError):
        pass

print(f"\nScore distribution ({len(scores)} scored):")
print(f"  Mean:   {statistics.mean(scores):.3f}")
print(f"  Median: {statistics.median(scores):.3f}")
print(f"  Stdev:  {statistics.stdev(scores):.3f}")
print(f"  Min:    {min(scores):.3f}")
print(f"  Max:    {max(scores):.3f}")

# Status from the event-specific column
status_col = [c for c in rows[0].keys() if c.endswith('_Status')]
status_key = status_col[0] if status_col else None
print(f"\nStatus column: {status_key}")

# NaN/zero scores in ACCEPTs
if status_key:
    accepts = [r for r in rows if r.get(status_key) == 'ACCEPT']
    bad_scores = [r for r in accepts if not r.get('Combined_Score') or float(r.get('Combined_Score', 0)) == 0]
    print(f"NaN/zero scores in ACCEPTs: {len(bad_scores)}")

# Verdict breakdown
verdicts = {}
for r in rows:
    v = r.get('Verdict', 'UNKNOWN')
    verdicts[v] = verdicts.get(v, 0) + 1
print(f"\nVerdict breakdown:")
for v, c in sorted(verdicts.items(), key=lambda x: -x[1]):
    print(f"  {v}: {c}")

# Status breakdown
if status_key:
    statuses = {}
    for r in rows:
        s = r.get(status_key, 'UNKNOWN')
        statuses[s] = statuses.get(s, 0) + 1
    print(f"\nStatus breakdown:")
    for s, c in sorted(statuses.items(), key=lambda x: -x[1]):
        print(f"  {s}: {c}")

# Top 10
print(f"\n=== TOP 10 ===")
print(f"{'Rank':<5} {'Name':<25} {'Score':<8} {'Verdict':<14} {'Why (first 80 chars)'}")
for r in rows[:10]:
    name = r.get('Name', 'N/A')[:24]
    score = r.get('Combined_Score', 'N/A')
    verdict = r.get('Verdict', 'N/A')
    reason = r.get('Specific_WHY', '')[:80]
    rank = r.get('Rank', '?')
    print(f"  {rank:<4} {name:<25} {score:<8} {verdict:<14} {reason}")

# Cutline (5 above + 5 below the accept/waitlist boundary)
print(f"\n=== CUTLINE (rank 46-55) ===")
for r in rows[45:55]:
    name = r.get('Name', 'N/A')[:24]
    score = r.get('Combined_Score', 'N/A')
    verdict = r.get('Verdict', 'N/A')
    status = r.get(status_key, '?') if status_key else '?'
    rank = r.get('Rank', '?')
    swiss = f"{r.get('Swiss_Wins', '?')}W-{r.get('Swiss_Losses', '?')}L"
    print(f"  #{rank:<4} {name:<25} {score:<8} {verdict:<14} {swiss:<8} → {status}")

# Enrichment hit rate
linkedin_hits = sum(1 for r in rows if r.get('LinkedIn_Headline', '').strip())
github_stars = sum(1 for r in rows if r.get('GitHub_Stars', '').strip() and r.get('GitHub_Stars', '0') != '0')
github_repos = sum(1 for r in rows if r.get('GitHub_Repos', '').strip() and r.get('GitHub_Repos', '0') != '0')
yoe = sum(1 for r in rows if r.get('Years_Experience', '').strip())
cv_events = sum(1 for r in rows if r.get('Total_CV_Events', '').strip() and r.get('Total_CV_Events', '0') != '0')
print(f"\nEnrichment hit rate:")
print(f"  LinkedIn headline: {linkedin_hits}/{len(rows)}")
print(f"  GitHub stars > 0:  {github_stars}/{len(rows)}")
print(f"  GitHub repos > 0:  {github_repos}/{len(rows)}")
print(f"  Years experience:  {yoe}/{len(rows)}")
print(f"  CV event history:  {cv_events}/{len(rows)}")

# Anomalies
print(f"\n=== ANOMALY CHECK ===")
anomalies = 0
for r in rows:
    rank = int(r.get('Rank', 999))
    verdict = r.get('Verdict', '')
    name = r.get('Name', '?')
    if rank <= 20 and verdict == 'NO':
        print(f"  ANOMALY: #{rank} {name} ranked high but verdict=NO")
        anomalies += 1
    if rank > 80 and verdict in ('STRONG YES', 'YES'):
        print(f"  ANOMALY: #{rank} {name} ranked low but verdict={verdict}")
        anomalies += 1

# Suspicious names
for r in rows:
    name = r.get('Name', '')
    if len(name) < 3 or name.lower() in ['test', 'asdf', 'xxx']:
        print(f"  SUSPICIOUS NAME: '{name}' (email: {r.get('Email', '?')})")
        anomalies += 1

if anomalies == 0:
    print("  No anomalies found.")

print("\n=== AUDIT COMPLETE ===")
