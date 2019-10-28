"""
Node objects for Mininet-WiFi.
Nodes provide a simple abstraction for interacting with stations, aps
and controllers. Local nodes are simply one or more processes on the local
machine.
Node: superclass for all (primarily local) network nodes.
Station: a virtual station. By default, a station is simply a shell; commands
    may be sent using Cmd (which waits for output), or using sendCmd(),
    which returns immediately, allowing subsequent monitoring using
    monitor(). Examples of how to run experiments using this
    functionality are provided in the examples/ directory. By default,
    stations share the root file system, but they may also specify private
    directories.
CPULimitedStation: a virtual station whose CPU bandwidth is limited by
    RT or CFS bandwidth limiting.
UserAP: a AP using the user-space switch from the OpenFlow
    reference implementation.
OVSAP: a AP using the Open vSwitch OpenFlow-compatible switch
    implementation (openvswitch.org).
"""

import os
import re
import math
from re import findall
import fileinput
from time import sleep
from distutils.version import StrictVersion
from sys import version_info as py_version_info

from mininet.log import info, error, debug
from mininet.util import (quietRun, errRun, errFail, mountCgroups,
                          numCores, retry, Python3, getincrementaldecoder,
                          moveIntf)
from mininet.node import Node
from mininet.moduledeps import moduleDeps, pathCheck, TUN
from mininet.link import Intf, OVSIntf
from mn_wifi.devices import DeviceRate
from mn_wifi.link import TCWirelessLink, TCLinkWirelessAP,\
    Association, wirelessLink, adhoc, mesh, master, managed, \
    physicalMesh, ITSLink
from mn_wifi.wmediumdConnector import w_server, w_pos, w_txpower, \
    w_gain, w_height, w_cst, wmediumd_mode
from mn_wifi.propagationModels import GetSignalRange, \
    GetPowerGivenRange, propagationModel


