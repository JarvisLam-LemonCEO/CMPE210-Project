#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

SUDO_PASSWORD="${SUDO_PASSWORD:-toor}"
RYU_PYTHON="${RYU_PYTHON:-/home/toor/anaconda3/envs/ryu-lab/bin/python}"
RYU_MANAGER="${RYU_MANAGER:-/home/toor/anaconda3/envs/ryu-lab/bin/ryu-manager}"
CONTROLLER_APP="${CONTROLLER_APP:-ml_lb.py}"
BENCH_SAMPLES="${BENCH_SAMPLES:-20}"
TRAIN_REQUESTS="${TRAIN_REQUESTS:-60}"
TRAIN_WARMUP="${TRAIN_WARMUP:-5}"
EVAL_OUTFILE="${EVAL_OUTFILE:-ml_latency_auto.csv}"

MININET_LOG="/tmp/maxwell_mininet.log"
MININET_TYPESCRIPT="/tmp/maxwell_mininet.typescript"
CONTROLLER_LOG="/tmp/maxwell_controller.log"

sudo_cmd() {
  printf "%s\n" "$SUDO_PASSWORD" | sudo -S "$@"
}

hpid() {
  local host="$1"
  ps -eo pid,cmd | grep "mininet:${host}" | grep -v grep | head -n1 | awk '{print $1}'
}

wait_for_host() {
  local host="$1"
  local tries="${2:-30}"
  local pid=""
  for _ in $(seq 1 "$tries"); do
    pid="$(hpid "$host")"
    if [[ -n "$pid" ]]; then
      echo "$pid"
      return 0
    fi
    sleep 1
  done
  return 1
}

start_mininet() {
  if [[ -n "$(hpid h1 || true)" ]]; then
    echo "Mininet already running."
    return 0
  fi

  rm -f "$MININET_LOG" "$MININET_TYPESCRIPT"
  sudo_cmd script -q -c "mn --topo single,4 --controller remote,ip=127.0.0.1 --switch ovsk,protocols=OpenFlow13 --mac" "$MININET_TYPESCRIPT" >"$MININET_LOG" 2>&1 &

  wait_for_host h1 >/dev/null
  wait_for_host h2 >/dev/null
  wait_for_host h3 >/dev/null
  wait_for_host h4 >/dev/null
}

start_backends() {
  local h2 h3 h4
  h2="$(wait_for_host h2)"
  h3="$(wait_for_host h3)"
  h4="$(wait_for_host h4)"

  sudo_cmd mnexec -a "$h2" pkill -f "python3 -m http.server 8000" >/dev/null 2>&1 || true
  sudo_cmd mnexec -a "$h3" pkill -f "python3 -m http.server 8000" >/dev/null 2>&1 || true
  sudo_cmd mnexec -a "$h4" pkill -f "python3 -m http.server 8000" >/dev/null 2>&1 || true

  sudo_cmd mnexec -a "$h2" python3 -m http.server 8000 --bind 10.0.0.2 >/tmp/h2_http.log 2>&1 &
  sudo_cmd mnexec -a "$h3" python3 -m http.server 8000 --bind 10.0.0.3 >/tmp/h3_http.log 2>&1 &
  sudo_cmd mnexec -a "$h4" python3 -m http.server 8000 --bind 10.0.0.4 >/tmp/h4_http.log 2>&1 &
  sleep 2
}

start_controller() {
  local app="${1:-$CONTROLLER_APP}"
  pkill -f "$RYU_MANAGER $SCRIPT_DIR/$app" >/dev/null 2>&1 || true
  rm -f "$CONTROLLER_LOG"
  nohup env PYTHONUNBUFFERED=1 "$RYU_PYTHON" "$RYU_MANAGER" "$SCRIPT_DIR/$app" >"$CONTROLLER_LOG" 2>&1 < /dev/null &
  sleep 4
}

restart_controller() {
  local app="${1:-$CONTROLLER_APP}"
  sudo_cmd ovs-ofctl -O OpenFlow13 del-flows s1 >/dev/null 2>&1 || true
  start_controller "$app"
}

status() {
  echo "Controller:"
  ps -ef | grep "$RYU_MANAGER $SCRIPT_DIR/$CONTROLLER_APP" | grep -v grep || true
  echo "Hosts:"
  for host in h1 h2 h3 h4; do
    echo "${host}: $(hpid "$host" || true)"
  done
  echo "Controller log:"
  sed -n '1,40p' "$CONTROLLER_LOG" 2>/dev/null || true
}

up() {
  sudo_cmd mn -c >/dev/null 2>&1 || true
  start_mininet
  start_backends
  start_controller "$CONTROLLER_APP"
  local h1
  h1="$(wait_for_host h1)"
  sudo_cmd mnexec -a "$h1" curl --no-keepalive -m 5 -s -o /dev/null http://10.0.0.100:8000/ || true
  status
}

sample() {
  SUDO_PASSWORD="$SUDO_PASSWORD" NUM_REQUESTS="$TRAIN_REQUESTS" WARMUP_REQUESTS="$TRAIN_WARMUP" python3 run_benchmark.py
}

train() {
  "$RYU_PYTHON" train_model.py
}

eval_latency() {
  restart_controller "$CONTROLLER_APP"
  bash benchmark_latency.sh "$EVAL_OUTFILE" "$BENCH_SAMPLES"
  python3 summarize_latency.py "$EVAL_OUTFILE"
}

all() {
  up
  sample
  train
  eval_latency
}

usage() {
  cat <<EOF
Usage: ./run_all.sh <command>

Commands:
  up      Start Mininet, backend HTTP servers, and ML controller
  sample  Collect training data with run_benchmark.py
  train   Train and save the best ML model
  eval    Restart controller and run latency benchmark
  all     Run up + sample + train + eval
  status  Show current environment status

Environment overrides:
  SUDO_PASSWORD, RYU_PYTHON, RYU_MANAGER, CONTROLLER_APP
  TRAIN_REQUESTS, TRAIN_WARMUP, BENCH_SAMPLES, EVAL_OUTFILE
EOF
}

cmd="${1:-all}"
case "$cmd" in
  up) up ;;
  sample) sample ;;
  train) train ;;
  eval) eval_latency ;;
  all) all ;;
  status) status ;;
  *) usage; exit 1 ;;
esac
