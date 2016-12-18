#!/usr/bin/env python2
# coding: utf-8

# vk4xmpp gateway, v3.0
# © simpleApps, 2013 — 2015.
# Program published under the MIT license.

__author__ = "mrDoctorWho <mrdoctorwho@gmail.com>"
__license__ = "MIT"
__version__ = "3.0"

import hashlib
import httplib
import logging
import os
import re
import signal
import socket
import sys
import threading
import time
from argparse import ArgumentParser

try:
	import ujson as json
except ImportError:
	import json

core = getattr(sys.modules["__main__"], "__file__", None)
root = "."
if core:
	core = os.path.abspath(core)
	root = os.path.dirname(core)
	if root:
		os.chdir(root)

sys.path.insert(0, "library")
sys.path.insert(1, "modules")
reload(sys).setdefaultencoding("utf-8")

# Now we can import our own modules
import xmpp
from itypes import Database
from stext import setVars, _
from defaults import *
from printer import *
from webtools import *

Transport = {}
Semaphore = threading.Semaphore()

# command line arguments
argParser = ArgumentParser()
argParser.add_argument("-c", "--config",
	help="set the general config file destination", default="Config.txt")
argParser.add_argument("-d", "--daemon",
	help="run in daemon mode (no auto-restart)", action="store_true")
args = argParser.parse_args()
Daemon = args.daemon
Config = args.config

startTime = int(time.time())

execfile(Config)
Print("#-# Config loaded successfully.")

# logger
logger = logging.getLogger("vk4xmpp")
logger.setLevel(LOG_LEVEL)
loggerHandler = logging.FileHandler(logFile)
formatter = logging.Formatter("%(asctime)s %(levelname)s:"
	"%(name)s: %(message)s", "%d.%m.%Y %H:%M:%S")
loggerHandler.setFormatter(formatter)
logger.addHandler(loggerHandler)

# now we can import the last modules
from writer import *
from longpoll import *
from settings import *
import vkapi as api
import utils

# Compatibility with old config files
if not ADMIN_JIDS and evalJID:
	ADMIN_JIDS.append(evalJID)

# Setting variables
# DefLang for language id, root for the translations directory
setVars(DefLang, root)

if THREAD_STACK_SIZE:
	threading.stack_size(THREAD_STACK_SIZE)
del formatter, loggerHandler

if os.name == "posix":
	OS = "{0} {2:.16} [{4}]".format(*os.uname())
else:
	import platform
	OS = "Windows {0}".format(*platform.win32_ver())

PYTHON_VERSION = "{0} {1}.{2}.{3}".format(sys.subversion[0], *sys.version_info)

# See extensions/.example.py for more information about handlers
Handlers = {"msg01": [], "msg02": [],
			"msg03": [], "evt01": [],
			"evt02": [], "evt03": [],
			"evt04": [], "evt05": [],
			"evt06": [], "evt07": [],
			"prs01": [], "prs02": [],
			"evt08": [], "evt09": []}

Stats = {"msgin": 0,  # from vk
		"msgout": 0,  # to vk
		"method": 0}


def runDatabaseQuery(query, args=(), set=False, many=True):
	"""
	Executes sql to the database
	"""
	semph = None
	if threading.currentThread() != "MainThread":
		semph = Semaphore
	with Database(DatabaseFile, semph) as db:
		db(query, args)
		if set:
			db.commit()
			result = None
		elif many:
			result = db.fetchall()
		else:
			result = db.fetchone()
	return result


def initDatabase(filename):
	"""
	Initializes database if it doesn't exist
	"""
	runDatabaseQuery("create table if not exists users"
		"(jid text, username text, token text, "
			"lastMsgID integer, rosterSet bool)", set=True)
	return True


def executeHandlers(type, list=()):
	"""
	Executes all handlers by type with list as list of args
	"""
	handlers = Handlers[type]
	for handler in handlers:
		utils.execute(handler, list)