class Node_wifi(Node):
    """A virtual network node is simply a shell in a network namespace.
       We communicate with it using pipes."""

    portBase = 0  # Nodes always start with eth0/port0, even in OF 1.0

    def __init__(self, name, inNamespace=True, **params):
        """name: name of node
           inNamespace: in network namespace?
           privateDirs: list of private directory strings or tuples
           params: Node parameters (see config() for details)"""

        # Make sure class actually works
        self.checkSetup()

        self.name = params.get('name', name)
        self.privateDirs = params.get('privateDirs', [])
        self.inNamespace = params.get('inNamespace', inNamespace)

        # Python 3 complains if we don't wait for shell exit
        self.waitExited = params.get('waitExited', Python3)

        # Stash configuration parameters for future reference
        self.params = params

        self.intfs = {}  # dict of port numbers to interfaces
        self.ports = {}  # dict of interfaces to port numbers
        self.wintfs = {}  # dict of wireless port numbers
        self.wports = {}  # dict of interfaces to port numbers
        self.wlanports = -1  # dict of wlan interfaces to port numbers
        self.nameToIntf = {}  # dict of interface names to Intfs

        # Make pylint happy
        (self.shell, self.execed, self.pid, self.stdin, self.stdout,
         self.lastPid, self.lastCmd, self.pollOut) = (
             None, None, None, None, None, None, None, None)
        self.waiting = False
        self.readbuf = ''

        # Incremental decoder for buffered reading
        self.decoder = getincrementaldecoder()

        # Start command interpreter shell
        self.master, self.slave = None, None  # pylint
        self.startShell()
        self.mountPrivateDirs()

    # File descriptor to node mapping support
    # Class variables and methods
    inToNode = {}  # mapping of input fds to nodes
    outToNode = {}  # mapping of output fds to nodes

    def plot(self, position):
        self.params['position'] = position.split(',')
        self.params['range'] = [0]
        self.plotted = True

    def get_wlan(self, intf):
        return self.params['wlan'].index(intf)

    def setMeshMode(self, intf=None, **kwargs):
        if intf:
            kwargs['intf'] = intf
        #wlan = self.get_wlan(kwargs['intf'])
        mesh(self, **kwargs)

    def setPhysicalMeshMode(self, intf=None, **kwargs):
        if intf:
            kwargs['intf'] = intf
        #wlan = self.get_wlan(kwargs['intf'])
        physicalMesh(self, **kwargs)

    def setAdhocMode(self, intf=None, **kwargs):
        if intf:
            kwargs['intf'] = intf
        wlan = self.get_wlan(kwargs['intf'])
        if isinstance(self.wintfs[wlan], adhoc):
            self.cmd('iw dev %s ibss leave' % self.params['wlan'][wlan])
        adhoc(self, **kwargs)

    def setManagedMode(self, intf=None):
        if intf:
            wlan = self.params['wlan'].index(intf)
        else:
            wlan = 0
            intf = self.params['wlan'][wlan]
        if isinstance(self.wintfs[wlan], mesh):
            self.cmd('iw dev %s del' % self.params['wlan'][wlan])
            intf = '%s-wlan%s' % (self, wlan)
            self.params['wlan'][wlan] = intf
        elif isinstance(self.wintfs[wlan], master):
            apconfname = "mn%d_%s.apconf" % (os.getpid(), intf)
            self.cmd('rm %s' % apconfname)
            self.cmd('pkill -f \'%s\'' % apconfname)
        self.cmd('iw dev %s set type managed' % (self.params['wlan'][wlan]))
        managed(self, wlan)

    def setMasterMode(self, intf=None, ssid='new-ssid', **kwargs):
        "set Interface to AP mode"
        if not ssid:
            ssid = self.name + '-ssid'
        wlan = self.get_wlan(intf)
        master(self, wlan, port=wlan)
        intf = self.wintfs[wlan]

        if int(intf.range) == 0:
            intf.range = intf.node.getRange(intf=intf.name)

        intf.ssid = ssid
        self.params['driver'] = 'nl80211'

        aps = [self]
        AccessPoint(aps, 'nl80211', setMaster=True)

    def setOCBMode(self, **params):
        ITSLink(self, **params)

    def wpa_cmd(self, pidfile, intf):
        wpasup_flags = ''
        if 'wpasup_flags' in self.params:
            wpasup_flags = self.params['wpasup_flags']
        return self.cmd("wpa_supplicant -B -Dnl80211 -P %s "
                        "-i %s -c %s.staconf %s"
                        % (pidfile, intf.name, intf.name, wpasup_flags))

    def wpa_pexec(self, pidfile, intf):
        wpasup_flags = ''
        if 'wpasup_flags' in self.params:
            wpasup_flags = self.params['wpasup_flags']
        return self.pexec("wpa_supplicant -B -Dnl80211 -P %s "
                          "-i %s -c %s.staconf %s"
                          % (pidfile, intf, intf.name, wpasup_flags))

    def configLinks(self):
        "Applies channel params and handover"
        from mn_wifi.mobility import mobility
        mobility.configLinks(self)

    def getMAC(self, intf):
        "get Mac Address of any Interface"
        try:
            _macMatchRegex = re.compile(r'..:..:..:..:..:..')
            debug('getting mac address from %s\n' % intf.name)
            macaddr = str(self.pexec('ip addr show %s' % intf.name))
            mac = _macMatchRegex.findall(macaddr)
            debug('\n%s' % mac[0])
            return mac[0]
        except:
            info('Please run sudo mn -c.\n')

    def ifbSupport(self, wlan, ifbID):
        "Support to Intermediate Functional Block (IFB) Devices"
        os.system('ip link set dev ifb%s netns %s' % (ifbID, self.pid))
        self.cmd('ip link set ifb%s up' % ifbID)
        self.cmd('tc qdisc add dev %s handle ffff: ingress' %
                 self.params['wlan'][wlan])
        self.cmd('tc filter add dev %s parent ffff: protocol ip u32 '
                 'match u32 0 0 action mirred egress redirect dev ifb%s'
                 % (self.params['wlan'][wlan], ifbID))
        self.ifb.append(ifbID)

    def getRange(self, intf=None, noiseLevel=0):
        "Get the Signal Range"
        interference_enabled = False
        if wmediumd_mode.mode == w_cst.INTERFERENCE_MODE:
            interference_enabled = True
        wlan = self.get_wlan(intf)
        if noiseLevel != 0:
            GetSignalRange.NOISE_LEVEL = noiseLevel
        if not isinstance(self, Station) and not isinstance(self, Car) \
                and not isinstance(self, AP):
            self = self.params['associatedTo'][0]
        value = GetSignalRange(self, wlan, interference_enabled)

        return int(value.dist)

    def setRange(self, value, intf=None):
        "Set Signal Range"
        from mn_wifi.plot import plot2d
        wlan = self.get_wlan(intf)
        intf = self.wintfs[wlan]
        intf.range = value
        intf.txpower = self.get_txpower_prop_model(0, intf)
        self.setTxPower(intf.txpower, intf=intf.name)
        self.updateGraph()
        self.configLinks()
        if plot2d.fig_exists():
            plot2d.updateCircleRadius(self)

    def updateGraph(self):
        "Update the Graph"
        from mn_wifi.plot import plot2d, plot3d
        cls = plot2d
        if plot3d.is3d:
            cls = plot3d
        if cls.fig_exists():
            cls.updateCircleRadius(self)
            cls.updateLine(self)
            cls.update(self)
            cls.pause()

    def setPosition(self, pos):
        "Set Position"
        self.params['position'] = [float(x) for x in pos.split(',')]
        self.updateGraph()

        if wmediumd_mode.mode == w_cst.INTERFERENCE_MODE:
            self.set_pos_wmediumd(self.params['position'])
        self.configLinks()

    def setAntennaGain(self, value, intf=None, setParam=True):
        "Set Antenna Gain"
        wlan = self.get_wlan(intf)
        self.wintfs[wlan].antennaGain = int(value)
        self.setGainWmediumd(wlan)
        if setParam:
            self.configLinks()

    def setAntennaHeight(self, value, intf=None):
        "Set Antenna Height"
        wlan = self.get_wlan(intf)
        self.params['antennaHeight'][wlan] = int(value)
        self.setHeightWmediumd(wlan)
        self.configLinks()

    def setChannel(self, channel, intf=None):
        "Set Channel"
        from mn_wifi.link import IntfWireless
        if intf:
            wlan = self.get_wlan(intf)
        else:
            wlan = 0
            intf = self.params['wlan'][wlan]
        if isinstance(self, AP) and isinstance(self.wintfs[wlan], mesh):
            IntfWireless.setChannel(self, channel, intf, AP=True)
        else:
            if isinstance(self.wintfs[wlan], mesh):
                mesh(self, channel=channel, intf=intf)
            elif isinstance(self.wintfs[wlan], adhoc):
                self.cmd('iw dev %s ibss leave' % self.params['wlan'][wlan])
                adhoc(self, channel=channel, intf=intf)

    def setTxPower(self, value, intf=None, setParam=True):
        "Set Tx Power"
        wlan = self.get_wlan(intf)
        self.pexec('iw dev %s set txpower fixed %s'
                   % (intf, (int(value) * 100)))
        self.wintfs[wlan].txpower = value
        self.setTXPowerWmediumd(wlan)
        if setParam:
            self.configLinks()

    def get_freq(self, intf):
        "Gets frequency based on channel number"
        channel = int(intf.channel)
        chan_list_2ghz = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11]
        chan_list_5ghz = [36, 40, 44, 48, 52, 56, 60, 64, 100,
                          104, 108, 112, 116, 120, 124, 128, 132,
                          136, 140, 149, 153, 157, 161, 165,
                          169, 171, 172, 173, 174, 175, 176,
                          177, 178, 179, 180, 181, 182, 183, 184,
                          185]
        freq_list_2ghz = [2.412, 2.417, 2.422, 2.427, 2.432, 2.437,
                          2.442, 2.447, 2.452, 2.457, 2.462]
        freq_list_5ghz = [5.18, 5.2, 5.22, 5.24, 5.26, 5.28, 5.30, 5.32,
                          5.50, 5.52, 5.54, 5.56, 5.58, 5.6, 5.62,
                          5.64, 5.66, 5.68, 5.7, 5.745, 5.765, 5.785,
                          5.805, 5.825, 5.845, 5.855, 5.86, 5.865, 5.87,
                          5.875, 5.88, 5.885, 5.89, 5.895, 5.9, 5.905,
                          5.91, 5.915, 5.92, 5.925]
        all_chan = chan_list_2ghz + chan_list_5ghz
        all_freq = freq_list_2ghz + freq_list_5ghz
        if channel in all_chan:
            idx = all_chan.index(channel)
            return all_freq[idx]
        else:
            return 2.412

    def get_rssi(self, node=None, wlan=0, dist=0):
        value = propagationModel(self, node, dist, wlan)
        return float(value.rssi)

    def set_pos_wmediumd(self, pos):
        "Set Position for wmediumd"
        if self.lastpos != pos:
            self.lastpos = pos
            for wlan, intf in enumerate(self.wintfs.values()):
                inc = '%s' % float('0.' + str(wlan))
                w_server.update_pos(w_pos(self.wmIface[wlan],
                    [(float(pos[0])+float(inc)), float(pos[1]), float(pos[2])]), True)

    def setGainWmediumd(self, wlan):
        "Set Antenna Gain for wmediumd"
        if wmediumd_mode.mode == w_cst.INTERFERENCE_MODE:
            gain_ = self.wintfs[wlan].antennaGain
            w_server.update_gain(w_gain(
                self.wmIface[wlan], int(gain_)))

    def setHeightWmediumd(self, wlan):
        "Set Antenna Height for wmediumd"
        if wmediumd_mode.mode == w_cst.INTERFERENCE_MODE:
            height_ = self.wintfs[wlan].antennaHeight
            w_server.update_height(w_height(
                self.wmIface[wlan], int(height_)))

    def setTXPowerWmediumd(self, wlan):
        "Set TxPower for wmediumd"
        if wmediumd_mode.mode == w_cst.INTERFERENCE_MODE:
            txpower_ = self.wintfs[wlan].txpower
            w_server.update_txpower(w_txpower(
                self.wmIface[wlan], int(txpower_)))

    def get_txpower_prop_model(self, wlan, intf):
        "Get Tx Power Given the propagation Model"
        interference_enabled = False
        if wmediumd_mode.mode == w_cst.INTERFERENCE_MODE:
            interference_enabled = True
        value = GetPowerGivenRange(self, wlan, intf.range,
                                   interference_enabled)
        return int(value.txpower)

    def get_txpower(self, intf):
        connected = self.cmd('iw dev %s link | awk \'{print $1}\'' % intf)
        cmd = 'iw dev %s info | grep txpower | awk \'{print $2}\'' % intf
        if connected != 'Not' or isinstance(self, AP):
            try:
                txpower = int(self.cmd(cmd))
            except:
                if isinstance(self, AP):
                    txpower = 14
                else:
                    txpower = 20
            return txpower

    def get_distance_to(self, dst):
        """Get the distance between two nodes
        :param self: source node
        :param dst: destination node"""
        pos_src = self.params['position']
        pos_dst = dst.params['position']
        x = (float(pos_src[0]) - float(pos_dst[0])) ** 2
        y = (float(pos_src[1]) - float(pos_dst[1])) ** 2
        z = (float(pos_src[2]) - float(pos_dst[2])) ** 2
        dist = math.sqrt(x + y + z)
        return round(dist, 2)

    def setAssociation(self, ap, intf=None, **params):
        "Force association to given AP"
        wlan = self.get_wlan(intf)

        dist = 100000
        if 'position' in self.params and 'position' in ap.params:
            dist = self.get_distance_to(ap)

        if dist < ap.params['range'][wlan] or dist == 100000:
            if self.params['associatedTo'][wlan] != ap:
                if self.params['associatedTo'][wlan]:
                    Association.disconnect(self, intf)
                    self.wintfs[0].rssi = 0
                Association.associate_infra(self, ap, wlan, 0)
                wirelessLink(self, wlan, dist)
            else:
                info ('%s is already connected!\n' % ap)
            self.configLinks()
        else:
            info("%s is out of range!\n" % (ap))

    def newWpanPort(self):
        "Return the next port number to allocate."
        self.wpanports += 1
        return self.wpanports

    def newWlanPort(self):
        "Return the next port number to allocate."
        self.wlanports += 1
        return self.wlanports

    def newPort(self):
        "Return the next port number to allocate."
        if len(self.ports) > 0:
            return max(self.ports.values()) + 1
        return self.portBase

    def newWPort(self):
        "Return the next port number to allocate."
        if len(self.wports) > 0:
            return max(self.wports.values()) + 1
        return self.portBase

    def addWAttr(self, intf, port=None):
        """Add an wireless interface.
           intf: interface
           port: port number (optional, typically OpenFlow port number)
           moveIntfFn: function to move interface (optional)"""
        if port is None:
            port = self.newWPort()
        self.wintfs[port] = intf
        self.wports[intf] = port

    def addWIntf(self, intf, port=None):
        """Add an interface.
           intf: interface
           port: port number (optional, typically OpenFlow port number)
           moveIntfFn: function to move interface (optional)"""
        if port is None:
            port = self.newPort()
        self.intfs[port] = intf
        self.ports[intf] = port
        self.nameToIntf[intf.name] = intf
        debug('\n')
        debug('added intf %s (%d) to node %s\n' % (
            intf, port, self.name))

    def connectionsTo(self, node):
        "Return [ intf1, intf2... ] for all intfs that connect self to node."
        # We could optimize this if it is important
        connections = []
        for intf in self.intfList():
            link = intf.link
            if link and link.intf2 != None and link.intf2 != 'wifi':
                node1, node2 = link.intf1.node, link.intf2.node
                if node1 == self and node2 == node:
                    connections += [ (intf, link.intf2) ]
                elif node1 == node and node2 == self:
                    connections += [ (intf, link.intf1) ]
        return connections

    # Convenience and configuration methods
    def setIP(self, ip, prefixLen=8, intf=None, **kwargs):
        """Set the IP address for an interface.
           intf: intf or intf name
           ip: IP address as a string
           prefixLen: prefix length, e.g. 8 for /8 or 16M addrs
           kwargs: any additional arguments for intf.setIP"""
        return self.intf(intf).setIP(ip, prefixLen, **kwargs)

    def setIP6(self, ip, prefixLen=64, intf=None, **kwargs):
        """Set the IP address for an interface.
           intf: intf or intf name
           ip: IP address as a string
           kwargs: any additional arguments for intf.setIP"""
        return self.intf(intf).setIP6(ip, prefixLen, **kwargs)

    def config(self, mac=None, ip=None, ipv6=None,
               defaultRoute=None, lo='up', **_params):
        """Configure Node according to (optional) parameters:
           mac: MAC address for default interface
           ip: IP address for default interface
           ip addr: arbitrary interface configuration
           Subclasses should override this method and call
           the parent class's config(**params)"""
        # If we were overriding this method, we would call
        # the superclass config method here as follows:
        # r = Parent.config( **_params )
        r = {}
        if not isinstance(self, Station) and not isinstance(self, Car):
            self.setParam(r, 'setMAC', mac=mac)
        self.setParam(r, 'setIP', ip=ip)
        self.setParam(r, 'setIPv6', ipv6=ipv6)
        self.setParam(r, 'setDefaultRoute', defaultRoute=defaultRoute)

        # This should be examined
        self.cmd('ip link set lo ' + lo)
        return r

    def configDefault(self, **moreParams):
        "Configure with default parameters"
        self.params.update(moreParams)
        self.config(**self.params)

    def __repr__(self):
        "More informative string representation"
        intfs = (','.join([ '%s:%s' % (i.name, i.IP())
                            for i in self.intfList() ]))
        return '<%s %s: %s pid=%s> ' % (
            self.__class__.__name__, self.name, intfs, self.pid)

    def __str__(self):
        "Abbreviated string representation"
        return self.name

    # Automatic class setup support
    isSetup = False

    @classmethod
    def setup(cls):
        "Make sure our class dependencies are available"
        pathCheck('mnexec', 'ip addr', moduleName='Mininet')

    def stop_(self):
        "Stops hostapd"
        from mn_wifi.plot import plot2d
        process = 'mn%d_%s' % (os.getpid(), self.name)
        os.system('pkill -f \'hostapd -B %s\'' % process)
        if plot2d.fig_exists():
            plot2d.setCircleColor(self, 'w')

    def start_(self):
        "Starts hostapd"
        from mn_wifi.plot import plot2d
        process = 'mn%d_%s' % (os.getpid(), self.name)
        os.system('hostapd -B %s-wlan1.apconf' % process)
        if plot2d.fig_exists():
            plot2d.setCircleColor(self, 'b')

    def hide(self):
        for wlan in self.params['wlan']:
            self.cmd('ip link set %s down' % wlan)
        from mn_wifi.plot import plot2d
        if plot2d.fig_exists():
            plot2d.hideNode(self)

    def show(self):
        for wlan in self.params['wlan']:
            self.cmd('ip link set %s up' % wlan)
        from mn_wifi.plot import plot2d
        if plot2d.fig_exists():
            plot2d.showNode(self)


