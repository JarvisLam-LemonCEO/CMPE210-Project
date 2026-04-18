# lb_nat_rr.py
# Ryu OpenFlow 1.3 L4 Load Balancer (Round Robin) with VIP using simple NAT.
#
# Topology: h1(client) -> s1 -> h2/h3/h4(servers)
# VIP: 10.0.0.100 (not assigned to any host)
# Services: TCP ports 8000 (HTTP) and 5201 (iperf3)
#
# Run:
#   ryu-manager lb_nat_rr.py
#   sudo mn --topo single,4 --controller remote,ip=127.0.0.1 --switch ovsk,protocols=OpenFlow13 --mac

from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import CONFIG_DISPATCHER, MAIN_DISPATCHER, set_ev_cls
from ryu.ofproto import ofproto_v1_3
from ryu.lib.packet import packet
from ryu.lib.packet import ethernet, arp, ipv4, tcp


class NatRoundRobinLB(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    VIP_IP = "10.0.0.100"
    VIP_MAC = "00:00:00:00:00:64"
    SERVICE_PORTS = [8000, 5201]

    BACKENDS = [
        {"ip": "10.0.0.2", "mac": "00:00:00:00:00:02"},
        {"ip": "10.0.0.3", "mac": "00:00:00:00:00:03"},
        {"ip": "10.0.0.4", "mac": "00:00:00:00:00:04"},
    ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.backend_index = 0

        # learned mappings
        self.ip_to_port = {}      # client/host IP -> switch port
        self.mac_to_port = {}     # dpid -> {mac -> port}

        # hardcoded backend ports for Mininet single,4 topology with --mac
        # h2 -> s1-eth2 -> port 2
        # h3 -> s1-eth3 -> port 3
        # h4 -> s1-eth4 -> port 4
        self.backend_port = {
            "10.0.0.2": 2,
            "10.0.0.3": 3,
            "10.0.0.4": 4,
        }

        # key: (client_ip, client_tcp_port, service_port) -> backend dict
        self.flow_map = {}

    def pick_backend_rr(self):
        backend = self.BACKENDS[self.backend_index]
        self.backend_index = (self.backend_index + 1) % len(self.BACKENDS)
        return backend

    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        dp = ev.msg.datapath
        ofp = dp.ofproto
        parser = dp.ofproto_parser

        # table-miss -> controller
        match = parser.OFPMatch()
        actions = [parser.OFPActionOutput(ofp.OFPP_CONTROLLER, ofp.OFPCML_NO_BUFFER)]
        self.add_flow(dp, priority=0, match=match, actions=actions)

        self.logger.info("Connected: dpid=%s", dp.id)

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
            hard_timeout=hard_timeout,
        )
        dp.send_msg(mod)

    def packet_out(self, dp, in_port, actions, data):
        ofp = dp.ofproto
        parser = dp.ofproto_parser

        out = parser.OFPPacketOut(
            datapath=dp,
            buffer_id=ofp.OFP_NO_BUFFER,
            in_port=in_port,
            actions=actions,
            data=data
        )
        dp.send_msg(out)

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def packet_in_handler(self, ev):
        msg = ev.msg
        dp = msg.datapath
        parser = dp.ofproto_parser
        in_port = msg.match["in_port"]

        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocol(ethernet.ethernet)
        if eth is None:
            return

        # ignore LLDP
        if eth.ethertype == 0x88cc:
            return

        dpid = dp.id
        self.mac_to_port.setdefault(dpid, {})
        self.mac_to_port[dpid][eth.src] = in_port

        # ARP handling
        arp_pkt = pkt.get_protocol(arp.arp)
        if arp_pkt:
            self.ip_to_port[arp_pkt.src_ip] = in_port

            # reply to ARP for VIP
            if arp_pkt.opcode == arp.ARP_REQUEST and arp_pkt.dst_ip == self.VIP_IP:
                self.reply_arp_for_vip(dp, in_port, eth.src, arp_pkt.src_ip, arp_pkt.src_mac)
            else:
                self.flood(dp, in_port, msg.data)
            return

        # IPv4 handling
        ip_pkt = pkt.get_protocol(ipv4.ipv4)
        if not ip_pkt:
            self.l2_fallback(dp, in_port, eth.dst, msg)
            return

        self.ip_to_port[ip_pkt.src] = in_port

        # TCP only
        tcp_pkt = pkt.get_protocol(tcp.tcp)
        if not tcp_pkt:
            self.l2_fallback(dp, in_port, eth.dst, msg)
            return

        # must be VIP traffic and one of allowed service ports
        if ip_pkt.dst != self.VIP_IP or tcp_pkt.dst_port not in self.SERVICE_PORTS:
            self.l2_fallback(dp, in_port, eth.dst, msg)
            return

        client_ip = ip_pkt.src
        client_tcp_port = tcp_pkt.src_port
        service_port = tcp_pkt.dst_port

        key = (client_ip, client_tcp_port, service_port)
        backend = self.flow_map.get(key)
        if backend is None:
            backend = self.pick_backend_rr()
            self.flow_map[key] = backend

        backend_ip = backend["ip"]
        backend_mac = backend["mac"]

        # use hardcoded backend port mapping
        backend_port = self.backend_port.get(backend_ip)
        if backend_port is None:
            self.logger.warning("Unknown backend port for %s.", backend_ip)
            self.flood(dp, in_port, msg.data)
            return

        client_port = self.ip_to_port.get(client_ip, in_port)
        client_mac = eth.src

        # forward rule: client -> VIP:service_port ==> client -> backend:service_port
        match_fwd = parser.OFPMatch(
            eth_type=0x0800,
            ip_proto=6,
            ipv4_src=client_ip,
            ipv4_dst=self.VIP_IP,
            tcp_src=client_tcp_port,
            tcp_dst=service_port
        )
        actions_fwd = [
            parser.OFPActionSetField(eth_src=self.VIP_MAC),
            parser.OFPActionSetField(eth_dst=backend_mac),
            parser.OFPActionSetField(ipv4_dst=backend_ip),
            parser.OFPActionOutput(backend_port),
        ]
        self.add_flow(dp, priority=200, match=match_fwd, actions=actions_fwd)

        # reverse rule: backend:service_port -> client ==> VIP:service_port -> client
        match_rev = parser.OFPMatch(
            eth_type=0x0800,
            ip_proto=6,
            ipv4_src=backend_ip,
            ipv4_dst=client_ip,
            tcp_src=service_port,
            tcp_dst=client_tcp_port
        )
        actions_rev = [
            parser.OFPActionSetField(eth_src=self.VIP_MAC),
            parser.OFPActionSetField(eth_dst=client_mac),
            parser.OFPActionSetField(ipv4_src=self.VIP_IP),
            parser.OFPActionOutput(client_port),
        ]
        self.add_flow(dp, priority=200, match=match_rev, actions=actions_rev)

        self.logger.info(
            "RR LB: %s:%s -> VIP:%s ==> %s:%s (port %s)",
            client_ip, client_tcp_port, service_port, backend_ip, service_port, backend_port
        )

        # send first packet immediately with NAT actions
        self.packet_out(dp, in_port, actions_fwd, msg.data)
        return

    def reply_arp_for_vip(self, dp, out_port, dst_mac, dst_ip, dst_hw):
        parser = dp.ofproto_parser
        ofp = dp.ofproto

        p = packet.Packet()
        p.add_protocol(ethernet.ethernet(
            dst=dst_mac,
            src=self.VIP_MAC,
            ethertype=0x0806
        ))
        p.add_protocol(arp.arp(
            opcode=arp.ARP_REPLY,
            src_mac=self.VIP_MAC,
            src_ip=self.VIP_IP,
            dst_mac=dst_hw,
            dst_ip=dst_ip
        ))
        p.serialize()

        actions = [parser.OFPActionOutput(out_port)]
        out = parser.OFPPacketOut(
            datapath=dp,
            buffer_id=ofp.OFP_NO_BUFFER,
            in_port=ofp.OFPP_CONTROLLER,
            actions=actions,
            data=p.data
        )
        dp.send_msg(out)

    def flood(self, dp, in_port, data):
        ofp = dp.ofproto
        parser = dp.ofproto_parser
        actions = [parser.OFPActionOutput(ofp.OFPP_FLOOD)]
        out = parser.OFPPacketOut(
            datapath=dp,
            buffer_id=ofp.OFP_NO_BUFFER,
            in_port=in_port,
            actions=actions,
            data=data
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

        data = None
        if msg.buffer_id == ofp.OFP_NO_BUFFER:
            data = msg.data

        out = parser.OFPPacketOut(
            datapath=dp,
            buffer_id=msg.buffer_id,
            in_port=in_port,
            actions=actions,
            data=data
        )
        dp.send_msg(out)