#!/usr/bin/env python3

import socket
import sys
import time
import struct
import threading
from collections import defaultdict

class ReliableUDPServer:
    def __init__(self, server_ip, server_port, sws):
        self.server_ip = server_ip
        self.server_port = server_port
        self.sws = sws
        self.sock = None
        self.client_addr = None
        
        self.MSS = 1180
        self.HEADER_SIZE = 20
        self.MAX_PAYLOAD = 1200
        
        self.send_base = 0
        self.next_seq_num = 0
        self.window = {}
        self.sack_blocks = []
        
        self.estimated_rtt = 0.08
        self.dev_rtt = 0.04
        self.rto = 0.24
        self.alpha = 0.125
        self.beta = 0.25
        
        self.rtt_samples = []
        
        self.dup_ack_count = defaultdict(int)
        self.fast_retransmit_threshold = 3
        self.last_dup_ack = None
        
        self.sacked_packets = set()
        
        self.file_data = b''
        self.file_size = 0
        self.eof_sent = False
        self.eof_seq_num = 0
        self.transfer_complete = False
        
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        
        self.packets_sent = 0
        self.packets_retransmitted = 0
        self.fast_retransmits = 0
        self.timeout_retransmits = 0
        self.sack_retransmits = 0
        self.start_time = 0

    def create_packet(self, seq_num, data):
        header = struct.pack('!I', seq_num) + b'\x00' * 16
        return header + data

    def parse_ack(self, packet):
        if len(packet) < 4:
            return None, []
        
        ack_num = struct.unpack('!I', packet[:4])[0]
        sack_blocks = []
        
        if len(packet) >= 20:
            sack_data = packet[4:20]
            
            for i in range(0, 16, 8):
                if i + 8 <= 16:
                    try:
                        sack_start = struct.unpack('!I', sack_data[i:i+4])[0]
                        sack_end = struct.unpack('!I', sack_data[i+4:i+8])[0]
                        
                        if sack_start > 0 and sack_end > sack_start and sack_start >= ack_num:
                            sack_blocks.append((sack_start, sack_end))
                    except:
                        break
        
        return ack_num, sack_blocks

    def update_sacked_packets(self):
        self.sacked_packets.clear()
        
        for sack_start, sack_end in self.sack_blocks:
            for seq_num in list(self.window.keys()):
                data, _ = self.window[seq_num]
                packet_end = seq_num + len(data)
                
                if seq_num >= sack_start and packet_end <= sack_end:
                    self.sacked_packets.add(seq_num)

    def update_rto(self, sample_rtt):
        self.rtt_samples.append(sample_rtt)
        
        if self.estimated_rtt == 0:
            self.estimated_rtt = sample_rtt
            self.dev_rtt = sample_rtt / 2
        else:
            self.estimated_rtt = (1 - self.alpha) * self.estimated_rtt + self.alpha * sample_rtt
            self.dev_rtt = (1 - self.beta) * self.dev_rtt + self.beta * abs(sample_rtt - self.estimated_rtt)
        
        self.rto = self.estimated_rtt + 4 * self.dev_rtt
        self.rto = max(0.11, min(2.0, self.rto))

    def send_data_packets(self):
        with self.lock:
            bytes_in_flight = self.next_seq_num - self.send_base
            available_window = self.sws - bytes_in_flight
            
            if available_window <= 0 and not (self.next_seq_num >= self.file_size and not self.eof_sent):
                return
            
            while available_window > 0 and self.next_seq_num < self.file_size:
                remaining_file = self.file_size - self.next_seq_num
                packet_size = min(self.MSS, remaining_file, available_window)
                
                data = self.file_data[self.next_seq_num:self.next_seq_num + packet_size]
                packet = self.create_packet(self.next_seq_num, data)
                self.sock.sendto(packet, self.client_addr)
                
                self.window[self.next_seq_num] = (data, time.time())
                
                self.next_seq_num += packet_size
                available_window -= packet_size
                self.packets_sent += 1
                
            if self.next_seq_num >= self.file_size and not self.eof_sent:
                eof_seq = self.file_size
                eof_packet = self.create_packet(eof_seq, b'EOF')
                self.sock.sendto(eof_packet, self.client_addr)
                self.window[eof_seq] = (b'EOF', time.time())
                self.eof_sent = True
                self.eof_seq_num = eof_seq
                self.packets_sent += 1

    def handle_ack(self, ack_num, sack_blocks):
        with self.lock:
            self.sack_blocks = sack_blocks
            
            if sack_blocks:
                self.update_sacked_packets()
            
            if ack_num == self.send_base:
                self.dup_ack_count[ack_num] += 1
                
                if self.dup_ack_count[ack_num] == self.fast_retransmit_threshold:
                    if self.send_base in self.window and self.send_base not in self.sacked_packets:
                        data, _ = self.window[self.send_base]
                        packet = self.create_packet(self.send_base, data)
                        self.sock.sendto(packet, self.client_addr)
                        self.window[self.send_base] = (data, time.time())
                        self.packets_retransmitted += 1
                        self.fast_retransmits += 1
                
                if sack_blocks and self.dup_ack_count[ack_num] >= self.fast_retransmit_threshold:
                    self.selective_retransmit(skip_send_base=True)
            
            if ack_num > self.send_base:
                if self.send_base in self.window:
                    _, send_time = self.window[self.send_base]
                    sample_rtt = time.time() - send_time
                    self.update_rto(sample_rtt)
                
                old_send_base = self.send_base
                self.send_base = ack_num
                
                for seq in list(self.window.keys()):
                    if seq < self.send_base:
                        del self.window[seq]
                        self.sacked_packets.discard(seq)
                
                self.dup_ack_count.clear()
                
                if self.eof_sent and hasattr(self, 'eof_seq_num'):
                    if self.send_base > self.eof_seq_num:
                        self.transfer_complete = True

    def selective_retransmit(self, skip_send_base=False):
        if not self.sack_blocks or not self.window:
            return
        
        sorted_sacks = sorted(self.sack_blocks, key=lambda x: x[0])
        
        first_sack_start = sorted_sacks[0][0]
        
        seqs_to_retransmit = []
        
        for seq_num in sorted(self.window.keys()):
            if seq_num >= self.send_base and seq_num < first_sack_start:
                if seq_num not in self.sacked_packets:
                    if skip_send_base and seq_num == self.send_base:
                        continue
                    
                    data, send_time = self.window[seq_num]
                    if time.time() - send_time > self.rto / 2:
                        seqs_to_retransmit.append(seq_num)
        
        for i in range(len(sorted_sacks) - 1):
            hole_start = sorted_sacks[i][1]
            hole_end = sorted_sacks[i + 1][0]
            
            for seq_num in sorted(self.window.keys()):
                if seq_num >= hole_start and seq_num < hole_end:
                    if seq_num not in self.sacked_packets:
                        data, send_time = self.window[seq_num]
                        if time.time() - send_time > self.rto / 2:
                            if seq_num not in seqs_to_retransmit:
                                seqs_to_retransmit.append(seq_num)
        
        for seq_num in sorted(seqs_to_retransmit)[:3]:
            if seq_num in self.window:
                data, _ = self.window[seq_num]
                packet = self.create_packet(seq_num, data)
                self.sock.sendto(packet, self.client_addr)
                self.window[seq_num] = (data, time.time())
                self.packets_retransmitted += 1
                self.sack_retransmits += 1

    def retransmit_timeout_packets(self):
        current_time = time.time()
        with self.lock:
            for seq_num in list(self.window.keys()):
                data, send_time = self.window[seq_num]
                
                if current_time - send_time > self.rto:
                    if seq_num in self.sacked_packets:
                        continue
                    
                    packet = self.create_packet(seq_num, data)
                    self.sock.sendto(packet, self.client_addr)
                    self.window[seq_num] = (data, current_time)
                    self.packets_retransmitted += 1
                    self.timeout_retransmits += 1

    def receive_thread(self):
        while not self.stop_event.is_set():
            try:
                self.sock.settimeout(0.1)
                packet, _ = self.sock.recvfrom(self.MAX_PAYLOAD)
                ack_num, sack_blocks = self.parse_ack(packet)
                
                if ack_num is not None:
                    self.handle_ack(ack_num, sack_blocks)
                    
            except socket.timeout:
                continue
            except Exception:
                if not self.stop_event.is_set():
                    pass

    def run(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind((self.server_ip, self.server_port))
        
        request, self.client_addr = self.sock.recvfrom(1)
        
        try:
            with open('data.txt', 'rb') as f:
                self.file_data = f.read()
                self.file_size = len(self.file_data)
        except FileNotFoundError:
            self.sock.close()
            return
        
        self.start_time = time.time()
        
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
            
            time.sleep(0.001)
        
        if self.transfer_complete:
            time.sleep(0.5)
        
        self.stop_event.set()
        recv_thread.join(timeout=1)
        self.sock.close()

def main():
    if len(sys.argv) != 4:
        sys.exit(1)
    
    server_ip = sys.argv[1]
    server_port = int(sys.argv[2])
    sws = int(sys.argv[3])
    
    server = ReliableUDPServer(server_ip, server_port, sws)
    server.run()

if __name__ == "__main__":
    main()