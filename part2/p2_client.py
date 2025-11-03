#!/usr/bin/env python3
"""
Reliable UDP File Transfer Client (Part 2)
Implements receiver with out-of-order handling and immediate ACKs
"""

import socket
import struct
import time
import sys


class ReliableUDPClient:
    def __init__(self, server_ip, server_port, pref_filename):
        self.server_ip = server_ip
        self.server_port = server_port
        self.server_addr = (server_ip, server_port)
        self.pref_filename = pref_filename
        self.MAX_PAYLOAD = 1180
        self.HEADER_SIZE = 20

        # Socket
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.settimeout(2.0)  # 2 second timeout for retries

        # Reception state
        self.next_expected_seq = 0
        self.recv_buffer = {}  # {seq: data} for out-of-order packets
        self.output_file = None

        # Statistics
        self.total_packets_received = 0
        self.total_acks_sent = 0
        self.duplicate_packets = 0
        self.total_bytes_received = 0

    def create_ack(self, ack_num, timestamp_echo):
        """Create ACK packet"""
        # ACK packet: ack_num in seq field, no data, echo timestamp
        header = struct.pack('!Idd', ack_num, 0.0, timestamp_echo)
        return header

    def parse_packet(self, packet):
        """Parse received packet"""
        if len(packet) < self.HEADER_SIZE:
            return None, None, None

        seq_num, timestamp, timestamp_echo = struct.unpack('!Idd', packet[:self.HEADER_SIZE])
        data = packet[self.HEADER_SIZE:]
        return seq_num, timestamp, data

    def send_ack(self, ack_num, timestamp_echo):
        """Send ACK to server"""
        ack_packet = self.create_ack(ack_num, timestamp_echo)
        self.sock.sendto(ack_packet, self.server_addr)
        self.total_acks_sent += 1

    def send_request(self):
        """Send file request to server with retries"""
        request = b'R'  # Request byte
        max_retries = 5
        retry_timeout = 2.0

        for attempt in range(max_retries):
            print(f"Sending request to server (attempt {attempt + 1}/{max_retries})")
            self.sock.sendto(request, self.server_addr)

            try:
                # Wait for first data packet
                self.sock.settimeout(retry_timeout)
                packet, addr = self.sock.recvfrom(2048)

                if addr == self.server_addr:
                    print("Request acknowledged by server")
                    # Set socket to blocking for main transfer
                    self.sock.settimeout(None)
                    return packet  # Return first packet
            except socket.timeout:
                print(f"Request timeout (attempt {attempt + 1})")
                continue

        print("Error: Failed to connect to server after 5 attempts")
        sys.exit(1)

    def write_data(self, seq, data):
        """Write data to file"""
        if self.output_file:
            self.output_file.write(data)
            self.total_bytes_received += len(data)

    def process_buffered_packets(self):
        """Process any consecutive buffered packets"""
        while self.next_expected_seq in self.recv_buffer:
            data = self.recv_buffer[self.next_expected_seq]
            self.write_data(self.next_expected_seq, data)
            del self.recv_buffer[self.next_expected_seq]
            self.next_expected_seq += len(data)

    def handle_packet(self, seq, timestamp, data):
        """Handle received data packet"""
        self.total_packets_received += 1

        # Check if this is EOF
        if data == b'EOF':
            print("EOF received - transfer complete")
            self.send_ack(seq, timestamp)
            return True  # Signal completion

        # Check if this is the expected packet
        if seq == self.next_expected_seq:
            # In-order packet
            self.write_data(seq, data)
            self.next_expected_seq += len(data)

            # Check if we can now process buffered packets
            self.process_buffered_packets()

            # Send ACK with updated next_expected_seq
            self.send_ack(self.next_expected_seq, timestamp)

        elif seq > self.next_expected_seq:
            # Out-of-order packet - buffer it
            if seq not in self.recv_buffer:
                self.recv_buffer[seq] = data
            else:
                self.duplicate_packets += 1

            # Send duplicate ACK (same next_expected_seq)
            self.send_ack(self.next_expected_seq, timestamp)

        else:
            # Old packet (seq < next_expected_seq) - duplicate
            self.duplicate_packets += 1
            # Send ACK anyway (might be lost ACK)
            self.send_ack(self.next_expected_seq, timestamp)

        return False  # Not done yet

    def run(self):
        """Main client loop"""
        print(f"Connecting to server {self.server_ip}:{self.server_port}")

        # Send request and get first packet
        first_packet = self.send_request()

        # Open output file with prefix
        output_filename = f"{self.pref_filename}received_data.txt"
        self.output_file = open(output_filename, 'wb')
        print(f"Receiving file to {output_filename}...")

        start_time = time.time()

        # Process first packet
        seq, timestamp, data = self.parse_packet(first_packet)
        if seq is not None:
            if self.handle_packet(seq, timestamp, data):
                # EOF in first packet (shouldn't happen for normal files)
                self.output_file.close()
                return

        # Main receive loop
        transfer_complete = False
        while not transfer_complete:
            try:
                packet, addr = self.sock.recvfrom(2048)

                if addr != self.server_addr:
                    continue

                seq, timestamp, data = self.parse_packet(packet)
                if seq is not None:
                    transfer_complete = self.handle_packet(seq, timestamp, data)

            except KeyboardInterrupt:
                print("\nClient interrupted")
                break
            except Exception as e:
                print(f"Error: {e}")
                break

        end_time = time.time()

        # Close file
        if self.output_file:
            self.output_file.close()

        # Statistics
        print(f"\n=== Transfer Complete ===")
        print(f"Total time: {end_time - start_time:.2f} seconds")
        print(f"Bytes received: {self.total_bytes_received}")
        print(f"Packets received: {self.total_packets_received}")
        print(f"ACKs sent: {self.total_acks_sent}")
        print(f"Duplicate packets: {self.duplicate_packets}")
        print(f"Out-of-order packets buffered: {len(self.recv_buffer)}")

        # Calculate throughput
        if end_time > start_time:
            throughput = self.total_bytes_received / (end_time - start_time) / 1024
            print(f"Throughput: {throughput:.2f} KB/s")

        # Verify file
        try:
            with open(output_filename, 'rb') as f:
                f.seek(0, 2)  # Seek to end
                received_size = f.tell()
            print(f"Received file size: {received_size} bytes")
        except:
            pass

        self.sock.close()


def main():
    if len(sys.argv) != 4:
        print("Usage: python3 p2_client.py <SERVER_IP> <SERVER_PORT> <PREF_FILENAME>")
        sys.exit(1)

    server_ip = sys.argv[1]
    server_port = int(sys.argv[2])
    pref_filename = sys.argv[3]

    client = ReliableUDPClient(server_ip, server_port, pref_filename)
    try:
        client.run()
    except KeyboardInterrupt:
        print("\nClient interrupted")
        if client.output_file:
            client.output_file.close()
        client.sock.close()


if __name__ == "__main__":
    main()
