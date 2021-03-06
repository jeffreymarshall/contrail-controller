#
# Copyright (c) 2013 Juniper Networks, Inc. All rights reserved.
#

"""
This file contains implementation of transforming user-exposed VNC
configuration model/schema to a representation needed by VNC Control Plane
(BGP-based)
"""

import gevent
# Import kazoo.client before monkey patching
from cfgm_common.zkclient import ZookeeperClient
from gevent import monkey
monkey.patch_all()
import sys
reload(sys)
sys.setdefaultencoding('UTF8')
import requests
import ConfigParser
import signal
import random
import time
import hashlib
import argparse

from cfgm_common import vnc_cgitb
from cfgm_common.exceptions import *
from config_db import *

from pysandesh.sandesh_base import *
from pysandesh.sandesh_logger import *
from pysandesh.gen_py.sandesh.ttypes import SandeshLevel
from cfgm_common.uve.virtual_network.ttypes import *
from sandesh_common.vns.ttypes import Module
from pysandesh.connection_info import ConnectionState
from pysandesh.gen_py.process_info.ttypes import ConnectionType as ConnType
from pysandesh.gen_py.process_info.ttypes import ConnectionStatus
from db import SchemaTransformerDB
from logger import SchemaTransformerLogger
from st_amqp import STAmqpHandle


# connection to api-server
_vnc_lib = None
# zookeeper client connection
_zookeeper_client = None


