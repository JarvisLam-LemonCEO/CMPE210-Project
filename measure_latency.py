import subprocess, statistics, time

VIP_URL = "http://10.0.0.100:8000/"
N = 100
SLEEP = 0.05

def find_h1_pid():
    out = subprocess.check_output(["bash", "-lc", "ps -eo pid,cmd | grep 'mininet:h1' | grep -v grep | head -n1"])
    return int(out.decode().strip().split(None, 1)[0])

def curl_time(h1_pid):
    cmd = f"sudo mnexec -a {h1_pid} curl --no-keepalive -s -o /dev/null -w '%{{time_total}}' {VIP_URL}"
    out = subprocess.check_output(["bash", "-lc", cmd])
    return float(out.decode().strip())

def percentile(xs, p):
    xs = sorted(xs)
    k = int(round((p/100.0) * (len(xs)-1)))
    return xs[k]

def main():
    h1 = find_h1_pid()
    times = []

    # warm-up
    for _ in range(5):
        try: curl_time(h1)
        except: pass

    for i in range(N):
        t = curl_time(h1)
        times.append(t)
        time.sleep(SLEEP)

    avg = statistics.mean(times)
    p95 = percentile(times, 95)
    print(f"N={N} avg={avg:.4f}s p95={p95:.4f}s min={min(times):.4f}s max={max(times):.4f}s")

if __name__ == "__main__":
    main()