def registerHandler(type, func):
	"""
	Registers handlers
	"""
	logger.info("main: add \"%s\" to handle type %s", func.func_name, type)
	for handler in Handlers[type]:
		if handler.func_name == func.func_name:
			Handlers[type].remove(handler)
	Handlers[type].append(func)


def getGatewayRev():
	"""
	Gets gateway revision using git or custom revision number
	"""
	number, hash = 317, 0
	shell = os.popen("git describe --always &"
		"& git log --pretty=format:''").readlines()
	if shell:
		number, hash = len(shell), shell[0]
	return "#%s-%s" % (number, hash)


def vk2xmpp(id):
	"""
	Converts vk ids to jabber ids and vice versa
	Returns id@TransportID if parameter "id" is int or str(int)
	Returns id if parameter "id" is id@TransportID
	Returns TransportID if "id" is TransportID
	"""
	if not utils.isNumber(id) and "@" in id:
		id = id.split("@")[0]
		if utils.isNumber(id):
			id = int(id)
	elif id != TransportID:
		id = u"%s@%s" % (id, TransportID)
	return id


REVISION = getGatewayRev()

# Escape xmpp non-allowed chars
badChars = [x for x in xrange(32) if x not in (9, 10, 13)] + [57003, 65535]
escape = re.compile("|".join(unichr(x) for x in badChars),
	re.IGNORECASE | re.UNICODE | re.DOTALL).sub

sortMsg = lambda first, second: first.get("id", 0) - second.get("id", 0)
require = lambda name: os.path.exists("extensions/%s.py" % name)
isdef = lambda var: var in globals()
findUserInDB = lambda source: runDatabaseQuery("select * from users where jid=?", (source,), many=False)


