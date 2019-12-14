
from opcua import ua
from threading import Lock
from functools import partial
from copy import deepcopy

from socket import INADDR_ANY # IPv4 '0.0.0.0'
IN6ADDR_ANY = '::'
from ipaddress import ip_address
from urllib.parse import urlparse


class LocalDiscoveryService(object):
    REG_EXPIRE_TIMEOUT = 600 # [s] registration expiration (remote servers only).
    MAX_REGISTRATIONS = 32 # [-] Limits the number of simultaneous registrations.

    class ServerDescription(object):
        def __init__(self, appDesc, uaDiscoveryConfiguration=None):
            assert(isinstance(appDesc, ua.uaprotocol_auto.ApplicationDescription))
            self.applicationDescription = appDesc
            self.discoveryConfiguration = uaDiscoveryConfiguration
            self.isExpired = False

    def __init__(self, parent=None):
        self._parent = parent
        self._lock = Lock() # server registration & expiration from different threads.
        self._known_servers = {} # _known_servers[appUri] = ServerDescription instance

    @property
    def thread_loop(self):
        return self._parent.thread_loop

    def find_servers(self, params, sockname=None):
        servers = []
        with self._lock:
            for srvDesc in self._known_servers.values():
                # No Filtering.
                if not params.ServerUris:
                    servers.append(srvDesc.applicationDescription)
                    continue
                # Filter on server uris.
                srv_uri = srvDesc.applicationDescription.ApplicationUri.split(":")
                for uri in params.ServerUris:
                    uri = uri.split(":")
                    if srv_uri[:len(uri)] == uri:
                        servers.append(srvDesc.applicationDescription)
                        break
        try:
            netloc = self.get_netloc_from_endpointurl(
                endpointUrl=getattr(params, 'EndpointUrl', None),
                sockname=sockname
            )
        except Exception:
            raise 
            self.logger.info("Failed to extract EndpointUrl from request parameters")
            return servers
        servers = deepcopy(servers)
        for appDesc in servers:
            appDesc.DiscoveryUrls = [self.replace_inaddr_any(url, netloc) for url in appDesc.DiscoveryUrls]
        return servers

    @staticmethod
    def get_netloc_from_endpointurl(endpointUrl=None, sockname=None):
        # Find the ip:port as seen by our client.
        netloc = None
        if endpointUrl:
            # use ip:port as provided within client request params.
            netloc = urlparse(endpointUrl).netloc or None
        if not netloc and sockname:
            # use ip:port extracted from our local interface.
            netloc = sockname[0] + ":" + str(sockname[1])
        if not netloc:
            raise Exception('Could not extract netloc from endpoint', endpointUrl)
        return netloc

    @staticmethod
    def replace_inaddr_any(urlStr, netloc):
        # If urlStr is '0.0.0.0:port' or '[::]:port', use netloc ip:port.
        parseResult = urlparse(urlStr)
        try:
            hostip = ip_address(parseResult.hostname)
        except ValueError:
            hostip = None
        if not netloc:
            pass
        elif hostip in (ip_address(INADDR_ANY), ip_address(IN6ADDR_ANY)):
            urlStr = parseResult._replace(netloc=netloc).geturl()
        return urlStr

    def add_server_description(self, srvDesc):
        assert(isinstance(srvDesc, LocalDiscoveryService.ServerDescription))
        appUri = srvDesc.applicationDescription.ApplicationUri
        # Prevent DOS by flooding with fake registrations,
        # but always allow existing registrations to renew.
        if appUri in self._known_servers:
            pass
        elif len(self._known_servers) >= self.MAX_REGISTRATIONS:
            raise Exception('Maximum number of registrations reached: {:d}'.format(self.MAX_REGISTRATIONS))
        with self._lock:
            self._known_servers[appUri] = srvDesc

    def _expire_server_description(self, srvDesc):
        """
          Expire a server registration. srvDesc must be a reference
          to the original (registered) description instance.
        """
        assert(isinstance(srvDesc, LocalDiscoveryService.ServerDescription))
        appUri = srvDesc.applicationDescription.ApplicationUri
        # Set expired flag, then check if the registration in _known_servers was
        # renewed. If not renewed before expiration, remove from _known_servers.
        srvDesc.isExpired = True 
        with self._lock:
            if self._known_servers[appUri].isExpired:
                del self._known_servers[appUri]

    def register_server(self, registeredServer, uaDiscoveryConfiguration=None):
        assert(isinstance(registeredServer, ua.uaprotocol_auto.RegisteredServer))
        appDesc = ua.ApplicationDescription()
        appDesc.ApplicationUri = registeredServer.ServerUri
        appDesc.ProductUri = registeredServer.ProductUri
        # FIXME: select name from client locale
        appDesc.ApplicationName = registeredServer.ServerNames[0]
        appDesc.ApplicationType = registeredServer.ServerType
        appDesc.DiscoveryUrls = registeredServer.DiscoveryUrls
        # FIXME: select discovery uri using reachability from client network
        appDesc.GatewayServerUri = registeredServer.GatewayServerUri
        # Create and add ServerDescription, so it is resolved by find_servers().
        srvDesc = LocalDiscoveryService.ServerDescription(appDesc, uaDiscoveryConfiguration)
        self.add_server_description(srvDesc)
        # Auto-expire server registrations after REG_EXPIRE_TIMEOUT seconds.
        expire_cb = partial(self._expire_server_description, srvDesc)
        self.thread_loop.call_later(self.REG_EXPIRE_TIMEOUT, expire_cb)

    def register_server2(self, params):
        return self.register_server(params.Server, params.DiscoveryConfiguration)
