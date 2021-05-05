#!/usr/bin/env python

from __future__ import absolute_import, print_function
import glob
import wave
import random
import struct
import datetime
import io
import logging
import os
import sys
import time
from logging import debug, info
import uuid
import cgi
import audioop
import requests
import tornado.ioloop
import tornado.websocket
import tornado.httpserver
import tornado.template
import tornado.web
import webrtcvad
from tornado.web import url
import json
from base64 import b64decode
import nexmo
import collections
import pickle
import librosa
import numpy as np
from sklearn.externals import joblib


# Only used for record function

logging.captureWarnings(True)

# Constants:
MS_PER_FRAME = 20  # Duration of a frame in ms
RATE = 16000
SILENCE = 10 # How many continuous frames of silence determine the end of a phrase
CLIP_MIN_MS = 200  # ms - the minimum audio clip that will be used
MAX_LENGTH = 3000  # Max length of a sound clip for processing in ms
VAD_SENSITIVITY = 3
CLIP_MIN_FRAMES = CLIP_MIN_MS // MS_PER_FRAME

# Global variables
conns = {}
conversation_uuids = collections.defaultdict(list)
nexmo_client = None
model = None

from dotenv import load_dotenv
load_dotenv()

# Environment Variables, these are set in .env locally
PORT = os.getenv("PORT")
MY_LVN = os.getenv("MY_LVN")
APP_ID = os.getenv("APP_ID")
ANSWERING_MACHINE_TEXT = os.getenv("ANSWERING_MACHINE_TEXT")

def _get_private_key():
	try:
		return os.getenv("PRIVATE_KEY")
	except:
		with open('private.key', 'r') as f:
			private_key = f.read()

	return private_key

PRIVATE_KEY = _get_private_key()
print(PRIVATE_KEY)
if not PRIVATE_KEY:
	with open('private.key', 'r') as f:
			PRIVATE_KEY = f.read()
print(PRIVATE_KEY)
class BufferedPipe(object):
	def __init__(self, max_frames, sink):
		"""
		Create a buffer which will call the provided `sink` when full.

		It will call `sink` with the number of frames and the accumulated bytes when it reaches
		`max_buffer_size` frames.
		"""
		self.sink = sink
		self.max_frames = max_frames

		self.count = 0
		self.payload = b''

	def append(self, data, id):
		""" Add another data to the buffer. `data` should be a `bytes` object. """

		self.count += 1
		self.payload += data

		if self.count == self.max_frames:
			self.process(id)

	def process(self, id):
		""" Process and clear the buffer. """
		self.sink(self.count, self.payload, id)
		self.count = 0
		self.payload = b''

class NexmoClient(object):
	def __init__(self):
		self.client = nexmo.Client(application_id=APP_ID, private_key=PRIVATE_KEY)

	def hangup(self,conversation_uuid):
		for event in conversation_uuids[conversation_uuid]:
			try:
				response = self.client.update_call(event["uuid"], action='hangup')
				debug("hangup uuid {} response: {}".format(event["conversation_uuid"], response))
			except Exception as e:
				debug("Hangup error",e)
		conversation_uuids[conversation_uuid].clear()

	def speak(self, conversation_uuid):
		uuids = [event["uuid"] for event in conversation_uuids[conversation_uuid] if event["from"] == MY_LVN and "ws" not in event["to"]]
		uuid = next(iter(uuids), None)
		debug(uuid)
		if uuid is not None:
			debug('found {}'.format(uuid))
			response = self.client.send_speech(uuid, text=ANSWERING_MACHINE_TEXT)
			debug("send_speech response",response)
		else:
			debug("{} does not exist in list {}".format(conversation_uuid, conversation_uuids[conversation_uuid]))

class AudioProcessor(object):
	def __init__(self, path, conversation_uuid):
		self._path = path
		self.conversation_uuid = conversation_uuid

	def process(self, count, payload, conversation_uuid):
		if count > CLIP_MIN_FRAMES :  # If the buffer is less than CLIP_MIN_MS, ignore it
			debug("record clip")
			fn = "rec-{}-{}.wav".format(conversation_uuid,datetime.datetime.now().strftime("%Y%m%dT%H%M%S"))
			output = wave.open(fn, 'wb')
			output.setparams(
				(1, 2, RATE, 0, 'NONE', 'not compressed'))
			output.writeframes(payload)
			output.close()
			prediction = model.predict_from_file(fn)
			info("prediction {}".format(prediction))

			self.remove_file(fn)

			if prediction == 0 or prediction == 1:
				info("** beep detected **")
				nexmo_client.speak(conversation_uuid)
		else:
			info('Discarding {} frames'.format(str(count)))

	def remove_file(self, wav_file):
		os.remove(wav_file)

class MLModel(object):
	def __init__(self):
		self.model = joblib.load(open("models/xgb.pkl","rb"))
# 		open('gradesdict.p', 'rb')
# 		self.model = pickle.load(open("models/GaussianProcessClassifier-20190807T1859.pkl", "rb"),protocol=2)
		info(self.model)

	def predict_from_file(self, wav_file, verbose=False):
		X, sample_rate = librosa.load(wav_file, res_type='kaiser_fast')
		mfccs_40 = np.mean(librosa.feature.mfcc(y=X, sr=sample_rate, n_mfcc=40).T,axis=0)
		prediction = self.model.predict([mfccs_40])
		info("GaussianProcessClassifier prediction {}".format(prediction))
		return prediction[0]

