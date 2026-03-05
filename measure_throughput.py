import subprocess, re, time

DURATION = 8
PORT = 5201
VIP_IP = "10.0.0.100"

def run_mn(cmd):
    return subprocess.check_output(["bash", "-lc", cmd]).decode()

def find_pid(host):
    out = subprocess.check_output(["bash", "-lc", f"ps -eo pid,cmd | grep 'mininet:{host}' | grep -v grep | head -n1"])
    return int(out.decode().strip().split(None, 1)[0])

def mnexec(pid, cmd):
    return subprocess.check_output(["bash", "-lc", f"sudo mnexec -a {pid} {cmd}"]).decode()

def main():
    h1 = find_pid("h1")
    h2 = find_pid("h2")
    h3 = find_pid("h3")
    h4 = find_pid("h4")

    # start iperf servers
    for pid in [h2, h3, h4]:
        try:
            mnexec(pid, f"pkill -f 'iperf3 -s' || true")
        except: pass
        mnexec(pid, f"iperf3 -s -p {PORT} -D")

    time.sleep(0.5)

    # client test to VIP
    out = mnexec(h1, f"iperf3 -c {VIP_IP} -p {PORT} -t {DURATION}")
    # parse sender line
    m = re.findall(r"sender.*?([0-9.]+)\s+Mbits/sec", out)
    if m:
        print("Throughput(Mbits/sec):", m[-1])
    else:
        print(out)

if __name__ == "__main__":
    main()