class VK(object):
	"""
	A base class for VK
	Contain functions which directly work with VK
	"""
	def __init__(self, token=None, source=None):
		self.token = token
		self.source = source
		self.pollConfig = {"mode": 66, "wait": 30, "act": "a_check"}
		self.pollServer = ""
		self.pollInitialzed = False
		self.online = False
		self.userID = 0
		self.methods = 0
		self.lists = []
		self.friends_fields = set(["screen_name"])
		self.cache = {}
		logger.debug("VK initialized (jid: %s)", source)

	getToken = lambda self: self.engine.token

	def checkToken(self):
		"""
		Checks the token
		"""
		try:
			int(self.engine.method("isAppUser"))
		except (api.VkApiError, TypeError, AttributeError):
			return False
		return True

	def auth(self, username=None, password=None):
		"""
		Initializes the APIBinding object and checks the token
		"""
		logger.debug("VK going to authenticate (jid: %s)", self.source)
		self.engine = api.APIBinding(self.token, DEBUG_API, self.source)
		if not self.checkToken():
			raise api.TokenError("The token is invalid (jid: %s, token: %s)" % (self.source, self.token))
		self.online = True
		return True

	def initPoll(self):
		"""
		Initializes longpoll
		Returns False if error occurred
		"""
		self.pollInitialzed = False
		logger.debug("longpoll: requesting server address (jid: %s)", self.source)
		try:
			response = self.method("messages.getLongPollServer", {"use_ssl": 1, "need_pts": 0})
		except Exception:
			response = None
		if not response:
			logger.warning("longpoll: no response!")
			return False
		self.pollServer = "https://%s" % response.pop("server")
		self.pollConfig.update(response)
		logger.debug("longpoll: server: %s (jid: %s)",
			self.pollServer, self.source)
		self.pollInitialzed = True
		return True

	def makePoll(self):
		"""
		Returns a socket connected to a poll server
		Raises api.LongPollError if poll not yet initialized (self.pollInitialzed)
		"""
		if not self.pollInitialzed:
			raise api.LongPollError("The Poll wasn't initialized yet")
		opener = api.AsyncHTTPRequest.getOpener(self.pollServer, self.pollConfig)
		return opener

	def method(self, method, args=None, force=False, notoken=False):
		"""
		This is a duplicate function of self.engine.method
		Needed to handle errors properly exactly in __main__
		Parameters:
			method: obviously VK API method
			args: method aruments
			nodecode: decode flag (make json.loads or not)
			force: says that method will be executed even the captcha and not online
		See library/vkapi.py for more information about exceptions
		Returns method execution result
		"""
		args = args or {}
		result = {}
		self.methods += 1
		Stats["method"] += 1
		if not self.engine.captcha and (self.online or force):
			try:
				result = self.engine.method(method, args, notoken=notoken)
			except (api.InternalServerError, api.AccessDenied) as e:
				if force:
					raise

			except api.CaptchaNeeded as e:
				executeHandlers("evt04", (self, self.engine.captcha["img"]))
				self.online = False

			except api.ValidationRequired:
				# TODO
				raise

			except api.NetworkNotFound as e:
				self.online = False

			except api.NotAllowed as e:
				if self.engine.lastMethod[0] == "messages.send":
					sendMessage(self.source,
						vk2xmpp(args.get("user_id", TransportID)),
						_("You're not allowed to perform this action."))

			except api.VkApiError as e:
				# There are several types of VkApiError
				# But the user defenitely must be removed.
				# The question is: how?
				# Should we completely exterminate them or just remove?
				roster = False
				m = e.message
				# Probably should be done in vkapi.py by status codes
				if m == "User authorization failed: user revoke access for this token.":
					roster = True
				elif m == "User authorization failed: invalid access_token.":
					sendMessage(self.source, TransportID,
						m + " Please, register again")
				utils.runThread(removeUser, (self.source, roster))
				logger.error("VK: apiError %s (jid: %s)", m, self.source)
				self.online = False
			else:
				return result
			logger.error("VK: error %s occurred while executing"
				" method(%s) (%s) (jid: %s)",
				e.__class__.__name__, method, e.message, self.source)
			return result

	@utils.threaded
	def disconnect(self):
		"""
		Stops all user handlers and removes them from Poll
		"""
		self.online = False
		logger.debug("VK: user %s has left", self.source)
		executeHandlers("evt06", (self,))
		self.method("account.setOffline")

	@staticmethod
	def formatName(data):
		name = escape("", "%(first_name)s %(last_name)s" % data)
		del data["first_name"]
		del data["last_name"]
		return name

	def getFriends(self, fields=None):
		"""
		Executes friends.get and formats it in key-value style
		Example: {1: {"name": "Pavel Durov", "online": False}
		Parameter fields is needed to receive advanced fields
		Which will be added in the result values
		"""
		fields = fields or self.friends_fields
		raw = self.method("friends.get", {"fields": str.join(",", fields)})
		friends = {}
		for friend in raw.get("items", []):
			uid = friend["id"]
			online = friend["online"]
			name = self.formatName(friend)
			friends[uid] = {"name": name, "online": online, "lists": friend.get("lists")}
			for key in fields:
				friends[uid][key] = friend.get(key)
		return friends

	def getLists(self):
		if not self.lists:
			self.lists = self.method("friends.getLists")
		return self.lists

	def getMessages(self, count=5, mid=0):
		"""
		Gets last messages list count 5 with last id mid
		"""
		values = {"out": 0, "filters": 1, "count": count}
		if mid:
			del values["count"]
			del values["filters"]
			values["last_message_id"] = mid
		return self.method("messages.get", values)

	def getUserID(self):
		"""
		Gets user id
		"""
		if not self.userID:
			self.userID = self.method("execute.getUserID_new")
		return self.userID

	@utils.cache
	def getGroupData(self, gid, fields=None):
		"""
		Gets group data (only name so far)
		"""
		fields = fields or ["name"]
		data = self.method("groups.getById", {"group_id": abs(gid), "fields": str.join(",", fields)})
		if data:
			data = data[0]
		return data

	@utils.cache
	def getUserData(self, uid, fields=None):
		"""
		Gets user data. Such as name, photo, etc
		"""
		if not fields:
			user = Transport.get(self.source)
			if user and uid in user.friends:
				return user.friends[uid]
			fields = ["screen_name"]
		data = self.method("users.get", {"user_ids": uid, "fields": str.join(",", fields)})
		if data:
			data = data[0]
			data["name"] = self.formatName(data)
		return data

	def sendMessage(self, body, id, mType="user_id", more={}):
		"""
		Sends message to VK id
		Parameters:
			body: message body
			id: user id
			mType: message type (user_id is for dialogs, chat_id is for chats)
			more: for advanced fields such as attachments
		"""
		Stats["msgout"] += 1
		values = {mType: id, "message": body, "type": 0}
		values.update(more)
		try:
			result = self.method("messages.send", values)
		except api.VkApiError:
			crashLog("messages.send")
			result = False
		return result


