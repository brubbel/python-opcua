"""
Internal server implementing opcu-ua interface.
Can be used on server side or to implement binary/https opc-ua servers
"""
from datetime import datetime, timedelta
from copy import copy
import os
import logging
from threading import Lock
from enum import Enum
try:
    from urllib.parse import urlparse
except ImportError:
    from urlparse import urlparse

try:
    import cPickle as pickle # python 2.x
except:
    import pickle # python 3.x will automatically load accelerated Pickle version

from opcua import ua
from opcua.common import utils
from opcua.common.callback import (CallbackType, ServerItemCallback,
                                   CallbackDispatcher)
from opcua.common.node import Node
from opcua.server.history import HistoryManager
from opcua.server.address_space import AddressSpace
from opcua.server.address_space import AttributeService
from opcua.server.address_space import ViewService
from opcua.server.address_space import NodeManagementService
from opcua.server.address_space import MethodService
from opcua.server.subscription_service import SubscriptionService
from opcua.server.discovery_service import LocalDiscoveryService
from opcua.server.standard_address_space import standard_address_space
from opcua.server.user_manager import UserManager
#from opcua.common import xmlimporter


class SessionState(Enum):
    Created = 0
    Activated = 1
    Closed = 2

class InternalServer(object):

    def __init__(self, aspace=None, parent=None):
        self.logger = logging.getLogger(__name__)

        self._parent = parent
        self.server_callback_dispatcher = CallbackDispatcher()

        self.endpoints = []
        self._channel_id_counter = 5
        self.disabled_clock = False  # for debugging we may want to disable clock that writes too much in log
        self._local_discovery_service = None # lazy-loading

        self.aspace = AddressSpace() if aspace is None else aspace
        self.attribute_service = AttributeService(self.aspace)
        self.view_service = ViewService(self.aspace)
        self.method_service = MethodService(self.aspace)
        self.node_mgt_service = NodeManagementService(self.aspace)

        if aspace is None:
            standard_address_space.fill_address_space(self.node_mgt_service)

        self.loop = None
        self.asyncio_transports = []
        self.subscription_service = SubscriptionService(self.aspace)

        self.history_manager = HistoryManager(self)

        # create a session to use on server side
        self.isession = InternalSession(self, \
          self.subscription_service, "Internal", user=UserManager.User.Admin)

        self.current_time_node = Node(self.isession, ua.NodeId(ua.ObjectIds.Server_ServerStatus_CurrentTime))
        self.setup_nodes()

    @property
    def user_manager(self):
        return self._parent.user_manager

    @property
    def thread_loop(self):
        if self.loop is None:
            raise Exception("InternalServer stopped: async threadloop is not running.")
        return self.loop

    @property
    def local_discovery_service(self):
        if self._local_discovery_service is None:
            self._local_discovery_service = LocalDiscoveryService(parent = self)
            for edp in self.endpoints:
                srvDesc = LocalDiscoveryService.ServerDescription(edp.Server)
                self._local_discovery_service.add_server_description(srvDesc)
        return self._local_discovery_service

    def setup_nodes(self):
        """
        Set up some nodes as defined by spec
        """
        uries = ["http://opcfoundation.org/UA/"]
        ns_node = Node(self.isession, ua.NodeId(ua.ObjectIds.Server_NamespaceArray))
        ns_node.set_value(uries)

    def start(self):
        self.logger.info("starting internal server")
        self.loop = utils.ThreadLoop()
        self.loop.start()
        self.subscription_service.set_loop(self.loop)
        Node(self.isession, ua.NodeId(ua.ObjectIds.Server_ServerStatus_State)).set_value(0, ua.VariantType.Int32)
        Node(self.isession, ua.NodeId(ua.ObjectIds.Server_ServerStatus_StartTime)).set_value(datetime.utcnow())
        if not self.disabled_clock:
            self._set_current_time()

    def stop(self):
        self.logger.info("stopping internal server")
        self.isession.close_session()
        if self.loop:
            self.loop.stop()
            self.loop = None
        self.subscription_service.set_loop(None)
        self.history_manager.stop()

    def is_running(self):
        return self.loop is not None

    def _set_current_time(self):
        self.current_time_node.set_value(datetime.utcnow())
        self.loop.call_later(1, self._set_current_time)

    def get_new_channel_id(self):
        self._channel_id_counter += 1
        return self._channel_id_counter

    def add_endpoint(self, endpoint):
        self.endpoints.append(endpoint)

    def get_endpoints(self, params=None, sockname=None):
        self.logger.info("get endpoint")
        if sockname:
            # return to client the ip address it has access to
            edps = []
            for edp in self.endpoints:
                edp1 = copy(edp)
                url = urlparse(edp1.EndpointUrl)
                url = url._replace(netloc=sockname[0] + ":" + str(sockname[1]))
                edp1.EndpointUrl = url.geturl()
                edps.append(edp1)
            return edps
        return self.endpoints[:]

    def create_session(self, name, user=UserManager.User.Anonymous, external=False):
        return InternalSession(self, self.subscription_service, name, user=user, external=external)

    def enable_history_data_change(self, node, period=timedelta(days=7), count=0):
        """
        Set attribute Historizing of node to True and start storing data for history
        """
        node.set_attribute(ua.AttributeIds.Historizing, ua.DataValue(True))
        node.set_attr_bit(ua.AttributeIds.AccessLevel, ua.AccessLevel.HistoryRead)
        node.set_attr_bit(ua.AttributeIds.UserAccessLevel, ua.AccessLevel.HistoryRead)
        self.history_manager.historize_data_change(node, period, count)

    def disable_history_data_change(self, node):
        """
        Set attribute Historizing of node to False and stop storing data for history
        """
        node.set_attribute(ua.AttributeIds.Historizing, ua.DataValue(False))
        node.unset_attr_bit(ua.AttributeIds.AccessLevel, ua.AccessLevel.HistoryRead)
        node.unset_attr_bit(ua.AttributeIds.UserAccessLevel, ua.AccessLevel.HistoryRead)
        self.history_manager.dehistorize(node)

    def enable_history_event(self, source, period=timedelta(days=7), count=0):
        """
        Set attribute History Read of object events to True and start storing data for history
        """
        event_notifier = source.get_event_notifier()
        if ua.EventNotifier.SubscribeToEvents not in event_notifier:
            raise ua.UaError("Node does not generate events", event_notifier)

        if ua.EventNotifier.HistoryRead not in event_notifier:
            event_notifier.add(ua.EventNotifier.HistoryRead)
            source.set_event_notifier(event_notifier)

        self.history_manager.historize_event(source, period, count)

    def disable_history_event(self, source):
        """
        Set attribute History Read of node to False and stop storing data for history
        """
        source.unset_attr_bit(ua.AttributeIds.EventNotifier, ua.EventNotifier.HistoryRead)
        self.history_manager.dehistorize(source)

    def subscribe_server_callback(self, event, handle):
        """
        Create a subscription from event to handle
        """
        self.server_callback_dispatcher.addListener(event, handle)

    def unsubscribe_server_callback(self, event, handle):
        """
        Remove a subscription from event to handle
        """
        self.server_callback_dispatcher.removeListener(event, handle)

    def set_attribute_value(self, nodeid, datavalue, attr=ua.AttributeIds.Value):
        """
        directly write datavalue to the Attribute, bypasing some checks and structure creation
        so it is a little faster
        """
        self.aspace.set_attribute_value(nodeid, ua.AttributeIds.Value, datavalue)


