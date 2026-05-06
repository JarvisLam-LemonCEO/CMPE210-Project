from __future__ import annotations

import csv
import os
import time
from typing import Dict, List, Tuple

from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import CONFIG_DISPATCHER, MAIN_DISPATCHER, DEAD_DISPATCHER, set_ev_cls
from ryu.ofproto import ofproto_v1_3
from ryu.lib import hub
from ryu.lib.packet import packet
from ryu.lib.packet import ethernet, arp, ipv4, tcp

try:
    import joblib
except ImportError:
    joblib = None


class MLLB(app_manager.RyuApp):
        """
    ML-Based Load Balancer Controller.

    This Ryu controller performs:
    1. ARP handling for the virtual IP.
    2. TCP load balancing to backend servers.
    3. OpenFlow rule installation.
    4. Port and flow statistics monitoring.
    5. ML-based backend selection.
    6. Decision logging for later training and analysis.
    """
    # Use OpenFlow 1.3
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    # Virtual Service Configuration
    # Virtual IP address that clients connect to
    VIP_IP = "10.0.0.100"
    # Virtual MAC address used by the load balancer
    VIP_MAC = "00:00:00:00:00:64"
    # TCP service ports supported by the load balancer
    SERVICE_PORTS = [8000, 5201]

    # Backend Server Configuration
    # Backend servers connected to switch ports 2, 3, and 4
    BACKENDS = [
        {"ip": "10.0.0.2", "mac": "00:00:00:00:00:02", "port": 2},
        {"ip": "10.0.0.3", "mac": "00:00:00:00:00:03", "port": 3},
        {"ip": "10.0.0.4", "mac": "00:00:00:00:00:04", "port": 4},
    ]

    # How often the controller polls switch statistics
    STATS_POLL_INTERVAL = 1.0

    # Exponential Moving Average factor for smoothing traffic rates
    EMA_ALPHA = 0.35

    # Thresholds used to detect overloaded backends
    HOTSPOT_ACTIVE_FLOW_SHARE = 0.5
    HOTSPOT_TRAFFIC_SHARE = 0.6

    # Penalty values added when a backend is overloaded
    FLOW_PENALTY_WEIGHT = 0.0015
    TX_SHARE_PENALTY_WEIGHT = 0.0010
    RX_SHARE_PENALTY_WEIGHT = 0.0010

    # Only log every 10 selections unless debug mode is enabled
    LOG_EVERY_N_SELECTIONS = 10

    def __init__(self, *args, **kwargs):
        # Initialize the SDN controller state.
        super().__init__(*args, **kwargs)
        # Directory where this controller file is located
        self.base_dir = os.path.dirname(os.path.abspath(__file__))
        # Path to trained ML model
        self.model_path = os.path.join(self.base_dir, "model.joblib")
        # Path to CSV file that stores backend selection decisions
        self.decisions_csv = os.path.join(self.base_dir, "decisions.csv")
        # Dictionary for quick backend lookup by IP address
        self.backend_by_ip = {b["ip"]: b for b in self.BACKENDS}
        # Feature columns expected by the trained ML model
        self.model_feature_cols = [
            "backend_index",
            "tx_rate_bps",
            "rx_rate_bps",
            "drop_delta",
            "active_flows_assigned",
            "total_tx_rate_bps",
            "total_rx_rate_bps",
            "total_active_flows",
            "tx_share",
            "rx_share",
            "active_flow_share",
            "tx_imbalance_bps",
            "rx_imbalance_bps",
            "flow_imbalance",
        ]

        # Connected OpenFlow switches
        self.datapaths: Dict[int, object] = {}

        # MAC learning table: switch_id -> MAC -> port
        self.mac_to_port: Dict[int, Dict[str, int]] = {}

        # IP learning table: IP -> switch port
        self.ip_to_port: Dict[str, int] = {}

        # Flow mapping:
        # (client_ip, client_tcp_src_port, service_port) -> backend_ip
        self.flow_map: Dict[Tuple[str, int, int], str] = {}

        # Number of active flows assigned to each backend
        self.active_flows: Dict[str, int] = {b["ip"]: 0 for b in self.BACKENDS}

        # Maps OpenFlow cookies to flow information
        self.cookie_to_flow: Dict[int, Tuple[Tuple[str, int, int], str]] = {}

        # Cookie counter used to uniquely identify installed flows
        self.next_cookie = 1

        # Counter for backend selections
        self.selection_count = 0

        # Enable detailed logging by setting ML_LB_DEBUG=1
        self.debug_logging = os.environ.get("ML_LB_DEBUG", "0") == "1"

        # Previous port statistics used to calculate traffic rate
        self.prev_port_stats: Dict[int, Dict[int, Dict[str, int]]] = {}
        # Current traffic features for each backend switch port
        self.port_features: Dict[int, Dict[str, float]] = {
            b["port"]: {
                "tx_rate_bps": 0.0,
                "rx_rate_bps": 0.0,
                "drop_delta": 0.0,
            }
            for b in self.BACKENDS
        }

        # Loaded ML model
        self.model = None
        self._load_model()

        # Create decisions.csv if it does not already exist
        self._init_decisions_csv()

        # Start background monitoring thread
        self.monitor_thread = hub.spawn(self._monitor)

    def _load_model(self) -> None:
         """
        Load the trained ML model from model.joblib.

        If no model is found, the controller falls back to
        least-loaded backend selection.
        """
        if joblib is None:
            self.logger.warning("joblib is not installed; ML model loading disabled.")
            return

        if os.path.exists(self.model_path):
            try:
                loaded = joblib.load(self.model_path)
                if isinstance(loaded, dict):
                    self.model = loaded.get("model")
                    feature_cols = loaded.get("feature_cols")
                    if feature_cols:
                        self.model_feature_cols = feature_cols
                else:
                    self.model = loaded
                if self.model is None:
                    raise RuntimeError("model payload did not contain a usable estimator")
                self.logger.info("Loaded ML model from %s", self.model_path)
            except Exception as exc:
                self.logger.warning("Failed to load model: %s", exc)
                self.model = None
        else:
            self.logger.info("No model found; controller will use least-loaded fallback.")

    def _init_decisions_csv(self) -> None:
        """
        Create decisions.csv and write the header row.

        This file records each backend selection decision made by
        the controller.
        """
        if not os.path.exists(self.decisions_csv):
            with open(self.decisions_csv, "w", newline="") as f:
                writer = csv.writer(f)
                writer.writerow([
                    "timestamp",
                    "client_ip",
                    "client_tcp_src",
                    "backend_ip",
                    "backend_port_no",
                    "policy",
                    "pred_latency",
                    "tx_rate_bps",
                    "rx_rate_bps",
                    "drop_delta",
                    "active_flows_assigned",
                    "backend_index",
                    "total_tx_rate_bps",
                    "total_rx_rate_bps",
                    "total_active_flows",
                    "tx_share",
                    "rx_share",
                    "active_flow_share",
                    "tx_imbalance_bps",
                    "rx_imbalance_bps",
                    "flow_imbalance",
                ])

    def _log_decision(
        self,
        client_ip: str,
        client_tcp_src: int,
        backend_ip: str,
        backend_port_no: int,
        policy: str,
        pred_latency: float,
        tx_rate_bps: float,
        rx_rate_bps: float,
        drop_delta: float,
        active_flows_assigned: int,
        backend_index: int,
        total_tx_rate_bps: float,
        total_rx_rate_bps: float,
        total_active_flows: int,
        tx_share: float,
        rx_share: float,
        active_flow_share: float,
        tx_imbalance_bps: float,
        rx_imbalance_bps: float,
        flow_imbalance: float,
    ) -> None:
        """
        Append one backend selection decision to decisions.csv.
        """
        with open(self.decisions_csv, "a", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([
                time.time(),
                client_ip,
                client_tcp_src,
                backend_ip,
                backend_port_no,
                policy,
                pred_latency,
                tx_rate_bps,
                rx_rate_bps,
                drop_delta,
                active_flows_assigned,
                backend_index,
                total_tx_rate_bps,
                total_rx_rate_bps,
                total_active_flows,
                tx_share,
                rx_share,
                active_flow_share,
                tx_imbalance_bps,
                rx_imbalance_bps,
                flow_imbalance,
            ])

    def _monitor(self) -> None:
        while True:
            for dp in list(self.datapaths.values()):
                self._request_port_stats(dp)
                self._request_flow_stats(dp)
            hub.sleep(self.STATS_POLL_INTERVAL)

    def _request_port_stats(self, datapath) -> None:
        ofp = datapath.ofproto
        parser = datapath.ofproto_parser
        req = parser.OFPPortStatsRequest(datapath, 0, ofp.OFPP_ANY)
        datapath.send_msg(req)

    def _request_flow_stats(self, datapath) -> None:
        parser = datapath.ofproto_parser
        req = parser.OFPFlowStatsRequest(datapath)
        datapath.send_msg(req)

    @set_ev_cls(ofp_event.EventOFPStateChange, [MAIN_DISPATCHER, DEAD_DISPATCHER])
    def _state_change_handler(self, ev) -> None:
        datapath = ev.datapath
        if ev.state == MAIN_DISPATCHER:
            if datapath.id not in self.datapaths:
                self.datapaths[datapath.id] = datapath
        elif ev.state == DEAD_DISPATCHER:
            self.datapaths.pop(datapath.id, None)

    @set_ev_cls(ofp_event.EventOFPPortStatsReply, MAIN_DISPATCHER)
    def _port_stats_reply_handler(self, ev) -> None:
        """
        Process switch port statistics.

        This calculates:
        - TX rate
        - RX rate
        - packet drop delta

        These values become ML features.
        """
        body = ev.msg.body
        dpid = ev.msg.datapath.id

        now = time.time()

        if dpid not in self.prev_port_stats:
            self.prev_port_stats[dpid] = {}

        for stat in body:
            port_no = stat.port_no
            prev = self.prev_port_stats[dpid].get(port_no)

            if prev is not None:
                # Time difference between current and previous sample
                dt = max(now - prev["ts"], 1e-6)

                # Calculate transmit and receive rate in bits per second
                tx_rate_bps = (stat.tx_bytes - prev["tx_bytes"]) * 8.0 / dt
                rx_rate_bps = (stat.rx_bytes - prev["rx_bytes"]) * 8.0 / dt

                # Calculate dropped packet difference
                drop_delta = (
                    (stat.rx_dropped - prev["rx_dropped"]) +
                    (stat.tx_dropped - prev["tx_dropped"])
                )

                # Only update features for backend ports
                if port_no in self.port_features:
                    prev_features = self.port_features[port_no]

                    self.port_features[port_no] = {
                        "tx_rate_bps": self._ema(prev_features["tx_rate_bps"], tx_rate_bps),
                        "rx_rate_bps": self._ema(prev_features["rx_rate_bps"], rx_rate_bps),
                        "drop_delta": float(drop_delta),
                    }

            # Store current statistics for the next calculation
            self.prev_port_stats[dpid][port_no] = {
                "ts": now,
                "tx_bytes": stat.tx_bytes,
                "rx_bytes": stat.rx_bytes,
                "rx_dropped": stat.rx_dropped,
                "tx_dropped": stat.tx_dropped,
            }

    @set_ev_cls(ofp_event.EventOFPFlowStatsReply, MAIN_DISPATCHER)
    def _flow_stats_reply_handler(self, ev) -> None:
        """
        Count active load-balanced flows per backend.

        This helps the controller know which backend is currently
        handling more traffic.
        """
        counts = {backend["ip"]: 0 for backend in self.BACKENDS}
        seen_cookies = set()

        for stat in ev.msg.body:
            # Only count high-priority load-balancer flows
            if stat.priority != 200 or stat.cookie == 0:
                continue

            match_items = getattr(stat.match, "items", lambda: [])()
            match = dict(match_items)

            # Only count flows going to the VIP
            if match.get("ipv4_dst") != self.VIP_IP:
                continue

            flow_info = self.cookie_to_flow.get(stat.cookie)

            if flow_info is None:
                continue

            seen_cookies.add(stat.cookie)

            _, backend_ip = flow_info

            if backend_ip in counts:
                counts[backend_ip] += 1

        # Remove stale flow records that no longer exist in the switch
        stale_cookies = [
            cookie for cookie in self.cookie_to_flow
            if cookie not in seen_cookies
        ]

        for cookie in stale_cookies:
            flow_key, _backend_ip = self.cookie_to_flow.pop(cookie)
            self.flow_map.pop(flow_key, None)

        # Update active flow counts
        self.active_flows = counts

    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def on_switch_features(self, ev) -> None:
        """
        Called when a switch connects.

        Installs the table-miss rule, which sends unknown packets
        to the controller.
        """
        dp = ev.msg.datapath
        ofp = dp.ofproto
        parser = dp.ofproto_parser

        # Match all packets
        match = parser.OFPMatch()

        # Send unmatched packets to controller
        actions = [
            parser.OFPActionOutput(
                ofp.OFPP_CONTROLLER,
                ofp.OFPCML_NO_BUFFER
            )
        ]

        self.add_flow(
            dp,
            priority=0,
            match=match,
            actions=actions
        )

        self.logger.info("ML LB switch connected: dpid=%s", dp.id)

    def add_flow(
        self,
        dp,
        priority,
        match,
        actions,
        idle_timeout=60,
        hard_timeout=0,
        cookie=0,
        send_flow_removed=False,
    ) -> None:
        """
        Install an OpenFlow rule into the switch.
        """
        ofp = dp.ofproto
        parser = dp.ofproto_parser

        # Apply the provided actions when the match is satisfied
        inst = [
            parser.OFPInstructionActions(
                ofp.OFPIT_APPLY_ACTIONS,
                actions
            )
        ]

        # Build the FlowMod message
        mod = parser.OFPFlowMod(
            datapath=dp,
            priority=priority,
            match=match,
            instructions=inst,
            idle_timeout=idle_timeout,
            hard_timeout=hard_timeout,
            cookie=cookie,
            flags=ofp.OFPFF_SEND_FLOW_REM if send_flow_removed else 0,
        )

        # Send flow rule to

    def packet_out(self, dp, in_port, actions, data) -> None:
        """
        Send a packet out from the switch immediately.

        This is used for the first packet of a new flow before
        the installed OpenFlow rule handles future packets.
        """
        ofp = dp.ofproto
        parser = dp.ofproto_parser

        out = parser.OFPPacketOut(
            datapath=dp,
            buffer_id=ofp.OFP_NO_BUFFER,
            in_port=in_port,
            actions=actions,
            data=data,
        )

        dp.send_msg(out)

    def pick_backend_least_loaded(self):
        """
        Fallback backend selection.

        Selects the backend with the fewest active flows.
        """
        best_ip = min(self.active_flows, key=self.active_flows.get)
        return self.backend_by_ip[best_ip]

    def _next_cookie(self) -> int:
        """
        Generate a unique cookie for tracking OpenFlow rules.
        """
        cookie = self.next_cookie
        self.next_cookie += 1
        return cookie

    def _ema(self, previous: float, current: float) -> float:
        """
        Calculate Exponential Moving Average.

        This smooths traffic-rate measurements so the ML model
        does not react too aggressively to short traffic spikes.
        """
        return ((1.0 - self.EMA_ALPHA) * previous) + (self.EMA_ALPHA * current)

    def _build_feature_dict(self, backend: dict) -> Dict[str, float]:
        """
        Build ML feature values for one backend server.
        """
        port_no = backend["port"]

        # Get latest traffic features for this backend port
        feats = self.port_features.get(
            port_no,
            {
                "tx_rate_bps": 0.0,
                "rx_rate_bps": 0.0,
                "drop_delta": 0.0
            },
        )

        # Total traffic across all backend ports
        total_tx = sum(f["tx_rate_bps"] for f in self.port_features.values())
        total_rx = sum(f["rx_rate_bps"] for f in self.port_features.values())

        # Total active flows across all backends
        total_active = sum(self.active_flows.values())

        tx_rate = feats["tx_rate_bps"]
        rx_rate = feats["rx_rate_bps"]
        active = self.active_flows[backend["ip"]]

        # Average values used to calculate imbalance
        backend_count = max(len(self.BACKENDS), 1)
        avg_tx = total_tx / backend_count
        avg_rx = total_rx / backend_count
        avg_active = total_active / backend_count

        return {
            "backend_index": float(port_no),
            "tx_rate_bps": tx_rate,
            "rx_rate_bps": rx_rate,
            "drop_delta": feats["drop_delta"],
            "active_flows_assigned": float(active),
            "total_tx_rate_bps": total_tx,
            "total_rx_rate_bps": total_rx,
            "total_active_flows": float(total_active),

            # Percentage share of traffic handled by this backend
            "tx_share": tx_rate / total_tx if total_tx > 0 else 0.0,
            "rx_share": rx_rate / total_rx if total_rx > 0 else 0.0,
            "active_flow_share": active / total_active if total_active > 0 else 0.0,

            # Difference from average backend load
            "tx_imbalance_bps": tx_rate - avg_tx,
            "rx_imbalance_bps": rx_rate - avg_rx,
            "flow_imbalance": active - avg_active,
        }

    def predict_best_backend(self):
        """
        Select the best backend.

        If ML model is available:
            Predict latency for each backend and choose the lowest score.

        If ML model is unavailable:
            Use least-loaded fallback.
        """
        if self.model is None:
            backend = self.pick_backend_least_loaded()
            feature_map = self._build_feature_dict(backend)
            return backend, "least_loaded_fallback", -1.0, feature_map

        best_backend = None
        best_pred = float("inf")
        best_score = float("inf")
        best_feature_map = None

        rows: List[List[float]] = []
        backend_feature_pairs = []

        # Build ML input rows for all backends
        for backend in self.BACKENDS:
            feature_map = self._build_feature_dict(backend)
            rows.append([feature_map[col] for col in self.model_feature_cols])
            backend_feature_pairs.append((backend, feature_map))

        # Predict latency for each backend
        predictions = self.model.predict(rows)

        for idx, pred in enumerate(predictions):
            backend, feature_map = backend_feature_pairs[idx]

            # Predicted latency from ML model
            pred_value = float(pred)

            # Extra penalties to avoid overloaded backends
            flow_penalty = 0.0
            tx_penalty = 0.0
            rx_penalty = 0.0

            if feature_map["active_flow_share"] > self.HOTSPOT_ACTIVE_FLOW_SHARE:
                flow_penalty = (
                    feature_map["active_flow_share"] - self.HOTSPOT_ACTIVE_FLOW_SHARE
                ) * self.FLOW_PENALTY_WEIGHT

            if feature_map["tx_share"] > self.HOTSPOT_TRAFFIC_SHARE:
                tx_penalty = (
                    feature_map["tx_share"] - self.HOTSPOT_TRAFFIC_SHARE
                ) * self.TX_SHARE_PENALTY_WEIGHT

            if feature_map["rx_share"] > self.HOTSPOT_TRAFFIC_SHARE:
                rx_penalty = (
                    feature_map["rx_share"] - self.HOTSPOT_TRAFFIC_SHARE
                ) * self.RX_SHARE_PENALTY_WEIGHT

            # Final score combines predicted latency and hotspot penalties
            score = pred_value + flow_penalty + tx_penalty + rx_penalty

            # Choose backend with lowest score
            if score < best_score:
                best_score = score
                best_pred = pred_value
                best_backend = backend
                best_feature_map = feature_map

        return best_backend, "ml_pred", best_pred, best_feature_map

    def _should_log_selection(self) -> bool:
        """
        Decide whether to print backend selection logs.
        """
        self.selection_count += 1
        return self.debug_logging or (
            self.selection_count % self.LOG_EVERY_N_SELECTIONS == 0
        )

    @set_ev_cls(ofp_event.EventOFPFlowRemoved, MAIN_DISPATCHER)
    def _flow_removed_handler(self, ev) -> None:
        """
        Handle expired OpenFlow rules.

        When a flow expires, remove it from the controller's
        internal tracking tables.
        """
        cookie = ev.msg.cookie

        flow_info = self.cookie_to_flow.pop(cookie, None)

        if flow_info is None:
            return

        flow_key, backend_ip = flow_info

        self.flow_map.pop(flow_key, None)

        if backend_ip in self.active_flows and self.active_flows[backend_ip] > 0:
            self.active_flows[backend_ip] -= 1

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def on_packet_in(self, ev) -> None:
        """
        Main packet-in handler.

        This function receives packets sent from the switch to the
        controller and decides what to do with them.
        """
        msg = ev.msg
        dp = msg.datapath
        parser = dp.ofproto_parser

        # Input port where the packet arrived
        in_port = msg.match["in_port"]

        # Parse raw packet data
        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocol(ethernet.ethernet)

        # Ignore invalid packets and LLDP packets
        if eth is None or eth.ethertype == 0x88cc:
            return

        dpid = dp.id

        # Learn source MAC address
        self.mac_to_port.setdefault(dpid, {})
        self.mac_to_port[dpid][eth.src] = in_port


        # ARP Handling
        arp_pkt = pkt.get_protocol(arp.arp)

        if arp_pkt:
            # Learn source IP to port mapping
            self.ip_to_port[arp_pkt.src_ip] = in_port

            # Reply to ARP request for VIP
            if arp_pkt.opcode == arp.ARP_REQUEST and arp_pkt.dst_ip == self.VIP_IP:
                self.reply_arp_for_vip(
                    dp,
                    in_port,
                    eth.src,
                    arp_pkt.src_ip,
                    arp_pkt.src_mac
                )
            else:
                # Flood normal ARP packets
                self.flood(dp, in_port, msg.data)

            return

        # IPv4 Handling
        ip_pkt = pkt.get_protocol(ipv4.ipv4)

        if not ip_pkt:
            self.l2_fallback(dp, in_port, eth.dst, msg)
            return

        # Learn source IP location
        self.ip_to_port[ip_pkt.src] = in_port

        # TCP Handling
        tcp_pkt = pkt.get_protocol(tcp.tcp)

        if not tcp_pkt:
            self.l2_fallback(dp, in_port, eth.dst, msg)
            return

        # If packet is not going to VIP/service port,
        # handle it as normal L2 switching traffic
        if ip_pkt.dst != self.VIP_IP or tcp_pkt.dst_port not in self.SERVICE_PORTS:
            self.l2_fallback(dp, in_port, eth.dst, msg)
            return

        # Client flow identity
        client_ip = ip_pkt.src
        client_tcp_src = tcp_pkt.src_port
        service_port = tcp_pkt.dst_port

        # Unique key for this TCP flow
        key = (client_ip, client_tcp_src, service_port)

        # Check whether this flow already has a backend assigned
        backend_ip = self.flow_map.get(key)

        if backend_ip is None:
            # Select backend using ML prediction or fallback
            backend, policy, pred_latency, feature_map = self.predict_best_backend()

            backend_ip = backend["ip"]

            # Save flow-to-backend mapping
            self.flow_map[key] = backend_ip

            # Increase active flow count for selected backend
            self.active_flows[backend_ip] += 1

            # Rebuild features after active flow count update
            feature_map = self._build_feature_dict(backend)

            # Log backend selection decision
            self._log_decision(
                client_ip=client_ip,
                client_tcp_src=client_tcp_src,
                backend_ip=backend_ip,
                backend_port_no=backend["port"],
                policy=policy,
                pred_latency=pred_latency,
                tx_rate_bps=feature_map["tx_rate_bps"],
                rx_rate_bps=feature_map["rx_rate_bps"],
                drop_delta=feature_map["drop_delta"],
                active_flows_assigned=int(feature_map["active_flows_assigned"]),
                backend_index=int(feature_map["backend_index"]),
                total_tx_rate_bps=feature_map["total_tx_rate_bps"],
                total_rx_rate_bps=feature_map["total_rx_rate_bps"],
                total_active_flows=int(feature_map["total_active_flows"]),
                tx_share=feature_map["tx_share"],
                rx_share=feature_map["rx_share"],
                active_flow_share=feature_map["active_flow_share"],
                tx_imbalance_bps=feature_map["tx_imbalance_bps"],
                rx_imbalance_bps=feature_map["rx_imbalance_bps"],
                flow_imbalance=feature_map["flow_imbalance"],
            )

        else:
            # Reuse existing backend for the same TCP flow
            backend = self.backend_by_ip[backend_ip]

        # Backend output information
        backend_port = backend["port"]
        backend_mac = backend["mac"]

        # Client output information
        client_port = self.ip_to_port.get(client_ip, in_port)
        client_mac = eth.src


        # Forward Flow Rule: Client -> Backend
        match_fwd = parser.OFPMatch(
            eth_type=0x0800,
            ip_proto=6,
            ipv4_src=client_ip,
            ipv4_dst=self.VIP_IP,
            tcp_src=client_tcp_src,
            tcp_dst=service_port,
        )

        actions_fwd = [
            # Rewrite Ethernet source to VIP MAC
            parser.OFPActionSetField(eth_src=self.VIP_MAC),

            # Rewrite Ethernet destination to backend MAC
            parser.OFPActionSetField(eth_dst=backend_mac),

            # Rewrite IP destination from VIP to backend IP
            parser.OFPActionSetField(ipv4_dst=backend["ip"]),

            # Send packet to backend server port
            parser.OFPActionOutput(backend_port),
        ]

        # Assign cookie for flow tracking
        flow_cookie = self._next_cookie()

        self.cookie_to_flow[flow_cookie] = (key, backend_ip)

        # Install forward rule
        self.add_flow(
            dp,
            priority=200,
            match=match_fwd,
            actions=actions_fwd,
            idle_timeout=60,
            cookie=flow_cookie,
            send_flow_removed=True,
        )

        # Reverse Flow Rule: Backend -> Client
        match_rev = parser.OFPMatch(
            eth_type=0x0800,
            ip_proto=6,
            ipv4_src=backend["ip"],
            ipv4_dst=client_ip,
            tcp_src=service_port,
            tcp_dst=client_tcp_src,
        )

        actions_rev = [
            # Rewrite Ethernet source to VIP MAC
            parser.OFPActionSetField(eth_src=self.VIP_MAC),

            # Rewrite Ethernet destination to client MAC
            parser.OFPActionSetField(eth_dst=client_mac),

            # Rewrite source IP from backend IP to VIP
            parser.OFPActionSetField(ipv4_src=self.VIP_IP),

            # Send packet back to client
            parser.OFPActionOutput(client_port),
        ]

        # Install reverse rule
        self.add_flow(
            dp,
            priority=200,
            match=match_rev,
            actions=actions_rev,
            idle_timeout=60
        )

        # Send the first packet immediately
        self.packet_out(dp, in_port, actions_fwd, msg.data)

    def reply_arp_for_vip(self, dp, out_port, dst_mac, dst_ip, dst_hw) -> None:
        """
        Reply to ARP requests for the virtual IP.

        This makes the client believe the VIP exists as a normal host.
        """
        parser = dp.ofproto_parser
        ofp = dp.ofproto

        # Build ARP reply packet
        p = packet.Packet()

        p.add_protocol(
            ethernet.ethernet(
                dst=dst_mac,
                src=self.VIP_MAC,
                ethertype=0x0806
            )
        )

        p.add_protocol(
            arp.arp(
                opcode=arp.ARP_REPLY,
                src_mac=self.VIP_MAC,
                src_ip=self.VIP_IP,
                dst_mac=dst_hw,
                dst_ip=dst_ip,
            )
        )

        p.serialize()

        actions = [parser.OFPActionOutput(out_port)]

        out = parser.OFPPacketOut(
            datapath=dp,
            buffer_id=ofp.OFP_NO_BUFFER,
            in_port=ofp.OFPP_CONTROLLER,
            actions=actions,
            data=p.data,
        )

        dp.send_msg(out)

    def flood(self, dp, in_port, data) -> None:
        """
        Flood packet out of all switch ports.

        Used mainly for normal ARP packets.
        """
        parser = dp.ofproto_parser
        ofp = dp.ofproto

        actions = [parser.OFPActionOutput(ofp.OFPP_FLOOD)]

        out = parser.OFPPacketOut(
            datapath=dp,
            buffer_id=ofp.OFP_NO_BUFFER,
            in_port=in_port,
            actions=actions,
            data=data,
        )

        dp.send_msg(out)

    def l2_fallback(self, dp, in_port, dst_mac, msg) -> None:
        """
        Basic Layer 2 switch fallback.

        If the traffic is not load-balancer traffic, this function
        forwards it like a normal learning switch.
        """
        ofp = dp.ofproto
        parser = dp.ofproto_parser
        dpid = dp.id

        self.mac_to_port.setdefault(dpid, {})

        # Find output port for destination MAC
        out_port = self.mac_to_port[dpid].get(dst_mac, ofp.OFPP_FLOOD)

        actions = [parser.OFPActionOutput(out_port)]

        # If destination is known, install a simple L2 forwarding rule
        if out_port != ofp.OFPP_FLOOD:
            match = parser.OFPMatch(eth_dst=dst_mac)
            self.add_flow(
                dp,
                priority=10,
                match=match,
                actions=actions
            )

        data = None if msg.buffer_id != ofp.OFP_NO_BUFFER else msg.data

        out = parser.OFPPacketOut(
            datapath=dp,
            buffer_id=msg.buffer_id,
            in_port=in_port,
            actions=actions,
            data=data,
        )

        dp.send_msg(out)