class User(object):
	"""
	Main class.
	Makes a “bridge” between VK & XMPP.
	"""
	def __init__(self, source=""):
		self.friends = {}
		self.typing = {}
		self.source = source

		self.lastMsgID = 0
		self.rosterSet = None
		self.username = None

		self.resources = set([])
		self.settings = Settings(source)
		self.last_udate = time.time()
		self.sync = threading._allocate_lock()
		logger.debug("User initialized (jid: %s)", self.source)

	def connect(self, username=None, password=None, token=None):
		"""
		Calls VK.auth() and calls captchaChallenge on captcha
		Updates db if auth() is done
		"""
		logger.debug("User connecting (jid: %s)", self.source)
		exists = False
		user = findUserInDB(self.source)  # check if user registered
		if user:
			exists = True
			logger.debug("User was found in the database... (jid: %s)", self.source)
			if not token:
				logger.debug("... but no token was given. Using the one from the database (jid: %s)", self.source)
				_, _, token, self.lastMsgID, self.rosterSet = user

		if not (token or password):
			logger.warning("User wasn't found in the database and no token or password was given!")
			raise RuntimeError("Either no token or password was given!")

		if password:
			logger.debug("Going to authenticate via password (jid: %s)", self.source)
			pwd = api.PasswordLogin(username, password).login()
			token = pwd.confirm()

		self.vk = vk = VK(token, self.source)
		try:
			vk.auth()
		except api.CaptchaNeeded:
			self.sendSubPresence()
			logger.error("User: running captcha challenge (jid: %s)", self.source)
			executeHandlers("evt04", (self, vk.engine.captcha["img"]))
			return True
		else:
			logger.debug("User seems to be authenticated (jid: %s)", self.source)
			if exists:
				# Anyways, it won't hurt anyone
				runDatabaseQuery("update users set token=? where jid=?",
					(vk.getToken(), self.source), True)
			else:
				runDatabaseQuery("insert into users (jid, token, lastMsgID, rosterSet) values (?,?,?,?)",
					(self.source, vk.getToken(),
						self.lastMsgID, self.rosterSet), True)
			executeHandlers("evt07", (self,))
			self.friends = vk.getFriends()
		return vk.online

	def markRosterSet(self):
		self.rosterSet = True
		runDatabaseQuery("update users set rosterSet=? where jid=?",
			(self.rosterSet, self.source), True)

	def initialize(self, force=False, send=True, resource=None):
		"""
		Initializes user after self.connect() is done:
			1. Receives friends list and set 'em to self.friends
			2. If #1 is done and roster is not yet set (self.rosterSet)
				then sends a subscription presence
			3. Calls sendInitPresnece() if parameter send is True
			4. Adds resource if resource parameter exists
		Parameters:
			force: force sending subscription presence
			send: needed to know if need to send init presence or not
			resource: add resource in self.resources to prevent unneeded stanza sending
		"""
		logger.debug("User: beginning user initialization (jid: %s)", self.source)
		Transport[self.source] = self
		if not self.friends:
			self.friends = self.vk.getFriends()
		if force or not self.rosterSet:
			logger.debug("User: sending subscription presence with force:%s (jid: %s)",
				force, self.source)
			import rostermanager
			rostermanager.Roster.checkRosterx(self, resource)
		if send:
			self.sendInitPresence()
		if resource:
			self.resources.add(resource)
		utils.runThread(self.vk.getUserID)
		self.sendMessages(True)
		Poll.add(self)
		utils.runThread(executeHandlers, ("evt05", (self,)))

	def sendInitPresence(self):
		"""
		Sends available presence to the user from all online friends
		"""
		if not self.vk.engine.captcha:
			if not self.friends:
				self.friends = self.vk.getFriends()
			logger.debug("User: sending init presence (friends count: %s) (jid %s)",
				len(self.friends), self.source)
			for uid, value in self.friends.iteritems():
				if value["online"]:
					sendPresence(self.source, vk2xmpp(uid), hash=USER_CAPS_HASH)
			sendPresence(self.source, TransportID, hash=TRANSPORT_CAPS_HASH)

	def sendOutPresence(self, destination, reason=None, all=False):
		"""
		Sends out presence (unavailable) to destination. Defines a reason, if set.
		Parameters:
			destination: to whom send the stanzas
			reason: offline status message
			all: send an unavailable from all friends or only the ones who's online
		"""
		logger.debug("User: sending out presence to %s", self.source)
		friends = self.friends.keys()
		if not all and friends:
			friends = filter(lambda key: self.friends[key]["online"], friends)

		for uid in friends + [TransportID]:
			sendPresence(destination, vk2xmpp(uid), "unavailable", reason=reason)

	def sendSubPresence(self, dist=None):
		"""
		Sends subscribe presence to self.source
		Parameteres:
			dist: friends list
		"""
		dist = dist or {}
		for uid, value in dist.iteritems():
			sendPresence(self.source, vk2xmpp(uid), "subscribe", value["name"])
		sendPresence(self.source, TransportID, "subscribe", IDENTIFIER["name"])
		# TODO: Only mark roster set when we received authorized/subscribed event from the user
		if dist:
			self.markRosterSet()

	def sendMessages(self, init=False, messages=None):
		"""
		Sends messages from vk to xmpp and call message01 handlers
		Paramteres:
			init: needed to know if function called at init (add time or not)
		Plugins notice (msg01):
			If plugin returs None then message will not be sent by transport's core,
				it shall be sent by plugin itself
			Otherwise, if plugin returns string,
				the message will be sent by transport's core
		"""
		with self.sync:
			date = 0
			if not messages:
				messages = self.vk.getMessages(200, self.lastMsgID).get("items")
			if not messages:
				return None
			messages = sorted(messages, sortMsg)
			for message in messages:
				# If message wasn't sent by our user
				if not message["out"]:
					Stats["msgin"] += 1
					fromjid = vk2xmpp(message["user_id"])
					body = uhtml(message["body"])
					iter = Handlers["msg01"].__iter__()
					for func in iter:
						try:
							result = func(self, message)
						except Exception:
							result = ""
							crashLog("handle.%s" % func.__name__)

						if result is None:
							for func in iter:
								utils.execute(func, (self, message))
							break
						else:
							body += result
					else:
						if self.settings.force_vk_date or init:
							date = message["date"]
						sendMessage(self.source, fromjid, escape("", body), date)
		if messages:
			self.lastMsgID = messages[-1]["id"]
			runDatabaseQuery("update users set lastMsgID=? where jid=?",
				(self.lastMsgID, self.source), True)

	def processPollResult(self, opener):
		"""
		Processes poll result
		Retur codes:
			0: need to reinit poll (add user to the poll buffer)
			1: all is fine (request again)
			-1: just continue iteration, ignoring this user
				(user won't be added for the next iteration)
		"""
		if DEBUG_POLL:
			logger.debug("longpoll: processing result (jid: %s)", self.source)

		if self.vk.engine.captcha:
			return -1

		data = None
		try:
			data = opener.read()
		except (httplib.BadStatusLine, socket.error, socket.timeout) as e:
			logger.warning("longpoll: got error `%s` (jid: %s)", e.__class__.__name__,
				self.source)
			return 0
		try:
			data = json.loads(data)
			if not data:
				raise ValueError()
		except ValueError:
			logger.error("longpoll: no data. Gonna request again (jid: %s)",
				self.source)
			return 1

		if "failed" in data:
			logger.debug("longpoll: failed. Searching for a new server (jid: %s)",
				self.source)
			return 0

		self.vk.pollConfig["ts"] = data["ts"]

		for evt in data.get("updates", ()):
			typ = evt.pop(0)

			if DEBUG_POLL:
				logger.debug("longpoll: got updates, processing event %s with arguments %s (jid: %s)", typ, str(evt), self.source)

			if typ == 4:  # new message
				if len(evt) == 7:
					message = None
					mid, flags, uid, date, subject, body, attachments = evt
					out = flags & 2 == 2
					chat = uid > 2000000000  # a groupchat always has uid > 2000000000
					if not out:
						if not attachments and not chat:
							message = [{"out": 0, "user_id": uid, "id": mid, "date": date, "body": body}]
						utils.runThread(self.sendMessages, (None, message), "sendMessages-%s" % self.source)
				else:
					logger.warning("longpoll: incorrect events number while trying to process arguments %s (jid: %s)", str(evt), self.source)

			elif typ == 8:  # user has joined
				uid = abs(evt[0])
				sendPresence(self.source, vk2xmpp(uid), hash=USER_CAPS_HASH)

			elif typ == 9:  # user has left
				uid = abs(evt[0])
				sendPresence(self.source, vk2xmpp(uid), "unavailable")

			elif typ == 61:  # user is typing
				if evt[0] not in self.typing:
					sendMessage(self.source, vk2xmpp(evt[0]), typ="composing")
				self.typing[evt[0]] = time.time()
		return 1

	def updateTypingUsers(self, cTime):
		"""
		Sends "paused" message event to stop user from composing a message
		Sends only if last typing activity in VK was more than 10 seconds ago
		"""
		for user, last in self.typing.items():
			if cTime - last > 7:
				del self.typing[user]
				sendMessage(self.source, vk2xmpp(user), typ="paused")

	def updateFriends(self, cTime):
		"""
		Updates friends list
		Sends subscribe presences if new friends found
		Sends unsubscribe presences if some friends disappeared
		"""
		if (cTime - self.last_udate) > 300 and not self.vk.engine.captcha:
			if self.settings.keep_online:
				self.vk.method("account.setOnline")
			self.last_udate = cTime
			friends = self.vk.getFriends()
			if not friends:
				logger.error("updateFriends: no friends received (jid: %s).",
					self.source)
				return None

			for uid in friends:
				if uid not in self.friends:
					self.sendSubPresence({uid: friends[uid]})
			for uid in self.friends:
				if uid not in friends:
					sendPresence(self.source, vk2xmpp(uid), "unsubscribe")
			self.friends = friends

	def reauth(self):
		"""
		Tries to execute self.initialize() again and connect() if needed
		Usually needed after captcha challenge is done
		"""
		logger.debug("calling reauth for user (jid: %s)", self.source)
		if not self.vk.online:
			self.connect()
		self.initialize()

	def captchaChallenge(self, key):
		"""
		Sets the captcha key and sends it to VK
		"""
		engine = self.vk.engine
		engine.captcha["key"] = key
		logger.debug("retrying for user (jid: %s)", self.source)
		if engine.retry():
			self.reauth()


