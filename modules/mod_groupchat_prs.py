# coding: utf-8
# This file is a part of VK4XMPP transport
# © simpleApps, 2013 — 2015.

"""
Module purpose is to handle presences from groupchats
"""

from __main__ import *
from __main__ import _


def handleChatErrors(source, prs):
	"""
	Handles error presences from groupchats
	"""
	# todo: leave on 401, 403, 405
	# and rejoin timer on 404, 503
	destination = prs.getTo().getStripped()
	error = prs.getErrorCode()
	status = prs.getStatusCode()
	nick = prs.getFrom().getResource()
	jid = prs.getJid()
	user = None
	errorType = prs.getTagAttr("error", "type")
	user = Chat.getUserObject(source)
	if user and source in getattr(user, "chats", {}):
		chat = user.chats[source]
		if chat.creation_failed:
			raise xmpp.NodeProcessed()

		if error == "409" and errorType == "cancel":
			id = vk2xmpp(destination)
			if id in chat.users:
				nick += "."
				if not chat.created and id == TransportID:
					chat.users[id]["name"] = nick
					chat.create(user)
				else:
					joinChat(source, nick, destination)

		if status == "303":
			if jid == user.source:
				chat.owner_nickname = prs.getNick()
				runDatabaseQuery("update groupchats where jid=? set nick=?",
					(source, chat.owner_nickname), set=True)
		else:
			logger.debug("groupchats: presence error (error #%s, status #%s)"
				"from source %s (jid: %s)" % (error, status, source, user.source if user else "unknown"))
	raise xmpp.NodeProcessed()


def handleChatPresences(source, prs):
	"""
	Makes old users leave
	Parameters:
		* source: stanza source
		* prs: xmpp.Presence object
	"""
	jid = prs.getJid() or ""
	if "@" in jid:
		user = Chat.getUserObject(source)
		if user and source in getattr(user, "chats", {}):
			chat = user.chats[source]
			if jid.split("@")[1] == TransportID and chat.created:
				id = vk2xmpp(jid)
				if id != TransportID and id not in chat.users.keys():
					if (time.gmtime().tm_mon, time.gmtime().tm_mday) == (4, 1):
						setAffiliation(source, "outcast", jid, reason=_("Get the hell outta here!"))
					else:
						leaveChat(source, jid, _("I am not welcomed here"))

				if (prs.getRole(), prs.getAffiliation()) == ("moderator", "owner"):
					if jid != TransportID:
						runDatabaseQuery("update groupchats set owner=? where jid=?", (source, jid), set=True)

			if jid.split("/")[0] == user.source:
				chat.owner_nickname = prs.getFrom().getResource()
				runDatabaseQuery("update groupchats set nick=? where jid=? ", (chat.owner_nickname, source), set=True)
			raise xmpp.NodeProcessed()

		elif user and prs.getType() != "unavailable":
			createFakeChat(user, source)


@utils.safe
def presence_handler(cl, prs):
	source = prs.getFrom().getStripped()
	status = prs.getStatus()
	if status or prs.getType() == "error":
		handleChatErrors(source, prs)
	handleChatPresences(source, prs)  # It won't be called if handleChatErrors was called in the first time


MOD_TYPE = "presence"
MOD_HANDLERS = ((presence_handler, "", "", True),)
MOD_FEATURES = []
