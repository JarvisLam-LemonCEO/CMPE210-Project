# ---------------------------------------------------------
# Import Required Libraries
# ---------------------------------------------------------
# sys         : used for command-line arguments
# statistics  : used for mean and standard deviation
import sys
import statistics

# ---------------------------------------------------------
# Read Input Filename
# ---------------------------------------------------------
# The CSV filename is passed as a command-line argument.
#
# Example:
# python analyze_latency.py latency_results.csv
fname = sys.argv[1]

# ---------------------------------------------------------
# Read Latency Values from CSV
# ---------------------------------------------------------
vals = []

with open(fname) as f:

    # Skip the CSV header line
    next(f)

    # Read each latency sample
    for line in f:

        # Remove whitespace/newlines
        s = line.strip()

        # Ignore empty lines and failed requests ("nan")
        if s and s.lower() != "nan":

            # Convert valid values to float
            vals.append(float(s))

# ---------------------------------------------------------
# Validate Samples
# ---------------------------------------------------------
# Exit if no valid latency samples were found.
if not vals:
    print("No valid latency samples found.")
    sys.exit(1)

# ---------------------------------------------------------
# Sort Values for Percentile Calculation
# ---------------------------------------------------------
vals_sorted = sorted(vals)

# ---------------------------------------------------------
# Percentile Function
# ---------------------------------------------------------
# Computes approximate percentile values.
#
# Example:
# P50 = median latency
# P95 = tail latency
def pct(xs, p):

    # Compute index position
    k = int(round((p / 100.0) * (len(xs) - 1)))

    return xs[k]

# ---------------------------------------------------------
# Print Statistical Results
# ---------------------------------------------------------
print(f"Samples: {len(vals)}")

# Average latency
print(f"Average: {statistics.mean(vals):.6f} s")

# Minimum latency
print(f"Min:     {min(vals):.6f} s")

# Maximum latency
print(f"Max:     {max(vals):.6f} s")

# 50th percentile latency (median)
print(f"P50:     {pct(vals_sorted, 50):.6f} s")

# 95th percentile latency (tail latency)
print(f"P95:     {pct(vals_sorted, 95):.6f} s")

# Standard deviation
# Only computed if more than one sample exists.
if len(vals) > 1:
    print(f"StdDev:  {statistics.stdev(vals):.6f} s")