class InternalSession(object):
    _counter = 10
    _auth_counter = 1000

    def __init__(self, internal_server, submgr, name, user=UserManager.User.Anonymous, external=False):
        self.logger = logging.getLogger(__name__)
        self.iserver = internal_server
        self.external = external  # define if session is external, we need to copy some objects if it is internal
        self.subscription_service = submgr
        self.name = name
        self.user = user
        self.nonce = None
        self.state = SessionState.Created
        self.session_id = ua.NodeId(self._counter)
        InternalSession._counter += 1
        self.authentication_token = ua.NodeId(self._auth_counter)
        InternalSession._auth_counter += 1
        self.subscriptions = []
        self.logger.info("Created internal session %s", self.name)
        self._lock = Lock()

    @property
    def user_manager(self):
        return self.iserver.user_manager

    @property
    def aspace(self):
        return self.iserver.aspace

    def __str__(self):
        return "InternalSession(name:{0}, user:{1}, id:{2}, auth_token:{3})".format(
            self.name, self.user, self.session_id, self.authentication_token)

    def get_endpoints(self, params=None, sockname=None):
        return self.iserver.get_endpoints(params, sockname)

    def create_session(self, params, sockname=None):
        self.logger.info("Create session request")

        result = ua.CreateSessionResult()
        result.SessionId = self.session_id
        result.AuthenticationToken = self.authentication_token
        result.RevisedSessionTimeout = params.RequestedSessionTimeout
        result.MaxRequestMessageSize = 65536
        self.nonce = utils.create_nonce(32)
        result.ServerNonce = self.nonce
        result.ServerEndpoints = self.get_endpoints(sockname=sockname)

        return result

    def close_session(self, delete_subs=True):
        self.logger.info("close session %s with subscriptions %s", self, self.subscriptions)
        self.state = SessionState.Closed
        self.delete_subscriptions(self.subscriptions[:])

    def activate_session(self, params):
        self.logger.info("activate session")
        result = ua.ActivateSessionResult()
        if self.state != SessionState.Created:
            raise utils.ServiceError(ua.StatusCodes.BadSessionIdInvalid)
        self.nonce = utils.create_nonce(32)
        result.ServerNonce = self.nonce
        for _ in params.ClientSoftwareCertificates:
            result.Results.append(ua.StatusCode())
        self.state = SessionState.Activated
        id_token = params.UserIdentityToken
        if isinstance(id_token, ua.UserNameIdentityToken):
            if self.user_manager.check_user_token(self, id_token) == False:
                raise utils.ServiceError(ua.StatusCodes.BadUserAccessDenied)
        self.logger.info("Activated internal session %s for user %s", self.name, self.user)
        return result

    def read(self, params):
        results = self.iserver.attribute_service.read(params)
        return results

    def history_read(self, params):
        return self.iserver.history_manager.read_history(params)

    def write(self, params):
        return self.iserver.attribute_service.write(params, self.user)

    def browse(self, params):
        return self.iserver.view_service.browse(params)

    def translate_browsepaths_to_nodeids(self, params):
        return self.iserver.view_service.translate_browsepaths_to_nodeids(params)

    def add_nodes(self, params):
        return self.iserver.node_mgt_service.add_nodes(params, self.user)

    def delete_nodes(self, params):
        return self.iserver.node_mgt_service.delete_nodes(params, self.user)

    def add_references(self, params):
        return self.iserver.node_mgt_service.add_references(params, self.user)

    def delete_references(self, params):
        return self.iserver.node_mgt_service.delete_references(params, self.user)

    def add_method_callback(self, methodid, callback):
        return self.aspace.add_method_callback(methodid, callback)

    def call(self, params):
        return self.iserver.method_service.call(params)

    def create_subscription(self, params, callback):
        result = self.subscription_service.create_subscription(params, callback)
        with self._lock:
            self.subscriptions.append(result.SubscriptionId)
        return result

    def modify_subscription(self, params, callback):
        return self.subscription_service.modify_subscription(params, callback)

    def create_monitored_items(self, params):
        subscription_result = self.subscription_service.create_monitored_items(params)
        self.iserver.server_callback_dispatcher.dispatch(
            CallbackType.ItemSubscriptionCreated, ServerItemCallback(params, subscription_result))
        return subscription_result

    def modify_monitored_items(self, params):
        subscription_result = self.subscription_service.modify_monitored_items(params)
        self.iserver.server_callback_dispatcher.dispatch(
            CallbackType.ItemSubscriptionModified, ServerItemCallback(params, subscription_result))
        return subscription_result

    def republish(self, params):
        return self.subscription_service.republish(params)

    def delete_subscriptions(self, ids):
        for i in ids:
            with self._lock:
                if i in self.subscriptions:
                    self.subscriptions.remove(i)
        return self.subscription_service.delete_subscriptions(ids)

    def delete_monitored_items(self, params):
        subscription_result = self.subscription_service.delete_monitored_items(params)
        self.iserver.server_callback_dispatcher.dispatch(
            CallbackType.ItemSubscriptionDeleted, ServerItemCallback(params, subscription_result))
        return subscription_result

    def publish(self, acks=None):
        if acks is None:
            acks = []
        return self.subscription_service.publish(acks)
