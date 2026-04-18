# SJSU Spring 2026 CMPE 210 Course Project
Members:
1. Hei Lam
2. Weiyu Wang
   
# Step 1 — Set Up SDN Environment and Basic Topology
### Goal
Create a simple network where:
```bash
Client → OpenFlow Switch → 3 Servers
```
and verify that traffic can flow through the Ryu controller.

## 1 Install Required Tools
If you are using Ubuntu or a Mininet VM:
```bash
sudo apt update
sudo apt install mininet
sudo apt install openvswitch-switch
sudo apt install python3-pip
sudo apt install iperf3
pip install ryu
pip install scikit-learn
pip install pandas
```
Verify installation:
```bash
mn --version
ryu-manager --version
iperf3 --version
```

## 2 Create the Network Topology
Run Mininet with a simple topology.
```bash 
sudo mn --topo single,4 --controller remote,ip=127.0.0.1 --switch ovsk,protocols=OpenFlow13 --mac
```

Explanation:
## Components

| Component | Meaning            |
| --------- | ------------------ |
| single,4  | 1 switch + 4 hosts |
| h1        | client             |
| h2        | server1            |
| h3        | server2            |
| h4        | server3            |

## 3 Start the Ryu Controller
Open another terminal and run:
```bash
ryu-manager ryu.app.simple_switch_13
```
This controller installs basic OpenFlow forwarding rules.

## 4 Verify Network Connectivity

```bash
pingall
```

Expected:

```bash
*** Results: 0% dropped
```

## 5 Test Client → Server Communication

```bash
mininet> h2 python3 -m http.server 8000 --bind 10.0.0.2 &
mininet> h3 python3 -m http.server 8000 --bind 10.0.0.3 &
mininet> h4 python3 -m http.server 8000 --bind 10.0.0.4 &
```

Test:

```bash
mininet> h1 curl 10.0.0.2:8000
```

## 6 Observe Flow Rules
Check OpenFlow rules:
```bash
sudo ovs-ofctl -O OpenFlow13 dump-flows s1
```
You will see rules installed by the controller.


# Step 2 — Round Robin Load Balancer

### Goal

* Client sends traffic to VIP (10.0.0.100)
* Controller selects backend
* NAT (DNAT + SNAT) applied
* Flow rules installed (fast path)

## Start Controller

```bash
ryu-manager lb_nat_rr.py
```

## Start Mininet

```bash
sudo mn -c
sudo mn --topo single,4 --controller remote,ip=127.0.0.1 --switch ovsk,protocols=OpenFlow13 --mac
```

```bash
mininet> pingall
```
## Start Backend Services

### HTTP

```bash
mininet> h2 python3 -m http.server 8000 --bind 10.0.0.2 &
mininet> h3 python3 -m http.server 8000 --bind 10.0.0.3 &
mininet> h4 python3 -m http.server 8000 --bind 10.0.0.4 &
```

This ensures flows + load tracking are initialized.
```bash
h1 curl --no-keepalive -m 5 10.0.0.100:8000
h1 curl --no-keepalive -m 5 10.0.0.100:8000
h1 curl --no-keepalive -m 5 10.0.0.100:8000
```

## Test VIP

```bash
mininet> h1 curl --no-keepalive 10.0.0.100:8000
```


# Step 3 — Least-Loaded Load Balancer

### Goal

Replace Round Robin with:

```bash
Least Loaded → choose server with minimum active flows
```
## Run Controller

```bash
ryu-manager lb_least_loaded.py
```
## Test

```bash
pingall
h2 python3 -m http.server 8000 --bind 10.0.0.2 &
h3 python3 -m http.server 8000 --bind 10.0.0.3 &
h4 python3 -m http.server 8000 --bind 10.0.0.4 &
h1 curl --no-keepalive -m 5 10.0.0.100:8000
h1 curl --no-keepalive -m 5 10.0.0.100:8000
h1 curl --no-keepalive -m 5 10.0.0.100:8000

mininet> h1 bash -lc 'for i in $(seq 1 20); do curl --no-keepalive -s -o /dev/null 10.0.0.100:8000; done'
```

