# author: Ramon Fontes (ramonrf@dca.fee.unicamp.br)


import os
import re
import subprocess
from time import sleep
from sys import version_info as py_version_info

from mininet.log import error, debug
from mn_wifi.devices import CustomRate, DeviceRange
from mn_wifi.manetRoutingProtocols import manetProtocols
from mn_wifi.wmediumdConnector import DynamicIntfRef, \
    w_starter, SNRLink, w_txpower, w_pos, \
    w_cst, w_server, ERRPROBLink, wmediumd_mode


class IntfWireless(object):

    "Basic interface object that can configure itself."

    def __init__(self, name, node=None, port=None, link=None,
                 mac=None, **params):
        """name: interface name (e.g. h1-eth0)
           node: owning node (where this intf most likely lives)
           link: parent link if we're part of a link
           other arguments are passed to config()"""
        self.node = node
        self.name = name
        self.link = link
        self.port = port
        self.mac = mac
        self.ip, self.ip6, self.prefixLen = None, None, None
        # if interface is lo, we know the ip is 127.0.0.1.
        # This saves an ip link/addr command per node
        if self.name == 'lo':
            self.ip = '127.0.0.1'

        node.addWIntf(self, port=port)

        # Save params for future reference
        self.params = params
        self.config(**params)

    def cmd(self, *args, **kwargs):
        "Run a command in our owning node"
        return self.node.cmd(*args, **kwargs)

    def pexec(self, *args, **kwargs):
        "Run a command in our owning node"
        return self.node.pexec(*args, **kwargs)

    def set_dev_type(self, type):
        self.iwdev_cmd('%s set type %s' % (self.name, type))

    def add_dev_type(self, new_name, type):
        self.iwdev_cmd('%s interface add %s type %s' % (self.name, new_name, type))

    def iwdev_cmd(self, *args):
        return self.cmd('iw dev', *args)

    def iwdev_pexec(self, *args):
        return self.pexec('iw dev', *args)

    def join_ibss(self, intf, ht_cap):
        return self.iwdev_cmd('{} ibss join {} {} {} 02:CA:FF:EE:BA:01'.
                              format(self.name, intf.ssid, intf.freq, ht_cap))

    def join_mesh(self, ssid, freq, ht_cap):
        return self.iwdev_cmd('{} mesh join {} freq {} {}'.
                              format(self.name, ssid, freq, ht_cap))

    def setFreq(self, freq, intf=None):
        return self.iwdev_cmd('{} set freq {}'.format(intf, freq))

    def format_freq(self, intf):
        return format(intf.freq, '.3f').replace('.', '')

    def setChanParam(self, channel, intf):
        intf.channel = str(channel)
        intf.freq = self.node.get_freq(intf)
        intf.freq = self.format_freq(intf)

    def setModeParam(self, mode, intf):
        if mode == 'a' or mode == 'ac':
            self.pexec('iw reg set US')
        intf.mode = mode

    def setChannel(self, channel, intf, AP = None):
        self.setChanParam(channel, intf)
        if AP and not isinstance(self.intf, mesh):
            self.pexec(
                'hostapd_cli -i %s chan_switch %s %s' % (
                    self.name, str(channel),
                    str(self.intf.freq).replace(".", "")))
        else:
            self.iwdev_cmd('%s set channel %s'
                           % (intf.name,
                              str(channel)))

    def ipAddr(self, *args):
        "Configure ourselves using ip link/addr"
        if self.name not in self.node.params['wlan']:
            self.cmd('ip addr flush ', self.name)
            return self.cmd('ip addr add', args[0], 'dev', self.name)
        else:
            if len(args) == 0:
                return self.cmd('ip addr show', self.name)
            else:
                if ':' not in args[0]:
                    self.cmd('ip addr flush ', self.name)
                    cmd = 'ip addr add %s dev %s' % (args[0], self.name)
                    if self.ip6:
                        cmd = cmd + ' && ip -6 addr add %s dev %s' % \
                                    (self.ip6, self.name)
                    return self.cmd(cmd)
                else:
                    self.cmd('ip -6 addr flush ', self.name)
                    return self.cmd('ip -6 addr add', args[0], 'dev', self.name)

    def ipLink(self, *args):
        "Configure ourselves using ip link"
        return self.cmd('ip link set', self.name, *args)

    def setMode(self, mode):
        self.mode = mode

    def setChannel(self, channel):
        self.channel = channel

    def setIP(self, ipstr, prefixLen=None, **args):
        """Set our IP address"""
        # This is a sign that we should perhaps rethink our prefix
        # mechanism and/or the way we specify IP addresses
        if '/' in ipstr:
            self.ip, self.prefixLen = ipstr.split('/')
            return self.ipAddr(ipstr)
        else:
            if prefixLen is None:
                raise Exception('No prefix length set for IP address %s'
                                % (ipstr,))
            self.ip, self.prefixLen = ipstr, prefixLen
            return self.ipAddr('%s/%s' % (ipstr, prefixLen))

    def setIP6(self, ipstr, prefixLen=None, **args):
        """Set our IP6 address"""
        # This is a sign that we should perhaps rethink our prefix
        # mechanism and/or the way we specify IP addresses
        if '/' in ipstr:
            self.ip6, self.prefixLen = ipstr.split('/')
            return self.ipAddr(ipstr)
        else:
            if prefixLen is None:
                raise Exception('No prefix length set for IP address %s'
                                % (ipstr,))
            self.ip6, self.prefixLen = ipstr, prefixLen
            return self.ipAddr('%s/%s' % (ipstr, prefixLen))

    def setMAC(self, macstr):
        """Set the MAC address for an interface.
           macstr: MAC address as string"""
        self.mac = macstr
        return (self.ipLink('down') +
                self.ipLink('address', macstr) +
                self.ipLink('up'))

    _ipMatchRegex = re.compile(r'\d+\.\d+\.\d+\.\d+')
    _macMatchRegex = re.compile(r'..:..:..:..:..:..')

    def updateIP(self):
        "Return updated IP address based on ip addr"
        # use pexec instead of node.cmd so that we dont read
        # backgrounded output from the cli.
        ipAddr, _err, _exitCode = self.node.pexec(
            'ip addr show %s' % self.name)
        if py_version_info < (3, 0):
            ips = self._ipMatchRegex.findall(ipAddr)
        else:
            ips = self._ipMatchRegex.findall(ipAddr.decode('utf-8'))
        self.ip = ips[0] if ips else None
        return self.ip

    def updateMAC(self):
        "Return updated MAC address based on ip addr"
        ipAddr = self.ipAddr()
        if py_version_info < (3, 0):
            macs = self._macMatchRegex.findall(ipAddr)
        else:
            macs = self._macMatchRegex.findall(ipAddr.decode('utf-8'))
        self.mac = macs[0] if macs else None
        return self.mac

    # Instead of updating ip and mac separately,
    # use one ipAddr call to do it simultaneously.
    # This saves an ipAddr command, which improves performance.

    def updateAddr(self):
        "Return IP address and MAC address based on ipAddr."
        ipAddr = self.ipAddr()
        if py_version_info < (3, 0):
            ips = self._ipMatchRegex.findall(ipAddr)
            macs = self._macMatchRegex.findall(ipAddr)
        else:
            ips = self._ipMatchRegex.findall(ipAddr.decode('utf-8'))
            macs = self._macMatchRegex.findall(ipAddr.decode('utf-8'))
        self.ip = ips[0] if ips else None
        self.mac = macs[0] if macs else None
        return self.ip, self.mac

    def IP(self):
        "Return IP address"
        return self.ip

    def MAC(self):
        "Return MAC address"
        return self.mac

    def isUp(self, setUp=False):
        "Return whether interface is up"
        if setUp:
            cmdOutput = self.ipLink('up')
            # no output indicates success
            if cmdOutput:
                # error( "Error setting %s up: %s " % ( self.name, cmdOutput ) )
                return False
            else:
                return True
        else:
            return "UP" in self.ipAddr()

    def rename(self, newname):
        "Rename interface"
        if self.node and self.name in self.node.nameToIntf:
            # rename intf in node's nameToIntf
            self.node.nameToIntf[newname] = self.node.nameToIntf.pop(self.name)
        self.ipLink('down')
        result = self.cmd('ip link set', self.name, 'name', newname)
        self.name = newname
        self.ipLink('up')
        return result

    # The reason why we configure things in this way is so
    # That the parameters can be listed and documented in
    # the config method.
    # Dealing with subclasses and superclasses is slightly
    # annoying, but at least the information is there!

    def setParam(self, results, method, **param):
        """Internal method: configure a *single* parameter
           results: dict of results to update
           method: config method name
           param: arg=value (ignore if value=None)
           value may also be list or dict"""
        name, value = list(param.items())[ 0 ]
        f = getattr(self, method, None)
        if not f or value is None:
            return
        if isinstance(value, list):
            result = f(*value)
        elif isinstance(value, dict):
            result = f(**value)
        else:
            result = f(value)
        results[ name ] = result
        return result

    def config(self, mac=None, ip=None, ipAddr=None, up=True, **_params):
        """Configure Node according to (optional) parameters:
           mac: MAC address
           ip: IP address
           ipAddr: arbitrary interface configuration
           Subclasses should override this method and call
           the parent class's config(**params)"""
        # If we were overriding this method, we would call
        # the superclass config method here as follows:
        # r = Parent.config( **params )
        r = {}
        self.setParam(r, 'setMAC', mac=mac)
        self.setParam(r, 'setIP', ip=ip)
        self.setParam(r, 'isUp', up=up)
        self.setParam(r, 'ipAddr', ipAddr=ipAddr)

        return r

    def delete(self):
        "Delete interface"
        self.cmd('iw dev ' + self.name + ' del')
        # We used to do this, but it slows us down:
        # if self.node.inNamespace:
        # Link may have been dumped into root NS
        # quietRun( 'ip link del ' + self.name )
        #self.node.delIntf(self)
        self.link = None

    def status(self):
        "Return intf status as a string"
        links, _err, _result = self.node.pexec('ip link show')
        if self.name in str(links):
            return "OK"
        else:
            return "MISSING"

    def __repr__(self):
        return '<%s %s>' % (self.__class__.__name__, self.name)

    def __str__(self):
        return self.name