def sendPresence(destination, source, pType=None, nick=None,
	reason=None, hash=None, show=None):
	"""
	Sends presence to destination from source
	Parameters:
		destination: to whom send the presence
		source: from who send the presence
		pType: the presence type
		nick: add <nick> tag
		reason: set status message
		hash: add caps hash
		show: add status show
	"""
	presence = xmpp.Presence(destination, pType,
		frm=source, status=reason, show=show)
	if nick:
		presence.setTag("nick", namespace=xmpp.NS_NICK)
		presence.setTagData("nick", nick)
	if hash:
		presence.setTag("c", {"node": CAPS_NODE, "ver": hash, "hash": "sha-1"}, xmpp.NS_CAPS)
	executeHandlers("prs02", (presence, destination, source))
	sender(Component, presence)


def sendMessage(destination, source, body=None, timestamp=0, typ="active", mtype="chat"):
	"""
	Sends message to destination from source
	Parameters:
		cl: xmpp.Client object
		destination: to whom send the message
		source: from who send the message
		body: message body
		timestamp: message timestamp (XEP-0091)
		typ: xmpp chatstates type (XEP-0085)
	"""
	msg = xmpp.Message(destination, body, mtype, frm=source)
	msg.setTag(typ, namespace=xmpp.NS_CHATSTATES)
	if timestamp:
		timestamp = time.gmtime(timestamp)
		msg.setTimestamp(time.strftime("%Y%m%dT%H:%M:%S", timestamp))
	executeHandlers("msg03", (msg, destination, source))
	sender(Component, msg)


