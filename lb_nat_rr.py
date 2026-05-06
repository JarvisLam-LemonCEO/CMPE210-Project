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

# Packet parsing libraries
from ryu.lib.packet import packet
from ryu.lib.packet import ethernet, arp, ipv4, tcp

# Round Robin NAT Load Balancer Controller
class NatRoundRobinLB(app_manager.RyuApp):
    """
    Ryu controller for a Layer 4 Round Robin load balancer.

    This controller:
    1. Creates a virtual IP address.
    2. Replies to ARP requests for the VIP.
    3. Receives TCP traffic sent to the VIP.
    4. Selects backend servers using Round Robin.
    5. Installs NAT forwarding rules with OpenFlow.
    6. Uses normal Layer 2 switching for non-VIP traffic.
    """

    # Use OpenFlow 1.3
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]


    # VIP Configuration
    # Virtual IP that clients connect to
    VIP_IP = "10.0.0.100"

    # Virtual MAC address used for the VIP
    VIP_MAC = "00:00:00:00:00:64"

    # TCP ports handled by this load balancer
    SERVICE_PORTS = [8000, 5201]


    # Backend Server Configuration
    # Real backend servers behind the VIP
    BACKENDS = [
        {"ip": "10.0.0.2", "mac": "00:00:00:00:00:02"},
        {"ip": "10.0.0.3", "mac": "00:00:00:00:00:03"},
        {"ip": "10.0.0.4", "mac": "00:00:00:00:00:04"},
    ]

    def __init__(self, *args, **kwargs):
        """
        Initialize controller runtime state.
        """
        super().__init__(*args, **kwargs)

        # Current backend index for Round Robin selection
        self.backend_index = 0

        # IP learning table:
        # host IP -> switch port
        self.ip_to_port = {}

        # MAC learning table:
        # switch ID -> {MAC address -> switch port}
        self.mac_to_port = {}

        # Hardcoded backend ports for Mininet single,4 topology with --mac
        #
        # h1 -> s1-eth1 -> port 1
        # h2 -> s1-eth2 -> port 2
        # h3 -> s1-eth3 -> port 3
        # h4 -> s1-eth4 -> port 4
        self.backend_port = {
            "10.0.0.2": 2,
            "10.0.0.3": 3,
            "10.0.0.4": 4,
        }

        # Sticky flow table:
        #
        # key:
        #   (client_ip, client_tcp_port, service_port)
        #
        # value:
        #   selected backend dictionary
        #
        # This ensures all packets from the same TCP flow
        # continue using the same backend.
        self.flow_map = {}

    # Round Robin Backend Selection
    def pick_backend_rr(self):
        """
        Select backend using Round Robin.

        Each new flow is assigned to the next backend in order:
        h2 -> h3 -> h4 -> h2 -> ...
        """
        backend = self.BACKENDS[self.backend_index]

        # Move index to next backend
        self.backend_index = (
            self.backend_index + 1
        ) % len(self.BACKENDS)

        return backend


    # Switch Connection Handler
    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        """
        Called when the switch connects to the controller.

        Installs a table-miss flow rule so unknown packets are sent
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

        # Priority 0 means lowest priority table-miss rule
        self.add_flow(
            dp,
            priority=0,
            match=match,
            actions=actions
        )

        self.logger.info("Connected: dpid=%s", dp.id)

    # OpenFlow Helper: Add Flow Rule
    def add_flow(
        self,
        dp,
        priority,
        match,
        actions,
        idle_timeout=60,
        hard_timeout=0
    ):
        """
        Install an OpenFlow rule into the switch.
        """
        ofp = dp.ofproto
        parser = dp.ofproto_parser

        # Tell switch to apply the provided actions
        inst = [
            parser.OFPInstructionActions(
                ofp.OFPIT_APPLY_ACTIONS,
                actions
            )
        ]

        # Build OpenFlow FlowMod message
        mod = parser.OFPFlowMod(
            datapath=dp,
            priority=priority,
            match=match,
            instructions=inst,
            idle_timeout=idle_timeout,
            hard_timeout=hard_timeout,
        )

        # Send rule to switch
        dp.send_msg(mod)

    # OpenFlow Helper: PacketOut
    def packet_out(self, dp, in_port, actions, data):
        """
        Immediately send the current packet out of the switch.

        This is used for the first packet of a new flow before
        future packets are handled by installed flow rules.
        """
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

    # Main Packet-In Handler
    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def packet_in_handler(self, ev):
        """
        Handles packets sent from the switch to the controller.
        """
        msg = ev.msg
        dp = msg.datapath
        parser = dp.ofproto_parser

        # Port where packet entered the switch
        in_port = msg.match["in_port"]

        # Parse raw packet data
        pkt = packet.Packet(msg.data)

        # Extract Ethernet header
        eth = pkt.get_protocol(ethernet.ethernet)

        # Ignore invalid packets
        if eth is None:
            return

        # Ignore LLDP packets
        if eth.ethertype == 0x88cc:
            return

        dpid = dp.id

        # Learn source MAC location
        self.mac_to_port.setdefault(dpid, {})
        self.mac_to_port[dpid][eth.src] = in_port

        # ARP Handling
        arp_pkt = pkt.get_protocol(arp.arp)

        if arp_pkt:

            # Learn source IP location
            self.ip_to_port[arp_pkt.src_ip] = in_port

            # If host asks for VIP MAC, reply directly
            if arp_pkt.opcode == arp.ARP_REQUEST and arp_pkt.dst_ip == self.VIP_IP:
                self.reply_arp_for_vip(
                    dp,
                    in_port,
                    eth.src,
                    arp_pkt.src_ip,
                    arp_pkt.src_mac
                )

            else:
                # Normal ARP traffic is flooded
                self.flood(
                    dp,
                    in_port,
                    msg.data
                )

            return


        # IPv4 Handling
        ip_pkt = pkt.get_protocol(ipv4.ipv4)

        # Non-IPv4 traffic uses normal Layer 2 forwarding
        if not ip_pkt:
            self.l2_fallback(
                dp,
                in_port,
                eth.dst,
                msg
            )
            return

        # Learn source IP location
        self.ip_to_port[ip_pkt.src] = in_port


        # TCP Handling
        tcp_pkt = pkt.get_protocol(tcp.tcp)

        # Non-TCP traffic uses normal Layer 2 forwarding
        if not tcp_pkt:
            self.l2_fallback(
                dp,
                in_port,
                eth.dst,
                msg
            )
            return

        # Only load balance traffic that goes to:
        #   VIP_IP and supported service ports
        if ip_pkt.dst != self.VIP_IP or tcp_pkt.dst_port not in self.SERVICE_PORTS:
            self.l2_fallback(
                dp,
                in_port,
                eth.dst,
                msg
            )
            return

        # Load-Balanced TCP Flow Handling
        client_ip = ip_pkt.src
        client_tcp_port = tcp_pkt.src_port
        service_port = tcp_pkt.dst_port

        # Unique TCP flow identifier
        key = (
            client_ip,
            client_tcp_port,
            service_port
        )

        # Check whether this flow already has a backend assigned
        backend = self.flow_map.get(key)

        if backend is None:

            # New flow: choose backend using Round Robin
            backend = self.pick_backend_rr()

            # Save sticky mapping for this flow
            self.flow_map[key] = backend

        backend_ip = backend["ip"]
        backend_mac = backend["mac"]

        # Find output port for selected backend
        backend_port = self.backend_port.get(backend_ip)

        # If backend port is unknown, flood as fallback
        if backend_port is None:
            self.logger.warning(
                "Unknown backend port for %s.",
                backend_ip
            )

            self.flood(
                dp,
                in_port,
                msg.data
            )

            return

        # Find client return port and MAC
        client_port = self.ip_to_port.get(
            client_ip,
            in_port
        )

        client_mac = eth.src

        # Forward DNAT Rule: Client -> Backend
        # Original packet:
        #   client_ip:client_tcp_port -> VIP_IP:service_port
        #
        # Rewritten packet:
        #   client_ip:client_tcp_port -> backend_ip:service_port

        match_fwd = parser.OFPMatch(
            eth_type=0x0800,
            ip_proto=6,
            ipv4_src=client_ip,
            ipv4_dst=self.VIP_IP,
            tcp_src=client_tcp_port,
            tcp_dst=service_port
        )

        actions_fwd = [

            # Rewrite Ethernet source to VIP MAC
            parser.OFPActionSetField(
                eth_src=self.VIP_MAC
            ),

            # Rewrite Ethernet destination to backend MAC
            parser.OFPActionSetField(
                eth_dst=backend_mac
            ),

            # Rewrite destination IP from VIP to backend
            parser.OFPActionSetField(
                ipv4_dst=backend_ip
            ),

            # Send packet to backend port
            parser.OFPActionOutput(
                backend_port
            ),
        ]

        # Install forward NAT flow
        self.add_flow(
            dp,
            priority=200,
            match=match_fwd,
            actions=actions_fwd
        )

        # Reverse SNAT Rule: Backend -> Client
        # Original packet:
        #   backend_ip:service_port -> client_ip:client_tcp_port
        #
        # Rewritten packet:
        #   VIP_IP:service_port -> client_ip:client_tcp_port

        match_rev = parser.OFPMatch(
            eth_type=0x0800,
            ip_proto=6,
            ipv4_src=backend_ip,
            ipv4_dst=client_ip,
            tcp_src=service_port,
            tcp_dst=client_tcp_port
        )

        actions_rev = [

            # Rewrite Ethernet source to VIP MAC
            parser.OFPActionSetField(
                eth_src=self.VIP_MAC
            ),

            # Rewrite Ethernet destination to client MAC
            parser.OFPActionSetField(
                eth_dst=client_mac
            ),

            # Rewrite source IP from backend IP to VIP IP
            parser.OFPActionSetField(
                ipv4_src=self.VIP_IP
            ),

            # Send packet back to client
            parser.OFPActionOutput(
                client_port
            ),
        ]

        # Install reverse NAT flow
        self.add_flow(
            dp,
            priority=200,
            match=match_rev,
            actions=actions_rev
        )

        self.logger.info(
            "RR LB: %s:%s -> VIP:%s ==> %s:%s (port %s)",
            client_ip,
            client_tcp_port,
            service_port,
            backend_ip,
            service_port,
            backend_port
        )

        # Send the first packet immediately using forward NAT actions
        self.packet_out(
            dp,
            in_port,
            actions_fwd,
            msg.data
        )

        return


    # ARP Reply for VIP
    def reply_arp_for_vip(
        self,
        dp,
        out_port,
        dst_mac,
        dst_ip,
        dst_hw
    ):
        """
        Reply to ARP requests for the VIP.

        This makes h1 believe that 10.0.0.100 exists on the network.
        """
        parser = dp.ofproto_parser
        ofp = dp.ofproto

        # Create a new packet
        p = packet.Packet()

        # Ethernet header for ARP reply
        p.add_protocol(
            ethernet.ethernet(
                dst=dst_mac,
                src=self.VIP_MAC,
                ethertype=0x0806
            )
        )

        # ARP reply payload
        p.add_protocol(
            arp.arp(
                opcode=arp.ARP_REPLY,
                src_mac=self.VIP_MAC,
                src_ip=self.VIP_IP,
                dst_mac=dst_hw,
                dst_ip=dst_ip
            )
        )

        # Serialize packet object into raw bytes
        p.serialize()

        # Send ARP reply back to requester
        actions = [
            parser.OFPActionOutput(
                out_port
            )
        ]

        out = parser.OFPPacketOut(
            datapath=dp,
            buffer_id=ofp.OFP_NO_BUFFER,
            in_port=ofp.OFPP_CONTROLLER,
            actions=actions,
            data=p.data
        )

        dp.send_msg(out)


    # Flood Helper
    def flood(
        self,
        dp,
        in_port,
        data
    ):
        """
        Flood packet out of all switch ports.

        Used for ARP broadcasts and unknown destinations.
        """
        ofp = dp.ofproto
        parser = dp.ofproto_parser

        actions = [
            parser.OFPActionOutput(
                ofp.OFPP_FLOOD
            )
        ]

        out = parser.OFPPacketOut(
            datapath=dp,
            buffer_id=ofp.OFP_NO_BUFFER,
            in_port=in_port,
            actions=actions,
            data=data
        )

        dp.send_msg(out)


    # Layer 2 Fallback Switch Logic
    def l2_fallback(
        self,
        dp,
        in_port,
        dst_mac,
        msg
    ):
        """
        Normal Layer 2 learning switch behavior.

        Used when traffic is not TCP traffic for the VIP.
        """
        ofp = dp.ofproto
        parser = dp.ofproto_parser
        dpid = dp.id

        # Make sure MAC table exists for this switch
        self.mac_to_port.setdefault(dpid, {})

        # Find output port for destination MAC.
        # If unknown, flood.
        out_port = self.mac_to_port[dpid].get(
            dst_mac,
            ofp.OFPP_FLOOD
        )

        actions = [
            parser.OFPActionOutput(
                out_port
            )
        ]

        # If destination is known, install simple L2 flow rule
        if out_port != ofp.OFPP_FLOOD:

            match = parser.OFPMatch(
                eth_dst=dst_mac
            )

            self.add_flow(
                dp,
                priority=10,
                match=match,
                actions=actions
            )

        # Use packet data only when packet is not buffered by switch
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
