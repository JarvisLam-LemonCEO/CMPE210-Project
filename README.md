# Spring 2026  CMPE 210 Course Project
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
pip install ryu
pip install scikit-learn
```
Verify installation:
```bash
mn --version
ryu-manager --version
```

## 2 Create the Network Topology
Run Mininet with a simple topology.
```bash 
sudo mn --topo single,4 --controller remote,ip=127.0.0.1 --switch ovsk,protocols=OpenFlow13 --mac
```

Explanation:
## Components

| Component | Meaning |
|-----------|---------|
| single,4  | 1 switch + 4 hosts |
| host1     | client |
| host2     | server1 |
| host3     | server2 |
| host4     | server3 |

## 3 Start the Ryu Controller
Open another terminal and run:
```bash
ryu-manager ryu.app.simple_switch_13
```
This controller installs basic OpenFlow forwarding rules.

## 4 Connect Mininet to the Controller
Run Mininet with controller IP:

```bash 
sudo mn --topo single,4 --controller remote,ip=127.0.0.1 --switch ovsk,protocols=OpenFlow13 --mac
```

## 5 Verify Network Connectivity
Inside Mininet:
```bash
pingall
```

Expected result:
```bash
*** Results: 0% dropped
```

## 6 Test Client → Server Communication
Start a simple HTTP server on each server.
Server 1:
```bash
mininet> h2 python3 -m http.server 8000 &
```
Server 2:
```bash
mininet> h3 python3 -m http.server 8000 &
```
Server 3:
```bash
mininet> h4 python3 -m http.server 8000 &
```

Now test from the client:
```bash
mininet> h1 curl 10.0.0.2:8000
```

## 7 Observe Flow Rules
Check OpenFlow rules:
```bash
sudo ovs-ofctl -O OpenFlow13 dump-flows s1
```
You will see rules installed by the controller.

--------------------------------------------------------
# Step 2 — Implement a working baseline load balancer:
- Client sends traffic to VIP (example: 10.0.0.100 )
- Ryu chooses one backend server ( 10.0.0.2/.3/.4 )
- Controller installs OpenFlow rules so subsequent packets are handled in-switch
- Return traffic is SNAT’d back to VIP so the client thinks it talked to one server

## Topology assumption (Mininet)
Use this exact layout:
- h1 client: 10.0.0.1
- h2 server1: 10.0.0.2
- h3 server2: 10.0.0.3
- h4 server3: 10.0.0.4
- s1 Open vSwitch connected to Ryu
VIP (not assigned to a host): 10.0.0.100

## 1. Start Mininet
Terminal A:
Run:
```bash
sudo mn --topo single,4 --controller remote,ip=127.0.0.1 --switch ovsk,protocols=OpenFlow13 --mac
```
Inside Mininet:
```bash
mininet> pingall
```

Keep this terminal open (you will run curl inside it later).

## 2. Run the Load Balancer Controller: lb_nat_rr.py
In a separate terminal B:
```bash
ryu-manager lb_nat_rr.py
```
You should see “Connected: dpid=...”.

## 3. Start HTTP servers on backends
Inside Mininet CLI:
```bash
mininet> h2 python3 -m http.server 8000 --bind 10.0.0.2 &
mininet> h3 python3 -m http.server 8000 --bind 10.0.0.3 &
mininet> h4 python3 -m http.server 8000 --bind 10.0.0.4 &
```

## 4. Test VIP Load Balancing
Still inside Mininet:
```bash
mininet> h1 curl -m 2 -v 10.0.0.100:8000
mininet> h1 curl -m 2 -v 10.0.0.100:8000
mininet> h1 curl -m 2 -v 10.0.0.100:8000
```
Watch the Ryu terminal logs — each new TCP source port should rotate the backend.

Note: because HTTP may reuse TCP connections, you can force new connections by adding --no-keepalive :

```bash
mininet> h1 curl --no-keepalive -m 2 10.0.0.100:8000
```
## 5. Confirm flows installed
In the Ubuntu VM (outside Mininet) open Terminal C:
```bash
sudo ovs-ofctl -O OpenFlow13 dump-flows s1
```
You should see forward rules matching ipv4_dst=10.0.0.100,tcp_dst=8000 and reverse rules matching ipv4_src=10.0.0.2/3/4,tcp_src=8000.

# Step 3 -- Least-Loaded SDN Load Balancer
### Goal
Replace the server selection logic:
```bash
Round Robin:
Server1 → Server2 → Server3 → repeat
```
with:
```bash
Least Loaded:
Pick server with the smallest current load
```
Load can be estimated using:
- number of active connections
- packet counters
- response latency
- bandwidth usage

For a course project, the simplest and common approach is:
Use number of active flows per server.

## 1. Run the Controller (Terminal A)
```bash
ryu-manager lb_least_loaded.py
```

## 2 Start the Mininet (Terminal B)
```bash
sudo mn --topo single,4 --controller remote,ip=127.0.0.1 --
switch ovsk,protocols=OpenFlow13 --mac
mininet> pingall
mininet> h2 python3 -m http.server 8000 --bind 10.0.0.2 &
mininet> h3 python3 -m http.server 8000 --bind 10.0.0.3 &
mininet> h4 python3 -m http.server 8000 --bind 10.0.0.4 &
mininet> h1 curl --no-keepalive 10.0.0.100:8000
```
### How to see least-loaded decisions

In the Ryu terminal, you’ll see lines like:
- eastLoaded select: 10.0.0.1:xxxxx -> 10.0.0.3 loads={...}

Tip: generate many new flows (new TCP source ports):
```bash
mininet> h1 bash -lc 'for i in $(seq 1 20); do curl --no-keepalive -s -o /dev/null 10.0.0.100:8000; done'
```

# Step 4 -- ML-Based Backend Selection (Predict Best server)
This step is where you turn your controller into an ML-based selector:
- The controller extracts real-time features (port utilization, drops, active-flow count) for each backend.
- A trained model predicts expected latency per backend.
- The controller chooses the backend with the lowest predicted latency.
- We build a simple dataset → train → deploy model loop.

### What you will build
1. ml_lb.py (Ryu controller)
    - Polls switch port stats every second
    - Computes per-backend features:
        - tx_rate_bps , rx_rate_bps
        - drop_delta
        - active_flows_assigned (controller-tracked)
    - Loads model.joblib and predicts latency per backend
    - Chooses backend with minimum predicted latency
    - Logs every decision to decisions.csv
2. run_benchmark.py (dataset collection)
    - Runs many curl requests from h1 namespace
    - Measures actual time_total
    - Joins each measured request with the last decision in decisions.csv
    - Writes training rows into dataset.csv
3. train_model.py (train model)
    - Trains a regressor (RandomForest) to predict latency
    - Saves model as model.joblib

## 1. Start controller (Terminal A)
```bash
ryu-manager ml_lb.py
```

## 2. Start Mininet (Terminal B)
```bash
sudo mn --topo single,4 --controller remote,ip=127.0.0.1 --switch ovsk,protocols=OpenFlow13 --mac
```
Inside Mininet:
```bash
mininet> pingall
mininet> h2 python3 -m http.server 8000 --bind 10.0.0.2 &
mininet> h3 python3 -m http.server 8000 --bind 10.0.0.3 &
mininet> h4 python3 -m http.server 8000 --bind 10.0.0.4 &
```

## 3. Collect dataset (Terminal C)
```bash
python3 run_benchmark.py
```
This produces:
- decisions.csv (controller decisions + feature snapshots)
- dataset.csv (features + measured latency_sec )

## 4. Train model
```bash
python3 train_model.py
```
This creates:
- model.joblib

## 5. Restart controller so it loads the model
Stop Terminal A ( Ctrl+C ) then:
```bash
 ryu-manager ml_lb.py