class TCWirelessLink(IntfWireless):
    """Interface customized by tc (traffic control) utility
       Allows specification of bandwidth limits (various methods)
       as well as delay, loss and max queue length"""

    # The parameters we use seem to work reasonably up to 1 Gb/sec
    # For higher data rates, we will probably need to change them.
    bwParamMax = 1000

    def bwCmds(self, bw=None, speedup=0, use_hfsc=False, use_tbf=False,
               latency_ms=None, enable_ecn=False, enable_red=False):
        "Return tc commands to set bandwidth"
        cmds, parent = [], ' root '
        if bw and (bw < 0 or bw > self.bwParamMax):
            error('Bandwidth limit', bw, 'is outside supported range 0..%d'
                  % self.bwParamMax, '- ignoring\n')
        elif bw is not None:
            # BL: this seems a bit brittle...
            if speedup > 0:
                bw = speedup
            # This may not be correct - we should look more closely
            # at the semantics of burst (and cburst) to make sure we
            # are specifying the correct sizes. For now I have used
            # the same settings we had in the mininet-hifi code.
            if use_hfsc:
                cmds += [ '%s qdisc add dev %s root handle 5:0 hfsc default 1',
                          '%s class add dev %s parent 5:0 classid 5:1 hfsc sc '
                          + 'rate %fMbit ul rate %fMbit' % (bw, bw) ]
            elif use_tbf:
                if latency_ms is None:
                    latency_ms = 15 * 8 / bw
                cmds += [ '%s qdisc add dev %s root handle 5: tbf ' +
                          'rate %fMbit burst 15000 latency %fms' %
                          (bw, latency_ms) ]
            else:
                cmds += [ '%s qdisc add dev %s root handle 5:0 htb default 1',
                          '%s class add dev %s parent 5:0 classid 5:1 htb ' +
                          'rate %fMbit burst 15k' % bw ]
            parent = ' parent 5:1 '
            # ECN or RED
            if enable_ecn:
                cmds += [ '%s qdisc add dev %s' + parent +
                          'handle 6: red limit 1000000 ' +
                          'min 30000 max 35000 avpkt 1500 ' +
                          'burst 20 ' +
                          'bandwidth %fmbit probability 1 ecn' % bw ]
                parent = ' parent 6: '
            elif enable_red:
                cmds += [ '%s qdisc add dev %s' + parent +
                          'handle 6: red limit 1000000 ' +
                          'min 30000 max 35000 avpkt 1500 ' +
                          'burst 20 ' +
                          'bandwidth %fmbit probability 1' % bw ]
                parent = ' parent 6: '

        return cmds, parent

    @staticmethod
    def delayCmds(parent, delay=None, jitter=None,
                  loss=None, max_queue_size=None):
        "Internal method: return tc commands for delay and loss"
        cmds = []
        if delay:
            delay_ = float(delay.replace("ms", ""))
        if delay and delay_ < 0:
            error( 'Negative delay', delay, '\n' )
        elif jitter and jitter < 0:
            error('Negative jitter', jitter, '\n')
        elif loss and (loss < 0 or loss > 100):
            error('Bad loss percentage', loss, '%%\n')
        else:
            # Delay/jitter/loss/max queue size
            netemargs = '%s%s%s%s' % (
                'delay %s ' % delay if delay is not None else '',
                '%s ' % jitter if jitter is not None else '',
                'loss %.5f ' % loss if loss is not None else '',
                'limit %d' % max_queue_size if max_queue_size is not None
                else '')
            if netemargs:
                cmds = [ '%s qdisc add dev %s ' + parent +
                         ' handle 10: netem ' +
                         netemargs ]
                parent = ' parent 10:1 '
        return cmds, parent

    def tc(self, cmd, tc='tc'):
        "Execute tc command for our interface"
        c = cmd % (tc, self)  # Add in tc command and our name
        debug(" *** executing command: %s\n" % c)
        return self.cmd(c)

    def config(self, bw=None, delay=None, jitter=None, loss=None,
               gro=False, speedup=0, use_hfsc=False, use_tbf=False,
               latency_ms=None, enable_ecn=False, enable_red=False,
               max_queue_size=None, **params):
        """Configure the port and set its properties.
            bw: bandwidth in b/s (e.g. '10m')
            delay: transmit delay (e.g. '1ms' )
            jitter: jitter (e.g. '1ms')
            loss: loss (e.g. '1%' )
            gro: enable GRO (False)
            txo: enable transmit checksum offload (True)
            rxo: enable receive checksum offload (True)
            speedup: experimental switch-side bw option
            use_hfsc: use HFSC scheduling
            use_tbf: use TBF scheduling
            latency_ms: TBF latency parameter
            enable_ecn: enable ECN (False)
            enable_red: enable RED (False)
            max_queue_size: queue limit parameter for netem"""

        # Support old names for parameters
        gro = not params.pop('disable_gro', not gro)

        result = IntfWireless.config(self, **params)

        def on(isOn):
            "Helper method: bool -> 'on'/'off'"
            return 'on' if isOn else 'off'

        # Set offload parameters with ethool
        self.cmd('ethtool -K', self,
                 'gro', on(gro))

        # Optimization: return if nothing else to configure
        # Question: what happens if we want to reset things?
        if (bw is None and not delay and not loss
                and max_queue_size is None):
            return

        # Clear existing configuration
        tcoutput = self.tc('%s qdisc show dev %s')
        if "priomap" not in tcoutput and "noqueue" not in tcoutput \
                and "fq_codel" not in tcoutput and "qdisc fq" not in tcoutput:
            cmds = [ '%s qdisc del dev %s root' ]
        else:
            cmds = []
        # Bandwidth limits via various methods
        bwcmds, parent = self.bwCmds(bw=bw, speedup=speedup,
                                     use_hfsc=use_hfsc, use_tbf=use_tbf,
                                     latency_ms=latency_ms,
                                     enable_ecn=enable_ecn,
                                     enable_red=enable_red)
        cmds += bwcmds

        # Delay/jitter/loss/max_queue_size using netem
        delaycmds, parent = self.delayCmds(delay=delay, jitter=jitter,
                                           loss=loss,
                                           max_queue_size=max_queue_size,
                                           parent=parent)
        cmds += delaycmds

        # Execute all the commands in our node
        debug("at map stage w/cmds: %s\n" % cmds)
        tcoutputs = [ self.tc(cmd) for cmd in cmds ]
        for output in tcoutputs:
            if output != '':
                error("*** Error: %s" % output)
        debug("cmds:", cmds, '\n')
        debug("outputs:", tcoutputs, '\n')
        result[ 'tcoutputs'] = tcoutputs
        result[ 'parent' ] = parent

        return result


