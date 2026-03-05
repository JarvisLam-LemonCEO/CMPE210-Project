# lb_least_loaded.py
# Ryu OpenFlow 1.3 L4 Load Balancer (Least-Loaded) with VIP using simple NAT.
#
# Topology: h1(client) -> s1 -> h2/h3/h4(servers)
# VIP: 10.0.0.100 (not assigned to any host)
# Service: TCP port 8000
#
# Selection policy:
#   Choose backend with the smallest number of active assigned flows.
#   (Active flows are tracked when we create a new client flow mapping.
#    Optional: You can decrement with FlowRemoved events; for most class demos, TTL is OK.)
#
# Run:
#   ryu-manager lb_least_loaded.py
#   sudo mn --topo single,4 --controller remote,ip=127.0.0.1 --switch ovsk,protocols=OpenFlow13 --mac

from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import CONFIG_DISPATCHER, MAIN_DISPATCHER, set_ev_cls
from ryu.ofproto import ofproto_v1_3
from ryu.lib.packet import packet
from ryu.lib.packet import ethernet, arp, ipv4, tcp
from ryu.lib import mac as mac_lib


class NatLeastLoadedLB(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    # ======= CONFIG =======
    VIP_IP = "10.0.0.100"
    VIP_MAC = "00:00:00:00:00:64"   # any unique MAC is fine
    SERVICE_PORT = 8000

    BACKENDS = [
        {"ip": "10.0.0.2", "mac": "00:00:00:00:00:02"},
        {"ip": "10.0.0.3", "mac": "00:00:00:00:00:03"},
        {"ip": "10.0.0.4", "mac": "00:00:00:00:00:04"},
    ]
    # ======================

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # learning tables
        self.ip_to_port = {}     # ip -> switch port
        self.mac_to_port = {}    # dpid -> {mac -> port}

        # backend port mapping once learned (best) or fallback to ip_to_port
        self.backend_port = {}   # backend_ip -> switch port

        # Least-loaded counters: backend_ip -> active flow count
        self.active_flows = {b["ip"]: 0 for b in self.BACKENDS}

        # Sticky mapping: (client_ip, client_tcp_src) -> backend_ip
        self.flow_map = {}

    # ---------- OF base ----------
    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def on_switch_features(self, ev):
        dp = ev.msg.datapath
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

    # ---------- Least-loaded selection ----------
    def pick_backend_least_loaded(self):
        # Choose backend with minimum active flow count.
        best_ip = min(self.active_flows, key=self.active_flows.get)
        for b in self.BACKENDS:
            if b["ip"] == best_ip:
                return b
        return self.BACKENDS[0]

    # ---------- PacketIn ----------
    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def on_packet_in(self, ev):
        msg = ev.msg
        dp = msg.datapath
        ofp = dp.ofproto
        parser = dp.ofproto_parser
        in_port = msg.match["in_port"]

        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocol(ethernet.ethernet)
        if eth is None or eth.ethertype == 0x88cc:
            return

        dpid = dp.id
        self.mac_to_port.setdefault(dpid, {})
        self.mac_to_port[dpid][eth.src] = in_port

        # ---- ARP ----
        arp_pkt = pkt.get_protocol(arp.arp)
        if arp_pkt:
            # learn
            self.ip_to_port[arp_pkt.src_ip] = in_port
            self.maybe_learn_backend_port(arp_pkt.src_ip, in_port)

            # Reply to ARP for VIP
            if arp_pkt.opcode == arp.ARP_REQUEST and arp_pkt.dst_ip == self.VIP_IP:
                self.reply_arp_for_vip(dp, in_port, eth.src, arp_pkt.src_ip, arp_pkt.src_mac)
            else:
                self.flood(dp, in_port, msg.data)
            return

        # ---- IPv4 ----
        ip_pkt = pkt.get_protocol(ipv4.ipv4)
        if not ip_pkt:
            self.l2_fallback(dp, in_port, eth.dst, msg)
            return

        # learn source IP -> port
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

        # Sticky mapping so reverse flow is consistent
        backend_ip = self.flow_map.get(key)
        if backend_ip is None:
            backend = self.pick_backend_least_loaded()
            backend_ip = backend["ip"]
            self.flow_map[key] = backend_ip
            self.active_flows[backend_ip] += 1
            self.logger.info("LeastLoaded select: %s:%s -> %s  loads=%s",
                             client_ip, client_tcp_src, backend_ip, self.active_flows)
        else:
            backend = next(b for b in self.BACKENDS if b["ip"] == backend_ip)

        backend_mac = backend["mac"]

        # Need backend port
        backend_port = self.backend_port.get(backend_ip) or self.ip_to_port.get(backend_ip)
        if backend_port is None:
            self.logger.warning("Unknown backend port for %s. Run pingall first.", backend_ip)
            self.flood(dp, in_port, msg.data)
            return

        client_port = self.ip_to_port.get(client_ip, in_port)
        client_mac = eth.src

        # Forward DNAT flow: client -> VIP:8000 ==> client -> backend:8000
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
        self.add_flow(dp, priority=200, match=match_fwd, actions=actions_fwd, idle_timeout=60)

        # Reverse SNAT flow: backend:8000 -> client ==> VIP:8000 -> client
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
        self.add_flow(dp, priority=200, match=match_rev, actions=actions_rev, idle_timeout=60)

        # For simplicity, rely on TCP retransmission for the first packet.
        return

    def maybe_learn_backend_port(self, ip, in_port):
        for b in self.BACKENDS:
            if b["ip"] == ip:
                self.backend_port[ip] = in_port

    # ---------- ARP/VIP helpers ----------
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
        """Basic learning-switch fallback for non-VIP traffic."""
        ofp = dp.ofproto
        parser = dp.ofproto_parser
        dpid = dp.id

        self.mac_to_port.setdefault(dpid, {})
        out_port = self.mac_to_port[dpid].get(dst_mac, ofp.OFPP_FLOOD)

        actions = [parser.OFPActionOutput(out_port)]
        if out_port != ofp.OFPP_FLOOD:
            match = parser.OFPMatch(eth_dst=dst_mac)
            self.add_flow(dp, priority=10, match=match, actions=actions, idle_timeout=60)

        data = None if msg.buffer_id != ofp.OFP_NO_BUFFER else msg.data
        out = parser.OFPPacketOut(
            datapath=dp, buffer_id=msg.buffer_id,
            in_port=in_port, actions=actions, data=data
        )
        dp.send_msg(out)