class Station(Node_wifi):
    "A station is simply a Node"
    pass


class Car(Node_wifi):
    "A car is simply a Node"
    pass


class CPULimitedStation( Station ):

    "CPU limited host"

    def __init__( self, name, sched='cfs', **kwargs ):
        Station.__init__( self, name, **kwargs )
        # Initialize class if necessary
        if not CPULimitedStation.inited:
            CPULimitedStation.init()
        # Create a cgroup and move shell into it
        self.cgroup = 'cpu,cpuacct,cpuset:/' + self.name
        errFail( 'cgcreate -g ' + self.cgroup )
        # We don't add ourselves to a cpuset because you must
        # specify the cpu and memory placement first
        errFail( 'cgclassify -g cpu,cpuacct:/%s %s' % ( self.name, self.pid ) )
        # BL: Setting the correct period/quota is tricky, particularly
        # for RT. RT allows very small quotas, but the overhead
        # seems to be high. CFS has a mininimum quota of 1 ms, but
        # still does better with larger period values.
        self.period_us = kwargs.get( 'period_us', 100000 )
        self.sched = sched
        if sched == 'rt':
            self.checkRtGroupSched()
            self.rtprio = 20

    def cgroupSet( self, param, value, resource='cpu' ):
        "Set a cgroup parameter and return its value"
        cmd = 'cgset -r %s.%s=%s /%s' % (
            resource, param, value, self.name )
        quietRun( cmd )
        nvalue = int( self.cgroupGet( param, resource ) )
        if nvalue != value:
            error( '*** error: cgroupSet: %s set to %s instead of %s\n'
                   % ( param, nvalue, value ) )
        return nvalue

    def cgroupGet( self, param, resource='cpu' ):
        "Return value of cgroup parameter"
        cmd = 'cgget -r %s.%s /%s' % (
            resource, param, self.name )
        return int( quietRun( cmd ).split()[ -1 ] )

    def cgroupDel( self ):
        "Clean up our cgroup"
        # info( '*** deleting cgroup', self.cgroup, '\n' )
        _out, _err, exitcode = errRun( 'cgdelete -r ' + self.cgroup )
        # Sometimes cgdelete returns a resource busy error but still
        # deletes the group; next attempt will give "no such file"
        return exitcode == 0 or ( 'no such file' in _err.lower() )

    def popen( self, *args, **kwargs ):
        """Return a Popen() object in node's namespace
           args: Popen() args, single list, or string
           kwargs: Popen() keyword args"""
        # Tell mnexec to execute command in our cgroup
        mncmd = kwargs.pop( 'mncmd', [ 'mnexec', '-g', self.name,
                                       '-da', str( self.pid ) ] )
        # if our cgroup is not given any cpu time,
        # we cannot assign the RR Scheduler.
        if self.sched == 'rt':
            if int( self.cgroupGet( 'rt_runtime_us', 'cpu' ) ) <= 0:
                mncmd += [ '-r', str( self.rtprio ) ]
            else:
                debug( '*** error: not enough cpu time available for %s.' %
                       self.name, 'Using cfs scheduler for subprocess\n' )
        return Station.popen( self, *args, mncmd=mncmd, **kwargs )

    def cleanup( self ):
        "Clean up Node, then clean up our cgroup"
        super( CPULimitedStation, self ).cleanup()
        retry( retries=3, delaySecs=.1, fn=self.cgroupDel )

    _rtGroupSched = False   # internal class var: Is CONFIG_RT_GROUP_SCHED set?

    @classmethod
    def checkRtGroupSched( cls ):
        "Check (Ubuntu,Debian) kernel config for CONFIG_RT_GROUP_SCHED for RT"
        if not cls._rtGroupSched:
            release = quietRun( 'uname -r' ).strip('\r\n')
            output = quietRun( 'grep CONFIG_RT_GROUP_SCHED /boot/config-%s' %
                               release )
            if output == '# CONFIG_RT_GROUP_SCHED is not set\n':
                error( '\n*** error: please enable RT_GROUP_SCHED '
                       'in your kernel\n' )
                exit( 1 )
            cls._rtGroupSched = True

    def chrt( self ):
        "Set RT scheduling priority"
        quietRun( 'chrt -p %s %s' % ( self.rtprio, self.pid ) )
        result = quietRun( 'chrt -p %s' % self.pid )
        firstline = result.split( '\n' )[ 0 ]
        lastword = firstline.split( ' ' )[ -1 ]
        if lastword != 'SCHED_RR':
            error( '*** error: could not assign SCHED_RR to %s\n' % self.name )
        return lastword

    def rtInfo( self, f ):
        "Internal method: return parameters for RT bandwidth"
        pstr, qstr = 'rt_period_us', 'rt_runtime_us'
        # RT uses wall clock time for period and quota
        quota = int( self.period_us * f )
        return pstr, qstr, self.period_us, quota

    def cfsInfo( self, f ):
        "Internal method: return parameters for CFS bandwidth"
        pstr, qstr = 'cfs_period_us', 'cfs_quota_us'
        # CFS uses wall clock time for period and CPU time for quota.
        quota = int( self.period_us * f * numCores() )
        period = self.period_us
        if f > 0 and quota < 1000:
            debug( '(cfsInfo: increasing default period) ' )
            quota = 1000
            period = int( quota / f / numCores() )
        # Reset to unlimited on negative quota
        if quota < 0:
            quota = -1
        return pstr, qstr, period, quota

    # BL comment:
    # This may not be the right API,
    # since it doesn't specify CPU bandwidth in "absolute"
    # units the way link bandwidth is specified.
    # We should use MIPS or SPECINT or something instead.
    # Alternatively, we should change from system fraction
    # to CPU seconds per second, essentially assuming that
    # all CPUs are the same.

    def setCPUFrac( self, f, sched=None ):
        """Set overall CPU fraction for this station
           f: CPU bandwidth limit (positive fraction, or -1 for cfs unlimited)
           sched: 'rt' or 'cfs'
           Note 'cfs' requires CONFIG_CFS_BANDWIDTH,
           and 'rt' requires CONFIG_RT_GROUP_SCHED"""
        if not sched:
            sched = self.sched
        if sched == 'rt':
            if not f or f < 0:
                raise Exception( 'Please set a positive CPU fraction'
                                 ' for sched=rt\n' )
            pstr, qstr, period, quota = self.rtInfo( f )
        elif sched == 'cfs':
            pstr, qstr, period, quota = self.cfsInfo( f )
        else:
            return
        # Set cgroup's period and quota
        setPeriod = self.cgroupSet( pstr, period )
        setQuota = self.cgroupSet( qstr, quota )
        if sched == 'rt':
            # Set RT priority if necessary
            sched = self.chrt()
        info( '(%s %d/%dus) ' % ( sched, setQuota, setPeriod ) )

    def setCPUs( self, cores, mems=0 ):
        "Specify (real) cores that our cgroup can run on"
        if not cores:
            return
        if isinstance( cores, list ):
            cores = ','.join( [ str( c ) for c in cores ] )
        self.cgroupSet( resource='cpuset', param='cpus',
                        value=cores )
        # Memory placement is probably not relevant, but we
        # must specify it anyway
        self.cgroupSet( resource='cpuset', param='mems',
                        value=mems)
        # We have to do this here after we've specified
        # cpus and mems
        errFail( 'cgclassify -g cpuset:/%s %s' % (
            self.name, self.pid ) )

    def config( self, cpu=-1, cores=None, **params ):
        """cpu: desired overall system CPU fraction
           cores: (real) core(s) this station can run on
           params: parameters for Node.config()"""
        r = Node.config( self, **params )
        # Was considering cpu={'cpu': cpu , 'sched': sched}, but
        # that seems redundant
        self.setParam( r, 'setCPUFrac', cpu=cpu )
        self.setParam( r, 'setCPUs', cores=cores )
        return r

    inited = False

    @classmethod
    def init( cls ):
        "Initialization for CPULimitedStation class"
        mountCgroups()
        cls.inited = True