def computeCapsHash(features=TransportFeatures):
	"""
	Computes a hash which will be placed in all presence stanzas
	"""
	result = "%(category)s/%(type)s//%(name)s<" % IDENTIFIER
	features = sorted(features)
	result += str.join("<", features) + "<"
	return hashlib.sha1(result).digest().encode("base64")


# TODO: rename me
def sender(cl, stanza, cb=None, args={}):
	"""
	Sends stanza. Writes a crashlog on error
	Parameters:
		cl: xmpp.Client object
		stanza: xmpp.Node object
		cb: callback function
		args: callback function arguments
	"""
	if cb:
		cl.SendAndCallForResponse(stanza, cb, args)
	else:
		try:
			cl.send(stanza)
		except Exception:
			disconnectHandler(True)


def updateCron():
	"""
	Calls the functions to update friends and typing users list
	"""
	while ALIVE:
		for user in Transport.values():
			cTime = time.time()
			user.updateTypingUsers(cTime)
			user.updateFriends(cTime)
		time.sleep(2)


def calcStats():
	"""
	Returns count(*) from users database
	"""
	countOnline = len(Transport)
	countTotal = runDatabaseQuery("select count(*) from users", many=False)[0]
	return [countTotal, countOnline]


def removeUser(user, roster=False, notify=True):
	"""
	Removes user from database
	Parameters:
		user: User class object or jid without resource
		roster: remove vk contacts from user's roster
			(only if User class object was in the first param)
	"""
	if isinstance(user, (str, unicode)):  # unicode is the default, but... who knows
		source = user
	elif user:
		source = user.source
	user = Transport.get(source)
	if notify:
		# Would russians understand the joke?
		sendMessage(source, TransportID,
			_("Your record was EXTERMINATED from the database."
				" Let us know if you feel exploited."), -1)
	logger.debug("User: removing user from db (jid: %s)" % source)
	runDatabaseQuery("delete from users where jid=?", (source,), True)
	logger.debug("User: deleted (jid: %s)", source)
	if source in Transport:
		del Transport[source]
	if roster and user:
		friends = user.friends
		user.exists = False  # Make the Daleks happy
		if friends:
			logger.debug("User: removing myself from roster (jid: %s)", source)
			for id in friends.keys() + [TransportID]:
				jid = vk2xmpp(id)
				sendPresence(source, jid, "unsubscribe")
				sendPresence(source, jid, "unsubscribed")
			user.settings.exterminate()
			executeHandlers("evt03", (user,))
		user.vk.online = False