class _4address(IntfWireless):

    node = None

    def __init__(self, node1, node2, port1=None, port2=None):
        """Create 4addr link to another node.
           node1: first node
           node2: second node
           intf: default interface class/constructor"""
        intf1 = None
        intf2 = None

        ap = node1 # ap
        cl = node2 # client
        cl_intfName = '%s.wds' % cl.name

        if 'position' not in node1.params:
            self.set_pos(node1)
        if 'position' not in node2.params:
            self.set_pos(node2)

        if cl_intfName not in cl.params['wlan']:

            wlan = cl.params['wlan'].index(port1) if port1 else 0
            apwlan = ap.params['wlan'].index(port2) if port2 else 0

            intf = cl.wintfs[wlan]
            ap_intf = ap.wintfs[apwlan]

            self.node = cl
            self.add4addrIface(wlan, cl_intfName)

            self.setMAC(intf)
            self.setMAC(ap_intf)
            self.bring4addrIfaceUP()

            intf.mode = ap_intf.mode
            intf.channel = ap_intf.channel
            intf.freq = ap_intf.freq
            intf.txpower = ap_intf.txpower
            intf.antennaGain = ap_intf.antennaGain
            cl.params['wlan'].append(cl_intfName)
            sleep(1)
            self.iwdev_cmd('%s connect %s %s' % (cl.params['wlan'][1],
                                                 ap_intf.ssid, ap_intf.mac))

            params1, params2 = {}, {}
            params1['port'] = cl.newPort()
            params2['port'] = ap.newPort()
            intf1 = IntfWireless(name=cl_intfName, node=cl, link=self, **params1)
            if hasattr(ap, 'wds'):
                ap.wds += 1
            else:
                ap.wds = 1
            intfName2 = ap.params['wlan'][apwlan] + '.sta%s' % ap.wds
            intf2 = IntfWireless(name=intfName2, node=ap, link=self, **params2)
            ap.params['wlan'].append(intfName2)

            _4addrAP(ap, (len(ap.params['wlan'])-1))
            _4addrClient(cl, (len(cl.params['wlan'])-1))

            cl.wintfs[1].mac = (intf.mac[:3] + '09' + intf.mac[5:])

        # All we are is dust in the wind, and our two interfaces
        self.intf1, self.intf2 = intf1, intf2

    def set_pos(self, node):
        nums = re.findall(r'\d+', node.name)
        if nums:
            id = int(hex(int(nums[0]))[2:])
            node.params['position'] = (10, round(id, 2), 0)

    def bring4addrIfaceUP(self):
        self.cmd('ip link set dev %s.wds up' % self.node)

    def setMAC(self, intf):
        self.cmd('ip link set dev %s.wds addr %s'
                 % (intf.node, intf.mac))

    def add4addrIface(self, wlan, intfName):
        self.iwdev_cmd('%s interface add %s type managed 4addr on' %
                       (self.node.params['wlan'][wlan], intfName))

    def status(self):
        "Return link status as a string"
        return "(%s %s)" % (self.intf1.status(), self.intf2)

    def __str__(self):
        return '%s<->%s' % (self.intf1, self.intf2)

    def delete(self):
        "Delete this link"
        self.intf1.delete()
        self.intf1 = None
        self.intf2.delete()
        self.intf2 = None

    def stop(self):
        "Override to stop and clean up link as needed"
        self.delete()


