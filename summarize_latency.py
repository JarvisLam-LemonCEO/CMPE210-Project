import sys
import statistics

fname = sys.argv[1]

vals = []
with open(fname) as f:
    next(f)
    for line in f:
        s = line.strip()
        if s and s.lower() != "nan":
            vals.append(float(s))
import sys
import statistics

fname = sys.argv[1]

vals = []
with open(fname) as f:
    next(f)
    for line in f:
        s = line.strip()
        if s and s.lower() != "nan":
            vals.append(float(s))

if not vals:
    print("No valid latency samples found.")
    sys.exit(1)

vals_sorted = sorted(vals)

def pct(xs, p):
    k = int(round((p / 100.0) * (len(xs) - 1)))
    return xs[k]

print(f"Samples: {len(vals)}")
print(f"Average: {statistics.mean(vals):.6f} s")
print(f"Min:     {min(vals):.6f} s")
print(f"Max:     {max(vals):.6f} s")
print(f"P50:     {pct(vals_sorted, 50):.6f} s")
print(f"P95:     {pct(vals_sorted, 95):.6f} s")
if len(vals) > 1:
    print(f"StdDev:  {statistics.stdev(vals):.6f} s")

if not vals:
    print("No valid latency samples found.")
    sys.exit(1)

vals_sorted = sorted(vals)

def pct(xs, p):
    k = int(round((p / 100.0) * (len(xs) - 1)))
    return xs[k]

print(f"Samples: {len(vals)}")
print(f"Average: {statistics.mean(vals):.6f} s")
print(f"Min:     {min(vals):.6f} s")
print(f"Max:     {max(vals):.6f} s")
print(f"P50:     {pct(vals_sorted, 50):.6f} s")
print(f"P95:     {pct(vals_sorted, 95):.6f} s")
if len(vals) > 1:
    print(f"StdDev:  {statistics.stdev(vals):.6f} s")