class AP(Node_wifi):
    """A Switch is a Node that is running (or has execed?)
       an OpenFlow switch."""
    portBase = 1  # Switches start with port 1 in OpenFlow
    dpidLen = 16  # digits in dpid passed to switch

    def __init__(self, name, dpid=None, opts='', listenPort=None, **params):
        """dpid: dpid hex string (or None to derive from name, e.g. s1 -> 1)
           opts: additional switch options
           listenPort: port to listen on for dpctl connections"""
        Node_wifi.__init__(self, name, **params)
        self.dpid = self.defaultDpid(dpid)
        self.opts = opts
        self.listenPort = listenPort
        if not self.inNamespace:
            self.controlIntf = Intf('lo', self, port=0)

    def defaultDpid(self, dpid=None):
        "Return correctly formatted dpid from dpid or switch name (s1 -> 1)"
        if dpid:
            # Remove any colons and make sure it's a good hex number
            if py_version_info < (3, 0):
                dpid = dpid.replace(':', '')
            else:
                dpid = dpid.translate(str.maketrans('', '', ':'))
            assert len(dpid) <= self.dpidLen and int(dpid, 16) >= 0
            return '0' * (self.dpidLen - len(dpid)) + dpid
        else:
            # Use hex of the first number in the switch name
            nums = re.findall(r'\d+', self.name)
            if nums:
                dpid = hex(int(nums[ 0 ]))[ 2: ]
            else:
                raise Exception('Unable to derive default datapath ID - '
                                'please either specify a dpid or use a '
                                'canonical ap name such as ap23.')
            return '1' + '0' * (self.dpidLen -1 - len(dpid)) + dpid

    def defaultIntf(self):
        "Return control interface"
        if self.controlIntf:
            return self.controlIntf
        else:
            return Node_wifi.defaultIntf(self)

    def sendCmd(self, *cmd, **kwargs):
        """Send command to Node.
           cmd: string"""
        kwargs.setdefault('printPid', False)
        if not self.execed:
            return Node_wifi.sendCmd(self, *cmd, **kwargs)
        else:
            error('*** Error: %s has execed and cannot accept commands' %
                  self.name)

    def connected(self):
        "Is the switch connected to a controller? (override this method)"
        # Assume that we are connected by default to whatever we need to
        # be connected to. This should be overridden by any OpenFlow
        # switch, but not by a standalone bridge.
        debug('Assuming', repr(self), 'is connected to a controller\n')
        return True

    def stop(self, deleteIntfs=True):
        """Stop switch
           deleteIntfs: delete interfaces? (True)"""
        if deleteIntfs:
            self.deleteIntfs()

    def __repr__(self):
        "More informative string representation"
        intfs = (','.join([ '%s:%s' % (i.name, i.IP())
                            for i in self.intfList() ]))
        return '<%s %s: %s pid=%s> ' % (
            self.__class__.__name__, self.name, intfs, self.pid)