class WirelessLinkAP(object):

    """A basic link is just a veth pair.
       Other types of links could be tunnels, link emulators, etc.."""

    # pylint: disable=too-many-branches
    def __init__(self, node, port=None, intfName=None, addr=None,
                 cls=None, params=None):
        """Create veth link to another node, making two new interfaces.
           node: first node
           port: node port number (optional)
           intf: default interface class/constructor
           cls: optional interface-specific constructors
           intfName: node interface name (optional)
           params: parameters for interface 1"""
        # This is a bit awkward; it seems that having everything in
        # params is more orthogonal, but being able to specify
        # in-line arguments is more convenient! So we support both.
        if params is None:
            params = {}

        if port is not None:
            params[ 'port' ] = port

        ifacename = 'wlan'

        if 'port' not in params:
            if intfName is None:
                nodelen = int(len(node.params['wlan']))
                currentlen = node.wlanports
                if nodelen > currentlen + 1:
                    params[ 'port' ] = node.newPort()
                else:
                    params[ 'port' ] = currentlen
                intfName = self.wlanName(node, ifacename, params[ 'port' ])
                intf1 = cls(name=intfName, node=node,
                            link=self, mac=addr, **params)
            else:
                params[ 'port' ] = node.newPort()
                node.newPort()
                intf1 = cls(name=intfName, node=node,
                            link=self, mac=addr, **params)
        else:
            intfName = self.wlanName(node, ifacename, params[ 'port' ])
            intf1 = cls(name=intfName, node=node,
                        link=self, mac=addr, **params)

        if not intfName:
            self.wlanName(node, ifacename, node.newWlanPort())

        intf2 = 'wifi'
        # All we are is dust in the wind, and our two interfaces
        self.intf1, self.intf2 = intf1, intf2
    # pylint: enable=too-many-branches

    @staticmethod
    def _ignore(*args, **kwargs):
        "Ignore any arguments"
        pass

    def wlanName(self, node, ifacename, n):
        "Construct a canonical interface name node-ethN for interface n."
        # Leave this as an instance method for now
        assert self
        return node.name + '-' + ifacename + repr(n)

    def delete(self):
        "Delete this link"
        self.intf1.delete()
        self.intf1 = None
        self.intf2 = None

    def stop(self):
        "Override to stop and clean up link as needed"
        self.delete()

    def status(self):
        "Return link status as a string"
        return "(%s %s)" % (self.intf1.status(), self.intf2)

    def __str__(self):
        return '%s<->%s' % (self.intf1, self.intf2)


class WirelessLinkStation(object):

    """A basic link is just a veth pair.
       Other types of links could be tunnels, link emulators, etc.."""

    # pylint: disable=too-many-branches
    def __init__(self, node, port=None, intfName=None, addr=None,
                 intf=IntfWireless, cls=None, params=None):
        """Create veth link to another node, making two new interfaces.
           node: first node
           port: node port number (optional)
           intf: default interface class/constructor
           cls: optional interface-specific constructors
           intfName: node interface name (optional)
           params: parameters for interface 1"""
        # This is a bit awkward; it seems that having everything in
        # params is more orthogonal, but being able to specify
        # in-line arguments is more convenient! So we support both.
        if params is None:
            params = {}

        if port is not None:
            params[ 'port' ] = port

        if 'port' not in params:
            params[ 'port' ] = node.newPort()

        if not intfName:
            ifacename = 'wlan'
            intfName = self.wlanName(node, ifacename, node.newWlanPort())

        if not cls:
            cls = intf

        intf1 = cls(name=intfName, node=node,
                    link=self, mac=addr, **params)
        intf2 = 'wifi'
        # All we are is dust in the wind, and our two interfaces
        self.intf1, self.intf2 = intf1, intf2
    # pylint: enable=too-many-branches

    @staticmethod
    def _ignore(*args, **kwargs):
        "Ignore any arguments"
        pass

    def wlanName(self, node, ifacename, n):
        "Construct a canonical interface name node-ethN for interface n."
        # Leave this as an instance method for now
        assert self
        return node.name + '-' + ifacename + repr(n)

    def delete(self):
        "Delete this link"
        self.intf1.delete()
        self.intf1 = None
        self.intf2 = None

    def stop(self):
        "Override to stop and clean up link as needed"
        self.delete()

    def status(self):
        "Return link status as a string"
        return "(%s %s)" % (self.intf1.status(), self.intf2)

    def __str__(self):
        return '%s<->%s' % (self.intf1, self.intf2)


class TCLinkWirelessStation(WirelessLinkStation):
    "Link with symmetric TC interfaces configured via opts"
    def __init__(self, node, port=None, intfName=None,
                 addr=None, cls=TCWirelessLink, **params):
        WirelessLinkStation.__init__(self, node=node, port=port,
                                     intfName=intfName,
                                     cls=cls, addr=addr,
                                     params=params)


class TCLinkWirelessAP(WirelessLinkAP):
    "Link with symmetric TC interfaces configured via opts"
    def __init__(self, node, port=None, intfName=None,
                 addr=None, cls=TCWirelessLink, **params):
        WirelessLinkAP.__init__(self, node, port=port,
                                intfName=intfName,
                                cls=cls, addr=addr,
                                params=params)


class master(TCWirelessLink):
    "master class"
    def __init__(self, node, wlan, port=None):
        self.name = node.params['wlan'][wlan]
        node.addWAttr(self, port=port)
        self.node = node
        self.params = {}
        self.stationsInRange = {}
        self.associatedStations = []
        self.range = 0
        self.txpower = 14
        self.driver = 'nl80211'
        self.ht_capab = ''
        self.beacon_int = ''
        self.isolate_clients = ''
        self.mac = ''
        self.ssid = ''
        self.encrypt = ''
        self.wpa_key_mgmt = ''
        self.passwd = ''
        self.authmode = ''
        self.config = ''
        self.rsn_pairwise = ''
        self.active_scan = ''
        self.radius_server = ''
        self.mode = 'g'
        self.freq = 2.412
        self.channel = 1
        self.antennaGain = 5.0
        self.antennaHeight = 1.0
        self.id = wlan
        self.ip = ''
        self.ip6 = ''
        self.link = None

        args = ['radius_identity', 'radius_passwd', 'ssid', 'encrypt',
                'passwd', 'mode', 'channel', 'authmode', 'range']
        for arg in args:
            if arg in node.params:
                setattr(self, arg, node.params[arg])


