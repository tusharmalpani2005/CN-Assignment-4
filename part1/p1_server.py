#!/usr/bin/env python3
"""
Reliable UDP File Transfer Server (Part 1)
Implements sliding window protocol with incremental file loading
"""

import socket
import struct
import time
import sys
import os
import select


class ReliableUDPServer:
    def __init__(self, server_ip, server_port, sws):
        self.server_ip = server_ip
        self.server_port = server_port
        self.SWS = sws  # Sender window size in bytes
        self.BUFFER_SIZE = sws * 4  # Read-ahead buffer (4x window)
        self.MAX_PAYLOAD = 1180  # Maximum data per packet
        self.HEADER_SIZE = 20  # 4 + 8 + 8 bytes

        # Socket
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind((self.server_ip, self.server_port))
        self.client_addr = None

        # File management
        self.file_handle = None
        self.file_size = 0
        self.next_seq_to_prepare = 0

        # Buffers
        self.send_buffer = {}  # {seq: data}
        self.in_flight = {}    # {seq: {'send_time': float, 'retx_count': int}}

        # Window management
        self.LAR = 0  # Last ACK Received
        self.LFS = 0  # Last Frame Sent

        # ACK tracking
        self.last_ack = 0
        self.dup_ack_count = 0

        # RTT and RTO
        self.estimated_rtt = 0.5
        self.dev_rtt = 0.25
        self.rto = 1.0

        # Statistics
        self.total_packets_sent = 0
        self.total_retransmissions = 0

    def create_packet(self, seq_num, timestamp, timestamp_echo, data):
        """Create a packet with header and data"""
        # Pack: sequence number (4 bytes), timestamp (8 bytes), timestamp_echo (8 bytes)
        header = struct.pack('!Idd', seq_num, timestamp, timestamp_echo)
        return header + data

    def parse_ack(self, packet):
        """Parse ACK packet"""
        if len(packet) < self.HEADER_SIZE:
            return None, None
        ack_num, _, timestamp_echo = struct.unpack('!Idd', packet[:self.HEADER_SIZE])
        return ack_num, timestamp_echo

    def load_file(self, filepath):
        """Open file and get size"""
        if not os.path.exists(filepath):
            print(f"Error: File {filepath} not found")
            sys.exit(1)

        self.file_handle = open(filepath, 'rb')
        self.file_size = os.path.getsize(filepath)
        print(f"Loaded file: {filepath}, size: {self.file_size} bytes")

    def ensure_buffer_filled(self):
        """Read ahead to keep buffer filled"""
        buffer_target = min(self.LAR + self.BUFFER_SIZE, self.file_size)

        while self.next_seq_to_prepare < buffer_target:
            chunk = self.file_handle.read(self.MAX_PAYLOAD)
            if not chunk:
                break

            self.send_buffer[self.next_seq_to_prepare] = chunk
            self.next_seq_to_prepare += len(chunk)

    def clean_old_packets(self):
        """Remove acknowledged packets from buffer"""
        # Keep some margin for potential retransmissions
        cleanup_threshold = max(0, self.LAR - self.SWS)
        to_remove = [seq for seq in self.send_buffer if seq < cleanup_threshold]
        for seq in to_remove:
            del self.send_buffer[seq]

    def send_packet(self, seq, data, is_retransmission=False):
        """Send a data packet"""
        timestamp = time.time()
        packet = self.create_packet(seq, timestamp, 0.0, data)
        self.sock.sendto(packet, self.client_addr)

        # Track in-flight
        if seq not in self.in_flight:
            self.in_flight[seq] = {'send_time': timestamp, 'retx_count': 0}
        else:
            self.in_flight[seq]['send_time'] = timestamp
            self.in_flight[seq]['retx_count'] += 1

        self.total_packets_sent += 1
        if is_retransmission:
            self.total_retransmissions += 1

    def send_packets_in_window(self):
        """Send packets within the current window"""
        while (self.LFS - self.LAR < self.SWS) and (self.LFS < self.next_seq_to_prepare):
            packet_data = self.send_buffer[self.LFS]
            self.send_packet(self.LFS, packet_data)
            self.LFS += len(packet_data)

    def update_rtt(self, sample_rtt):
        """Update RTT estimates and RTO"""
        if sample_rtt <= 0:
            return

        # Exponentially weighted moving average
        self.estimated_rtt = 0.875 * self.estimated_rtt + 0.125 * sample_rtt
        self.dev_rtt = 0.75 * self.dev_rtt + 0.25 * abs(sample_rtt - self.estimated_rtt)

        # Calculate RTO
        self.rto = self.estimated_rtt + 4.0 * self.dev_rtt

    def handle_ack(self, ack_num, timestamp_echo):
        """Process received ACK"""
        # Update RTT if we have a valid timestamp echo
        if timestamp_echo > 0 and ack_num in self.in_flight:
            sample_rtt = time.time() - self.in_flight[ack_num]['send_time']
            if sample_rtt > 0:
                self.update_rtt(sample_rtt)

        # Check if this is a new ACK or duplicate
        if ack_num > self.last_ack:
            # New ACK - advance window
            self.LAR = ack_num
            self.last_ack = ack_num
            self.dup_ack_count = 0

            # Remove acknowledged packets from in-flight
            to_remove = [seq for seq in self.in_flight if seq < ack_num]
            for seq in to_remove:
                del self.in_flight[seq]

            # Clean old buffered packets
            self.clean_old_packets()

            # Send more packets if window allows
            self.ensure_buffer_filled()
            self.send_packets_in_window()

        elif ack_num == self.last_ack:
            # Duplicate ACK
            self.dup_ack_count += 1

            # Fast retransmit on 3rd duplicate ACK
            if self.dup_ack_count == 3:
                print(f"Fast retransmit triggered for seq {ack_num}")
                if ack_num in self.send_buffer:
                    self.send_packet(ack_num, self.send_buffer[ack_num], is_retransmission=True)
                self.dup_ack_count = 0

    def get_oldest_unacked_seq(self):
        """Get sequence number of oldest unacknowledged packet"""
        if not self.in_flight:
            return None
        return min(self.in_flight.keys())

    def get_timeout_deadline(self):
        """Calculate when the next timeout should occur"""
        oldest_seq = self.get_oldest_unacked_seq()
        if oldest_seq is None:
            return None

        send_time = self.in_flight[oldest_seq]['send_time']
        return send_time + self.rto

    def handle_timeout(self):
        """Handle retransmission timeout"""
        oldest_seq = self.get_oldest_unacked_seq()
        if oldest_seq is not None:
            print(f"Timeout - retransmitting seq {oldest_seq}")
            if oldest_seq in self.send_buffer:
                self.send_packet(oldest_seq, self.send_buffer[oldest_seq], is_retransmission=True)
                # Exponential backoff
                self.rto = self.rto * 2

    def send_eof(self):
        """Send EOF packet to signal end of transfer"""
        eof_packet = self.create_packet(self.file_size, time.time(), 0.0, b'EOF')
        self.sock.sendto(eof_packet, self.client_addr)
        print("EOF packet sent")

    def wait_for_client(self):
        """Wait for client request"""
        print(f"Server listening on {self.server_ip}:{self.server_port}")
        data, addr = self.sock.recvfrom(1024)
        self.client_addr = addr
        print(f"Client connected: {addr}")
        return data

    def run(self):
        """Main server loop"""
        # Wait for client request
        self.wait_for_client()

        # Load file
        self.load_file('data.txt')

        # Initialize buffers
        self.ensure_buffer_filled()

        # Send initial window
        self.send_packets_in_window()

        start_time = time.time()

        # Main loop
        while self.LAR < self.file_size:
            # Calculate timeout for select
            deadline = self.get_timeout_deadline()
            if deadline is not None:
                timeout = max(0.001, deadline - time.time())
            else:
                timeout = 1.0

            # Wait for ACK or timeout
            ready, _, _ = select.select([self.sock], [], [], timeout)

            if ready:
                # Receive ACK
                try:
                    packet, addr = self.sock.recvfrom(1024)
                    ack_num, timestamp_echo = self.parse_ack(packet)

                    if ack_num is not None:
                        self.handle_ack(ack_num, timestamp_echo)
                except Exception as e:
                    print(f"Error receiving ACK: {e}")
            else:
                # Timeout occurred
                self.handle_timeout()

        # Send EOF
        self.send_eof()

        # Wait for EOF acknowledgment (with timeout)
        eof_acked = False
        for _ in range(5):
            ready, _, _ = select.select([self.sock], [], [], 1.0)
            if ready:
                try:
                    packet, _ = self.sock.recvfrom(1024)
                    ack_num, _ = self.parse_ack(packet)
                    if ack_num == self.file_size:
                        eof_acked = True
                        break
                except:
                    pass
            else:
                # Retry EOF
                self.send_eof()

        end_time = time.time()

        # Statistics
        print(f"\n=== Transfer Complete ===")
        print(f"Total time: {end_time - start_time:.2f} seconds")
        print(f"File size: {self.file_size} bytes")
        print(f"Total packets sent: {self.total_packets_sent}")
        print(f"Retransmissions: {self.total_retransmissions}")
        print(f"Throughput: {self.file_size / (end_time - start_time) / 1024:.2f} KB/s")

        # Cleanup
        if self.file_handle:
            self.file_handle.close()
        self.sock.close()


def main():
    if len(sys.argv) != 4:
        print("Usage: python3 p1_server.py <SERVER_IP> <SERVER_PORT> <SWS>")
        sys.exit(1)

    server_ip = sys.argv[1]
    server_port = int(sys.argv[2])
    sws = int(sys.argv[3])

    server = ReliableUDPServer(server_ip, server_port, sws)
    try:
        server.run()
    except KeyboardInterrupt:
        print("\nServer interrupted")
        if server.file_handle:
            server.file_handle.close()
        server.sock.close()


if __name__ == "__main__":
    main()
