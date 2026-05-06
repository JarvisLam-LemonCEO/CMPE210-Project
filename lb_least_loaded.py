# lb_least_loaded.py
# Ryu OpenFlow 1.3 L4 Load Balancer (Least-Loaded) with VIP using simple NAT.
#
# Topology: h1(client) -> s1 -> h2/h3/h4(servers)
# VIP: 10.0.0.100
# Services: TCP ports 8000 (HTTP) and 5201 (iperf3)
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



# NAT Least-Loaded Load Balancer Controller
class NatLeastLoadedLB(app_manager.RyuApp):
    """
    Ryu controller for a Layer 4 load balancer.

    This controller:
    1. Responds to ARP requests for the VIP.
    2. Receives TCP traffic sent to the VIP.
    3. Selects the backend with the fewest active flows.
    4. Installs OpenFlow NAT rules for client-to-server traffic.
    5. Installs reverse NAT rules for server-to-client traffic.
    6. Falls back to normal L2 switching for non-VIP traffic.
    """

    # Tell Ryu to use OpenFlow 1.3
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    # Virtual IP Configuration
    # Clients connect to this virtual IP instead of a real backend IP
    VIP_IP = "10.0.0.100"

    # Fake MAC address used for the virtual IP
    VIP_MAC = "00:00:00:00:00:64"

    # TCP ports handled by this load balancer
    SERVICE_PORTS = [8000, 5201]

    # Backend Server Configuration
    # Backend servers behind the load balancer
    BACKENDS = [
        {
            "ip": "10.0.0.2",
            "mac": "00:00:00:00:00:02"
        },
        {
            "ip": "10.0.0.3",
            "mac": "00:00:00:00:00:03"
        },
        {
            "ip": "10.0.0.4",
            "mac": "00:00:00:00:00:04"
        },
    ]

    def __init__(self, *args, **kwargs):
        """
        Initialize controller state.
        """
        super().__init__(*args, **kwargs)

        # Maps IP address to switch port
        # Example: "10.0.0.1" -> 1
        self.ip_to_port = {}

        # MAC learning table
        # Format: dpid -> {mac_address -> switch_port}
        self.mac_to_port = {}

        # Hardcoded backend ports for Mininet single,4 topology
        #
        # In this topology:
        #   h1 is usually on port 1
        #   h2 is usually on port 2
        #   h3 is usually on port 3
        #   h4 is usually on port 4
        self.backend_port = {
            "10.0.0.2": 2,
            "10.0.0.3": 3,
            "10.0.0.4": 4,
        }

        # Track active TCP flow count per backend
        #
        # Example:
        # {
        #   "10.0.0.2": 2,
        #   "10.0.0.3": 1,
        #   "10.0.0.4": 0
        # }
        self.active_flows = {
            b["ip"]: 0
            for b in self.BACKENDS
        }

        # Sticky flow mapping
        #
        # Once a client TCP flow is assigned to a backend,
        # future packets from the same flow keep using that backend.
        #
        # Key format:
        #   (client_ip, client_tcp_src_port, service_port)
        #
        # Value:
        #   backend_ip
        self.flow_map = {}

    # Switch Connection Handler
    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def on_switch_features(self, ev):
        """
        Called when the OpenFlow switch first connects.

        Installs the table-miss rule so unmatched packets are sent
        to the controller.
        """
        dp = ev.msg.datapath
        ofp = dp.ofproto
        parser = dp.ofproto_parser

        # Empty match means match all packets
        match = parser.OFPMatch()

        # Send unmatched packets to the controller
        actions = [
            parser.OFPActionOutput(
                ofp.OFPP_CONTROLLER,
                ofp.OFPCML_NO_BUFFER
            )
        ]

        # Priority 0 = lowest priority table-miss rule
        self.add_flow(
            dp,
            priority=0,
            match=match,
            actions=actions
        )

        self.logger.info("Switch connected: dpid=%s", dp.id)

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

        # Apply actions when packets match this rule
        inst = [
            parser.OFPInstructionActions(
                ofp.OFPIT_APPLY_ACTIONS,
                actions
            )
        ]

        # Build FlowMod message
        mod = parser.OFPFlowMod(
            datapath=dp,
            priority=priority,
            match=match,
            instructions=inst,
            idle_timeout=idle_timeout,
            hard_timeout=hard_timeout
        )

        # Send rule to switch
        dp.send_msg(mod)


    # OpenFlow Helper: Packet Out
    def packet_out(self, dp, in_port, actions, data):
        """
        Immediately send a packet out of the switch.

        Used for the first packet of a new flow, before the installed
        flow rules handle future packets automatically.
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

    # Least-Loaded Backend Selection
    def pick_backend_least_loaded(self):
        """
        Select backend with the fewest active flows.
        """
        # Find backend IP with the smallest active flow count
        best_ip = min(
            self.active_flows,
            key=self.active_flows.get
        )

        # Return backend object matching that IP
        for b in self.BACKENDS:
            if b["ip"] == best_ip:
                return b

        # Safety fallback
        return self.BACKENDS[0]


    # Main Packet-In Handler
    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def on_packet_in(self, ev):
        """
        Main packet handling function.

        Every packet sent from the switch to the controller arrives here.
        """
        msg = ev.msg
        dp = msg.datapath
        parser = dp.ofproto_parser

        # Switch port where packet entered
        in_port = msg.match["in_port"]

        # Parse raw packet bytes
        pkt = packet.Packet(msg.data)

        # Extract Ethernet header
        eth = pkt.get_protocol(ethernet.ethernet)

        # Ignore invalid packets and LLDP packets
        if eth is None or eth.ethertype == 0x88cc:
            return

        dpid = dp.id

        # Learn source MAC address location
        self.mac_to_port.setdefault(dpid, {})
        self.mac_to_port[dpid][eth.src] = in_port

        # ARP Handling
        arp_pkt = pkt.get_protocol(arp.arp)

        if arp_pkt:

            # Learn source IP address location
            self.ip_to_port[arp_pkt.src_ip] = in_port

            # If host asks "Who has VIP_IP?",
            # controller replies with VIP_MAC.
            if arp_pkt.opcode == arp.ARP_REQUEST and arp_pkt.dst_ip == self.VIP_IP:
                self.reply_arp_for_vip(
                    dp,
                    in_port,
                    eth.src,
                    arp_pkt.src_ip,
                    arp_pkt.src_mac
                )

            else:
                # For normal ARP packets, flood them like a switch
                self.flood(
                    dp,
                    in_port,
                    msg.data
                )

            return

        # IPv4 Handling
        ip_pkt = pkt.get_protocol(ipv4.ipv4)

        # If packet is not IPv4, use normal L2 forwarding
        if not ip_pkt:
            self.l2_fallback(
                dp,
                in_port,
                eth.dst,
                msg
            )
            return

        # Learn source IP address location
        self.ip_to_port[ip_pkt.src] = in_port


        # TCP Handling
        tcp_pkt = pkt.get_protocol(tcp.tcp)

        # If packet is not TCP, use normal L2 forwarding
        if not tcp_pkt:
            self.l2_fallback(
                dp,
                in_port,
                eth.dst,
                msg
            )
            return

        # Only handle traffic that is:
        #   1. Going to the virtual IP
        #   2. Using a supported service port
        if ip_pkt.dst != self.VIP_IP or tcp_pkt.dst_port not in self.SERVICE_PORTS:
            self.l2_fallback(
                dp,
                in_port,
                eth.dst,
                msg
            )
            return


        # New or Existing Load-Balanced TCP Flow
        client_ip = ip_pkt.src
        client_tcp_src = tcp_pkt.src_port
        service_port = tcp_pkt.dst_port

        # Unique flow key based on client IP, source TCP port, and service port
        key = (
            client_ip,
            client_tcp_src,
            service_port
        )

        # Check if this flow already has a backend assigned
        backend_ip = self.flow_map.get(key)

        if backend_ip is None:

            # New flow: select backend using least-loaded policy
            backend = self.pick_backend_least_loaded()
            backend_ip = backend["ip"]

            # Save sticky mapping for this flow
            self.flow_map[key] = backend_ip

            # Increase active flow count for selected backend
            self.active_flows[backend_ip] += 1

            self.logger.info(
                "LeastLoaded select: %s:%s -> %s:%s loads=%s",
                client_ip,
                client_tcp_src,
                backend_ip,
                service_port,
                self.active_flows
            )

        else:

            # Existing flow: reuse same backend
            backend = next(
                b for b in self.BACKENDS
                if b["ip"] == backend_ip
            )

        # Get backend MAC and switch port
        backend_mac = backend["mac"]
        backend_port = self.backend_port.get(backend_ip)

        # If backend port is unknown, flood packet as fallback
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

        # Find client port and MAC address
        client_port = self.ip_to_port.get(
            client_ip,
            in_port
        )

        client_mac = eth.src

        # Forward DNAT Rule: Client -> Backend
        #
        # Original packet:
        #   client_ip:client_tcp_src -> VIP_IP:service_port
        #
        # Rewritten packet:
        #   client_ip:client_tcp_src -> backend_ip:service_port
        match_fwd = parser.OFPMatch(
            eth_type=0x0800,
            ip_proto=6,
            ipv4_src=client_ip,
            ipv4_dst=self.VIP_IP,
            tcp_src=client_tcp_src,
            tcp_dst=service_port
        )

        actions_fwd = [

            # Rewrite Ethernet source as VIP MAC
            parser.OFPActionSetField(
                eth_src=self.VIP_MAC
            ),

            # Rewrite Ethernet destination as backend MAC
            parser.OFPActionSetField(
                eth_dst=backend_mac
            ),

            # Rewrite IP destination from VIP to backend IP
            parser.OFPActionSetField(
                ipv4_dst=backend_ip
            ),

            # Send packet to selected backend port
            parser.OFPActionOutput(
                backend_port
            ),
        ]

        self.add_flow(
            dp,
            priority=200,
            match=match_fwd,
            actions=actions_fwd,
            idle_timeout=60
        )


        # Reverse SNAT Rule: Backend -> Client
        # Original packet:
        #   backend_ip:service_port -> client_ip:client_tcp_src
        #
        # Rewritten packet:
        #   VIP_IP:service_port -> client_ip:client_tcp_src

        match_rev = parser.OFPMatch(
            eth_type=0x0800,
            ip_proto=6,
            ipv4_src=backend_ip,
            ipv4_dst=client_ip,
            tcp_src=service_port,
            tcp_dst=client_tcp_src
        )

        actions_rev = [

            # Rewrite Ethernet source as VIP MAC
            parser.OFPActionSetField(
                eth_src=self.VIP_MAC
            ),

            # Rewrite Ethernet destination as client MAC
            parser.OFPActionSetField(
                eth_dst=client_mac
            ),

            # Rewrite IP source from backend IP to VIP IP
            parser.OFPActionSetField(
                ipv4_src=self.VIP_IP
            ),

            # Send packet back to client port
            parser.OFPActionOutput(
                client_port
            ),
        ]

        self.add_flow(
            dp,
            priority=200,
            match=match_rev,
            actions=actions_rev,
            idle_timeout=60
        )

        # Send current packet immediately using NAT actions
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
        Reply to ARP request for the virtual IP.

        This makes the client believe that the VIP is reachable.
        """
        parser = dp.ofproto_parser
        ofp = dp.ofproto

        # Create ARP reply packet
        p = packet.Packet()

        # Ethernet header
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

        # Convert packet object to raw bytes
        p.serialize()

        # Send ARP reply back to requesting host
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

        Used for unknown destinations and normal ARP traffic.
        """
        parser = dp.ofproto_parser
        ofp = dp.ofproto

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


    # Layer 2 Fallback Switching
    def l2_fallback(
        self,
        dp,
        in_port,
        dst_mac,
        msg
    ):
        """
        Normal Layer 2 learning switch behavior.

        Used when traffic is not for the VIP load balancer.
        """
        ofp = dp.ofproto
        parser = dp.ofproto_parser
        dpid = dp.id

        # Make sure MAC table exists for this switch
        self.mac_to_port.setdefault(dpid, {})

        # Find output port for destination MAC,
        # otherwise flood if destination is unknown
        out_port = self.mac_to_port[dpid].get(
            dst_mac,
            ofp.OFPP_FLOOD
        )

        actions = [
            parser.OFPActionOutput(
                out_port
            )
        ]

        # If destination MAC is known, install L2 forwarding rule
        if out_port != ofp.OFPP_FLOOD:

            match = parser.OFPMatch(
                eth_dst=dst_mac
            )

            self.add_flow(
                dp,
                priority=10,
                match=match,
                actions=actions,
                idle_timeout=60
            )

        # Use buffered packet if possible
        data = None if msg.buffer_id != ofp.OFP_NO_BUFFER else msg.data

        out = parser.OFPPacketOut(
            datapath=dp,
            buffer_id=msg.buffer_id,
            in_port=in_port,
            actions=actions,
            data=data
        )

        dp.send_msg(out)