def checkPID():
	"""
	Gets a new PID and kills the previous PID
	by signal 15 and then by 9
	"""
	pid = os.getpid()
	if os.path.exists(pidFile):
		old = rFile(pidFile)
		if old:
			Print("#-# Killing the previous instance: ", False)
			old = int(old)
			if pid != old:
				try:
					os.kill(old, 15)
					time.sleep(3)
					os.kill(old, 9)
				except OSError as e:
					if e.errno != 3:
						Print("%d %s.\n" % (old, e.message), False)
				else:
					Print("%d killed.\n" % old, False)
	wFile(pidFile, str(pid))


def loadExtensions(dir):
	"""
	Loads extensions
	"""
	for file in os.listdir(dir):
		if not file.startswith(".") and file.endswith(".py"):
			execfile("%s/%s" % (dir, file), globals())


def connect():
	"""
	Just makes a connection to the jabber server
	Returns False if failed, True if completed
	"""
	global Component
	Component = xmpp.Component(Host, debug=DEBUG_XMPPPY)
	Print("\n#-# Connecting: ", False)
	if not Component.connect((Server, Port)):
		Print("failed.\n", False)
		return False
	else:
		Print("ok.\n", False)
		Print("#-# Auth: ", False)
		if not Component.auth(TransportID, Password):
			Print("failed (%s/%s)!\n"
				% (Component.lastErr, Component.lastErrCode), True)
			return False
		else:
			Print("ok.\n", False)
			Component.RegisterDisconnectHandler(disconnectHandler)
			Component.set_send_interval(STANZA_SEND_INTERVAL)
	return True