class AccessPoint(AP):
    """An AccessPoint is a Switch equipped with wireless interface that is
    running (or has execed?) an OpenFlow switch."""

    write_mac = False

    def __init__(self, aps, driver, setMaster=False):
        'configure ap'
        self.name = ''
        self.check_nm(aps, driver, setMaster)
        self.configure(aps)

    def check_nm(self, aps, driver, setMaster):
        for ap in aps:
            for wlan in range(len(ap.params['wlan'])):
                if not setMaster:
                    self.configAP(ap, wlan)

            if 'vssids' in ap.params:
                for i in range(1, ap.params['vssids'] + 1):
                    iface = '%s-%s' % (ap.wintfs[0], i)
                    ap.params['wlan'].append(iface)
                    master(ap, i)
            ap.params['driver'] = driver

        self.restartNetworkManager()

    def configure(self, aps):
        """Configure APs
        :param aps: list of access points"""
        for ap in aps:
            if 'link' not in ap.params:
                if 'phywlan' in ap.params:
                    for wlan, intf in enumerate(ap.wintfs.values()):
                        self.setConfig(intf, aps, wlan)

                for wlan, intf in enumerate(ap.wintfs.values()):
                    self.setConfig(intf, aps, wlan)

    def setConfig(self, intf, aplist=None, wlan=0):
        """Configure AP
        :param ap: ap node
        :param aplist: list of aps
        :param wlan: wlan id"""
        if intf.ssid:
            if intf.encrypt and 'config' not in intf.node.params:
                if intf.encrypt == 'wpa':
                    intf.node.auth_algs = 1
                    if 'ieee80211r' in intf.node.params \
                            and intf.node.params['ieee80211r'] == 'yes':
                        intf.node.wpa_key_mgmt = 'FT-EAP'
                    else:
                        intf.node.wpa_key_mgmt = 'WPA-EAP'
                    intf.node.rsn_pairwise = 'TKIP CCMP'
                    intf.node.wpa_passphrase = intf.passwd
                elif intf.encrypt == 'wpa2' \
                        or intf.encrypt == 'wpa3':
                    intf.node.auth_algs = 1
                    if 'ieee80211r' in intf.node.params \
                            and intf.node.params['ieee80211r'] == 'yes' \
                            and not intf.authmode:
                        intf.node.wpa_key_mgmt = 'FT-PSK'
                    elif intf.authmode == '8021x':
                        intf.node.wpa_key_mgmt = 'WPA-EAP'
                    else:
                        intf.node.wpa_key_mgmt = 'WPA-PSK'
                    intf.node.rsn_pairwise = 'CCMP'
                    if not intf.authmode:
                        intf.node.wpa_passphrase = intf.passwd
                elif intf.encrypt == 'wep':
                    intf.node.auth_algs = 2
                    intf.node.wep_key0 = intf.passwd

            if intf.mode == 'adhoc':
                adhoc(intf.node, wlan)
            else:
                self.setHostapdConfig(intf, wlan, aplist)

    def get_mode_config(self, intf):
        cmd = ''
        if intf.mode == 'n':
            if 'band' in intf.node.params:
                if intf.node.params['band'] == '5' or intf.node.params['band'] == 5:
                    cmd = cmd + ("\nhw_mode=a")
                else:
                    cmd = cmd + ("\nhw_mode=g")
                intf.node.params.pop("band", None)
            else:
                cmd = cmd + ("\nhw_mode=g")
        elif intf.mode == 'a':
            cmd = cmd + ('\ncountry_code=US')
            cmd = cmd + ("\nhw_mode=%s" % intf.mode)
        elif intf.mode == 'ac':
            cmd = cmd + ('\ncountry_code=US')
            cmd = cmd + ("\nhw_mode=a")
        elif intf.mode == 'ax':
            cmd = cmd + ('\ncountry_code=US')
            cmd = cmd + ("\nhw_mode=a")
            cmd = cmd + ("\nieee80211ax=1")
        else:
            cmd = cmd + ("\nhw_mode=%s" % intf.mode)
        return cmd

    def setHostapdConfig(self, intf, wlan, aplist):
        "Set hostapd config"
        cmd = ("echo \'")
        args = ['max_num_sta', 'beacon_int', 'rsn_preauth']

        if 'phywlan' in intf.node.params:
            cmd = cmd + ("interface=%s" % intf.node.params.get('phywlan'))
        else:
            cmd = cmd + ("interface=%s" % intf.name)

        cmd = cmd + ("\ndriver=%s" % intf.node.params['driver'])
        cmd = cmd + ("\nssid=%s" % intf.ssid)
        cmd = cmd + ('\nwds_sta=1')

        cmd += self.get_mode_config(intf)  # get mode

        cmd = cmd + ("\nchannel=%s" % intf.channel)

        for arg in args:
            if arg in intf.node.params:
                cmd = cmd + ('\n%s=%s' % (arg, intf.node.params[arg]))

        if 'ht_capab' in intf.node.params:
            cmd = cmd + ('\nht_capab=%s' % intf.node.params['ht_capab'])
        if 'beacon_int' in intf.node.params:
            cmd = cmd + ('\nbeacon_int=%s' % intf.node.params['beacon_int'])
        if 'isolate_clients' in intf.node.params:
            cmd = cmd + ('\nap_isolate=1')
        if 'config' in intf.node.params:
            config = intf.node.params['config']
            if config is not []:
                config = intf.node.params['config'].split(',')
                # ap.params.pop("config", None)
                for conf in config:
                    cmd = cmd + "\n" + conf
        else:
            if intf.authmode == '8021x':
                cmd = cmd + ("\nieee8021x=1")
                cmd = cmd + ("\nwpa_key_mgmt=WPA-EAP")
                if intf.encrypt:
                    cmd = cmd + ("\nauth_algs=%s" % intf.node.auth_algs)
                    cmd = cmd + ("\nwpa=2")
                cmd = cmd + ('\neap_server=0')
                cmd = cmd + ('\neapol_version=2')

                if not intf.radius_server:
                    intf.radius_server = '127.0.0.1'
                cmd = cmd + ("\nwpa_pairwise=TKIP CCMP")
                cmd = cmd + ("\neapol_key_index_workaround=0")
                cmd = cmd + ("\nown_ip_addr=%s" % intf.radius_server)
                cmd = cmd + ("\nnas_identifier=%s.example.com" % intf.node.name)
                cmd = cmd + ("\nauth_server_addr=%s" % intf.radius_server)
                cmd = cmd + ("\nauth_server_port=1812")
                if 'shared_secret' not in intf.node.params:
                    intf.node.params['shared_secret'] = 'secret'
                cmd = cmd + ("\nauth_server_shared_secret=%s"
                             % intf.node.params['shared_secret'])
            else:
                if intf.encrypt:
                    if 'wpa' in intf.encrypt:
                        cmd = cmd + ("\nauth_algs=%s" % intf.node.auth_algs)
                        if intf.encrypt == 'wpa2' \
                                or intf.encrypt == 'wpa3':
                            cmd = cmd + ("\nwpa=2")
                        else:
                            cmd = cmd + ("\nwpa=1")
                        if intf.encrypt == 'wpa3':
                            cmd = cmd + ("\nwpa_key_mgmt=WPA-PSK SAE")
                        else:
                            cmd = cmd + ("\nwpa_key_mgmt=%s" % intf.node.wpa_key_mgmt)
                        cmd = cmd + ("\nwpa_pairwise=%s" % intf.node.rsn_pairwise)
                        cmd = cmd + ("\nwpa_passphrase=%s" % intf.node.wpa_passphrase)
                    elif intf.encrypt == 'wep':
                        cmd = cmd + ("\nauth_algs=%s" % intf.node.auth_algs)
                        cmd = cmd + ("\nwep_default_key=%s" % 0)
                        cmd = cmd + self.verifyWepKey(intf.node.wep_key0)

                if intf.mode == 'ac':
                    cmd = cmd + ("\nwmm_enabled=1")
                    cmd = cmd + ("\nieee80211ac=1")
                elif intf.mode == 'n':
                    cmd = cmd + ("\nwmm_enabled=1")
                    cmd = cmd + ("\nieee80211n=1")

                if 'ieee80211r' in intf.node.params and \
                                intf.node.params['ieee80211r'] is 'yes':
                    if 'mobility_domain' in intf.node.params:
                        cmd = cmd + ("\nmobility_domain=%s" %
                                     intf.node.params['mobility_domain'])
                        # cmd = cmd + ("\nown_ip_addr=127.0.0.1")
                        cmd = cmd + ("\nnas_identifier=%s.example.com"
                                     % intf.node.name)
                        for apref in aplist:
                            cmd = cmd + ('\nr0kh=%s r0kh-%s.example.com '
                                         '000102030405060708090a0b0c0d0e0f'
                                         % (apref.wintfs[wlan].mac,
                                            aplist.index(apref)))
                            cmd = cmd + ('\nr1kh=%s %s '
                                         '000102030405060708090a0b0c0d0e0f'
                                         % (apref.wintfs[wlan].mac,
                                            apref.wintfs[wlan].mac))
                        #cmd = cmd + ('\nrsn_preauth=1')
                        cmd = cmd + ('\npmk_r1_push=1')
                        cmd = cmd + ('\nft_over_ds=1')
                        cmd = cmd + ('\nft_psk_generate_local=1')
        if 'vssids' in intf.node.params:
            for i in range(1, intf.node.params['vssids']+1):
                intf.txpower = intf.node.wintfs[0].txpower
                intf.antennaGain = intf.node.wintfs[0].antennaGain
                intf.antennaHeight = intf.node.wintfs[0].antennaHeight
                ssid = intf.node.wintfs[i].ssid
                cmd = cmd + ('\n')
                cmd = cmd + ("\nbss=%s" % intf.node.params['wlan'][i])
                cmd = cmd + ("\nssid=%s" % ssid)
                if intf.node.wintfs[wlan].encrypt:
                    if intf.node.wintfs[i].encrypt == 'wep':
                        cmd = cmd + ("\nauth_algs=%s" % intf.node.auth_algs)
                        cmd = cmd + ("\nwep_default_key=0")
                        cmd = cmd + self.verifyWepKey(intf.node.wep_key0)
        cmd = cmd + ("\nctrl_interface=/var/run/hostapd")
        cmd = cmd + ("\nctrl_interface_group=0")
        self.APConfigFile(cmd, intf)

        if 'vssids' in intf.node.params:
            TCLinkWirelessAP(intf.node, intfName=intf.name)
        self.setIPMAC(intf)

        if 'phywlan' in intf.node.params:
            intf = intf.node.params['phywlan']
            intf.node.params.pop('phywlan', None)

        if wmediumd_mode.mode == 4:
            self.setBw(intf, wlan)

        intf.freq = intf.node.get_freq(intf)

    def setBw(self, intf, wlan):
        "Set bw"
        if 'bw' in intf.node.params:
            bw = intf.node.params['bw'][wlan]
        else:
            bw = self.getRate(intf)
        intf.node.cmd("tc qdisc replace dev %s \
                root handle 2: tbf rate %sMbit burst 15000 "
                 "latency 1ms" % (intf, bw))
        # Reordering packets
        intf.node.cmd('tc qdisc add dev %s parent 2:1 handle 10: '
                 'pfifo limit 1000' % (intf))

    def getRate(self, intf):
        if 'model' in intf.node.params:
            return DeviceRate(intf).rate
        else:
            mode = intf.mode

            if mode == 'a':
                rate = 54
            elif mode == 'b':
                rate = 11
            elif mode == 'g':
                rate = 54
            elif mode == 'n':
                rate = 300
            elif mode == 'ac':
                rate = 600
            else:
                rate = 54
            return rate

    def verifyWepKey(self, wep_key0):
        "Check WEP key"
        if len(wep_key0) == 10 or len(wep_key0) == 26 or len(wep_key0) == 32:
            cmd = ("\nwep_key0=%s" % wep_key0)
        elif len(wep_key0) == 5 or len(wep_key0) == 13 or len(wep_key0) == 16:
            cmd = ("\nwep_key0=\"%s\"" % wep_key0)
        else:
            info("Warning! Wep Key is wrong!\n")
            exit(1)
        return cmd

    _macMatchRegex = re.compile(r'..:..:..:..:..:..')

    def setIPMAC(self, intf):
        if intf.mac:
            intf.setMAC(intf.mac)
        else:
            intf.mac = intf.node.getMAC(intf)

        if intf.mac:
            self.checkNetworkManager(intf)

        if 'inNamespace' in intf.node.params and 'ip' in intf.node.params:
            intf.node.setIP(intf.node.params['ip'], intf=intf.name)

    def restartNetworkManager(self):
        """Restart network manager if the mac address of the AP
        is not included at /etc/NetworkManager/NetworkManager.conf"""
        nms = 'network-manager'
        nm = 'NetworkManager'
        nm_is_running = os.system('service %s status 2>&1 | grep '
                                  '-ic running >/dev/null 2>&1' % nms)
        if self.write_mac and nm_is_running != 256:
            info('Mac Address(es) of AP(s) is(are) being added into '
                 '/etc/%s/%s.conf\n' % (nm, nm))
            info('Restarting %s...\n' % nms)
            os.system('service %s restart' % nms)
        self.write_mac = False

    def configAP(self, node, wlan):
        TCLinkWirelessAP(node)
        master(node, wlan, port=wlan)
        #cls.links.append(link)
        if 'phywlan' in node.params:
            TCLinkWirelessAP(node, intfName1=node.params['phywlan'])
            master(node)

    def checkNetworkManager(self, intf):
        "add mac address into /etc/NetworkManager/NetworkManager.conf"
        mac = intf.mac
        nm = 'NetworkManager'
        unmanaged = 'unmanaged-devices'
        unmatch = ""
        if os.path.exists('/etc/%s/%s.conf' % (nm, nm)):
            if os.path.isfile('/etc/%s/%s.conf' % (nm, nm)):
                self.resultIface = open('/etc/%s/%s.conf' % (nm, nm))
                lines = self.resultIface

            isNew = True
            for n in lines:
                if unmanaged in n:
                    unmatch = n
                    echo = n
                    echo.replace(" ", "")
                    echo = echo[:-1] + ";"
                    isNew = False
            if isNew:
                os.system("echo '#' >> /etc/%s/%s.conf" % (nm, nm))
                echo = "[keyfile]\n%s=" % unmanaged

            if mac not in unmatch:
                echo = echo + "mac:" + mac + ';'
                for line in fileinput.input('/etc/%s/%s.conf' % (nm, nm),
                                            inplace=1):
                    if isNew:
                        self.write_to_file(line, unmatch, echo, '#')
                    else:
                        self.write_to_file(line, unmatch, echo, unmanaged)
                self.write_mac = True

    def write_to_file(self, line, unmatch, echo, str_):
        if line.__contains__(str_):
            print(line.replace(unmatch, echo))
        else:
            print(line.rstrip())

    def APConfigFile(self, cmd, intf):
        "run an Access Point and create the config file"
        if 'phywlan' in intf.node.params:
            intf = intf.node.params['phywlan']
            intf.node.cmd('ip link set %s down' % intf)
            intf.node.cmd('ip link set %s up' % intf)
        apconfname = "mn%d_%s.apconf" % (os.getpid(), intf)
        content = cmd + ("\' > %s" % apconfname)
        intf.node.cmd(content)
        cmd = self.get_hostapd_cmd(intf)
        try:
            intf.node.cmd(cmd)
            if int(intf.channel) == 0 or intf.channel == 'acs_survey':
                info("*** Waiting for ACS... It takes 10 seconds.\n")
                sleep(10)
        except:
            info("*** error with hostapd. Please, run sudo mn -c in order " \
            "to fix it or check if hostapd is working properly in " \
            "your system.")
            exit(1)

    def get_hostapd_cmd(self, intf):
        apconfname = "mn%d_%s.apconf" % (os.getpid(), intf.name)
        hostapd_flags = ''
        if 'hostapd_flags' in intf.node.params:
            hostapd_flags = intf.node.params['hostapd_flags']
        cmd = ("hostapd -B %s %s" % (apconfname, hostapd_flags))
        return cmd