Check logs:

```bash
LeastLoaded select: ... loads={...}
```
# Step 4 — ML-Based Backend Selection

## Goal

Use machine learning to dynamically select the best backend server based on runtime conditions.

---

## Train Model

```bash
python3 train_model.py
```
The script trains multiple regression models:
-Decision Tree Regressor
-Random Forest Regressor
-Ridge Regression
It evaluates each model using RMSE (Root Mean Square Error)
The best-performing model is selected based on RMSE.

The trained model is saved as:

```bash
model.joblib
```
## Run ML Controller

```bash
ryu-manager ml_lb.py
```

The controller uses the trained model to:
Predict the latency of each backend server
Select the backend with the lowest predicted latency
Additional penalties are applied to prevent load imbalance

## Test
```bash
h1 curl --no-keepalive 10.0.0.100:8000
```

Run multiple times to observe dynamic backend selection.

## Key Idea

Instead of using fixed strategies such as Round Robin or Least Loaded,
the ML-based approach predicts performance using real-time features, including:

-traffic rate
-active flows
-load imbalance

This enables more adaptive and efficient load balancing decisions.


# Step 5 — Experiment Suite

## IMPORTANT UPDATE

All benchmarking MUST use:

```bash
VIP = 10.0.0.100
```

DO NOT test:

```bash
h1 curl 10.0.0.2
```

under LB controllers.



## Latency

```bash
./benchmark_latency.sh rr_latency.csv 20
./benchmark_latency.sh ll_latency.csv 20
```


## Summarize

```bash
python3 summarize_latency.py rr_latency.csv
python3 summarize_latency.py ll_latency.csv
```


## Throughput
1. Start Controller (Round Robin or Least Loaded)
```bash
# For Round Robin
ryu-manager lb_nat_rr.py

# For Least Loaded
ryu-manager lb_least_loaded.py

#You can also benchmark the ML-based controller using:
ryu-manager ml_lb.py
```
2. Start Mininet
   Inside Mininet
   ```bash
   sudo mn -c
   sudo mn --topo single,4 --controller remote,ip=127.0.0.1 --switch ovsk,protocols=OpenFlow13 --mac
   pingall
   ```
3. Create the big file on all servers
   ```bash
   h2 bash -lc 'mkdir -p /tmp/www && dd if=/dev/zero of=/tmp/www/bigfile.bin bs=1M count=50'
   h3 bash -lc 'mkdir -p /tmp/www && dd if=/dev/zero of=/tmp/www/bigfile.bin bs=1M count=50'
   h4 bash -lc 'mkdir -p /tmp/www && dd if=/dev/zero of=/tmp/www/bigfile.bin bs=1M count=50'
   ```
4. Start HTTP servers
   ```bash
   h2 bash -lc 'mkdir -p /tmp/www && dd if=/dev/zero of=/tmp/www/bigfile.bin bs=1M count=50'
   h3 bash -lc 'mkdir -p /tmp/www && dd if=/dev/zero of=/tmp/www/bigfile.bin bs=1M count=50'
   h4 bash -lc 'mkdir -p /tmp/www && dd if=/dev/zero of=/tmp/www/bigfile.bin bs=1M count=50'
   ```
5. Warm up the VIP once
   ```bash
   h1 curl --no-keepalive -o /dev/null -m 10 http://10.0.0.100:8000/bigfile.bin
   ```
6. Measure 5 runs
   ```bash
   h1 bash -lc 'for i in $(seq 1 5); do curl --no-keepalive -o /dev/null -s -w "%{time_total}\n" http://10.0.0.100:8000/bigfile.bin; done'
   ```
7. Compare throughput
   For a 50 MB file:
   ```bash
   Throughput (Mbps) = 50 × 8 / time
   ```
   
## Packet Drops

```bash
./measure_drops.sh
```


