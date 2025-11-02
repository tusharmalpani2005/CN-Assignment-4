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
        
        self.MSS = 1180
        self.HEADER_SIZE = 20
        self.MAX_PAYLOAD = 1200
        
        self.recv_base = 0
        self.recv_buffer = {}
        self.received_data = []
        
        self.sack_blocks = []
        
        self.output_file = None
        self.transfer_complete = False
        self.eof_ack_num = 0
        
        self.packets_received = 0
        self.duplicate_packets = 0
        self.out_of_order_packets = 0
        self.start_time = 0
        
        self.lock = threading.Lock()
        self.stop_event = threading.Event()

    def parse_packet(self, packet):
        if len(packet) < self.HEADER_SIZE:
            return None, None
        
        seq_num = struct.unpack('!I', packet[:4])[0]
        data = packet[self.HEADER_SIZE:]
        
        return seq_num, data

    def create_ack_packet(self, ack_num, sack_blocks):
        header = struct.pack('!I', ack_num)
        
        sack_data = b''
        for i, (start, end) in enumerate(sack_blocks[:2]):
            if i < 2:
                sack_data += struct.pack('!II', start, end)
        
        sack_data = sack_data.ljust(16, b'\x00')
        
        return header + sack_data

    def update_sack_blocks(self):
        self.sack_blocks = []
        
        if not self.recv_buffer:
            return
        
        sorted_seqs = sorted(self.recv_buffer.keys())
        
        sorted_seqs = [seq for seq in sorted_seqs if seq > self.recv_base]
        
        if not sorted_seqs:
            return
        
        current_start = sorted_seqs[0]
        current_end = current_start + len(self.recv_buffer[current_start])
        
        for seq in sorted_seqs[1:]:
            data_len = len(self.recv_buffer[seq])
            
            if seq == current_end:
                current_end = seq + data_len
            else:
                self.sack_blocks.append((current_start, current_end))
                current_start = seq
                current_end = seq + data_len
        
        self.sack_blocks.append((current_start, current_end))
        
        self.sack_blocks = self.sack_blocks[:2]

    def handle_packet(self, seq_num, data):
        with self.lock:
            self.packets_received += 1
            
            if data == b'EOF':
                if seq_num == self.recv_base:
                    self.transfer_complete = True
                    final_ack = seq_num + 3
                    self.eof_ack_num = final_ack
                    ack_packet = self.create_ack_packet(final_ack, [])
                    for _ in range(3):
                        self.sock.sendto(ack_packet, self.server_addr)
                    return True
                else:
                    self.recv_buffer[seq_num] = data
                    self.update_sack_blocks()
                    ack_packet = self.create_ack_packet(self.recv_base, self.sack_blocks)
                    self.sock.sendto(ack_packet, self.server_addr)
                    return False
            
            if seq_num == self.recv_base:
                self.write_data(data)
                self.recv_base += len(data)
                
                while self.recv_base in self.recv_buffer:
                    buffered_data = self.recv_buffer[self.recv_base]
                    
                    if buffered_data == b'EOF':
                        del self.recv_buffer[self.recv_base]
                        self.transfer_complete = True
                        final_ack = self.recv_base + 3
                        self.eof_ack_num = final_ack
                        ack_packet = self.create_ack_packet(final_ack, [])
                        for _ in range(3):
                            self.sock.sendto(ack_packet, self.server_addr)
                        return True
                    
                    self.write_data(buffered_data)
                    del self.recv_buffer[self.recv_base]
                    self.recv_base += len(buffered_data)
                
                self.update_sack_blocks()
                
            elif seq_num < self.recv_base:
                self.duplicate_packets += 1
                
            else:
                if seq_num not in self.recv_buffer:
                    self.recv_buffer[seq_num] = data
                    self.out_of_order_packets += 1
                    self.update_sack_blocks()
                else:
                    self.duplicate_packets += 1
            
            ack_packet = self.create_ack_packet(self.recv_base, self.sack_blocks)
            self.sock.sendto(ack_packet, self.server_addr)
            
            return False

    def write_data(self, data):
        if self.output_file:
            self.output_file.write(data)
            self.output_file.flush()

    def send_request(self):
        max_retries = 5
        timeout = 2.0
        
        for attempt in range(max_retries):
            try:
                self.sock.sendto(b'1', self.server_addr)
                
                self.sock.settimeout(timeout)
                packet, addr = self.sock.recvfrom(self.MAX_PAYLOAD)
                
                self.sock.settimeout(None)
                return packet
                
            except socket.timeout:
                continue
            except Exception:
                if attempt < max_retries - 1:
                    time.sleep(0.5)
                    continue
                else:
                    raise
        
        raise Exception("Failed to connect to server after maximum retries")

    def run(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        
        self.output_file = open('received_data.txt', 'wb')
        
        try:
            first_packet = self.send_request()
            
            self.start_time = time.time()
            
            seq_num, data = self.parse_packet(first_packet)
            if seq_num is not None and data is not None:
                self.handle_packet(seq_num, data)
            
            last_activity = time.time()
            idle_timeout = 5.0
            
            while not self.transfer_complete:
                try:
                    self.sock.settimeout(0.5)
                    packet, addr = self.sock.recvfrom(self.MAX_PAYLOAD)
                    
                    seq_num, data = self.parse_packet(packet)
                    if seq_num is not None and data is not None:
                        is_eof = self.handle_packet(seq_num, data)
                        if is_eof:
                            break
                    
                    last_activity = time.time()
                    
                except socket.timeout:
                    if time.time() - last_activity > idle_timeout:
                        break
                    continue
                    
                except Exception:
                    break
            
            if self.transfer_complete:
                final_ack = self.eof_ack_num if hasattr(self, 'eof_ack_num') else self.recv_base
                
                for i in range(5):
                    ack_packet = self.create_ack_packet(final_ack, [])
                    self.sock.sendto(ack_packet, self.server_addr)
                    time.sleep(0.05)
            
        finally:
            if self.output_file:
                self.output_file.close()
            
            self.sock.close()

def main():
    if len(sys.argv) != 3:
        sys.exit(1)
    
    server_ip = sys.argv[1]
    server_port = int(sys.argv[2])
    
    client = ReliableUDPClient(server_ip, server_port)
    client.run()

if __name__ == "__main__":
    main()