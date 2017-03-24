"""
GBXRemote 2 client for python 3.5+ part of PyPlanet.
"""
import asyncio
import json
import logging
import struct
from xmlrpc.client import dumps, loads

from pyplanet.core.exceptions import TransportException
from pyplanet.core.events.manager import SignalManager

logger = logging.getLogger(__name__)


class GbxRemote:
	"""
	The GbxClient holds the connection to the dedicated server. Maintains the queries and the handlers it got.
	"""
	MAX_REQUEST_SIZE  = 2000000 # 2MB
	MAX_RESPONSE_SIZE = 4000000 # 4MB

	def __init__(self, host, port, event_pool=None, user=None, password=None, api_version='2013-04-16', instance=None):
		"""
		Initiate the GbxRemote client.
		:param host: Host of the dedicated server.
		:param port: Port of the dedicated XML-RPC server.
		:param event_pool: Asyncio pool to execute the handling on.
		:param user: User to authenticate with, in most cases this is 'SuperAdmin'
		:param password: Password to authenticate with.
		:param api_version: API Version to use. In most cases you won't override the default because version changes
							should be abstracted by the other core components.
		:param instance: Instance of the app.
		:type host: str
		:type port: str int
		:type event_pool: asyncio.BaseEventPool
		:type user: str
		:type password: str
		:type api_version: str
		:type instance: pyplanet.core.instance.Instance
		"""
		self.host = host
		self.port = port
		self.user = user
		self.password = password
		self.api_version = api_version
		self.instance = instance

		self.event_loop = event_pool or asyncio.get_event_loop()
		self.gbx_methods = list()

		self.handlers = dict()
		self.handler_nr = 0x80000000

		self.script_handlers = dict()

		self.reader = None
		self.writer = None

	@classmethod
	def create_from_settings(cls, instance, conf):
		"""
		Create an instance from configuration given for the specific pool
		:param instance: Instance of the app.
		:param conf: Settings for pool.
		:type conf: dict
		:return: Instance of XML-RPC GbxClient.
		:rtype: pyplanet.core.gbx.client.GbxClient
		"""
		return cls(
			instance=instance,
			host=conf['HOST'], port=conf['PORT'], user=conf['USER'], password=conf['PASSWORD']
		)

	def get_next_handler(self):
		handler = self.handler_nr
		if self.handler_nr == 0xffffffff:
			logger.debug('GBX: Reached max handler numbers, RESETTING TO ZERO!')
			self.handler_nr = 0x80000000
		else:
			self.handler_nr += 1
		return handler

	async def connect(self):
		"""
		Make connection to the server. This will first check the protocol version and after successful connection
		also authenticate, set the API version and enable callbacks.
		"""
		logger.debug('Trying to connect to the dedicated server...')

		# Create socket (produces coroutine).
		self.reader, self.writer = await asyncio.open_connection(
			host=self.host,
			port=self.port,
			loop=self.event_loop,
		)
		_, header = struct.unpack_from('<L11s', await self.reader.readexactly(15))
		if header.decode() != 'GBXRemote 2':
			raise TransportException('Server is not a valid GBXRemote 2 server.')
		logger.debug('Dedicated connection established!')

		# From now we need to start listening.
		self.event_loop.create_task(self.listen())

		# Startup tasks.
		await self.execute('Authenticate', self.user, self.password)
		await asyncio.gather(
			self.execute('SetApiVersion', self.api_version),
			self.execute('EnableCallbacks', True),
		)

		# Fetch gbx methods.
		self.gbx_methods = await self.execute('system.listMethods')

		# Check for scripted mode.
		mode = await self.execute('GetGameMode')
		settings = await self.execute('GetModeScriptSettings')
		if mode == 0:
			if 'S_UseScriptCallbacks' in settings:
				settings['S_UseScriptCallbacks'] = True
			if 'S_UseLegacyCallback' in settings:
				settings['S_UseLegacyCallback'] = False
			if 'S_UseLegacyXmlRpcCallbacks' in settings:
				settings['S_UseLegacyXmlRpcCallbacks'] = False
			await asyncio.gather(
				self.execute('SetModeScriptSettings', settings),
				self.execute('TriggerModeScriptEventArray', 'XmlRpc.EnableCallbacks', ['true'])
			)

		logger.debug('Dedicated authenticated, API version set and callbacks enabled!')

	async def execute(self, method, *args):
		"""
		Query the dedicated server and return the results. This method is a coroutine and should be awaited on.
		The result you get will be a tuple with data inside (the response payload).

		:param method: Server method.
		:param args: Arguments.
		:type method: str
		:type args: any
		:return: Tuple with response data (after awaiting).
		:rtype: Future<tuple>
		"""
		request_bytes = dumps(args, methodname=method, allow_none=True).encode()
		length_bytes = len(request_bytes).to_bytes(4, byteorder='little')
		handler = self.get_next_handler()

		handler_bytes = handler.to_bytes(4, byteorder='little')

		# Create new future to be returned.
		self.handlers[handler] = future = asyncio.Future()

		# Send to server.
		self.writer.write(length_bytes + handler_bytes + request_bytes)

		return await asyncio.wait_for(future, 30.0) # Wait for maximum of 30 seconds, then force complete future.

	async def listen(self):
		"""
		Listen to socket.
		"""
		while True:
			head = await self.reader.readexactly(8)
			size, handle = struct.unpack_from('<LL', head)
			body = await self.reader.readexactly(size)
			data, method = loads(body, use_builtin_types=True)

			if len(data) == 1:
				data = data[0]

			self.event_loop.create_task(self.handle_payload(handle, method, data))

	async def handle_payload(self, handle_nr, method, data):
		"""
		Handle a callback/response payload.
		:param handle_nr: Handler ID
		:param method: Method name
		:param data: Parsed payload data.
		"""
		if handle_nr in self.handlers:
			await self.handle_response(handle_nr, data)
		else:
			if method == 'ManiaPlanet.ModeScriptCallbackArray':
				await self.handle_scripted(handle_nr, method, data)
			else:
				await self.handle_callback(handle_nr, method, data)

	async def handle_response(self, handle_nr, data):
		logger.debug('GBX: Received response to handler {}'.format(handle_nr))
		handler = self.handlers.pop(handle_nr)
		handler.set_result(data)
		handler.done()

	async def handle_callback(self, handle_nr, method, data):
		logger.debug('GBX: Received callback: {}: {}'.format(method, data))
		signal = SignalManager.get_callback(method)
		if signal:
			await signal.send_robust(data)

	async def handle_scripted(self, handle_nr, method, data):
		# Unpack first.
		method, raw = data

		# Check if we only have one response array length, mostly this is the case due to the gbx handling.
		# Only when we don't get any response we don't have this!
		if len(raw) == 1:
			raw = raw[0]

		# Try to parse JSON, mostly the case.
		try:
			payload = json.loads(raw)
		except Exception as e:
			logger.debug('GBX: JSON Parsing of script callback failed! {}'.format(str(e)))
			payload = raw

		# Check if payload contains a responseid, when it does, we call the scripted handler future object.
		if type(payload) is dict and 'responseid' in payload:
			try:
				# Try to parse responseid to integer, can maybe fail due to script incompatibility.
				response_id = int(payload['responseid'])
			except:
				logger.warning('GBX: Can\'t parse responseid in script callback into integer!')
				return

			if response_id in self.script_handlers:
				logger.debug('GBX: Received scripted response to method: {} and responseid: {}'.format(method, response_id))
				handler = self.script_handlers.pop(response_id)
				handler.set_result(payload)
				handler.done()
				return
			else:
				# We don't have this handler registered, throw warning in console.
				logger.warning('GBX: Received scripted resopnse with responseid, but no hander was registered! Payload: {}'.format(payload))
				return

		# If not, we should just throw it as an ordinary callback.
		logger.debug('GBX: Received scripted callback: {}: {}'.format(method, payload))

		signal = SignalManager.get_callback('Script.{}'.format(method))
		if signal:
			await signal.send_robust(payload)