class managed(TCWirelessLink):
    "managed class"
    def __init__(self, node, wlan):
        self.name = node.params['wlan'][wlan]
        node.addWIntf(self, port=wlan)
        node.addWAttr(self, port=wlan)
        self.node = node
        self.apsInRange = {}
        self.range = 0
        self.ssid = ''
        self.mac = ''
        self.scan_freq = ''
        self.freq_list = ''
        self.encrypt = ''
        self.radius_identity = ''
        self.radius_passwd = ''
        self.passwd = ''
        self.config = ''
        self.authmode = ''
        self.txpower = 14
        self.id = wlan
        self.rssi = -60
        self.mode = 'g'
        self.freq = 2.412
        self.channel = 1
        self.antennaGain = 5.0
        self.antennaHeight = 1.0
        self.associatedTo = ''
        self.ip = node.params['ip']
        self.ip6 = node.params['ip6']
        self.link = None

        args = ['radius_identity', 'radius_passwd', 'ssid', 'encrypt',
                'passwd', 'mode', 'channel', 'authmode', 'range']
        for arg in args:
            if arg in node.params:
                setattr(self, arg, node.params[arg])


class _4addrClient(TCWirelessLink):
    "managed class"
    def __init__(self, node, wlan):
        self.node = node
        self.ip = None
        self.mac = node.wintfs[wlan-1].mac
        self.range = node.wintfs[0].range
        self.txpower = 0
        self.name = node.params['wlan'][wlan]
        self.stationsInRange = {}
        self.associatedStations = []
        self.apsInRange = {}
        self.params = {}
        node.addWIntf(self)
        node.addWAttr(self)


class _4addrAP(TCWirelessLink):
    "managed class"
    def __init__(self, node, wlan):
        self.node = node
        self.ip = None
        self.mac = node.wintfs[0].mac
        self.range = node.wintfs[0].range
        self.txpower = 0
        self.name = node.params['wlan'][wlan]
        self.stationsInRange = {}
        self.associatedStations = []
        self.params = {}
        node.addWIntf(self)
        node.addWAttr(self)


class wmediumd(TCWirelessLink):
    "Wmediumd Class"
    wlinks = []
    links = []
    txpowers = []
    positions = []
    nodes = []

    def __init__(self, fading_coefficient, noise_threshold, stations,
                 aps, cars, propagation_model, maclist=None):

        self.configureWmediumd(fading_coefficient, noise_threshold, stations,
                               aps, cars, propagation_model, maclist)

    @classmethod
    def configureWmediumd(cls, fading_coefficient, noise_threshold, stations,
                          aps, cars, propagation_model, maclist):
        "Configure wmediumd"
        intfrefs = []
        isnodeaps = []
        fading_coefficient = fading_coefficient
        noise_threshold = noise_threshold

        cls.nodes = stations + aps + cars
        for node in cls.nodes:
            node.wmIface = []
            for wlan, intf in enumerate(node.wintfs.values()):
                node.wmIface.append(wlan)
                node.wmIface[wlan] = DynamicIntfRef(node, intf=wlan)
                intfrefs.append(node.wmIface[wlan])

                if (isinstance(intf, master)
                    or (node in aps and (not isinstance(intf, managed)
                                         and not isinstance(intf, adhoc)))):
                    isnodeaps.append(1)
                else:
                    isnodeaps.append(0)
            for mac in maclist:
                for key in mac:
                    if key == node:
                        key.wmIface.append(DynamicIntfRef(key, intf=len(key.wmIface)))
                        key.params['wlan'].append(mac[key][1])
                        key.params['mac'].append(mac[key][0])
                        key.params['range'].append(0)
                        key.params['freq'].append(key.params['freq'][0])
                        key.params['antennaGain'].append(0)
                        key.params['txpower'].append(14)
                        intfrefs.append(key.wmIface[len(key.wmIface) - 1])
                        isnodeaps.append(0)

        if wmediumd_mode.mode == w_cst.INTERFERENCE_MODE:
            set_interference()
        elif wmediumd_mode.mode == w_cst.SPECPROB_MODE:
            spec_prob_link()
        elif wmediumd_mode.mode == w_cst.ERRPROB_MODE:
            set_error_prob()
        else:
            set_snr()
        start_wmediumd(intfrefs, wmediumd.links, wmediumd.positions,
                       fading_coefficient, noise_threshold,
                       wmediumd.txpowers, isnodeaps, propagation_model,
                       maclist)


class start_wmediumd(object):
    def __init__(cls, intfrefs, links, positions,
                 fading_coefficient, noise_threshold, txpowers, isnodeaps,
                 propagation_model, maclist):

        w_starter.start(intfrefs, links, pos=positions,
                        fading_coefficient=fading_coefficient,
                        noise_threshold=noise_threshold,
                        txpowers=txpowers, isnodeaps=isnodeaps,
                        ppm=propagation_model, maclist=maclist)


class set_interference(object):

    def __init__(self):
        self.interference()

    @classmethod
    def interference(cls):
        'configure interference model'
        for node in wmediumd.nodes:
            if 'position' not in node.params:
                posX = 0
                posY = 0
                posZ = 0
            else:
                posX = float(node.params['position'][0])
                posY = float(node.params['position'][1])
                posZ = float(node.params['position'][2])
            node.lastpos = [posX, posY, posZ]

            for wlan, intf in enumerate(node.wintfs.values()):
                if wlan >= 1:
                    posX += 0.1
                wmediumd.positions.append(w_pos(node.wmIface[wlan],
                                                [posX, posY, posZ]))
                wmediumd.txpowers.append(w_txpower(
                    node.wmIface[wlan], float(intf.txpower)))


class spec_prob_link(object):
    "wmediumd: spec prob link"
    def __init__(self):
        'do nothing'


class set_error_prob(object):
    "wmediumd: set error prob"
    def __init__(self):
        self.error_prob()

    @classmethod
    def error_prob(cls):
        "wmediumd: error prob"
        for node in wmediumd.wlinks:
            wmediumd.links.append(ERRPROBLink(node[0].wmIface[0],
                                              node[1].wmIface[0], node[2]))
            wmediumd.links.append(ERRPROBLink(node[1].wmIface[0],
                                              node[0].wmIface[0], node[2]))


class set_snr(object):
    "wmediumd: set snr"
    def __init__(self):
        self.snr()

    @classmethod
    def snr(cls):
        "wmediumd: snr"
        for node in wmediumd.wlinks:
            wmediumd.links.append(SNRLink(node[0].wmIface[0], node[1].wmIface[0],
                                          node[0].params['rssi'][0] - (-91)))
            wmediumd.links.append(SNRLink(node[1].wmIface[0], node[0].wmIface[0],
                                          node[0].params['rssi'][0] - (-91)))