```
Now the controller will start logging decisions with policy=ml_pred (when prediction works), otherwise fallback least_loaded .

# Step 5 — Experiment Suite + Results (Latency / Throughput / Drops)
### Goal
Run the same workload under 3 controllers, and collect:
1. Latency (HTTP response time)
2. Throughput (iperf3)
3. Packet drop rate (OVS port stats)

Then summarize into:
- results_latency.csv
- results_throughput.csv
- results_drops.csv
- plus optional plots

## 1. Standardize your workload topology
Use the same Mininet topology every time:
```bash
sudo mn --topo single,4 --controller remote,ip=127.0.0.1 \ --switch ovsk,protocols=OpenFlow13 --mac
```
Inside Mininet:
```bash
mininet> pingall
mininet> h2 python3 -m http.server 8000 --bind 10.0.0.2 &
mininet> h3 python3 -m http.server 8000 --bind 10.0.0.3 &
mininet> h4 python3 -m http.server 8000 --bind 10.0.0.4 &
```
## 2. Run the measure latency
Run it (while Mininet + servers + controller are running):
```bash
python3 measure_latency.py
```
## 3. measure_throughput.py (iperf3 throughput)
This starts iperf3 servers on h2/h3/h4 and uses VIP to test.
Important: iperf3 is not HTTP, but it’s a clean throughput test if your LB handles TCP to a chosen port. Use port 5201 for iperf3 and temporarily set controller SERVICE_PORT=5201 (one run per controller).

Run:
```bash
python3 measure_throughput.py
```
## 4. measure_drops.sh (OVS port drops)
This reads port drop counters before/after your test window and prints deltas.

Run (in another terminal while you run latency tests):
```bash
./measure_drops.sh
```
## Run experiments across 3 controllers
For each policy
1. Start the controller
2. Start Mininet + servers
3. Run latency test
4. (Optional) Run throughput test (switch controller service port to 5201)
5. Save outputs

Suggested run order
1. Round Robin: lb_nat_rr.py
2. Least Loaded: your Step 3 controller
3. ML LB: ml_lb.py (after model trained)

# Step 6 — Automated Multi-Trial Evaluation + Plots + Final Summary
### Goal
For each policy (RR / Least-Loaded / ML):
- Run K trials of the same workload (e.g., 100 HTTP requests)
- Collect latency samples
- Compute:
    - mean
    - p50, p95
    - std
    - 95% confidence interval
- Produce:
    - summary.csv
    - latency_boxplot.png
    - latency_cdf.png

step6_collect.sh to collect multi-trial
latency samples
This script assumes:
- Mininet is running
- Servers are running
- Your chosen controller is running
- VIP is 10.0.0.100:8000
It will run K trials and save raw latency samples.

## 1. Start the controller you want
Round Robin controller:
```bash
ryu-manager lb_nat_rr.py
```
Least loaded controller:
```bash
ryu-manager lb_least_loaded.py
```
ML controller:
```bash
ryu-manager ml_lb.py
```

## 2. Start Mininet + servers (if not already running)
```bash
sudo mn --topo single,4 --controller remote,ip=127.0.0.1 --switch ovsk,protocols=OpenFlow13 --mac
mininet> pingall
mininet> h2 python3 -m http.server 8000 --bind 10.0.0.2 &
mininet> h3 python3 -m http.server 8000 --bind 10.0.0.3 &
mininet> h4 python3 -m http.server 8000 --bind 10.0.0.4 &
```

## 3. Collect trials
Run (outside Mininet):
```bash
./step6_collect.sh round_robin 5 100
./step6_collect.sh least_loaded 5 100
./step6_collect.sh ml_pred 5 100
```

## 4. Analyze + plot
```bash
python3 step6_analyze.py
```
Outputs:
- summary.csv
- latency_boxplot.png
- latency_cdf.png
