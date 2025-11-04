#!/usr/bin/env python3
"""
Reliable UDP server — sends file data with sequence numbers and supports SACK
and retransmission logic.
"""

import socket
import sys
import time
import struct
import threading
from collections import defaultdict


class ReliableUDPServer:
    """Server implementing a reliable UDP sender with SACK support."""

    def __init__(self, server_ip, server_port, sws):
        self.server_ip = server_ip
        self.server_port = server_port
        self.sws = sws
        self.sock = None
        self.client_addr = None

        self.mss = 1180
        self.header_size = 20
        self.max_payload = 1200

        self.send_base = 0
        self.next_seq_num = 0
        self.window = {}
        self.sack_blocks = []

        self.estimated_rtt = 0.05
        self.dev_rtt = 0.025
        self.rto = 0.1
        self.alpha = 0.125
        self.beta = 0.25

        self.dup_ack_count = defaultdict(int)
        self.fast_retransmit_threshold = 3

        self.sacked_packets = set()

        self.file_data = b''
        self.file_size = 0
        self.eof_sent = False
        self.eof_seq_num = 0
        self.transfer_complete = False

        self.lock = threading.Lock()
        self.stop_event = threading.Event()

    def create_packet(self, seq_num, data):
        """Return a packet bytes object with a 4-byte sequence number header."""
        header = struct.pack('!I', seq_num) + b'\x00' * 16
        return header + data

    def parse_ack(self, packet):
        """Parse an ACK packet and return (ack_num, sack_blocks)."""
        if len(packet) < 4:
            return None, []

        try:
            ack_num = struct.unpack('!I', packet[:4])[0]
        except struct.error:
            return None, []

        sack_blocks = []
        if len(packet) >= 20:
            sack_data = packet[4:20]
            for i in range(0, 16, 8):
                try:
                    sack_start = struct.unpack('!I', sack_data[i:i+4])[0]
                    sack_end = struct.unpack('!I', sack_data[i+4:i+8])[0]
                except struct.error:
                    break

                if 0 < sack_start < sack_end and sack_start >= ack_num:
                    sack_blocks.append((sack_start, sack_end))

        return ack_num, sack_blocks

    def update_sacked_packets(self):
        """Populate self.sacked_packets set using current SACK blocks."""
        self.sacked_packets.clear()
        for sack_start, sack_end in self.sack_blocks:
            for seq_num in list(self.window.keys()):
                data, _ = self.window[seq_num]
                packet_end = seq_num + len(data)
                if seq_num >= sack_start and packet_end <= sack_end:
                    self.sacked_packets.add(seq_num)

    def update_rto(self, sample_rtt):
        """Update RTO using exponential weighted moving averages."""
        if self.estimated_rtt == 0:
            self.estimated_rtt = sample_rtt
            self.dev_rtt = sample_rtt / 2
        else:
            self.estimated_rtt = (1 - self.alpha) * self.estimated_rtt + self.alpha * sample_rtt
            self.dev_rtt = (1 - self.beta) * self.dev_rtt + \
                self.beta * abs(sample_rtt - self.estimated_rtt)
        self.rto = self.estimated_rtt + 4 * self.dev_rtt
        self.rto = max(0.05, min(2.0, self.rto))  # Clamp between 50ms and 2s

    def send_data_packets(self):
        """Send as many data packets as the send window allows."""
        with self.lock:
            # Count ALL unique unacked bytes (including SACKed packets)
            bytes_in_flight = sum(len(data) for data, _ in self.window.values())
            available_window = self.sws - bytes_in_flight

            if available_window <= 0 and \
                not (self.next_seq_num >= self.file_size and not self.eof_sent):
                return

            while available_window > 0 and self.next_seq_num < self.file_size:
                remaining_file = self.file_size - self.next_seq_num
                packet_size = min(self.mss, remaining_file, available_window)
                data = self.file_data[self.next_seq_num:self.next_seq_num + packet_size]
                packet = self.create_packet(self.next_seq_num, data)
                self.sock.sendto(packet, self.client_addr)
                self.window[self.next_seq_num] = (data, time.time())
                self.next_seq_num += packet_size
                available_window -= packet_size

            if self.next_seq_num >= self.file_size and not self.eof_sent:
                eof_seq = self.file_size
                eof_packet = self.create_packet(eof_seq, b'EOF')
                self.sock.sendto(eof_packet, self.client_addr)
                self.window[eof_seq] = (b'EOF', time.time())
                self.eof_sent = True
                self.eof_seq_num = eof_seq

    def handle_ack(self, ack_num, sack_blocks):
        """Process an ACK and update send window, RTT and retransmissions."""
        with self.lock:
            self.sack_blocks = sack_blocks
            if sack_blocks:
                self.update_sacked_packets()

            # duplicate ACK handling
            if ack_num == self.send_base:
                self.dup_ack_count[ack_num] += 1
                if self.dup_ack_count[ack_num] == self.fast_retransmit_threshold:
                    if self.send_base in self.window and self.send_base not in self.sacked_packets:
                        data, _ = self.window[self.send_base]
                        packet = self.create_packet(self.send_base, data)
                        self.sock.sendto(packet, self.client_addr)
                        self.window[self.send_base] = (data, time.time())

                if sack_blocks and self.dup_ack_count[ack_num] >= self.fast_retransmit_threshold:
                    self.selective_retransmit(skip_send_base=True)
                return

            # new cumulative ACK
            if ack_num > self.send_base:
                # Sample RTT from ANY newly acknowledged packet
                for seq_num in list(self.window.keys()):
                    if seq_num >= self.send_base and seq_num < ack_num:
                        if seq_num not in self.sacked_packets:
                            _, send_time = self.window[seq_num]
                            sample_rtt = time.time() - send_time
                            self.update_rto(sample_rtt)
                            break

                self.send_base = ack_num
                for seq in list(self.window.keys()):
                    if seq < self.send_base:
                        del self.window[seq]
                        self.sacked_packets.discard(seq)

                self.dup_ack_count.clear()

                if self.eof_sent and self.send_base > self.eof_seq_num:
                    self.transfer_complete = True

    def selective_retransmit(self, skip_send_base=False):
        """Retransmit ALL packets in SACK holes immediately (no throttling)."""
        if not self.sack_blocks or not self.window:
            return

        sorted_sacks = sorted(self.sack_blocks, key=lambda x: x[0])
        first_sack_start = sorted_sacks[0][0]
        seqs_to_retransmit = []

        # Packets before first SACK block
        for seq_num in sorted(self.window.keys()):
            if self.send_base <= seq_num < first_sack_start and seq_num not in self.sacked_packets:
                if skip_send_base and seq_num == self.send_base:
                    continue
                seqs_to_retransmit.append(seq_num)

        # Packets in holes between SACK blocks
        for i in range(len(sorted_sacks) - 1):
            hole_start = sorted_sacks[i][1]
            hole_end = sorted_sacks[i + 1][0]
            for seq_num in sorted(self.window.keys()):
                if hole_start <= seq_num < hole_end and seq_num not in self.sacked_packets:
                    if seq_num not in seqs_to_retransmit:
                        seqs_to_retransmit.append(seq_num)

        # Retransmit ALL (no [:3] limit!)
        for seq_num in sorted(seqs_to_retransmit):
            if seq_num in self.window:
                data, _ = self.window[seq_num]
                packet = self.create_packet(seq_num, data)
                self.sock.sendto(packet, self.client_addr)
                self.window[seq_num] = (data, time.time())

    def retransmit_timeout_packets(self):
        """Retransmit any packet whose send time exceeded RTO."""
        current_time = time.time()
        with self.lock:
            for seq_num in list(self.window.keys()):
                if seq_num in self.sacked_packets:
                    continue
                data, send_time = self.window[seq_num]
                if current_time - send_time > self.rto:
                    packet = self.create_packet(seq_num, data)
                    self.sock.sendto(packet, self.client_addr)
                    self.window[seq_num] = (data, current_time)

    def receive_thread(self):
        """Background thread that receives ACKs from the client."""
        while not self.stop_event.is_set():
            try:
                self.sock.settimeout(0.1)
                packet, _ = self.sock.recvfrom(self.max_payload)
                ack_num, sack_blocks = self.parse_ack(packet)
                if ack_num is not None:
                    self.handle_ack(ack_num, sack_blocks)
            except socket.timeout:
                continue
            except OSError:
                break

    def run(self):
        """Main server loop: bind, wait for request, and serve the requested file."""
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind((self.server_ip, self.server_port))

        _, self.client_addr = self.sock.recvfrom(1)

        try:
            with open('data.txt', 'rb') as f:
                self.file_data = f.read()
                self.file_size = len(self.file_data)
        except FileNotFoundError:
            self.sock.close()
            return

        recv_thread = threading.Thread(target=self.receive_thread)
        recv_thread.daemon = True
        recv_thread.start()

        max_wait_after_eof = 10.0
        eof_send_time = None

        while not self.transfer_complete and not self.stop_event.is_set():
            self.send_data_packets()
            self.retransmit_timeout_packets()

            if self.eof_sent and eof_send_time is None:
                eof_send_time = time.time()

            if eof_send_time and (time.time() - eof_send_time) > max_wait_after_eof:
                break

            # Minimal sleep to avoid CPU spin
            time.sleep(0.0001)

        if self.transfer_complete:
            time.sleep(0.2)

        self.stop_event.set()
        recv_thread.join(timeout=1)
        self.sock.close()


def main():
    """CLI entry point: expects server_ip server_port sws."""
    if len(sys.argv) != 4:
        sys.exit(1)

    server_ip = sys.argv[1]
    server_port = int(sys.argv[2])
    sws = int(sys.argv[3])

    server = ReliableUDPServer(server_ip, server_port, sws)
    server.run()


if __name__ == "__main__":
    main()