class wirelessLink(object):

    dist = 0
    noise = 0
    equationLoss = '(dist * 2) / 1000'
    equationDelay = '(dist / 10) + 1'
    equationLatency = '(dist / 10)/2'
    equationBw = ' * (1.01 ** -dist)'
    ifb = False

    def __init__(self, intf, dist=0):
        latency_ = self.getLatency(dist)
        loss_ = self.getLoss(dist)
        bw_ = self.getBW(intf, dist)
        self.config_tc(intf, bw_, loss_, latency_)

    def getDelay(self, dist):
        "Based on RandomPropagationDelayModel"
        return eval(self.equationDelay)

    def getLatency(self, dist):
        return eval(self.equationLatency)

    def getLoss(self, dist):
        return eval(self.equationLoss)

    def getBW(self, intf, dist):
        # dist is used by eval
        custombw = CustomRate(intf).rate
        rate = eval(str(custombw) + self.equationBw)

        if rate <= 0.0:
            rate = 0.1
        return rate

    @classmethod
    def delete(cls, node):
        "Delete interfaces"
        for intf in node.wintfs.values():
            node.cmd('iw dev ' + intf.name + ' del')
            node.delIntf(intf.name)
            node.intf = None

    @classmethod
    def config_tc(cls, intf, bw, loss, latency):
        if cls.ifb:
            iface = 'ifb%s' % intf.node.ifb[intf.id]
            cls.tc(intf.node, iface, bw, loss, latency)
        cls.tc(intf.node, intf.name, bw, loss, latency)

    @classmethod
    def tc(cls, node, iface, bw, loss, latency):
        cmd = "tc qdisc replace dev %s root handle 2: netem " % iface
        rate = "rate %.4fmbit " % bw
        cmd += rate
        if latency > 0.1:
            latency = "latency %.2fms " % latency
            cmd += latency
        if loss > 0.1:
            loss = "loss %.1f%% " % loss
            cmd += loss
        node.pexec(cmd)


class ITSLink(IntfWireless):

    def __init__(self, node, intf=None, channel=161):
        "configure ieee80211p"
        self.node = node
        wlan = node.params['wlan'].index(intf)

        if isinstance(node.ints[wlan], master):
            self.kill_hostapd(node, intf)

        node.wintfs[wlan].channel = channel
        node.wintfs[wlan].freq = node.get_freq(intf)
        self.name = intf
        if isinstance(node.ints[wlan], master):
            intf = '%s-ocb' % node.name
            self.add_ocb_mode(intf)
        else:
            self.set_ocb_mode()
        node.addWIntf(self, port=wlan)
        node.addWAttr(self, port=wlan)
        self.configure_ocb(intf)

    def kill_hostapd(self, node, intf):
        node.setManagedMode(intf)

    def add_ocb_mode(self, new_name):
        "Set OCB Interface"
        wlan = self.node.params['wlan'].index(self.name)
        self.ipLink('down')
        self.node.delIntf(self.name)
        self.add_dev_type(new_name, 'ocb')
        # we set the port to remove the existing wlan from node.intfs
        IntfWireless(name=new_name, node=self.node, port=1)
        self.name = new_name
        self.setMAC(self.node.params['mac'][wlan])
        self.ipLink('up')

    def set_ocb_mode(self):
        "Set OCB Interface"
        self.ipLink('down')
        self.set_dev_type('ocb')
        self.ipLink('up')

    def configure_ocb(self, intf):
        "Configure Wireless OCB"
        freq = str(intf.freq).replace(".", "")
        self.iwdev_cmd('%s ocb join %s 20MHz' % (self.name, freq))


class wifiDirectLink(IntfWireless):

    def __init__(self, node, intf=None):
        "configure wifi-direct"
        self.node = node

        wlan = node.params['wlan'].index(intf)
        intf = node.wintfs[wlan]
        self.mac = intf.mac
        self.name = intf.name
        self.range = intf.range
        self.txpower = intf.txpower
        self.ip6 = intf.ip6
        self.ip = intf.ip

        node.addWIntf(self, port=wlan)
        node.addWAttr(self, port=wlan)

        filename = self.get_filename(intf)
        self.config_(intf, filename)

        cmd = self.get_wpa_cmd(filename, intf)
        node.cmd(cmd)

    @classmethod
    def get_filename(cls, intf):
        suffix = 'wifiDirect.conf'
        filename = "mn%d_%s_%s" % (os.getpid(), intf.name, suffix)
        return filename

    @classmethod
    def get_wpa_cmd(cls, filename, intf):
        cmd = ('wpa_supplicant -B -Dnl80211 -c%s -i%s' %
               (filename, intf.name))
        return cmd

    @classmethod
    def config_(cls, intf, filename):
        cmd = ("echo \'")
        cmd += 'ctrl_interface=/var/run/wpa_supplicant\
              \nap_scan=1\
              \np2p_go_ht40=1\
              \ndevice_name=%s\
              \ndevice_type=1-0050F204-1\
              \np2p_no_group_iface=1' % (intf.name)
        cmd += ("\' > %s" % filename)
        cls.set_config(cmd)

    @classmethod
    def set_config(cls, cmd):
        subprocess.check_output(cmd, shell=True)


class physicalWifiDirectLink(wifiDirectLink):

    def __init__(self, node, intf=None):
        "configure wifi-direct"
        self.name = intf
        node.addWIntf(self)
        node.addWAttr(self)

        filename = self.get_filename(intf)
        self.config_(intf, filename)

        cmd = self.get_wpa_cmd(filename, intf)
        os.system(cmd)


class adhoc(IntfWireless):

    node = None

    def __init__(self, node, intf=None, ssid='adhocNet',
                 channel=1, mode='g', passwd=None, ht_cap='',
                 proto=None, **params):
        """Configure AdHoc
        node: name of the node
        self: custom association class/constructor
        params: parameters for station"""
        self.node = node

        wlan = node.params['wlan'].index(intf)
        intf = node.wintfs[wlan]

        self.id = intf.id
        self.mac = intf.mac
        self.ip6 = intf.ip6

        if 'mp' in intf.name:
            self.iwdev_cmd('%s del' % intf.name)
            node.params['wlan'][wlan] = intf.name.replace('mp', 'wlan')

        self.name = intf.name

        node.addWIntf(self, port=wlan)
        node.addWAttr(self, port=wlan)

        intf.ssid = ssid
        self.setChanParam(channel, intf)
        self.setModeParam(mode, intf)
        self.configureAdhoc(intf, passwd, ht_cap)

        self.freq = intf.freq
        self.channel = intf.channel
        self.mode = intf.mode
        self.range = intf.range

        if proto:
            manetProtocols(proto, node, wlan, **params)

    def configureAdhoc(self, intf, passwd, ht_cap):
        "Configure Wireless Ad Hoc"
        self.set_dev_type('ibss')
        self.ipLink('up')

        if passwd:
            self.setSecuredAdhoc(intf, passwd)
        else:
            self.join_ibss(intf, ht_cap)

    def setSecuredAdhoc(self, intf, passwd):
        "Set secured adhoc"
        cmd = 'ctrl_interface=/var/run/wpa_supplicant GROUP=wheel\n'
        cmd += 'ap_scan=2\n'
        cmd += 'network={\n'
        cmd += '         ssid="%s"\n' % intf.ssid
        cmd += '         mode=1\n'
        cmd += '         frequency=%s\n' % str(intf.freq).replace('.', '')
        cmd += '         proto=RSN\n'
        cmd += '         key_mgmt=WPA-PSK\n'
        cmd += '         pairwise=CCMP\n'
        cmd += '         group=CCMP\n'
        cmd += '         psk="%s"\n' % passwd
        cmd += '}'

        fileName = '%s.staconf' % intf.name
        os.system('echo \'%s\' > %s' % (cmd, fileName))
        pidfile = "mn%d_%s_wpa.pid" % (os.getpid(), intf.node.name)
        intf.node.wpa_cmd(pidfile, intf)


