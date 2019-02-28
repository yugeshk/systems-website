"""
KNXIPInterface manages KNX/IP Tunneling or Routing connections.

* It searches for available devices and connects with the corresponding connect method.
* It passes KNX telegrams from the network and
* provides callbacks after having received a telegram from the network.

"""
import ipaddress
from enum import Enum
from platform import system as get_os_name

import netifaces
from xknx.exceptions import XKNXException

from .const import DEFAULT_MCAST_PORT
from .gateway_scanner import GatewayScanFilter, GatewayScanner
from .routing import Routing
from .tunnel import Tunnel


class ConnectionType(Enum):
    """Enum class for different types of KNX/IP Connections."""

    AUTOMATIC = 0
    TUNNELING = 1
    ROUTING = 2


class ConnectionConfig:
    """
    Connection configuration.

    Handles:
    * type of connection:
        * AUTOMATIC for using GatewayScanner for searching and finding KNX/IP devices in the network.
        * TUNNELING connect to a specific KNX/IP tunneling device.
        * ROUTING use KNX/IP multicast routing.
    * local_ip: Local ip of the interface though which KNXIPInterface should connect.
    * gateway_ip: IP of KNX/IP tunneling device.
    * gateway_port: Port of KNX/IP tunneling device.
    * auto_reconnect: Auto reconnect to KNX/IP tunneling device if connection cannot be established.
    * auto_reconnect_wait: Wait n seconds before trying to reconnect to KNX/IP tunneling device.
    * scan_filter: For AUTOMATIC connection, limit scan with the given filter
    * bind_to_multicast_addr: Bind to the multicast address instead of the local IP (ROUTING only)
    """

    # pylint: disable=too-few-public-methods,too-many-instance-attributes

    def __init__(self,
                 connection_type: ConnectionType = ConnectionType.AUTOMATIC,
                 local_ip: str = None,
                 gateway_ip: str = None,
                 gateway_port: int = DEFAULT_MCAST_PORT,
                 auto_reconnect: bool = False,
                 auto_reconnect_wait: int = 3,
                 scan_filter: GatewayScanFilter = GatewayScanFilter(),
                 bind_to_multicast_addr: bool = True):
        """Initialize ConnectionConfig class."""
        # pylint: disable=too-many-arguments
        self.connection_type = connection_type
        self.local_ip = local_ip
        self.gateway_ip = gateway_ip
        self.gateway_port = gateway_port
        self.auto_reconnect = auto_reconnect
        self.auto_reconnect_wait = auto_reconnect_wait
        self.scan_filter = scan_filter
        self.bind_to_multicast_addr = bind_to_multicast_addr

    def __eq__(self, other):
        """Equality for ConnectionConfig class (used in unit tests)."""
        return self.__dict__ == other.__dict__


