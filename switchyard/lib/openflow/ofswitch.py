import socket
import ssl
from threading import Thread
import time
from heapq import heappush, heappop, heapreplace
from copy import deepcopy

from switchyard.lib.packet import *
from switchyard.lib.openflow import *
from switchyard.lib.address import *
from switchyard.lib.common import *


class FullBuffer(Exception):
    pass


class PacketBufferManager(object):

    def __init__(self, buffsize):
        self._buffsize = buffsize
        self._buffer = {}

    def add(self, pkt):
        id = len(self._buffer) + 1
        if id > self._buffsize:
            raise FullBuffer()

        self._buffer[id] = deepcopy(pkt)
        return id

    def pop(self, id):
        return self._buffer.pop(id)

    def lookup(self, id):
        return self._buffer[id]


class TableEntry(object):
    def __init__(self, fmod):
        self._match = fmod.match
        self._cookie = fmod.cookie
        self._idle_timeout = fmod.idle_timeout
        self._hard_timeout = fmod.hard_timeout
        self._actions = fmod.actions
        self._priority = fmod.priority
        self._flags = fmod.flags
        self._packets_matched = 0
        self._bytes_matched = 0

    @property
    def priority(self):
        return self._priority

    @property
    def match(self):
        return self._match

    def __cmp__(self, other):
        return cmp(self.priority, other.priority)

    def __hash__(self):
        return self._cookie


class FlowTable(object):
    def __init__(self):
        self._table = []

    def delete(self, matcher, strict=False):
        tbd = []
        for entry in self._table:
            if entry.match.overlaps_with(matcher, strict):
                tbd.append(entry)

        log_debug("{} table entries deleted".format(len(tbd)))

        # for each entry, remove it, and if flags say so, emit a
        # flow removed message
        notify = []
        for entry in tbd:
            if FlowModFlags.SendFlowRemove in entry.get_flags:
                notify.append(entry)
            self._table.remove(entry)
        return notify

    def match_packet(self, pkt):
        for entry in self._table:
            if entry.match.matches_packet(pkt):
                return entry.actions
        return None


class SwitchAction(object):
    '''
    idea: want to wrap an OpenflowAction object and *apply* it on a packet.
    do this as a functor (__call__).

    for some actions, need access to net object to do the output, other actions
    just need to modify the packet.

    is that it?  maybe just give a reference to the switch itself?
    '''
    pass    