class mesh(IntfWireless):

    node = None

    def __init__(self, node, intf=None, mode='g', channel=1,
                 ssid='meshNet', passwd=None, ht_cap=''):
        """Configure wireless mesh
        node: name of the node
        self: custom association class/constructor
        params: parameters for node"""
        self.node = node

        wlan = node.params['wlan'].index(intf)
        intf = node.wintfs[wlan]
        iface = intf

        self.name = self.name = '%s-mp%s' % (node, intf.name[-1:])
        self.id = intf.id
        self.mac = intf.mac
        self.ip6 = intf.ip6
        self.ip = intf.ip

        self.range = intf.range
        self.ssid = ssid

        node.addWIntf(self, port=wlan)
        node.addWAttr(self, port=wlan)

        self.setMeshIface(node, mode, channel, wlan, iface)
        self.configureMesh(node, ssid, ht_cap, passwd, iface)

    def set_mesh_type(self, intf):
        return '%s interface add %s type mp' % (intf.name, self.name)

    def setMeshIface(self, node, mode, channel, wlan, intf):
        if isinstance(intf, adhoc):
            self.set_dev_type('managed')
        self.iwdev_cmd(self.set_mesh_type(intf))
        node.cmd('ip link set %s down' % intf)

        self.setMAC(intf.mac)
        node.params['wlan'][wlan] = self.name

        self.setChannel(channel, intf)
        self.setModeParam(mode, intf)

        self.freq = intf.freq
        self.channel = intf.channel
        self.mode = intf.mode

        self.ipLink('up')

    def configureMesh(self, node, ssid, ht_cap, passwd, intf):
        "Configure Wireless Mesh Interface"
        if passwd:
            self.setSecuredMesh(node, passwd, intf)
        else:
            self.associate(ssid, ht_cap, intf)

    def associate(self, ssid, ht_cap, intf):
        "Performs Mesh Association"
        self.join_mesh(ssid, intf.freq, ht_cap)

    def setSecuredMesh(self, node, passwd, intf):
        "Set secured mesh"
        cmd = 'ctrl_interface=/var/run/wpa_supplicant\n'
        cmd += 'ctrl_interface_group=adm\n'
        cmd += 'user_mpm=1\n'
        cmd += 'network={\n'
        cmd += '         ssid="%s"\n' % intf.ssid
        cmd += '         mode=5\n'
        cmd += '         frequency=%s\n' \
               % str(intf.freq).replace('.', '')
        cmd += '         key_mgmt=SAE\n'
        cmd += '         psk="%s"\n' % passwd
        cmd += '}'

        fileName = '%s.staconf' % (intf.name)
        os.system('echo \'%s\' > %s' % (cmd, fileName))
        pidfile = "mn%d_%s_wpa.pid" % (os.getpid(), intf.name)
        node.wpa_cmd(pidfile, intf)


class physicalMesh(IntfWireless):

    def __init__(self, node, intf=None, channel=1, ssid='meshNet'):
        """Configure wireless mesh
        node: name of the node
        self: custom association class/constructor
        params: parameters for node"""
        wlan = 0
        self.name = ''
        self.node = node

        node.wintfs[wlan].ssid = ssid
        if int(node.wintfs[wlan].range) == 0:
            intf = node.params['wlan'][wlan]
            node.wintfs[wlan].range = node.getRange(intf, 95)

        self.name = intf
        self.setPhysicalMeshIface(node, wlan, intf, channel, ssid)
        freq = self.format_freq(node.wintfs[wlan])
        ht_cap = ''
        self.join_mesh(ssid, freq, ht_cap)

    def ipLink(self, state=None):
        "Configure ourselves using ip link"
        os.system('ip link set %s %s' % (self.name, state))

    def setPhysicalMeshIface(self, node, wlan, intf, channel, ssid):
        iface = 'phy%s-mp%s' % (node, wlan)
        self.ipLink('down')
        while True:
            id = ''
            cmd = 'ip link show | grep %s' % iface
            try:
                id = subprocess.check_output(cmd, shell=True).split("\n")
            except:
                pass
            if len(id) == 0:
                cmd = ('iw dev %s interface add %s type mp' %
                       (intf, iface))
                self.name = iface
                subprocess.check_output(cmd, shell=True)
            else:
                try:
                    if channel:
                        cmd = ('iw dev %s set channel %s' %
                               (iface, channel))
                        subprocess.check_output(cmd, shell=True)
                    self.ipLink('up')
                    command = ('iw dev %s mesh join %s' % (iface, ssid))
                    subprocess.check_output(command, shell=True)
                    break
                except:
                    break