class UserAP(AP):
    "User-space AP."

    dpidLen = 12

    def __init__(self, name, dpopts='--no-slicing', **kwargs):
        """Init.
           name: name for the switch
           dpopts: additional arguments to ofdatapath (--no-slicing)"""
        AP.__init__(self, name, **kwargs)
        pathCheck('ofdatapath', 'ofprotocol',
                  moduleName='the OpenFlow reference user switch' +
                  '(openflow.org)')
        if self.listenPort:
            self.opts += ' --listen=ptcp:%i ' % self.listenPort
        else:
            self.opts += ' --listen=punix:/tmp/%s.listen' % self.name
        self.dpopts = dpopts

    @classmethod
    def setup(cls):
        "Ensure any dependencies are loaded; if not, try to load them."
        if not os.path.exists('/dev/net/tun'):
            moduleDeps(add=TUN)

    def dpctl(self, *args):
        "Run dpctl command"
        listenAddr = None
        if not self.listenPort:
            listenAddr = 'unix:/tmp/%s.listen' % self.name
        else:
            listenAddr = 'tcp:127.0.0.1:%i' % self.listenPort
        return self.cmd('dpctl ' + ' '.join(args) + ' ' + listenAddr)

    def connected(self):
        "Is the ap connected to a controller?"
        status = self.dpctl('status')
        return ('remote.is-connected=true' in status and
                'local.is-connected=true' in status)

    @staticmethod
    def TCReapply(intf):
        """Unfortunately user switch and Mininet are fighting
           over tc queuing disciplines. To resolve the conflict,
           we re-create the user switch's configuration, but as a
           leaf of the TCIntf-created configuration."""
        if isinstance(intf, TCWirelessLink):
            ifspeed = 10000000000  # 10 Gbps
            minspeed = ifspeed * 0.001

            res = intf.config(**intf.params)

            if res is None:  # link may not have TC parameters
                return

            # Re-add qdisc, root, and default classes user switch created, but
            # with new parent, as setup by Mininet's TCIntf
            parent = res['parent']
            intf.tc("%s qdisc add dev %s " + parent +
                    " handle 1: htb default 0xfffe")
            intf.tc("%s class add dev %s classid 1:0xffff parent 1: htb rate "
                    + str(ifspeed))
            intf.tc("%s class add dev %s classid 1:0xfffe parent 1:0xffff " +
                    "htb rate " + str(minspeed) + " ceil " + str(ifspeed))

    def start(self, controllers):
        """Start OpenFlow reference user datapath.
           Log to /tmp/sN-{ofd,ofp}.log.
           controllers: list of controller objects"""
        # Add controllers
        clist = ','.join([ 'tcp:%s:%d' % (c.IP(), c.port)
                           for c in controllers ])
        ofdlog = '/tmp/' + self.name + '-ofd.log'
        ofplog = '/tmp/' + self.name + '-ofp.log'
        intfs = [ str(i) for i in self.intfList() if not i.IP() ]

        self.cmd('ofdatapath -i ' + ','.join(intfs) +
                 ' punix:/tmp/' + self.name + ' -d %s ' % self.dpid +
                 self.dpopts +
                 ' 1> ' + ofdlog + ' 2> ' + ofdlog + ' &')
        self.cmd('ofprotocol unix:/tmp/' + self.name +
                 ' ' + clist +
                 ' --fail=closed ' + self.opts +
                 ' 1> ' + ofplog + ' 2>' + ofplog + ' &')
        if "no-slicing" not in self.dpopts:
            # Only TCReapply if slicing is enable
            sleep(1)  # Allow ofdatapath to start before re-arranging qdisc's
            for intf in self.intfList():
                if not intf.IP():
                    self.TCReapply(intf)

    def stop(self, deleteIntfs=True):
        """Stop OpenFlow reference user datapath.
           deleteIntfs: delete interfaces? (True)"""
        # self.cmd('kill %ofdatapath')
        # self.cmd('kill %ofprotocol')
        # super(UserAP, self).stop(deleteIntfs)


