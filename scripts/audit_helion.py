"""Audit Helion Hackathon results."""
import csv
from pathlib import Path
from collections import Counter

RANKED = Path("/Users/nickita/cv-rank/results/run_20260306_212348/RANKED.csv")

with open(RANKED, newline="", encoding="utf-8") as f:
    rows = list(csv.DictReader(f))

print(f"Total: {len(rows)} ranked applicants\n")

# Status breakdown
statuses = Counter(r.get("Helion_Hackathon_Status", "") for r in rows)
print("Status breakdown:")
for s, c in statuses.most_common():
    print(f"  {s}: {c}")

# Verdict breakdown
verdicts = Counter(r.get("Verdict", "") for r in rows)
print("\nVerdict breakdown:")
for v, c in verdicts.most_common():
    print(f"  {v}: {c}")

# Score distribution
scores = []
for r in rows:
    try:
        scores.append(float(r.get("Combined_Score", 0)))
    except (ValueError, TypeError):
        pass

if scores:
    scores.sort(reverse=True)
    print(f"\nScore distribution:")
    print(f"  Mean: {sum(scores)/len(scores):.3f}")
    print(f"  Median: {scores[len(scores)//2]:.3f}")
    print(f"  Range: {min(scores):.3f} — {max(scores):.3f}")

# P_Show distribution
pshows = []
for r in rows:
    ps = r.get("P_Show", "")
    if ps and ps != "":
        try:
            pshows.append(float(ps.replace("%", "")) / 100)
        except (ValueError, TypeError):
            pass

if pshows:
    print(f"\nP_Show distribution:")
    print(f"  Mean: {sum(pshows)/len(pshows):.1%}")
    print(f"  With P_Show: {len(pshows)}/{len(rows)}")

# Missing fields
missing_email = sum(1 for r in rows if not r.get("Email"))
missing_verdict = sum(1 for r in rows if not r.get("Verdict"))
missing_score = sum(1 for r in rows if not r.get("Combined_Score"))
missing_why = sum(1 for r in rows if not r.get("Specific_WHY"))
print(f"\nMissing fields:")
print(f"  Email: {missing_email}, Verdict: {missing_verdict}, Score: {missing_score}, WHY: {missing_why}")

# Top 15
print(f"\n{'='*100}")
print(f"TOP 15 ACCEPTS:")
print(f"{'='*100}")
print(f"{'Rank':<5} {'Name':<25} {'Score':<7} {'Verdict':<12} {'P_Show':<7} {'WHY'}")
print(f"{'-'*5} {'-'*25} {'-'*7} {'-'*12} {'-'*7} {'-'*40}")
for r in rows[:15]:
    name = r.get("Name", "")[:24]
    score = r.get("Combined_Score", "")
    verdict = r.get("Verdict", "")
    pshow = r.get("P_Show", "")
    why = r.get("Specific_WHY", "")[:60]
    rank = r.get("Rank", "")
    print(f"{rank:<5} {name:<25} {score:<7} {verdict:<12} {pshow:<7} {why}")

# Accept/reject boundary (5 above, 5 below cutline at rank 120)
print(f"\n{'='*100}")
print(f"ACCEPT/WAITLIST BOUNDARY (ranks 116-125):")
print(f"{'='*100}")
print(f"{'Rank':<5} {'Name':<25} {'Score':<7} {'Verdict':<12} {'P_Show':<7} {'Status':<10} {'WHY'}")
print(f"{'-'*5} {'-'*25} {'-'*7} {'-'*12} {'-'*7} {'-'*10} {'-'*40}")
for r in rows[115:125]:
    name = r.get("Name", "")[:24]
    score = r.get("Combined_Score", "")
    verdict = r.get("Verdict", "")
    pshow = r.get("P_Show", "")
    status = r.get("Helion_Hackathon_Status", "")
    why = r.get("Specific_WHY", "")[:50]
    rank = r.get("Rank", "")
    print(f"{rank:<5} {name:<25} {score:<7} {verdict:<12} {pshow:<7} {status:<10} {why}")

# Enrichment hit rate
linkedin_hl = sum(1 for r in rows if r.get("LinkedIn_Headline"))
yrs_exp = sum(1 for r in rows if r.get("Years_Experience"))
print(f"\nEnrichment hit rate:")
print(f"  LinkedIn headline: {linkedin_hl}/{len(rows)}")
print(f"  Years experience: {yrs_exp}/{len(rows)}")