class WSHandler(tornado.websocket.WebSocketHandler):
	def initialize(self):
		# Create a buffer which will call `process` when it is full:
		self.frame_buffer = None
		# Setup the Voice Activity Detector
		self.tick = None
		self.id = None
		self.vad = webrtcvad.Vad()

		# Level of sensitivity
		self.vad.set_mode(VAD_SENSITIVITY)
		self.processor = None
		self.path = None

	def open(self, path):
		info("client connected")
		debug(self.request.uri)
		self.path = self.request.uri
		self.tick = 0

	def on_message(self, message):
		# Check if message is Binary or Text
		if type(message) != str:
			if self.vad.is_speech(message, RATE):
				debug("SPEECH from {}".format(self.id))
				self.tick = SILENCE
				self.frame_buffer.append(message, self.id)
			else:
				debug("Silence from {} TICK: {}".format(self.id, self.tick))
				self.tick -= 1
				if self.tick == 0:
					# Force processing and clearing of the buffer
					self.frame_buffer.process(self.id)
		else:
			info(message)
			# Here we should be extracting the meta data that was sent and attaching it to the connection object
			data = json.loads(message)

			if data.get('content-type'):
				conversation_uuid = data.get('conversation_uuid') #change to use
				self.id = conversation_uuid
				conns[self.id] = self
				self.processor = AudioProcessor(
					self.path, conversation_uuid).process
				self.frame_buffer = BufferedPipe(MAX_LENGTH // MS_PER_FRAME, self.processor)
				self.write_message('ok')

	def on_close(self):
		# Remove the connection from the list of connections
		del conns[self.id]
		info("client disconnected")

class EventHandler(tornado.web.RequestHandler):
	@tornado.web.asynchronous
	def post(self):
		data = json.loads(self.request.body)
		if data["status"] == "answered":
			debug(data)

			conversation_uuid = data["conversation_uuid"]
			conversation_uuids[conversation_uuid].append(data)

		if data["to"] == MY_LVN and data["status"] == "completed":
			conversation_uuid = data["conversation_uuid"]
			nexmo_client.hangup(conversation_uuid)
		self.content_type = 'text/plain'
		self.write('ok')
		self.finish()

class EnterPhoneNumberHandler(tornado.web.RequestHandler):
	@tornado.web.asynchronous
	def get(self):
		debug(self.request)
		ncco =[
			{
			"action": "talk",
			"text": "Please enter a phone number to dial"
			},
			{
			"action": "input",
			"eventUrl": [self.request.protocol +"://" + self.request.host +"/ivr"],
			"timeOut":10,
			"maxDigits":12,
			"submitOnHash":True
			}
		]

		self.write(json.dumps(ncco))
		self.set_header("Content-Type", 'application/json; charset="utf-8"')
		self.finish()


class AcceptNumberHandler(tornado.web.RequestHandler):
	@tornado.web.asynchronous
	def post(self):
		data = json.loads(self.request.body)
		debug(data)
		ncco = [
			  {
				"action": "talk",
				"text": "Thanks. Connecting you now"
			  },
			 {
			 "action": "connect",
			  # "eventUrl": [self.request.protocol +"://" + self.request.host  + "/event"],
			   "from": MY_LVN,
			   "endpoint": [
				 {
				   "type": "phone",
				   "number": data["dtmf"]
				 }
			   ]
			 },
			  {
				 "action": "connect",
				 # "eventUrl": [self.request.protocol +"://" + self.request.host  +"/event"],
				 "from": MY_LVN,
				 "endpoint": [
					 {
						"type": "websocket",
						"uri" : "ws://"+self.request.host +"/socket",
						"content-type": "audio/l16;rate=16000",
						"headers": {
							"conversation_uuid":data["conversation_uuid"] #change to user
						}
					 }
				 ]
			   }
			]
		self.write(json.dumps(ncco))
		self.set_header("Content-Type", 'application/json; charset="utf-8"')
		self.finish()

class PingHandler(tornado.web.RequestHandler):
	@tornado.web.asynchronous
	def get(self):
		self.write('ok')
		self.set_header("Content-Type", 'text/plain')
		self.finish()
def main():
	try:
		global nexmo_client, model
		nexmo_client = NexmoClient()
		model = MLModel()

		logging.getLogger().setLevel(logging.INFO)

		application = tornado.web.Application([
			url(r"/ping", PingHandler),
			(r"/event", EventHandler),
			(r"/ncco", EnterPhoneNumberHandler),
			(r"/ivr", AcceptNumberHandler),
			url(r"/(.*)", WSHandler),
		])
		http_server = tornado.httpserver.HTTPServer(application)
		port = int(os.getenv('PORT', 8000))
		http_server.listen(port)
		tornado.ioloop.IOLoop.instance().start()
	except KeyboardInterrupt:
		pass  # Suppress the stack-trace on quit


if __name__ == "__main__":
	main()
