#!/usr/bin/env python3

import socket
import sys
import time
import struct
import threading
import os
from collections import defaultdict

class ReliableUDPServer:
    def __init__(self, server_ip, server_port, sws):
        self.server_ip = server_ip
        self.server_port = server_port
        self.sws = sws  # Sender Window Size in bytes
        self.sock = None
        self.client_addr = None
        
        # Packet parameters
        self.MSS = 1180  # Maximum Segment Size (1200 - 20 header)
        self.HEADER_SIZE = 20
        self.MAX_PAYLOAD = 1200
        
        # Sliding window variables
        self.send_base = 0  # Oldest unacknowledged byte
        self.next_seq_num = 0  # Next sequence number to send
        self.window = {}  # seq_num -> (data, timestamp)
        self.sack_blocks = []  # List of SACK blocks received
        
        # RTT estimation (Jacobson/Karels algorithm)
        self.estimated_rtt = 0.1  # Initial estimate (100ms)
        self.dev_rtt = 0.01  # Initial deviation
        self.rto = 0.2  # Initial retransmission timeout
        self.alpha = 0.125  # RTT smoothing factor
        self.beta = 0.25  # Deviation smoothing factor
        
        # Fast retransmit
        self.dup_ack_count = defaultdict(int)
        self.fast_retransmit_threshold = 3
        
        # File data
        self.file_data = b''
        self.file_size = 0
        self.eof_sent = False
        self.eof_seq_num = 0
        self.transfer_complete = False
        
        # Threading
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        
        # Statistics
        self.packets_sent = 0
        self.packets_retransmitted = 0
        self.start_time = 0

    def create_packet(self, seq_num, data):
        """Create a packet with header and data"""
        # Header: 4 bytes seq_num + 16 bytes reserved
        header = struct.pack('!I', seq_num) + b'\x00' * 16
        return header + data

    def parse_ack(self, packet):
        """Parse ACK packet to get ack number and SACK blocks"""
        if len(packet) < 4:
            return None, []
        
        ack_num = struct.unpack('!I', packet[:4])[0]
        sack_blocks = []
        
        # Parse SACK blocks from reserved field (16 bytes)
        # Each SACK block is 8 bytes (4 bytes start, 4 bytes end)
        if len(packet) >= 20:
            sack_data = packet[4:20]
            for i in range(0, 16, 8):
                try:
                    sack_start = struct.unpack('!I', sack_data[i:i+4])[0]
                    sack_end = struct.unpack('!I', sack_data[i+4:i+8])[0]
                    if sack_start > 0 and sack_end > sack_start:
                        sack_blocks.append((sack_start, sack_end))
                except:
                    break
        
        return ack_num, sack_blocks

    def update_rto(self, sample_rtt):
        """Update RTO based on RTT sample"""
        if self.estimated_rtt == 0:
            self.estimated_rtt = sample_rtt
            self.dev_rtt = sample_rtt / 2
        else:
            self.estimated_rtt = (1 - self.alpha) * self.estimated_rtt + self.alpha * sample_rtt
            self.dev_rtt = (1 - self.beta) * self.dev_rtt + self.beta * abs(sample_rtt - self.estimated_rtt)
        
        # Jacobson/Karels RTO calculation
        self.rto = self.estimated_rtt + 4 * self.dev_rtt
        # Bound RTO between 0.1 and 3 seconds
        self.rto = max(0.1, min(3.0, self.rto))

    def send_data_packets(self):
        """Send data packets within the sliding window"""
        with self.lock:
            # Calculate how many bytes we can send
            bytes_in_flight = self.next_seq_num - self.send_base
            available_window = self.sws - bytes_in_flight
            
            if available_window <= 0 and not (self.next_seq_num >= self.file_size and not self.eof_sent):
                return
            
            # Send new packets
            while available_window > 0 and self.next_seq_num < self.file_size:
                # Determine packet size
                remaining_file = self.file_size - self.next_seq_num
                packet_size = min(self.MSS, remaining_file, available_window)
                
                # Get data for this packet
                data = self.file_data[self.next_seq_num:self.next_seq_num + packet_size]
                
                # Create and send packet
                packet = self.create_packet(self.next_seq_num, data)
                self.sock.sendto(packet, self.client_addr)
                
                # Store packet for potential retransmission
                self.window[self.next_seq_num] = (data, time.time())
                
                self.next_seq_num += packet_size
                available_window -= packet_size
                self.packets_sent += 1
                
            # Send EOF if all data is sent (but not necessarily acknowledged)
            if self.next_seq_num >= self.file_size and not self.eof_sent:
                # EOF is sent at the sequence number after all data
                eof_seq = self.file_size
                eof_packet = self.create_packet(eof_seq, b'EOF')
                self.sock.sendto(eof_packet, self.client_addr)
                self.window[eof_seq] = (b'EOF', time.time())
                self.eof_sent = True
                self.eof_seq_num = eof_seq
                self.packets_sent += 1
                print(f"Sent EOF at seq {eof_seq}")

    def handle_ack(self, ack_num, sack_blocks):
        """Handle received ACK and SACK blocks"""
        with self.lock:
            # Update SACK blocks
            self.sack_blocks = sack_blocks
            
            # Check for duplicate ACK (fast retransmit)
            if ack_num == self.send_base:
                self.dup_ack_count[ack_num] += 1
                
                if self.dup_ack_count[ack_num] >= self.fast_retransmit_threshold:
                    # Fast retransmit
                    if self.send_base in self.window:
                        data, _ = self.window[self.send_base]
                        packet = self.create_packet(self.send_base, data)
                        self.sock.sendto(packet, self.client_addr)
                        self.window[self.send_base] = (data, time.time())
                        self.packets_retransmitted += 1
                        self.dup_ack_count[ack_num] = 0
                        print(f"Fast retransmit for seq {self.send_base}")
            
            # Cumulative ACK - advance send_base
            if ack_num > self.send_base:
                # Calculate RTT for acknowledged packets
                if self.send_base in self.window:
                    _, send_time = self.window[self.send_base]
                    sample_rtt = time.time() - send_time
                    self.update_rto(sample_rtt)
                
                # Remove acknowledged packets from window
                old_send_base = self.send_base
                self.send_base = ack_num
                
                # Clear acknowledged packets
                for seq in list(self.window.keys()):
                    if seq < self.send_base:
                        del self.window[seq]
                
                # Reset duplicate ACK count
                self.dup_ack_count.clear()
                
                # Check if transfer is complete (EOF has been acknowledged)
                if self.eof_sent and hasattr(self, 'eof_seq_num'):
                    # EOF is acknowledged if send_base > eof_seq_num + 3 (EOF is 3 bytes)
                    if self.send_base > self.eof_seq_num:
                        self.transfer_complete = True
                        print(f"Transfer complete! EOF acknowledged at seq {self.eof_seq_num}")
                        print(f"Total packets sent: {self.packets_sent}, Retransmitted: {self.packets_retransmitted}")

    def retransmit_timeout_packets(self):
        """Check for timeout and retransmit packets"""
        current_time = time.time()
        with self.lock:
            for seq_num in list(self.window.keys()):
                data, send_time = self.window[seq_num]
                
                # Check if packet has timed out
                if current_time - send_time > self.rto:
                    # Check if this packet is covered by SACK
                    is_sacked = False
                    for sack_start, sack_end in self.sack_blocks:
                        if seq_num >= sack_start and seq_num < sack_end:
                            is_sacked = True
                            break
                    
                    if not is_sacked:
                        # Retransmit packet
                        packet = self.create_packet(seq_num, data)
                        self.sock.sendto(packet, self.client_addr)
                        self.window[seq_num] = (data, current_time)
                        self.packets_retransmitted += 1
                        print(f"Timeout retransmit for seq {seq_num}, RTO={self.rto:.3f}s")

    def receive_thread(self):
        """Thread to receive ACKs from client"""
        while not self.stop_event.is_set():
            try:
                self.sock.settimeout(0.1)
                packet, _ = self.sock.recvfrom(self.MAX_PAYLOAD)
                ack_num, sack_blocks = self.parse_ack(packet)
                
                if ack_num is not None:
                    self.handle_ack(ack_num, sack_blocks)
                    
            except socket.timeout:
                continue
            except Exception as e:
                if not self.stop_event.is_set():
                    print(f"Receive error: {e}")

    def run(self):
        """Main server loop"""
        # Create socket
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind((self.server_ip, self.server_port))
        print(f"Server listening on {self.server_ip}:{self.server_port}")
        print(f"Sender Window Size: {self.sws} bytes")
        
        # Wait for client request
        print("Waiting for client request...")
        request, self.client_addr = self.sock.recvfrom(1)
        print(f"Received request from {self.client_addr}")
        
        # Load file
        try:
            with open('data.txt', 'rb') as f:
                self.file_data = f.read()
                self.file_size = len(self.file_data)
                print(f"Loaded file: {self.file_size} bytes")
        except FileNotFoundError:
            print("Error: data.txt not found")
            self.sock.close()
            return
        
        # Start timer
        self.start_time = time.time()
        
        # Start receive thread
        recv_thread = threading.Thread(target=self.receive_thread)
        recv_thread.daemon = True
        recv_thread.start()
        
        # Main sending loop
        max_wait_after_eof = 10.0  # Maximum time to wait for EOF acknowledgment
        eof_send_time = None
        
        while not self.transfer_complete and not self.stop_event.is_set():
            # Send new packets
            self.send_data_packets()
            
            # Check for timeouts and retransmit
            self.retransmit_timeout_packets()
            
            # If EOF has been sent, track timeout
            if self.eof_sent and eof_send_time is None:
                eof_send_time = time.time()
            
            # Check if we've waited too long for EOF acknowledgment
            if eof_send_time and (time.time() - eof_send_time) > max_wait_after_eof:
                print(f"Timeout waiting for EOF acknowledgment after {max_wait_after_eof} seconds")
                break
            
            # Small sleep to prevent CPU spinning
            time.sleep(0.001)
        
        # Wait a bit for final ACKs
        if self.transfer_complete:
            time.sleep(1.0)  # Wait longer to ensure client processes EOF
        
        # Calculate statistics
        elapsed_time = time.time() - self.start_time
        throughput = (self.file_size / elapsed_time) / 1024 / 1024 if elapsed_time > 0 else 0
        
        print(f"\nTransfer Statistics:")
        print(f"  File size: {self.file_size} bytes")
        print(f"  Transfer time: {elapsed_time:.2f} seconds")
        print(f"  Throughput: {throughput:.2f} MB/s")
        print(f"  Packets sent: {self.packets_sent}")
        print(f"  Packets retransmitted: {self.packets_retransmitted}")
        print(f"  Final RTO: {self.rto:.3f} seconds")
        
        # Cleanup
        self.stop_event.set()
        recv_thread.join(timeout=1)
        self.sock.close()
        print("Server shutdown complete")

def main():
    if len(sys.argv) != 4:
        print("Usage: python3 p1_server.py <SERVER_IP> <SERVER_PORT> <SWS>")
        sys.exit(1)
    
    server_ip = sys.argv[1]
    server_port = int(sys.argv[2])
    sws = int(sys.argv[3])
    
    server = ReliableUDPServer(server_ip, server_port, sws)
    server.run()

if __name__ == "__main__":
    main()