class OpenflowSwitch(object):
    '''
    An Openflow v1.0 switch.
    '''

    def __init__(self, switchyard_net, switchid):
        self._switchid = switchid  # aka, dpid
        self._controller_connections = []
        self._switchyard_net = switchyard_net
        self._running = True
        self._buffer_manager = PacketBufferManager(100)
        self._xid = 0
        self._miss_len = 1500
        self._flags = OpenflowConfigFlags.FragNormal
        self._ready = False
        self._table = FlowTable()

    def add_controller(self, host, port):
        log_debug("Switch connecting to controller {}:{}".format(host, port))
        sock = socket.socket()  # ssl.wrap_socket(socket.socket())
        sock.settimeout(1.0)
        sock.connect((host, port))
        t = Thread(target=self._controller_thread, args=(sock,))
        self._controller_connections.append((t,sock))
        t.start()

    @property
    def xid(self):
        self._xid += 1
        return self._xid

    def _send_packet_in(self, port, packet):
        ofpkt = OpenflowHeader.build(OpenflowType.PacketIn, self.xid)
        ofpkt[1].packet = packet.to_bytes()[:self._miss_len]
        ofpkt[1].buffer_id = self._buffer_manager.add(packet)
        ofpkt[1].reason = OpenflowPacketInReason.NoMatch
        ofpkt[1].in_port = port[-1]
        for _,sock in self._controller_connections:
            send_openflow_message(sock, ofpkt)

    def _controller_thread(self, sock):
        def _send_removal_notification(notifylist):
            raise Exception("Implement me")

        def _hello_handler(pkt):
            log_debug("Hello version: {}".format(pkt[0].version))
            self._ready = True

        def _features_request_handler(pkt):
            header = OpenflowHeader(OpenflowType.FeaturesReply, self.xid)
            featuresreply = OpenflowSwitchFeaturesReply()
            featuresreply.dpid_low48 = self._switchid
            for i, intf in enumerate(self._switchyard_net.ports()):
                featuresreply.ports.append(
                    OpenflowPhysicalPort(i, intf.ethaddr, intf.name))
            log_debug("Sending features reply: {}".format(featuresreply))
            send_openflow_message(sock, header + featuresreply)

        def _set_config_handler(pkt):
            setconfig = pkt[1]
            self._flags = setconfig.flags
            self._miss_len = setconfig.miss_send_len
            log_debug("Set config: flags {} misslen {}".format(
                self._flags, self._miss_len))

        def _get_config_request_handler(pkt):
            log_debug("Get Config request")
            header = OpenflowHeader(OpenflowType.GetConfigReply, self.xid)
            reply = OpenflowGetConfigReply()
            reply.flags = self._flags
            reply.miss_send_len = self._miss_len
            send_openflow_message(sock, header + reply)

        def _flow_mod_handler(pkt):
            log_debug("Flow mod")
            fmod = pkt[1]
            if fmod.command == FlowModCommand.Add:
                log_debug ("Add")
            elif fmod.command == FlowModCommand.Modify:
                log_debug ("Modify")
            elif fmod.command == FlowModCommand.ModifyStrict:
                log_debug ("ModStrict")
            elif fmod.command == FlowModCommand.Delete:
                notify = self._table.delete(fmod.match)
            elif fmod.command == FlowModCommand.DeleteStrict:
                notify = self._table.delete(fmod.match, strict=True)
            else:
                raise Exception("Unknown flowmod command {}".format(fmod.command))

            if notify:
                _send_removal_notification(notify)

        def _barrier_request_handler(pkt):
            log_debug("Barrier request")
            reply = OpenflowHeader(OpenflowType.BarrierReply, xid=header.xid)
            send_openflow_message(sock, reply)

        def _packet_out_handler(pkt):
            actions = pkt[1].actions
            if pkt[1].buffer_id != 0xffffffff:
                outpkt = self._buffer_manager.pop(pkt[1].buffer_id)
            else:
                outpkt = pkt[1].packet
            in_port = pkt[1].in_port
            log_debug ("pkt {} buffid {} actions {} inport {}".format(outpkt, pkt[1].buffer_id, actions, in_port))
            self._process_actions(actions, outpkt)

        _handler_map = {
            OpenflowType.Hello: _hello_handler,
            OpenflowType.FeaturesRequest: _features_request_handler,
            OpenflowType.SetConfig: _set_config_handler,
            OpenflowType.GetConfigRequest: _get_config_request_handler,
            OpenflowType.FlowMod: _flow_mod_handler,
            OpenflowType.BarrierRequest: _barrier_request_handler,
            OpenflowType.PacketOut: _packet_out_handler,
        }

        def _unknown_type_handler(pkt):
            log_debug("Unknown OF message type: {}".format(pkt[0].type))

        pkt = Packet()
        pkt += OpenflowHeader(OpenflowType.Hello, self.xid)
        send_openflow_message(sock, pkt)

        while self._running:
            try:
                pkt = receive_openflow_message(sock)
            except socket.timeout:
                continue

            if pkt is not None:
                header = pkt[0]
                _handler_map.get(header.type, _unknown_type_handler)(pkt)


    def _process_actions(self, actions, packet):
        for a in actions:
            self._apply_action(packet, a)

    def _apply_action(self, packet, action):
        debugger()
        raise Exception("Not implemented yet")

    def datapath_loop(self):
        log_debug("datapath loop: not ready to receive")
        while not self._ready:
            time.sleep(0.5)

        log_debug("datapath loop: READY to receive")
        while True:
            try:
                port, packet = self._switchyard_net.recv_packet(timeout=1.0)
            except Shutdown:
                break
            except NoPackets:
                continue

            log_info("Packet arrived: {}->{}".format(port, packet))
            actions = self._table.match_packet(packet)
            if not actions:
                self._send_packet_in(port, packet)
            else:
                self._process_actions(actions, packet)


    def shutdown(self):
        self._running = False
        for t,sock in self._controller_connections:
            t.join()


def main(net, host='localhost', port=6633, switchid=EthAddr("de:ad:00:00:be:ef")):
    switch = OpenflowSwitch(net, switchid)
    switch.add_controller('localhost', 6633)
    switch.datapath_loop()
    switch.shutdown()
    net.shutdown()
