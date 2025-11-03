#!/usr/bin/env python3
"""
Reliable UDP client for assignment — receives data with sequence numbers,
sends ACKs and SACK blocks. This module implements a simple reliable
transfer over UDP (client side).
"""

import socket
import sys
import time
import struct
import threading


class ReliableUDPClient:  # pylint: disable=too-many-instance-attributes
    """
    Client implementing a reliable UDP receiver with basic SACK support.
    """

    def __init__(self, server_ip, server_port):
        """
        Initialize client state.
        """
        self.server_ip = server_ip
        self.server_port = server_port
        self.server_addr = (server_ip, server_port)
        self.sock = None

        # Use snake_case attribute names to satisfy style checks.
        self.mss = 1180
        self.header_size = 20
        self.max_payload = 1200

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
        """
        Parse incoming packet and return (seq_num, data). If packet too small,
        returns (None, None).
        """
        if len(packet) < self.header_size:
            return None, None

        seq_num = struct.unpack('!I', packet[:4])[0]
        data = packet[self.header_size:]

        return seq_num, data

    def create_ack_packet(self, ack_num, sack_blocks):
        """
        Create an ACK packet: 4-byte ACK number followed by up to two SACK blocks
        (each start,end as 4-byte unsigned ints). SACK area is padded to 16 bytes.
        """
        header = struct.pack('!I', ack_num)

        sack_data = b''
        for i, (start, end) in enumerate(sack_blocks[:2]):
            if i < 2:
                sack_data += struct.pack('!II', start, end)

        sack_data = sack_data.ljust(16, b'\x00')

        return header + sack_data

    def update_sack_blocks(self):
        """
        Update self.sack_blocks from the current recv_buffer and recv_base.
        Keeps at most two SACK blocks.
        """
        self.sack_blocks = []

        if not self.recv_buffer:
            return

        sorted_seqs = sorted(self.recv_buffer.keys())

        # only include segments beyond current receive base
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

        # keep only first two blocks
        self.sack_blocks = self.sack_blocks[:2]

    def handle_packet(self, seq_num, data):
        """
        Handle a received packet (store, write in-order data, update stats),
        send appropriate ACKs. Returns True if EOF was processed and transfer
        should terminate; otherwise False.
        """
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

                # EOF arrived out-of-order: buffer it and ack current base
                self.recv_buffer[seq_num] = data
                self.update_sack_blocks()
                ack_packet = self.create_ack_packet(self.recv_base, self.sack_blocks)
                self.sock.sendto(ack_packet, self.server_addr)
                return False

            if seq_num == self.recv_base:
                self.write_data(data)
                self.recv_base += len(data)

                # write any consecutive buffered segments
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
        """
        Write data to output file if open.
        """
        if self.output_file:
            self.output_file.write(data)
            self.output_file.flush()

    def send_request(self):
        """
        Send initial request to server and wait for first packet. Retries a
        few times on timeout or socket errors.
        """
        max_retries = 5
        timeout = 2.0

        for attempt in range(max_retries):
            try:
                self.sock.sendto(b'1', self.server_addr)

                self.sock.settimeout(timeout)
                packet, _ = self.sock.recvfrom(self.max_payload)

                self.sock.settimeout(None)
                return packet

            except socket.timeout:
                # retry on timeout
                continue
            except OSError as exc:
                # socket-level errors: retry a few times then give up
                if attempt < max_retries - 1:
                    time.sleep(0.5)
                    continue
                raise ConnectionError("Failed to connect to server after maximum retries") from exc

        # Fallback: should not normally be reached
        raise ConnectionError("Failed to connect to server after maximum retries")

    def run(self):
        """
        Main receive loop: send request, receive packets, handle them until EOF
        or idle timeout.
        Uses context managers for the socket and output file to satisfy linting.
        """
        # Use context managers so resources are properly closed and pylint is satisfied.
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock, \
            open('received_data.txt', 'wb') as output_file:
            # keep attributes for compatibility with rest of class
            self.sock = sock
            self.output_file = output_file

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
                        packet, _ = self.sock.recvfrom(self.max_payload)

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

                    except OSError:
                        break

                if self.transfer_complete:
                    final_ack = self.eof_ack_num if hasattr(self, 'eof_ack_num') else self.recv_base

                    for _ in range(5):
                        ack_packet = self.create_ack_packet(final_ack, [])
                        self.sock.sendto(ack_packet, self.server_addr)
                        time.sleep(0.05)

            finally:
                # clear references; actual closing is handled by context managers
                self.output_file = None
                self.sock = None


def main():
    """
    Entry point: expects server IP and port as command-line arguments.
    """
    if len(sys.argv) != 3:
        sys.exit(1)

    server_ip = sys.argv[1]
    server_port = int(sys.argv[2])

    client = ReliableUDPClient(server_ip, server_port)
    client.run()


if __name__ == "__main__":
    main()