class KNXIPInterface():
    """Class for managing KNX/IP Tunneling or Routing connections."""

    def __init__(self, xknx, connection_config=ConnectionConfig()):
        """Initialize KNXIPInterface class."""
        self.xknx = xknx
        self.interface = None
        self.connection_config = connection_config

    async def start(self):
        """Start interface. Connecting KNX/IP device with the selected method."""
        if self.connection_config.connection_type == ConnectionType.AUTOMATIC:
            await self.start_automatic(
                self.connection_config.scan_filter)
        elif self.connection_config.connection_type == ConnectionType.ROUTING:
            if self.connection_config.local_ip is not None:
                await self.start_routing(
                    self.connection_config.local_ip,
                    self.connection_config.bind_to_multicast_addr)
            else:
                prefer_routing = self.connection_config.scan_filter
                prefer_routing.routing = True
                await self.start_automatic(prefer_routing)
        elif self.connection_config.connection_type == ConnectionType.TUNNELING:
            await self.start_tunnelling(
                self.connection_config.local_ip,
                self.connection_config.gateway_ip,
                self.connection_config.gateway_port,
                self.connection_config.auto_reconnect,
                self.connection_config.auto_reconnect_wait)

    async def start_automatic(self, scan_filter: GatewayScanFilter):
        """Start GatewayScanner and connect to the found device."""
        gatewayscanner = GatewayScanner(self.xknx, scan_filter=scan_filter)
        gateways = await gatewayscanner.scan()

        if not gateways:
            raise XKNXException("No Gateways found")

        gateway = gateways[0]
        if gateway.supports_tunnelling and \
                scan_filter.routing is not True:
            await self.start_tunnelling(gateway.local_ip,
                                        gateway.ip_addr,
                                        gateway.port,
                                        self.connection_config.auto_reconnect,
                                        self.connection_config.auto_reconnect_wait)
        elif gateway.supports_routing:
            bind_to_multicast_addr = get_os_name() != "Darwin"  # = Mac OS
            await self.start_routing(gateway.local_ip, bind_to_multicast_addr)

    async def start_tunnelling(self, local_ip, gateway_ip, gateway_port,
                               auto_reconnect, auto_reconnect_wait):
        """Start KNX/IP tunnel."""
        # pylint: disable=too-many-arguments
        if local_ip is None:
            local_ip = self.find_local_ip(gateway_ip=gateway_ip)
        self.xknx.logger.debug("Starting tunnel to %s:%s from %s", gateway_ip, gateway_port, local_ip)
        self.interface = Tunnel(
            self.xknx,
            self.xknx.own_address,
            local_ip=local_ip,
            gateway_ip=gateway_ip,
            gateway_port=gateway_port,
            telegram_received_callback=self.telegram_received,
            auto_reconnect=auto_reconnect,
            auto_reconnect_wait=auto_reconnect_wait)
        await self.interface.start()

    async def start_routing(self, local_ip, bind_to_multicast_addr):
        """Start KNX/IP Routing."""
        self.xknx.logger.debug("Starting Routing from %s", local_ip)
        self.interface = Routing(
            self.xknx,
            self.telegram_received,
            local_ip,
            bind_to_multicast_addr)
        await self.interface.start()

    async def stop(self):
        """Stop connected interfae (either Tunneling or Routing)."""
        if self.interface is not None:
            await self.interface.stop()
            self.interface = None

    def telegram_received(self, telegram):
        """Put received telegram into queue. Callback for having received telegram."""
        self.xknx.loop.create_task(
            self.xknx.telegrams.put(telegram))

    async def send_telegram(self, telegram):
        """Send telegram to connected device (either Tunneling or Routing)."""
        await self.interface.send_telegram(telegram)

    def find_local_ip(self, gateway_ip: str) -> str:
        """Find local IP address on same subnet as gateway."""
        def _scan_interfaces(gateway: ipaddress.IPv4Address) -> str:
            """Return local IP address on same subnet as given gateway."""
            for interface in netifaces.interfaces():
                try:
                    af_inet = netifaces.ifaddresses(interface)[netifaces.AF_INET]
                    for link in af_inet:
                        network = ipaddress.IPv4Network((link["addr"],
                                                         link["netmask"]),
                                                        strict=False)
                        if gateway in network:
                            self.xknx.logger.debug("Using interface: %s", interface)
                            return link["addr"]
                except KeyError:
                    self.xknx.logger.debug("Could not find IPv4 address on interface %s", interface)
                    continue

        def _find_default_gateway() -> ipaddress.IPv4Address:
            """Return IP address of default gateway."""
            gws = netifaces.gateways()
            return ipaddress.IPv4Address(gws['default'][netifaces.AF_INET][0])

        gateway = ipaddress.IPv4Address(gateway_ip)
        local_ip = _scan_interfaces(gateway)
        if local_ip is None:
            self.xknx.logger.debug(
                "No interface on same subnet as gateway found. Falling back to default gateway.")
            default_gateway = _find_default_gateway()
            local_ip = _scan_interfaces(default_gateway)
        return local_ip
