# ml_lb.py
# ML-Based SDN Load Balancer (OpenFlow 1.3) with VIP + NAT
# - VIP: 10.0.0.100:8000
# - Backends: 10.0.0.2/3/4
# - Features from switch stats: tx/rx rate, drops, active assigned flows
# - Model predicts latency; choose backend with lowest predicted latency
#
# Run:
#   ryu-manager ml_lb.py
#   sudo mn --topo single,4 --controller remote,ip=127.0.0.1 --switch ovsk,protocols=OpenFlow13 --mac
#
# Notes:
# - This controller logs decisions to decisions.csv
# - If model.joblib is missing, it falls back to "least_loaded" selection.

import csv
import os
import time
import uuid

from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import CONFIG_DISPATCHER, MAIN_DISPATCHER, set_ev_cls
from ryu.lib import hub
from ryu.lib.packet import packet
from ryu.lib.packet import ethernet, arp, ipv4, tcp
from ryu.lib import mac as mac_lib
from ryu.ofproto import ofproto_v1_3

# ML
try:
    import joblib
except Exception:
    joblib = None


class MLLatencyLB(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    VIP_IP = "10.0.0.100"
    VIP_MAC = "00:00:00:00:00:64"
    SERVICE_PORT = 8000

    BACKENDS = [
        {"ip": "10.0.0.2", "mac": "00:00:00:00:00:02"},
        {"ip": "10.0.0.3", "mac": "00:00:00:00:00:03"},
        {"ip": "10.0.0.4", "mac": "00:00:00:00:00:04"},
    ]

    STATS_INTERVAL_SEC = 1.0
    MODEL_PATH = "model.joblib"
    DECISION_LOG = "decisions.csv"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.dp = None

        # learning
        self.ip_to_port = {}     # ip -> switch port
        self.mac_to_port = {}    # dpid -> {mac -> port}

        # load accounting (controller-side "active assigned flows")
        self.active_flows = {b["ip"]: 0 for b in self.BACKENDS}
        self.flow_map = {}       # (client_ip, client_tcp_src) -> backend_ip

        # port stats snapshots: port_no -> last stats
        # store: tx_bytes, rx_bytes, tx_dropped, rx_dropped, timestamp
        self.port_stats_last = {}
        # derived rates per port: port_no -> dict
        self.port_rates = {}

        # backend port mapping once learned: backend_ip -> port_no
        self.backend_port = {}

        # ML model
        self.model = None
        self.load_model()

        # decision log file init
        self.init_decision_log()

        # background stats polling thread
        self.stats_thread = hub.spawn(self.poll_stats_loop)

    # ---------- Model + Logging ----------
    def load_model(self):
        if joblib is None:
            self.logger.warning("joblib not available; ML disabled.")
            return
        if os.path.exists(self.MODEL_PATH):
            try:
                self.model = joblib.load(self.MODEL_PATH)
                self.logger.info("Loaded ML model from %s", self.MODEL_PATH)
            except Exception as e:
                self.logger.warning("Failed to load model %s: %s", self.MODEL_PATH, e)
        else:
            self.logger.warning("No %s found; will fallback to least-loaded.", self.MODEL_PATH)

    def init_decision_log(self):
        if not os.path.exists(self.DECISION_LOG):
            with open(self.DECISION_LOG, "w", newline="") as f:
                w = csv.writer(f)
                w.writerow([
                    "decision_id", "ts",
                    "client_ip", "client_tcp_src",
                    "chosen_backend",
                    "b_tx_rate", "b_rx_rate", "b_drop_delta", "b_active",
                    "s_tx_rate", "s_rx_rate", "s_drop_delta", "s_active",
                    "t_tx_rate", "t_rx_rate", "t_drop_delta", "t_active",
                    "policy"
                ])

    def log_decision(self, decision_row):
        with open(self.DECISION_LOG, "a", newline="") as f:
            csv.writer(f).writerow(decision_row)

    # ---------- OpenFlow base ----------
    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def on_switch_features(self, ev):
        dp = ev.msg.datapath
        self.dp = dp
        ofp = dp.ofproto
        parser = dp.ofproto_parser

        # table-miss -> controller
        match = parser.OFPMatch()
        actions = [parser.OFPActionOutput(ofp.OFPP_CONTROLLER, ofp.OFPCML_NO_BUFFER)]
        self.add_flow(dp, priority=0, match=match, actions=actions)

        self.logger.info("Switch connected: dpid=%s", dp.id)

    def add_flow(self, dp, priority, match, actions, idle_timeout=60, hard_timeout=0):
        ofp = dp.ofproto
        parser = dp.ofproto_parser
        inst = [parser.OFPInstructionActions(ofp.OFPIT_APPLY_ACTIONS, actions)]
        mod = parser.OFPFlowMod(
            datapath=dp,
            priority=priority,
            match=match,
            instructions=inst,
            idle_timeout=idle_timeout,
            hard_timeout=hard_timeout
        )
        dp.send_msg(mod)

    # ---------- Stats polling ----------
    def poll_stats_loop(self):
        while True:
            try:
                if self.dp is not None:
                    self.request_port_stats(self.dp)
            except Exception as e:
                self.logger.debug("stats loop error: %s", e)
            hub.sleep(self.STATS_INTERVAL_SEC)

    def request_port_stats(self, dp):
        parser = dp.ofproto_parser
        ofp = dp.ofproto
        req = parser.OFPPortStatsRequest(dp, 0, ofp.OFPP_ANY)
        dp.send_msg(req)

    @set_ev_cls(ofp_event.EventOFPPortStatsReply, MAIN_DISPATCHER)
    def on_port_stats_reply(self, ev):
        now = time.time()
        for s in ev.msg.body:
            port_no = s.port_no
            tx_bytes = getattr(s, "tx_bytes", 0)
            rx_bytes = getattr(s, "rx_bytes", 0)
            tx_dropped = getattr(s, "tx_dropped", 0)
            rx_dropped = getattr(s, "rx_dropped", 0)

            prev = self.port_stats_last.get(port_no)
            if prev:
                dt = max(1e-6, now - prev["ts"])
                tx_rate_bps = 8.0 * (tx_bytes - prev["tx_bytes"]) / dt
                rx_rate_bps = 8.0 * (rx_bytes - prev["rx_bytes"]) / dt
                drop_delta = (tx_dropped - prev["tx_dropped"]) + (rx_dropped - prev["rx_dropped"])

                self.port_rates[port_no] = {
                    "tx_rate_bps": max(0.0, tx_rate_bps),
                    "rx_rate_bps": max(0.0, rx_rate_bps),
                    "drop_delta": max(0, drop_delta),
                    "ts": now
                }

            self.port_stats_last[port_no] = {
                "tx_bytes": tx_bytes,
                "rx_bytes": rx_bytes,
                "tx_dropped": tx_dropped,
                "rx_dropped": rx_dropped,
                "ts": now
            }

    # ---------- Packet handling ----------
    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def on_packet_in(self, ev):
        msg = ev.msg
        dp = msg.datapath
        parser = dp.ofproto_parser
        ofp = dp.ofproto
        in_port = msg.match["in_port"]

        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocol(ethernet.ethernet)
        if eth is None or eth.ethertype == 0x88cc:
            return

        dpid = dp.id
        self.mac_to_port.setdefault(dpid, {})
        self.mac_to_port[dpid][eth.src] = in_port

        # ARP
        arp_pkt = pkt.get_protocol(arp.arp)
        if arp_pkt:
            self.ip_to_port[arp_pkt.src_ip] = in_port
            # learn backend ports if ARP from backend IP
            self.maybe_learn_backend_port(arp_pkt.src_ip, in_port)

            if arp_pkt.opcode == arp.ARP_REQUEST and arp_pkt.dst_ip == self.VIP_IP:
                self.reply_arp_for_vip(dp, in_port, eth.src, arp_pkt.src_ip, arp_pkt.src_mac)
            else:
                self.flood(dp, in_port, msg.data)
            return

        # IPv4
        ip_pkt = pkt.get_protocol(ipv4.ipv4)
        if not ip_pkt:
            self.l2_fallback(dp, in_port, eth.dst, msg)
            return

        self.ip_to_port[ip_pkt.src] = in_port
        self.maybe_learn_backend_port(ip_pkt.src, in_port)

        tcp_pkt = pkt.get_protocol(tcp.tcp)
        if not tcp_pkt:
            self.l2_fallback(dp, in_port, eth.dst, msg)
            return

        # Only handle VIP:8000
        if ip_pkt.dst != self.VIP_IP or tcp_pkt.dst_port != self.SERVICE_PORT:
            self.l2_fallback(dp, in_port, eth.dst, msg)
            return

        client_ip = ip_pkt.src
        client_tcp_src = tcp_pkt.src_port
        key = (client_ip, client_tcp_src)

        # If already mapped, reuse
        chosen_backend_ip = self.flow_map.get(key)
        policy = "sticky"
        if chosen_backend_ip is None:
            chosen_backend_ip, policy = self.choose_backend_ml_or_fallback()
            self.flow_map[key] = chosen_backend_ip
            self.active_flows[chosen_backend_ip] += 1

        backend = next(b for b in self.BACKENDS if b["ip"] == chosen_backend_ip)
        backend_ip = backend["ip"]
        backend_mac = backend["mac"]

        # Need backend port learned
        backend_port = self.backend_port.get(backend_ip)
        if backend_port is None:
            self.logger.warning("Unknown switch port for backend %s. Run pingall first.", backend_ip)
            self.flood(dp, in_port, msg.data)
            return

        client_port = self.ip_to_port.get(client_ip, in_port)
        client_mac = eth.src

        # Forward DNAT flow (client -> VIP -> backend)
        match_fwd = parser.OFPMatch(
            eth_type=0x0800, ip_proto=6,
            ipv4_src=client_ip, ipv4_dst=self.VIP_IP,
            tcp_src=client_tcp_src, tcp_dst=self.SERVICE_PORT
        )
        actions_fwd = [
            parser.OFPActionSetField(eth_src=self.VIP_MAC),
            parser.OFPActionSetField(eth_dst=backend_mac),
            parser.OFPActionSetField(ipv4_dst=backend_ip),
            parser.OFPActionOutput(backend_port),
        ]
        self.add_flow(dp, priority=200, match=match_fwd, actions=actions_fwd)

        # Reverse SNAT flow (backend -> client, rewrite src to VIP)
        match_rev = parser.OFPMatch(
            eth_type=0x0800, ip_proto=6,
            ipv4_src=backend_ip, ipv4_dst=client_ip,
            tcp_src=self.SERVICE_PORT, tcp_dst=client_tcp_src
        )
        actions_rev = [
            parser.OFPActionSetField(eth_src=self.VIP_MAC),
            parser.OFPActionSetField(eth_dst=client_mac),
            parser.OFPActionSetField(ipv4_src=self.VIP_IP),
            parser.OFPActionOutput(client_port),
        ]
        self.add_flow(dp, priority=200, match=match_rev, actions=actions_rev)

        # Log decision with feature snapshot
        decision_id = str(uuid.uuid4())
        ts = time.time()
        snap = self.snapshot_features()

        row = [
            decision_id, ts,
            client_ip, client_tcp_src,
            backend_ip,
            # backend1 (10.0.0.2)
            snap["10.0.0.2"]["tx_rate_bps"], snap["10.0.0.2"]["rx_rate_bps"], snap["10.0.0.2"]["drop_delta"], snap["10.0.0.2"]["active"],
            # backend2 (10.0.0.3)
            snap["10.0.0.3"]["tx_rate_bps"], snap["10.0.0.3"]["rx_rate_bps"], snap["10.0.0.3"]["drop_delta"], snap["10.0.0.3"]["active"],
            # backend3 (10.0.0.4)
            snap["10.0.0.4"]["tx_rate_bps"], snap["10.0.0.4"]["rx_rate_bps"], snap["10.0.0.4"]["drop_delta"], snap["10.0.0.4"]["active"],
            policy
        ]
        self.log_decision(row)

        self.logger.info("Decision %s policy=%s client=%s:%s -> %s",
                         decision_id[:8], policy, client_ip, client_tcp_src, backend_ip)

        # For robustness, rely on retransmission for first packet. Next packets hit fast-path.
        return

    def maybe_learn_backend_port(self, ip, in_port):
        for b in self.BACKENDS:
            if b["ip"] == ip:
                self.backend_port[ip] = in_port

    # ---------- Backend choice ----------
    def snapshot_features(self):
        # Return dict backend_ip -> features
        snap = {}
        for b in self.BACKENDS:
            ip = b["ip"]
            port = self.backend_port.get(ip)
            rates = self.port_rates.get(port, {}) if port is not None else {}
            snap[ip] = {
                "tx_rate_bps": float(rates.get("tx_rate_bps", 0.0)),
                "rx_rate_bps": float(rates.get("rx_rate_bps", 0.0)),
                "drop_delta": int(rates.get("drop_delta", 0)),
                "active": int(self.active_flows.get(ip, 0))
            }
        return snap

    def choose_backend_ml_or_fallback(self):
        snap = self.snapshot_features()

        # If ML model loaded, predict latency per backend and choose min
        if self.model is not None:
            best_ip = None
            best_pred = None
            for ip, f in snap.items():
                # Feature vector (keep consistent with train_model.py)
                X = [[f["tx_rate_bps"], f["rx_rate_bps"], f["drop_delta"], f["active"]]]
                try:
                    pred = float(self.model.predict(X)[0])
                except Exception:
                    pred = None
                if pred is None:
                    continue
                if best_pred is None or pred < best_pred:
                    best_pred = pred
                    best_ip = ip
            if best_ip is not None:
                return best_ip, "ml_pred"

        # Fallback: least-loaded by active flow count
        best_ip = min(self.active_flows, key=self.active_flows.get)
        return best_ip, "least_loaded"

    # ---------- Utility packet ops ----------
    def reply_arp_for_vip(self, dp, out_port, dst_mac, dst_ip, dst_hw):
        parser = dp.ofproto_parser
        ofp = dp.ofproto

        p = packet.Packet()
        p.add_protocol(ethernet.ethernet(dst=dst_mac, src=self.VIP_MAC, ethertype=0x0806))
        p.add_protocol(arp.arp(
            opcode=arp.ARP_REPLY,
            src_mac=self.VIP_MAC, src_ip=self.VIP_IP,
            dst_mac=dst_hw, dst_ip=dst_ip
        ))
        p.serialize()

        actions = [parser.OFPActionOutput(out_port)]
        out = parser.OFPPacketOut(
            datapath=dp, buffer_id=ofp.OFP_NO_BUFFER,
            in_port=ofp.OFPP_CONTROLLER, actions=actions, data=p.data
        )
        dp.send_msg(out)

    def flood(self, dp, in_port, data):
        parser = dp.ofproto_parser
        ofp = dp.ofproto
        actions = [parser.OFPActionOutput(ofp.OFPP_FLOOD)]
        out = parser.OFPPacketOut(
            datapath=dp, buffer_id=ofp.OFP_NO_BUFFER,
            in_port=in_port, actions=actions, data=data
        )
        dp.send_msg(out)

    def l2_fallback(self, dp, in_port, dst_mac, msg):
        ofp = dp.ofproto
        parser = dp.ofproto_parser
        dpid = dp.id

        self.mac_to_port.setdefault(dpid, {})
        out_port = self.mac_to_port[dpid].get(dst_mac, ofp.OFPP_FLOOD)

        actions = [parser.OFPActionOutput(out_port)]
        if out_port != ofp.OFPP_FLOOD:
            match = parser.OFPMatch(eth_dst=dst_mac)
            self.add_flow(dp, priority=10, match=match, actions=actions)

        data = None if msg.buffer_id != ofp.OFP_NO_BUFFER else msg.data
        out = parser.OFPPacketOut(
            datapath=dp, buffer_id=msg.buffer_id,
            in_port=in_port, actions=actions, data=data
        )
        dp.send_msg(out)