class OVSAP(AP):
    "Open vSwitch AP. Depends on ovs-vsctl."

    def __init__(self, name, failMode='secure', datapath='kernel',
                 inband=False, protocols=None,
                 reconnectms=1000, stp=False, batch=False, **params):
        """name: name for switch
           failMode: controller loss behavior (secure|open)
           datapath: userspace or kernel mode (kernel|user)
           inband: use in-band control (False)
           protocols: use specific OpenFlow version(s) (e.g. OpenFlow13)
                      Unspecified (or old OVS version) uses OVS default
           reconnectms: max reconnect timeout in ms (0/None for default)
           stp: enable STP (False, requires failMode=standalone)
           batch: enable batch startup (False)"""
        AP.__init__(self, name, **params)
        self.failMode = failMode
        self.datapath = datapath
        self.inband = inband
        self.protocols = protocols
        self.reconnectms = reconnectms
        self.stp = stp
        self._uuids = []  # controller UUIDs
        self.batch = batch
        self.commands = []  # saved commands for batch startup

    @classmethod
    def setup(cls):
        "Make sure Open vSwitch is installed and working"
        pathCheck('ovs-vsctl',
                  moduleName='Open vSwitch (openvswitch.org)')
        # This should no longer be needed, and it breaks
        # with OVS 1.7 which has renamed the kernel module:
        #  moduleDeps( subtract=OF_KMOD, add=OVS_KMOD )
        out, err, exitcode = errRun('ovs-vsctl -t 1 show')
        if exitcode:
            error(out + err +
                  'ovs-vsctl exited with code %d\n' % exitcode +
                  '*** Error connecting to ovs-db with ovs-vsctl\n'
                  'Make sure that Open vSwitch is installed, '
                  'that ovsdb-server is running, and that\n'
                  '"ovs-vsctl show" works correctly.\n'
                  'You may wish to try '
                  '"service openvswitch-switch start".\n')
            exit(1)
        version = quietRun('ovs-vsctl --version')
        cls.OVSVersion = findall(r'\d+\.\d+', version)[ 0 ]

    @classmethod
    def isOldOVS(cls):
        "Is OVS ersion < 1.10?"
        return StrictVersion(cls.OVSVersion) < StrictVersion('1.10')

    def dpctl(self, *args):
        "Run ovs-ofctl command"
        return self.cmd('ovs-ofctl', args[ 0 ], self, *args[ 1: ])

    def vsctl(self, *args, **kwargs):
        "Run ovs-vsctl command (or queue for later execution)"
        if self.batch:
            cmd = ' '.join(str(arg).strip() for arg in args)
            self.commands.append(cmd)
        else:
            return self.cmd('ovs-vsctl', *args, **kwargs)

    @staticmethod
    def TCReapply(intf):
        """Unfortunately OVS and Mininet are fighting
           over tc queuing disciplines. As a quick hack/
           workaround, we clear OVS's and reapply our own."""
        if isinstance(intf, TCWirelessLink):
            intf.config(**intf.params)

    def attach(self, intf):
        "Connect a data port"
        self.vsctl('add-port', self, intf)
        self.cmd('ip link set', intf, 'up')
        self.TCReapply(intf)

    def detach(self, intf):
        "Disconnect a data port"
        self.vsctl('del-port', self, intf)

    def controllerUUIDs(self, update=False):
        """Return ovsdb UUIDs for our controllers
           update: update cached value"""
        if not self._uuids or update:
            controllers = self.cmd('ovs-vsctl -- get Bridge', self,
                                   'Controller').strip()
            if controllers.startswith('[') and controllers.endswith(']'):
                controllers = controllers[ 1 :-1 ]
                if controllers:
                    self._uuids = [ c.strip()
                                    for c in controllers.split(',') ]
        return self._uuids

    def connected(self):
        "Are we connected to at least one of our controllers?"
        for uuid in self.controllerUUIDs():
            if 'true' in self.vsctl('-- get Controller',
                                    uuid, 'is_connected'):
                return True
        return self.failMode == 'standalone'

    def deleteIface(self, intf_):
        for intf in self.intfs.values():
            if intf.name == intf_:
                self.delIntf(intf)

    def intfOpts(self, intf):
        "Return OVS interface options for intf"
        opts = ''
        if not self.isOldOVS():
            # ofport_request is not supported on old OVS
            opts += ' ofport_request=%s' % self.ports[ intf ]
            # Patch ports don't work well with old OVS
            if isinstance(intf, OVSIntf):
                intf1, intf2 = intf.link.intf1, intf.link.intf2
                peer = intf1 if intf1 != intf else intf2

                opts += ' type=patch options:peer=%s' % peer
        return '' if not opts else ' -- set Interface %s' % intf + opts

    def bridgeOpts(self):
        "Return OVS bridge options"
        opts = (' other_config:datapath-id=%s' % self.dpid +
                ' fail_mode=%s' % self.failMode)
        if not self.inband:
            opts += ' other-config:disable-in-band=true'
        if self.datapath == 'user':
            opts += ' datapath_type=netdev'
        if self.protocols and not self.isOldOVS():
            opts += ' protocols=%s' % self.protocols
        if self.stp and self.failMode == 'standalone':
            opts += ' stp_enable=true'
        return opts

    def start(self, controllers):
        "Start up a new OVS OpenFlow switch using ovs-vsctl"
        if self.inNamespace:
            raise Exception(
                'OVS kernel AP does not work in a namespace')

        int(self.dpid, 16)  # DPID must be a hex string
        # Command to add interfaces
        intfs = ''.join(' -- add-port %s %s' % (self, intf) +
                        self.intfOpts(intf)
                        for intf in self.intfList()
                        if self.ports[ intf ] and not intf.IP())

        # Command to create controller entries
        clist = [ (self.name + c.name, '%s:%s:%d' %
                   (c.protocol, c.IP(), c.port))
                  for c in controllers ]
        if self.listenPort:
            clist.append((self.name + '-listen',
                          'ptcp:%s' % self.listenPort))
        ccmd = '-- --id=@%s create Controller target=\\"%s\\"'
        if self.reconnectms:
            ccmd += ' max_backoff=%d' % self.reconnectms
        cargs = ' '.join(ccmd % (name, target)
                         for name, target in clist)
        # Controller ID list
        cids = ','.join('@%s' % name for name, _target in clist)
        # Try to delete any existing bridges with the same name
        if not self.isOldOVS():
            cargs += ' -- --if-exists del-br %s' % self
        # One ovs-vsctl command to rule them all!
        self.vsctl(cargs +
                   ' -- add-br %s' % self +
                   ' -- set bridge %s controller=[%s]' % (self, cids) +
                   self.bridgeOpts() +
                   intfs)
        # If necessary, restore TC config overwritten by OVS
        if not self.batch:
            for intf in self.intfList():
                self.TCReapply(intf)

    # This should be ~ int( quietRun( 'getconf ARG_MAX' ) ),
    # but the real limit seems to be much lower
    argmax = 128000

    @classmethod
    def batchStartup(cls, aps, run=errRun):
        """Batch startup for OVS
           aps: aps to start up
           run: function to run commands (errRun)"""
        info('...')
        cmds = 'ovs-vsctl'
        for ap in aps:
            if ap.isOldOVS():
                # Ideally we'd optimize this also
                run('ovs-vsctl del-br %s' % ap)
            for cmd in ap.commands:
                cmd = cmd.strip()
                # Don't exceed ARG_MAX
                if len(cmds) + len(cmd) >= cls.argmax:
                    run(cmds, shell=True)
                    cmds = 'ovs-vsctl'
                cmds += ' ' + cmd
                ap.cmds = []
                ap.batch = False
        if cmds:
            run(cmds, shell=True)
        # Reapply link config if necessary...
        for ap in aps:
            for intf in ap.intfs:
                if isinstance(intf, TCWirelessLink):
                    intf.config(**intf.params)
        return aps

    def stop(self, deleteIntfs=True):
        """Terminate OVS switch.
           deleteIntfs: delete interfaces? (True)"""
        self.cmd('ovs-vsctl del-br', self)
        if self.datapath == 'user':
            self.cmd('ip link del', self)
        super(OVSAP, self).stop(deleteIntfs)

    @classmethod
    def batchShutdown(cls, aps, run=errRun):
        "Shut down a list of OVS switches"
        delcmd = 'del-br %s'
        if aps and not aps[ 0 ].isOldOVS():
            delcmd = '--if-exists ' + delcmd
        # First, delete them all from ovsdb
        run('ovs-vsctl ' + ' -- '.join(delcmd % s for s in aps))
        # Next, shut down all of the processes
        pids = ' '.join(str(ap.pid) for ap in aps)
        run('kill -HUP ' + pids)
        for ap in aps:
            ap.shell = None
        return aps


OVSKernelAP = OVSAP
physicalAP = OVSAP

class OVSBridgeAP( OVSAP ):
    "OVSBridge is an OVSAP in standalone/bridge mode"

    def __init__( self, *args, **kwargs ):
        """stp: enable Spanning Tree Protocol (False)
           see OVSSwitch for other options"""
        kwargs.update( failMode='standalone' )
        OVSAP.__init__( self, *args, **kwargs )

    def start( self, controllers ):
        "Start bridge, ignoring controllers argument"
        OVSAP.start( self, controllers=[] )

    def connected( self ):
        "Are we forwarding yet?"
        if self.stp:
            status = self.dpctl( 'show' )
            return 'STP_FORWARD' in status and not 'STP_LEARN' in status
        else:
            return True
