import threading
import time
import queue
import ssl
import socket
import select
import hashlib
from collections import defaultdict
from logging import getLogger
log = getLogger('transport')
from . import callback_manager
from . import configuration
from .socket_utils import SERVER_PORT, address_to_hostport, hostport_to_address

PROTOCOL_VERSION: int = 2


class Transport:
	connected: bool
	successful_connects: int

	def __init__(self, serializer):
		self.serializer = serializer
		self.callback_manager = callback_manager.CallbackManager()
		self.connected = False
		self.successful_connects = 0

	def transport_connected(self):
		self.successful_connects += 1
		self.connected = True
		self.callback_manager.call_callbacks('transport_connected')

class TCPTransport(Transport):
	buffer: bytes
	closed: bool
	
	def __init__(self, serializer, address, timeout=0, insecure=False):
		super().__init__(serializer=serializer)
		self.closed = False
		#Buffer to hold partially received data
		self.buffer = B''
		self.queue = queue.Queue()
		self.address = address
		self.server_sock = None
		self.queue_thread = None
		self.timeout = timeout
		self.reconnector_thread = ConnectorThread(self)
		self.insecure=insecure

	def run(self):
		self.closed = False
		try:
			self.server_sock = self.create_outbound_socket(*self.address, insecure=self.insecure)
			self.server_sock.connect(self.address)
		except ssl.SSLCertVerificationError as ex:
			fingerprint=None
			try:
				tmp_con = self.create_outbound_socket(*self.address, insecure = True)
				tmp_con.connect(self.address)
				certBin = tmp_con.getpeercert(True)
				tmp_con.close()
				fingerprint = hashlib.sha256(certBin).hexdigest().lower()
			except Exception: pass
			config = configuration.get_config()
			if hostport_to_address(self.address) in config['trusted_certs'][hostport_to_address(self.address)]==fingerprint:
				self.insecure=True
				return self.run()
			self.last_fail_fingerprint = fingerprint
			self.callback_manager.call_callbacks('certificate_authentication_failed')
			raise
		except Exception:
			self.callback_manager.call_callbacks('transport_connection_failed')
			raise
		self.transport_connected()
		self.queue_thread = threading.Thread(target=self.send_queue)
		self.queue_thread.daemon = True
		self.queue_thread.start()
		while self.server_sock is not None:
			try:
				readers, writers, error = select.select([self.server_sock], [], [self.server_sock])
			except socket.error:
				self.buffer = b''
				break
			if self.server_sock in error:
				self.buffer = b""
				break
			if self.server_sock in readers:
				try:
					self.handle_server_data()
				except socket.error:
					self.buffer = b''
					break
		self.connected = False
		self.callback_manager.call_callbacks('transport_disconnected')
		self._disconnect()

	def create_outbound_socket(self, host, port, insecure=False):
		address = socket.getaddrinfo(host, port)[0]
		server_sock = socket.socket(*address[:3])
		if self.timeout:
			server_sock.settimeout(self.timeout)
		server_sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
		server_sock.ioctl(socket.SIO_KEEPALIVE_VALS, (1, 60000, 2000))
		ctx = (ssl.SSLContext())
		if insecure: ctx.verify_mode = ssl.CERT_NONE
		ctx.check_hostname = not insecure
		ctx.load_default_certs()
		server_sock = ctx.wrap_socket(sock=server_sock, server_hostname=host)
		return server_sock

	def getpeercert(self, binary_form=False):
		if self.server_sock is None: return None
		return self.server_sock.getpeercert(binary_form)

	def handle_server_data(self):
		# This approach may be problematic:
		# See also server.py handle_data in class Client.
		buffSize = 16384
		data = self.buffer + self.server_sock.recv(buffSize)
		self.buffer = b''
		if not data:
			self._disconnect()
			return
		if b'\n' not in data:
			self.buffer += data
			return
		while b'\n' in data:
			line, sep, data = data.partition(b'\n')
			self.parse(line)
		self.buffer += data

	def parse(self, line):
		obj = self.serializer.deserialize(line)
		if 'type' not in obj:
			return
		callback = "msg_"+obj['type']
		del obj['type']
		self.callback_manager.call_callbacks(callback, **obj)

	def send_queue(self):
		while True:
			item = self.queue.get()
			if item is None:
				return
			try:
				self.server_sock.sendall(item)
			except socket.error:
				return

	def send(self, type, **kwargs):
		obj = self.serializer.serialize(type=type, **kwargs)
		if self.connected:
			self.queue.put(obj)

	def _disconnect(self):
		"""Disconnect the transport due to an error, without closing the connector thread."""
		if not self.connected:
			return
		if self.queue_thread is not None:
			self.queue.put(None)
			self.queue_thread.join()
		clear_queue(self.queue)
		self.server_sock.close()
		self.server_sock = None

	def close(self):
		self.callback_manager.call_callbacks('transport_closing')
		self.reconnector_thread.running = False
		self._disconnect()
		self.closed = True
		self.reconnector_thread = ConnectorThread(self)

class RelayTransport(TCPTransport):

	def __init__(self, serializer, address, timeout=0, channel=None, connection_type=None, protocol_version=PROTOCOL_VERSION, insecure=False):
		super().__init__(address=address, serializer=serializer, timeout=timeout, insecure=insecure)
		log.info("Connecting to %s channel %s" % (address, channel))
		self.channel = channel
		self.connection_type = connection_type
		self.protocol_version = protocol_version
		self.callback_manager.register_callback('transport_connected', self.on_connected)

	def on_connected(self):
		self.send('protocol_version', version=self.protocol_version)
		if self.channel is not None:
			self.send('join', channel=self.channel, connection_type=self.connection_type)
		else:
			self.send('generate_key')

class ConnectorThread(threading.Thread):

	def __init__(self, connector, connect_delay=5):
		super().__init__()
		self.connect_delay = connect_delay
		self.running = True
		self.connector = connector
		self.name = self.name + "_connector_loop"
		self.daemon = True

	def run(self):
		while self.running:
			try:
				self.connector.run()
			except socket.error:
				time.sleep(self.connect_delay)
				continue
			else:
				time.sleep(self.connect_delay)
		log.info("Ending control connector thread %s" % self.name)

def clear_queue(queue):
	try:
		while True:
			queue.get_nowait()
	except Exception:
		pass