def initializeUsers():
	"""
	Initializes users by sending them "probe" presence
	"""
	Print("#-# Initializing users", False)
	users = runDatabaseQuery("select jid from users")
	for user in users:
		Print(".", False)
		sendPresence(user[0], TransportID, "probe")
	Print("\n#-# Component %s initialized well." % TransportID)


def runMainActions():
	"""
	Running the main actions to make the transport work
	"""
	for num, event in enumerate(Handlers["evt01"]):
		utils.runThread(event, name=("extension-%d" % num))
	utils.runThread(Poll.process, name="longPoll")
	utils.runThread(updateCron)
	import modulemanager
	Manager = modulemanager.ModuleManager
	Manager.load(Manager.list())
	global USER_CAPS_HASH, TRANSPORT_CAPS_HASH
	USER_CAPS_HASH = computeCapsHash(UserFeatures)
	TRANSPORT_CAPS_HASH = computeCapsHash(TransportFeatures)


def main():
	"""
	Running main actions to start the transport
	Such as pid, db, connect
	"""
	if RUN_AS:
		import pwd
		uid = pwd.getpwnam(RUN_AS).pw_uid
		logger.warning("switching to user %s:%s", RUN_AS, uid)
		os.setuid(uid)
	checkPID()
	initDatabase(DatabaseFile)
	if connect():
		initializeUsers()
		runMainActions()
		logger.info("transport initialized at %s", TransportID)
	else:
		disconnectHandler(False)


def disconnectHandler(crash=True):
	"""
	Handles disconnect
	And writes a crash log if crash parameter is True
	"""
	executeHandlers("evt02")
	if crash:
		crashLog("main.disconnect")
	logger.critical("disconnecting from the server")
	try:
		Component.disconnect()
	except AttributeError:
		pass
	global ALIVE
	ALIVE = False
	if not Daemon:
		logger.warning("the trasnport is going to be restarted!")
		Print("Restarting...")
		time.sleep(5)
		os.execl(sys.executable, sys.executable, *sys.argv)
	else:
		logger.info("the transport is shutting down!")
		os._exit(-1)


def exit(signal=None, frame=None):
	"""
	Just stops the transport and sends unavailable presence
	"""
	status = "Shutting down by %s" % ("SIGTERM" if signal == 15 else "SIGINT")
	Print("#! %s" % status, False)
	for user in Transport.itervalues():
		user.sendOutPresence(user.source, status, all=True)
		Print("." * len(user.friends), False)
	Print("\n")
	executeHandlers("evt02")
	try:
		os.remove(pidFile)
	except OSError:
		pass
	os._exit(0)


def loop():
	"""
	The main loop which is used to call the stanza parser
	"""
	while ALIVE:
		try:
			Component.iter(6)
		except Exception:
			logger.critical("disconnected")
			crashLog("component.iter")
			disconnectHandler(True)


if __name__ == "__main__":
	signal.signal(signal.SIGTERM, exit)
	signal.signal(signal.SIGINT, exit)
	loadExtensions("extensions")
	transportSettings = Settings(TransportID, user=False)
	try:
		main()
	except Exception:
		crashLog("main")
		os._exit(1)
	loop()

# This is the end!
