#!/usr/bin/env python3

import socket
import sys
import time
import struct
import threading
from collections import defaultdict

class ReliableUDPClient:
    def __init__(self, server_ip, server_port):
        self.server_ip = server_ip
        self.server_port = server_port
        self.server_addr = (server_ip, server_port)
        self.sock = None
        
        # Packet parameters
        self.MSS = 1180  # Maximum Segment Size
        self.HEADER_SIZE = 20
        self.MAX_PAYLOAD = 1200
        
        # Receive buffer management
        self.recv_base = 0  # Next expected sequence number
        self.recv_buffer = {}  # Out-of-order packets: seq_num -> data
        self.received_data = []  # In-order data segments
        
        # SACK management
        self.sack_blocks = []  # List of (start, end) tuples for received blocks
        
        # File handling
        self.output_file = None
        self.transfer_complete = False
        self.eof_ack_num = 0
        
        # Statistics
        self.packets_received = 0
        self.duplicate_packets = 0
        self.out_of_order_packets = 0
        self.start_time = 0
        
        # Threading
        self.lock = threading.Lock()
        self.stop_event = threading.Event()

    def parse_packet(self, packet):
        """Parse received packet to extract sequence number and data"""
        if len(packet) < self.HEADER_SIZE:
            return None, None
        
        # Extract sequence number from first 4 bytes
        seq_num = struct.unpack('!I', packet[:4])[0]
        
        # Extract data (skip 20-byte header)
        data = packet[self.HEADER_SIZE:]
        
        return seq_num, data

    def create_ack_packet(self, ack_num, sack_blocks):
        """Create ACK packet with cumulative ACK and SACK blocks"""
        # Create header with ACK number
        header = struct.pack('!I', ack_num)
        
        # Add SACK blocks in the reserved 16 bytes
        sack_data = b''
        for i, (start, end) in enumerate(sack_blocks[:2]):  # Max 2 SACK blocks
            if i < 2:  # We have space for 2 SACK blocks
                sack_data += struct.pack('!II', start, end)
        
        # Pad to 16 bytes
        sack_data = sack_data.ljust(16, b'\x00')
        
        return header + sack_data

    def update_sack_blocks(self):
        """Update SACK blocks based on received out-of-order packets"""
        self.sack_blocks = []
        
        if not self.recv_buffer:
            return
        
        # Sort sequence numbers in buffer
        sorted_seqs = sorted(self.recv_buffer.keys())
        
        # Create SACK blocks for contiguous ranges
        current_start = sorted_seqs[0]
        current_end = current_start + len(self.recv_buffer[current_start])
        
        for seq in sorted_seqs[1:]:
            data_len = len(self.recv_buffer[seq])
            
            if seq == current_end:
                # Contiguous with current block
                current_end = seq + data_len
            else:
                # Gap found, save current block and start new one
                self.sack_blocks.append((current_start, current_end))
                current_start = seq
                current_end = seq + data_len
        
        # Add the last block
        self.sack_blocks.append((current_start, current_end))
        
        # Keep only the most recent SACK blocks
        self.sack_blocks = self.sack_blocks[:2]

    def handle_packet(self, seq_num, data):
        """Process received packet and update receive window"""
        with self.lock:
            self.packets_received += 1
            
            # Check for EOF
            if data == b'EOF':
                print(f"Received EOF signal at seq {seq_num}")
                # Check if we've received all data before EOF
                if seq_num == self.recv_base:
                    # All data received, acknowledge EOF
                    self.transfer_complete = True
                    # Send ACK for EOF (advance by 3 bytes for "EOF")
                    final_ack = seq_num + 3
                    self.eof_ack_num = final_ack  # Store for later use
                    ack_packet = self.create_ack_packet(final_ack, [])
                    # Send multiple ACKs to ensure delivery
                    for _ in range(3):
                        self.sock.sendto(ack_packet, self.server_addr)
                    print(f"Sent final ACK for EOF: {final_ack}")
                    return True
                else:
                    # We're missing data before EOF, treat EOF as out-of-order
                    print(f"EOF received but missing data. Expected: {self.recv_base}, EOF at: {seq_num}")
                    # Store EOF for later processing
                    self.recv_buffer[seq_num] = data
                    self.update_sack_blocks()
                    # Continue normal ACK
                    ack_packet = self.create_ack_packet(self.recv_base, self.sack_blocks)
                    self.sock.sendto(ack_packet, self.server_addr)
                    return False
            
            # Check if packet is expected
            if seq_num == self.recv_base:
                # In-order packet
                self.write_data(data)
                self.recv_base += len(data)
                
                # Check if we can deliver buffered packets
                while self.recv_base in self.recv_buffer:
                    buffered_data = self.recv_buffer[self.recv_base]
                    
                    # Check if this is the EOF marker
                    if buffered_data == b'EOF':
                        print(f"Found buffered EOF at seq {self.recv_base}")
                        del self.recv_buffer[self.recv_base]
                        self.transfer_complete = True
                        # Send ACK for EOF
                        final_ack = self.recv_base + 3
                        self.eof_ack_num = final_ack  # Store for later use
                        ack_packet = self.create_ack_packet(final_ack, [])
                        for _ in range(3):
                            self.sock.sendto(ack_packet, self.server_addr)
                        print(f"Sent final ACK for buffered EOF: {final_ack}")
                        return True
                    
                    self.write_data(buffered_data)
                    del self.recv_buffer[self.recv_base]
                    self.recv_base += len(buffered_data)
                
                # Update SACK blocks after processing
                self.update_sack_blocks()
                
            elif seq_num < self.recv_base:
                # Duplicate packet (already received)
                self.duplicate_packets += 1
                
            else:
                # Out-of-order packet (future packet)
                if seq_num not in self.recv_buffer:
                    self.recv_buffer[seq_num] = data
                    self.out_of_order_packets += 1
                    self.update_sack_blocks()
                else:
                    self.duplicate_packets += 1
            
            # Send ACK with SACK blocks
            ack_packet = self.create_ack_packet(self.recv_base, self.sack_blocks)
            self.sock.sendto(ack_packet, self.server_addr)
            
            return False

    def write_data(self, data):
        """Write data to output file"""
        if self.output_file:
            self.output_file.write(data)
            self.output_file.flush()

    def send_request(self):
        """Send initial file request to server with retries"""
        max_retries = 5
        timeout = 2.0
        
        for attempt in range(max_retries):
            try:
                print(f"Sending request to server (attempt {attempt + 1}/{max_retries})")
                self.sock.sendto(b'1', self.server_addr)
                
                # Wait for first data packet
                self.sock.settimeout(timeout)
                packet, addr = self.sock.recvfrom(self.MAX_PAYLOAD)
                
                # Successfully received first packet
                print("Connected to server, starting file transfer...")
                self.sock.settimeout(None)  # Remove timeout for data transfer
                return packet
                
            except socket.timeout:
                print(f"Request timeout, retrying...")
                continue
            except Exception as e:
                print(f"Request error: {e}")
                if attempt < max_retries - 1:
                    time.sleep(0.5)
                    continue
                else:
                    raise
        
        raise Exception("Failed to connect to server after maximum retries")

    def run(self):
        """Main client loop"""
        # Create socket
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        
        # Open output file
        self.output_file = open('received_data.txt', 'wb')
        
        print(f"Connecting to server at {self.server_ip}:{self.server_port}")
        
        try:
            # Send initial request and get first packet
            first_packet = self.send_request()
            
            # Start timer
            self.start_time = time.time()
            
            # Process first packet
            seq_num, data = self.parse_packet(first_packet)
            if seq_num is not None and data is not None:
                self.handle_packet(seq_num, data)
            
            # Receive loop
            last_activity = time.time()
            idle_timeout = 5.0  # Timeout after 5 seconds of no activity
            
            while not self.transfer_complete:
                try:
                    # Set socket timeout for idle detection
                    self.sock.settimeout(0.5)
                    packet, addr = self.sock.recvfrom(self.MAX_PAYLOAD)
                    
                    # Parse and handle packet
                    seq_num, data = self.parse_packet(packet)
                    if seq_num is not None and data is not None:
                        is_eof = self.handle_packet(seq_num, data)
                        if is_eof:
                            break
                    
                    last_activity = time.time()
                    
                except socket.timeout:
                    # Check for idle timeout
                    if time.time() - last_activity > idle_timeout:
                        print("Transfer timeout - no data received")
                        break
                    continue
                    
                except Exception as e:
                    print(f"Receive error: {e}")
                    break
            
            # Send a few more ACKs to ensure server gets them
            if self.transfer_complete:
                print("Sending final ACK burst to confirm EOF...")
                final_ack = self.recv_base
                if hasattr(self, 'eof_ack_num'):
                    final_ack = self.eof_ack_num
                
                for i in range(5):
                    ack_packet = self.create_ack_packet(final_ack, [])
                    self.sock.sendto(ack_packet, self.server_addr)
                    time.sleep(0.05)
                print(f"Sent 5 final ACKs for sequence {final_ack}")
            
        finally:
            # Calculate statistics
            elapsed_time = time.time() - self.start_time if self.start_time > 0 else 0
            file_size = self.output_file.tell() if self.output_file else 0
            throughput = (file_size / elapsed_time) / 1024 / 1024 if elapsed_time > 0 else 0
            
            print(f"\nTransfer Statistics:")
            print(f"  File size received: {file_size} bytes")
            print(f"  Transfer time: {elapsed_time:.2f} seconds")
            print(f"  Throughput: {throughput:.2f} MB/s")
            print(f"  Packets received: {self.packets_received}")
            print(f"  Duplicate packets: {self.duplicate_packets}")
            print(f"  Out-of-order packets: {self.out_of_order_packets}")
            print(f"  Final recv_base: {self.recv_base}")
            
            # Cleanup
            if self.output_file:
                self.output_file.close()
                if self.transfer_complete:
                    print("File transfer completed successfully!")
                else:
                    print("File transfer incomplete")
            
            self.sock.close()
            print("Client shutdown complete")

def main():
    if len(sys.argv) != 3:
        print("Usage: python3 p1_client.py <SERVER_IP> <SERVER_PORT>")
        sys.exit(1)
    
    server_ip = sys.argv[1]
    server_port = int(sys.argv[2])
    
    client = ReliableUDPClient(server_ip, server_port)
    client.run()

if __name__ == "__main__":
    main()