class SchemaTransformer(object):

    REACTION_MAP = {
        'routing_instance': {
            'self': ['virtual_network'],
        },
        'virtual_machine_interface': {
            'self': ['virtual_machine', 'port_tuple', 'virtual_network',
                     'bgp_as_a_service'],
            'virtual_network': ['virtual_machine', 'port_tuple',
                                'bgp_as_a_service'],
            'logical_router': ['virtual_network'],
            'instance_ip': ['virtual_machine', 'port_tuple', 'bgp_as_a_service', 'virtual_network'],
            'floating_ip': ['virtual_machine', 'port_tuple'],
            'alias_ip': ['virtual_machine', 'port_tuple'],
            'virtual_machine': ['virtual_network'],
            'port_tuple': ['virtual_network'],
            'bgp_as_a_service': [],
        },
        'virtual_network': {
            'self': ['network_policy', 'route_table'],
            'routing_instance': ['network_policy'],
            'network_policy': [],
            'virtual_machine_interface': [],
            'route_table': [],
            'bgpvpn': [],
        },
        'virtual_machine': {
            'self': ['service_instance'],
            'virtual_machine_interface': ['service_instance'],
            'service_instance': ['virtual_machine_interface']
        },
        'port_tuple': {
            'self': ['service_instance'],
            'virtual_machine_interface': ['service_instance'],
            'service_instance': ['virtual_machine_interface']
        },
        'service_instance': {
            'self': ['network_policy', 'virtual_machine', 'port_tuple'],
            'route_table': ['network_policy', 'virtual_machine', 'port_tuple'],
            'routing_policy': ['network_policy'],
            'route_aggregate': ['network_policy'],
            'virtual_machine': ['network_policy'],
            'port_tuple': ['network_policy'],
            'network_policy': ['virtual_machine', 'port_tuple']
        },
        'network_policy': {
            'self': ['virtual_network', 'network_policy', 'service_instance'],
            'service_instance': ['virtual_network'],
            'network_policy': ['virtual_network'],
            'virtual_network': ['virtual_network', 'network_policy',
                                'service_instance']
        },
        'security_group': {
            'self': ['security_group'],
            'security_group': [],
        },
        'route_table': {
            'self': ['virtual_network', 'service_instance', 'logical_router'],
            'virtual_network': ['service_instance'],
            'logical_router': ['service_instance'],
        },
        'logical_router': {
            'self': ['route_table'],
            'virtual_machine_interface': [],
            'route_table': [],
            'bgpvpn': [],
        },
        'floating_ip': {
            'self': ['virtual_machine_interface'],
        },
        'alias_ip': {
            'self': ['virtual_machine_interface'],
        },
        'instance_ip': {
            'self': ['virtual_machine_interface'],
        },
        'bgp_as_a_service': {
            'self': ['bgp_router'],
            'virtual_machine_interface': ['bgp_router']
        },
        'bgp_router': {
            'self': [],
            'bgp_as_a_service': [],
        },
        'global_system_config': {
            'self': [],
        },
        'routing_policy': {
            'self': ['service_instance'],
        },
        'route_aggregate': {
            'self': ['service_instance'],
        },
        'bgpvpn': {
            'self': ['virtual_network', 'logical_router'],
            'virtual_network': [],
            'logical_router': [],
        },
    }

    _schema_transformer = None

    def __init__(self, st_logger=None, args=None):
        self._args = args
        self._fabric_rt_inst_obj = None

        if st_logger is not None:
            self.logger = st_logger
        else:
            # Initialize logger
            self.logger = SchemaTransformerLogger(args)

        # Initialize amqp
        self._vnc_amqp = STAmqpHandle(self.logger, self.REACTION_MAP,
                                      self._args)
        self._vnc_amqp.establish()
        try:
            # Initialize cassandra
            self._object_db = SchemaTransformerDB(self, _zookeeper_client)
            DBBaseST.init(self, self.logger, self._object_db)
            DBBaseST._sandesh = self.logger._sandesh
            DBBaseST._vnc_lib = _vnc_lib
            ServiceChain.init()
            self.reinit()
            SchemaTransformer._schema_transformer = self
            self._vnc_amqp._db_resync_done.set()
        except Exception as e:
            # If any of the above tasks like CassandraDB read fails, cleanup
            # the RMQ constructs created earlier and then give up.
            SchemaTransformer.destroy_instance()
            SchemaTransformer._schema_transformer = None
            raise e
    # end __init__

    # Clean up stale objects
    def reinit(self):
        GlobalSystemConfigST.reinit()
        BgpRouterST.reinit()
        BgpvpnST.reinit()
        LogicalRouterST.reinit()
        gevent.sleep(0.001)
        for si in ServiceInstanceST.list_vnc_obj():
            try:
                si_st = ServiceInstanceST.locate(si.get_fq_name_str(), si)
                if si_st is None:
                    continue
                for ref in si.get_virtual_machine_back_refs() or []:
                    vm_name = ':'.join(ref['to'])
                    vm = VirtualMachineST.locate(vm_name)
                    si_st.virtual_machines.add(vm_name)
                props = si.get_service_instance_properties()
                if not props.auto_policy:
                    continue
                si_st.add_properties(props)
            except Exception as e:
                self.logger.error("Error in reinit service instance %s: %s" % (
                    si.get_fq_name_str(), str(e)))

        vn_list = list(VirtualNetworkST.list_vnc_obj())
        vn_id_list = set([vn.uuid for vn in vn_list])
        ri_dict = {}
        service_ri_dict = {}
        ri_deleted = {}
        for ri in DBBaseST.list_vnc_obj('routing_instance'):
            delete = False
            if ri.parent_uuid not in vn_id_list:
                delete = True
                ri_deleted.setdefault(ri.parent_uuid, []).append(ri.uuid)
            else:
                try:
                    # if the RI was for a service chain and service chain no
                    # longer exists, delete the RI
                    sc_id = RoutingInstanceST._get_service_id_from_ri(
                        ri.get_fq_name_str())
                    if sc_id:
                        if sc_id not in ServiceChain:
                            delete = True
                        else:
                            service_ri_dict[ri.get_fq_name_str()] = ri
                    else:
                        ri_dict[ri.get_fq_name_str()] = ri
                except Exception as e:
                    self.logger.error(
                        "Error while reinitializing routing instance %s: %s"%(
                        ri.get_fq_name_str(), str(e)))
            if delete:
                try:
                    ri_obj = RoutingInstanceST(ri.get_fq_name_str(), ri)
                    ri_obj.delete_obj()
                except NoIdError:
                    pass
                except Exception as e:
                    self.logger.error(
                        "Error while deleting routing instance %s: %s"%(
                        ri.get_fq_name_str(), str(e)))
        # end for ri

        sg_list = list(SecurityGroupST.list_vnc_obj())
        sg_id_list = [sg.uuid for sg in sg_list]
        sg_acl_dict = {}
        vn_acl_dict = {}
        for acl in DBBaseST.list_vnc_obj('access_control_list', fields=['access_control_list_hash']):
            delete = False
            if acl.parent_type == 'virtual-network':
                if acl.parent_uuid in vn_id_list:
                    vn_acl_dict[acl.uuid] = acl
                else:
                    delete = True
            elif acl.parent_type == 'security-group':
                if acl.parent_uuid in sg_id_list:
                    sg_acl_dict[acl.uuid] = acl
                else:
                    delete = True
            else:
                delete = True

            if delete:
                try:
                    _vnc_lib.access_control_list_delete(id=acl.uuid)
                except NoIdError:
                    pass
                except Exception as e:
                    self.logger.error("Error while deleting acl %s: %s"%(
                            acl.uuid, str(e)))
        # end for acl

        gevent.sleep(0.001)
        for sg in sg_list:
            try:
                SecurityGroupST.locate(sg.get_fq_name_str(), sg, sg_acl_dict)
            except Exception as e:
                self.logger.error("Error in reinit security-group %s: %s" % (
                    sg.get_fq_name_str(), str(e)))

        # update sg rules after all SG objects are initialized to avoid
        # rewriting of ACLs multiple times
        for sg in SecurityGroupST.values():
            try:
                sg.update_policy_entries()
            except Exception as e:
                self.logger.error("Error in updating SG policies %s: %s" % (
                    sg.name, str(e)))

        gevent.sleep(0.001)
        RouteTargetST.reinit()
        for vn in vn_list:
            if vn.uuid in ri_deleted:
                vn_ri_list = vn.get_routing_instances() or []
                new_vn_ri_list = [vn_ri for vn_ri in vn_ri_list
                                  if vn_ri['uuid'] not in ri_deleted[vn.uuid]]
                vn.routing_instances = new_vn_ri_list
            try:
                VirtualNetworkST.locate(vn.get_fq_name_str(), vn, vn_acl_dict)
            except Exception as e:
                self.logger.error("Error in reinit virtual network %s: %s" % (
                    vn.get_fq_name_str(), str(e)))
        for ri_name, ri_obj in ri_dict.items():
            try:
                RoutingInstanceST.locate(ri_name, ri_obj)
            except Exception as e:
                self.logger.error("Error in reinit routing instance %s: %s" % (
                    ri_name, str(e)))
        # Initialize service instance RI's after Primary RI's
        for si_ri_name, si_ri_obj in service_ri_dict.items():
            try:
                RoutingInstanceST.locate(si_ri_name, si_ri_obj)
            except Exception as e:
                self.logger.error("Error in reinit routing instance %s: %s" % (
                    si_ri_name, str(e)))

        NetworkPolicyST.reinit()
        gevent.sleep(0.001)
        VirtualMachineInterfaceST.reinit()

        gevent.sleep(0.001)
        InstanceIpST.reinit()
        gevent.sleep(0.001)
        FloatingIpST.reinit()
        AliasIpST.reinit()

        gevent.sleep(0.001)
        RoutingPolicyST.reinit()
        gevent.sleep(0.001)
        RouteAggregateST.reinit()
        gevent.sleep(0.001)
        PortTupleST.reinit()
        BgpAsAServiceST.reinit()
        RouteTableST.reinit()

        # evaluate virtual network objects first because other objects,
        # e.g. vmi, depend on it.
        for vn_obj in VirtualNetworkST.values():
            try:
                vn_obj.evaluate()
            except Exception as e:
                self.logger.error("Error in reinit evaluate virtual network %s: %s" % (
                    vn_obj.name, str(e)))
        for cls in DBBaseST.get_obj_type_map().values():
            if cls is VirtualNetworkST:
                continue
            for obj in cls.values():
                try:
                    obj.evaluate()
                except Exception as e:
                    self.logger.error("Error in reinit evaluate %s %s: %s" % (
                        cls.obj_type, obj.name, str(e)))
        self.process_stale_objects()
    # end reinit

    def cleanup(self):
        # TODO cleanup sandesh context
        pass
    # end cleanup

    def process_stale_objects(self):
        for sc in ServiceChain.values():
            if sc.created_stale:
                sc.destroy()
            if sc.present_stale:
                sc.delete()
            for rinst in RoutingInstanceST.values():
                if rinst.stale_route_targets:
                    rinst.update_route_target_list(
                            rt_del=rinst.stale_route_targets)
    # end process_stale_objects

    @classmethod
    def get_instance(cls):
        return cls._schema_transformer

    @classmethod
    def destroy_instance(cls):
        inst = cls.get_instance()
        inst._vnc_amqp.close()
        for obj_cls in DBBaseST.get_obj_type_map().values():
            obj_cls.reset()
        DBBase.clear()
        inst._object_db = None
        cls._schema_transformer = None

    def sighup_handler(self):
        if self._conf_file:
            config = ConfigParser.SafeConfigParser()
            config.read(self._conf_file)
            if 'DEFAULTS' in config.sections():
                try:
                    collectors = config.get('DEFAULTS', 'collectors')
                    if type(collectors) is str:
                        collectors = collectors.split()
                        new_chksum = hashlib.md5("".join(collectors)).hexdigest()
                        if new_chksum != self._chksum:
                            self._chksum = new_chksum
                            config.random_collectors = random.sample(collectors, len(collectors))
                            self.logger.sandesh_reconfig_collectors(config)
                except ConfigParser.NoOptionError as e:
                    pass
    # end sighup_handler