class Association(IntfWireless):

    @classmethod
    def setSNRWmediumd(cls, sta, ap, snr):
        "Send SNR to wmediumd"
        w_server.send_snr_update(SNRLink(sta.wmIface[0],
                                         ap.wmIface[0], snr))
        w_server.send_snr_update(SNRLink(ap.wmIface[0],
                                         sta.wmIface[0], snr))

    @classmethod
    def configureWirelessLink(cls, wlan, intf, ap_intf):
        dist = intf.node.get_distance_to(ap_intf.node)
        if dist <= ap_intf.range:
            if not wmediumd_mode.mode == w_cst.INTERFERENCE_MODE:
                if intf.rssi == 0:
                    cls.updateParams(intf, ap_intf)
            if ap_intf != intf.associatedTo or \
                    not intf.associatedTo:
                cls.associate_infra(intf, ap_intf)
                if wmediumd_mode.mode == w_cst.WRONG_MODE:
                    if dist >= 0.01:
                        wirelessLink(intf, dist)
                if intf.node != ap_intf.associatedStations:
                    ap_intf.associatedStations.append(intf.node)
            if not wmediumd_mode.mode == w_cst.INTERFERENCE_MODE:
                cls.setRSSI(intf, ap_intf, wlan, dist)

    @classmethod
    def setRSSI(cls, intf, ap_intf, wlan, dist):
        rssi = intf.node.get_rssi(ap_intf.node, wlan, dist)
        intf.rssi = rssi
        if ap_intf.node not in intf.apsInRange:
            intf.apsInRange[ap_intf.node] = rssi
            ap_intf.stationsInRange[intf.node] = rssi

    @classmethod
    def updateParams(cls, intf, ap_intf):
        intf.freq = ap_intf.node.get_freq(intf)
        intf.channel = ap_intf.channel
        intf.mode = ap_intf.mode
        intf.ssid = ap_intf.ssid

    @classmethod
    def associate(cls, wlan, intf, ap_intf):
        "Associate to Access Point"
        if 'position' in intf.node.params:
            cls.configureWirelessLink(wlan, intf, ap_intf)
        else:
            cls.associate_infra(intf, ap_intf)

    @classmethod
    def associate_noEncrypt(cls, intf, ap_intf):
        #iwconfig is still necessary, since iw doesn't include essid like iwconfig does.
        debug(cls.iwconfig_con(intf.name, ap_intf.ssid, ap_intf.mac)+'\n')
        intf.node.pexec(cls.iwconfig_con(intf, ap_intf.ssid, ap_intf.mac))

    @classmethod
    def iwconfig_con(cls, intf, ssid, mac):
        cmd = 'iwconfig %s essid %s ap %s' % (intf, ssid, mac)
        return cmd

    @classmethod
    def disconnect(cls, intf):
        intf.node.pexec('iw dev %s disconnect' % intf.name)
        intf.rssi = 0
        intf.associatedTo = ''
        intf.channel = 0

    @classmethod
    def associate_infra(cls, intf, ap_intf):
        associated = 0
        if 'ieee80211r' in ap_intf.node.params and ap_intf.node.params['ieee80211r'] == 'yes' \
        and (not intf.encrypt or 'wpa' in intf.encrypt):
            if not intf.associatedTo:
                command = ('ps -aux | grep %s | wc -l' % intf.name)
                np = int(subprocess.check_output(command, shell=True))
                if np == 2:
                    cls.wpa(intf, ap_intf)
                else:
                    cls.handover_ieee80211r(intf, ap_intf)
            else:
                cls.handover_ieee80211r(intf, ap_intf)
            associated = 1
        elif not ap_intf.encrypt:
            associated = 1
            cls.associate_noEncrypt(intf, ap_intf)
        else:
            if not intf.associatedTo:
                if 'wpa' in ap_intf.encrypt \
                and (not intf.encrypt or 'wpa' in intf.encrypt):
                    cls.wpa(intf, ap_intf)
                    associated = 1
                elif ap_intf.encrypt == 'wep':
                    cls.wep(intf, ap_intf)
                    associated = 1
        if associated:
            cls.update(intf, ap_intf)

    @classmethod
    def wpaFile(cls, intf, ap_intf):
        cmd = ''
        if not ap_intf.config or not intf.config:
            if not ap_intf.authmode:
                if not intf.passwd:
                    passwd = ap_intf.passwd
                else:
                    passwd = intf.passwd

        if 'wpasup_globals' not in intf.node.params \
                or ('wpasup_globals' in intf.node.params
                    and 'ctrl_interface=' not in intf.node.params['wpasup_globals']):
            cmd = 'ctrl_interface=/var/run/wpa_supplicant\n'
        if 'wpasup_globals' in intf.node.params:
            cmd += intf.node.params['wpasup_globals'] + '\n'
        cmd = cmd + 'network={\n'

        if intf.config:
            config = intf.config
            if config is not []:
                config = intf.config.split(',')
                intf.node.params.pop("config", None)
                for conf in config:
                    cmd += "   " + conf + "\n"
        else:
            cmd += '   ssid=\"%s\"\n' % ap_intf.ssid
            if not ap_intf.authmode:
                cmd += '   psk=\"%s\"\n' % passwd
                encrypt = ap_intf.encrypt
                if ap_intf.encrypt == 'wpa3':
                    encrypt = 'wpa2'
                cmd += '   proto=%s\n' % encrypt.upper()
                cmd += '   pairwise=%s\n' % ap_intf.rsn_pairwise
                if ap_intf.active_scan:
                    cmd += '   scan_ssid=1\n'
                if intf.scan_freq:
                    cmd += '   scan_freq=%s\n' % intf.scan_freq
                if intf.freq_list:
                    cmd += '   freq_list=%s\n' % intf.freq_list
            wpa_key_mgmt = ap_intf.wpa_key_mgmt
            if ap_intf.encrypt == 'wpa3':
                wpa_key_mgmt = 'SAE'
            cmd += '   key_mgmt=%s\n' % wpa_key_mgmt
            if 'bgscan_threshold' in intf.node.params:
                if 'bgscan_module' not in intf.node.params:
                    intf.node.params['bgscan_module'] = 'simple'
                bgscan = 'bgscan=\"%s:%d:%d:%d\"' % \
                         (intf.bgscan_module, intf.s_inverval,
                          intf.bgscan_threshold, intf.l_interval)
                cmd += '   %s\n' % bgscan
            if ap_intf.authmode == '8021x':
                cmd += '   eap=PEAP\n'
                cmd += '   identity=\"%s\"\n' % intf.radius_identity
                cmd += '   password=\"%s\"\n' % intf.radius_passwd
                cmd += '   phase2=\"autheap=MSCHAPV2\"\n'
        cmd += '}'

        fileName = '%s.staconf' % intf.name
        os.system('echo \'%s\' > %s' % (cmd, fileName))

    @classmethod
    def wpa(cls, intf, ap_intf):
        pidfile = "mn%d_%s_%s_wpa.pid" % (os.getpid(), intf.node.name, intf.id)
        cls.wpaFile(intf, ap_intf)
        intf.node.wpa_pexec(pidfile, intf)

    @classmethod
    def handover_ieee80211r(cls, intf, ap_intf):
        intf.node.pexec('wpa_cli -i %s roam %s' % (intf.name, ap_intf.mac))

    @classmethod
    def wep(cls, intf, ap_intf):
        if not intf.passwd:
            passwd = ap_intf.passwd
        else:
            passwd = intf.passwd
        cls.wep_connect(passwd, intf, ap_intf)

    @classmethod
    def wep_connect(cls, passwd, intf, ap_intf):
        intf.node.pexec('iw dev %s connect %s key d:0:%s' % (intf.name, ap_intf.ssid, passwd))

    @classmethod
    def update(cls, intf, ap_intf):
        no_upt = ['active_scan', 'bgscan']
        if intf.associatedTo not in no_upt:
            if intf.associatedTo \
                    and intf.node in intf.associatedTo.params['associatedStations']:
                intf.associatedTo.params['associatedStations'].remove(intf.node)
            cls.updateParams(intf, ap_intf)
            ap_intf.associatedStations.append(intf.node)
            intf.associatedTo = ap_intf.node