# end class SchemaTransformer


def parse_args(args_str):
    # Turn off help, so we      all options in response to -h
    conf_parser = argparse.ArgumentParser(add_help=False)

    conf_parser.add_argument("-c", "--conf_file", action='append',
                             help="Specify config file", metavar="FILE")
    args, remaining_argv = conf_parser.parse_known_args(args_str.split())

    defaults = {
        'rabbit_server': 'localhost',
        'rabbit_port': '5672',
        'rabbit_user': 'guest',
        'rabbit_password': 'guest',
        'rabbit_vhost': None,
        'rabbit_ha_mode': False,
        'cassandra_server_list': '127.0.0.1:9160',
        'api_server_ip': '127.0.0.1',
        'api_server_port': '8082',
        'api_server_use_ssl': False,
        'zk_server_ip': '127.0.0.1',
        'zk_server_port': '2181',
        'collectors': None,
        'http_server_port': '8087',
        'log_local': False,
        'log_level': SandeshLevel.SYS_DEBUG,
        'log_category': '',
        'log_file': Sandesh._DEFAULT_LOG_FILE,
        'trace_file': '/var/log/contrail/schema.err',
        'use_syslog': False,
        'syslog_facility': Sandesh._DEFAULT_SYSLOG_FACILITY,
        'cluster_id': '',
        'logging_conf': '',
        'logger_class': None,
        'sandesh_send_rate_limit': SandeshSystem.get_sandesh_send_rate_limit(),
        'bgpaas_port_start': 50000,
        'bgpaas_port_end': 50512,
        'rabbit_use_ssl': False,
        'kombu_ssl_version': '',
        'kombu_ssl_keyfile': '',
        'kombu_ssl_certfile': '',
        'kombu_ssl_ca_certs': '',
        'zk_timeout': 400,
        'logical_routers_enabled': True,
        'acl_direction_comp': False,
    }
    secopts = {
        'use_certs': False,
        'keyfile': '',
        'certfile': '',
        'ca_certs': '',
    }
    ksopts = {
        'admin_user': 'user1',
        'admin_password': 'password1',
        'admin_tenant_name': 'default-domain'
    }
    cassandraopts = {
        'cassandra_user': None,
        'cassandra_password': None,
    }
    sandeshopts = {
        'sandesh_keyfile': '/etc/contrail/ssl/private/server-privkey.pem',
        'sandesh_certfile': '/etc/contrail/ssl/certs/server.pem',
        'sandesh_ca_cert': '/etc/contrail/ssl/certs/ca-cert.pem',
        'sandesh_ssl_enable': False,
        'introspect_ssl_enable': False
    }

    saved_conf_file = args.conf_file
    if args.conf_file:
        config = ConfigParser.SafeConfigParser()
        config.read(args.conf_file)
        defaults.update(dict(config.items("DEFAULTS")))
        if ('SECURITY' in config.sections() and
                'use_certs' in config.options('SECURITY')):
            if config.getboolean('SECURITY', 'use_certs'):
                secopts.update(dict(config.items("SECURITY")))
        if 'KEYSTONE' in config.sections():
            ksopts.update(dict(config.items("KEYSTONE")))

        if 'CASSANDRA' in config.sections():
                cassandraopts.update(dict(config.items('CASSANDRA')))
        if 'SANDESH' in config.sections():
            sandeshopts.update(dict(config.items('SANDESH')))
            if 'sandesh_ssl_enable' in config.options('SANDESH'):
                sandeshopts['sandesh_ssl_enable'] = config.getboolean(
                    'SANDESH', 'sandesh_ssl_enable')
            if 'introspect_ssl_enable' in config.options('SANDESH'):
                sandeshopts['introspect_ssl_enable'] = config.getboolean(
                    'SANDESH', 'introspect_ssl_enable')

    # Override with CLI options
    # Don't surpress add_help here so it will handle -h
    parser = argparse.ArgumentParser(
        # Inherit options from config_parser
        parents=[conf_parser],
        # print script description with -h/--help
        description=__doc__,
        # Don't mess with format of description
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    defaults.update(secopts)
    defaults.update(ksopts)
    defaults.update(cassandraopts)
    defaults.update(sandeshopts)
    parser.set_defaults(**defaults)
    def _bool(s):
        """Convert string to bool (in argparse context)."""
        if s.lower() not in ['true', 'false']:
            raise ValueError('Need bool; got %r' % s)
        return {'true': True, 'false': False}[s.lower()]

    parser.add_argument(
        "--cassandra_server_list",
        help="List of cassandra servers in IP Address:Port format",
        nargs='+')
    parser.add_argument(
        "--reset_config", action="store_true",
        help="Warning! Destroy previous configuration and start clean")
    parser.add_argument("--api_server_ip",
                        help="IP address of API server")
    parser.add_argument("--api_server_port",
                        help="Port of API server")
    parser.add_argument("--api_server_use_ssl",
                        help="Use SSL to connect with API server")
    parser.add_argument("--zk_server_ip",
                        help="IP address:port of zookeeper server")
    parser.add_argument("--collectors",
                        help="List of VNC collectors in ip:port format",
                        nargs="+")
    parser.add_argument("--http_server_port",
                        help="Port of local HTTP server")
    parser.add_argument("--log_local", action="store_true",
                        help="Enable local logging of sandesh messages")
    parser.add_argument(
        "--log_level",
        help="Severity level for local logging of sandesh messages")
    parser.add_argument(
        "--log_category",
        help="Category filter for local logging of sandesh messages")
    parser.add_argument("--log_file",
                        help="Filename for the logs to be written to")
    parser.add_argument("--trace_file", help="Filename for the error "
                        "backtraces to be written to")
    parser.add_argument("--use_syslog", action="store_true",
                        help="Use syslog for logging")
    parser.add_argument("--syslog_facility",
                        help="Syslog facility to receive log lines")
    parser.add_argument("--admin_user",
                        help="Name of keystone admin user")
    parser.add_argument("--admin_password",
                        help="Password of keystone admin user")
    parser.add_argument("--admin_tenant_name",
                        help="Tenant name for keystone admin user")
    parser.add_argument("--cluster_id",
                        help="Used for database keyspace separation")
    parser.add_argument(
        "--logging_conf",
        help=("Optional logging configuration file, default: None"))
    parser.add_argument(
        "--logger_class",
        help=("Optional external logger class, default: None"))
    parser.add_argument("--cassandra_user", help="Cassandra user name")
    parser.add_argument("--cassandra_password", help="Cassandra password")
    parser.add_argument("--sandesh_send_rate_limit", type=int,
            help="Sandesh send rate limit in messages/sec")
    parser.add_argument("--rabbit_server", help="Rabbitmq server address")
    parser.add_argument("--rabbit_port", help="Rabbitmq server port")
    parser.add_argument("--rabbit_user", help="Username for rabbit")
    parser.add_argument("--rabbit_vhost", help="vhost for rabbit")
    parser.add_argument("--rabbit_password", help="password for rabbit")
    parser.add_argument("--rabbit_ha_mode", action='store_true',
        help="True if the rabbitmq cluster is mirroring all queue")
    parser.add_argument("--bgpaas_port_start", type=int,
                        help="Start port for bgp-as-a-service proxy")
    parser.add_argument("--bgpaas_port_end", type=int,
                        help="End port for bgp-as-a-service proxy")
    parser.add_argument("--zk_timeout", type=int,
                        help="Timeout for ZookeeperClient")
    parser.add_argument("--logical_routers_enabled", type=_bool,
                        help="Enabled logical routers")
    parser.add_argument("--acl_direction_comp", type=_bool,
                        help="Acl direction compression")

    args = parser.parse_args(remaining_argv)
    args.conf_file = saved_conf_file
    if type(args.cassandra_server_list) is str:
        args.cassandra_server_list = args.cassandra_server_list.split()
    if type(args.collectors) is str:
        args.collectors = args.collectors.split()
    args.sandesh_config = SandeshConfig(args.sandesh_keyfile,
        args.sandesh_certfile, args.sandesh_ca_cert,
        args.sandesh_ssl_enable, args.introspect_ssl_enable)

    return args
# end parse_args


transformer = None


def run_schema_transformer(st_logger, args):
    global _vnc_lib

    st_logger.notice("Elected master Schema Transformer node. Initializing...")

    def connection_state_update(status, message=None):
        ConnectionState.update(
            conn_type=ConnType.APISERVER, name='ApiServer',
            status=status, message=message or '',
            server_addrs=['%s:%s' % (args.api_server_ip,
                                     args.api_server_port)])
    # end connection_state_update

    # Retry till API server is up
    connected = False
    connection_state_update(ConnectionStatus.INIT)
    while not connected:
        try:
            _vnc_lib = VncApi(
                args.admin_user, args.admin_password, args.admin_tenant_name,
                args.api_server_ip, args.api_server_port,
                api_server_use_ssl=args.api_server_use_ssl)
            connected = True
            connection_state_update(ConnectionStatus.UP)
        except requests.exceptions.ConnectionError as e:
            # Update connection info
            connection_state_update(ConnectionStatus.DOWN, str(e))
            time.sleep(3)
        except (RuntimeError, ResourceExhaustionError):
            # auth failure or haproxy throws 503
            time.sleep(3)

    global transformer
    transformer = SchemaTransformer(st_logger, args)
    transformer._conf_file = args.conf_file
    transformer._chksum = ""
    # checksum of collector list
    if args.collectors:
        transformer._chksum = hashlib.md5("".join(args.collectors)).hexdigest()

    """ @sighup
    SIGHUP handler to indicate configuration changes
    """
    gevent.signal(signal.SIGHUP, transformer.sighup_handler)

    try:
        gevent.joinall(transformer._vnc_amqp._vnc_kombu.greenlets())
    except KeyboardInterrupt:
        SchemaTransformer.destroy_instance()
        raise
# end run_schema_transformer


def main(args_str=None):
    global _zookeeper_client

    if not args_str:
        args_str = ' '.join(sys.argv[1:])
    args = parse_args(args_str)
    args._args_list = args_str
    if args.cluster_id:
        client_pfx = args.cluster_id + '-'
        zk_path_pfx = args.cluster_id + '/'
    else:
        client_pfx = ''
        zk_path_pfx = ''

    # randomize collector list
    args.random_collectors = args.collectors
    if args.collectors:
        args.random_collectors = random.sample(args.collectors,
                                               len(args.collectors))

    # Initialize logger
    st_logger = SchemaTransformerLogger(args)

    # Initialize AMQP handler then close it to be sure remain queue of a
    # precedent run is cleaned
    vnc_amqp = STAmqpHandle(st_logger, SchemaTransformer.REACTION_MAP, args)
    vnc_amqp.establish()
    vnc_amqp.close()
    st_logger.debug("Removed remained AMQP queue")

    # Waiting to be elected as master node
    _zookeeper_client = ZookeeperClient(client_pfx+"schema", args.zk_server_ip,
                                        zk_timeout=args.zk_timeout)
    st_logger.notice("Waiting to be elected as master...")
    _zookeeper_client.master_election(zk_path_pfx + "/schema-transformer",
                                      os.getpid(), run_schema_transformer,
                                      st_logger, args)
# end main


def server_main():
    vnc_cgitb.enable(format='text')
    main()
# end server_main

if __name__ == '__main__':
    server_main()
