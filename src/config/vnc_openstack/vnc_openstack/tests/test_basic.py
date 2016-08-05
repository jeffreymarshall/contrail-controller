import sys
import json

from testtools.matchers import Equals, Contains
from testtools import ExpectedException
import webtest.app

sys.path.append('../common/tests')
from test_utils import *
import test_common

import test_case

class TestBasic(test_case.NeutronBackendTestCase):
    def read_resource(self, url_pfx, id):
        context = {'operation': 'READ',
                   'user_id': '',
                   'roles': ''}
        data = {'fields': None,
                'id': id}
        body = {'context': context, 'data': data}
        resp = self._api_svr_app.post_json('/neutron/%s' %(url_pfx), body)
        return json.loads(resp.text)
    # end read_resource

    def list_resource(self, url_pfx,
        proj_uuid=None, req_fields=None, req_filters=None):
        if proj_uuid == None:
            proj_uuid = self._vnc_lib.fq_name_to_id('project',
            fq_name=['default-domain', 'default-project'])

        context = {'operation': 'READALL',
                   'user_id': '',
                   'tenant_id': proj_uuid,
                   'roles': '',
                   'is_admin': 'False'}
        data = {'fields': req_fields, 'filters': req_filters or {}}
        body = {'context': context, 'data': data}
        resp = self._api_svr_app.post_json(
            '/neutron/%s' %(url_pfx), body)
        return json.loads(resp.text)
    # end list_resource

    def test_list_with_inconsistent_members(self):
        # 1. create collection
        # 2. list, verify full collection
        # 3. mess with one in vnc_to_neutron, verify collection-1
        # 4. restore, list, verify full collection
        proj_obj = self._vnc_lib.project_read(
            fq_name=['default-domain', 'default-project'])

        objects = {}
        for (obj_type, obj_class, create_method_name) in \
            [('virtual_network', vnc_api.VirtualNetwork,
              'virtual_network_create'),
             ('network_ipam', vnc_api.NetworkIpam,
              'network_ipam_create'),
             ('network_policy', vnc_api.NetworkPolicy,
              'network_policy_create'),
             ('logical_router', vnc_api.LogicalRouter,
              'logical_router_create'),
             ('security_group', vnc_api.SecurityGroup,
              'security_group_create'),
             ('route_table', vnc_api.RouteTable,
              'route_table_create'),
             ('service_instance', vnc_api.ServiceInstance,
              'service_instance_create')]:
            objects[obj_type] = [obj_class('%s-%s' %(self.id(), i))
                                 for i in range(3)]
            for obj in objects[obj_type]:
                create_method = getattr(self._vnc_lib, create_method_name)
                create_method(obj)

        objects['virtual_machine_interface'] = \
            [vnc_api.VirtualMachineInterface('%s-%s' %(self.id(), i), proj_obj)
             for i in range(3)]
        for obj in objects['virtual_machine_interface']:
            obj.add_virtual_network(vnc_api.VirtualNetwork())
            self._vnc_lib.virtual_machine_interface_create(obj)

        vn_obj = vnc_api.VirtualNetwork(self.id())
        sn0_id = str(uuid.uuid4())
        sn1_id = str(uuid.uuid4())
        sn2_id = str(uuid.uuid4())
        vn_obj.add_network_ipam(vnc_api.NetworkIpam(),
            vnc_api.VnSubnetsType(
                [vnc_api.IpamSubnetType(vnc_api.SubnetType('1.1.1.0', 28),
                                        subnet_uuid=sn0_id),
                 vnc_api.IpamSubnetType(vnc_api.SubnetType('2.2.2.0', 28),
                                        subnet_uuid=sn1_id),
                 vnc_api.IpamSubnetType(vnc_api.SubnetType('3.3.3.0', 28),
                                        subnet_uuid=sn2_id)]))
        self._vnc_lib.virtual_network_create(vn_obj)

        fip_pool_obj = vnc_api.FloatingIpPool(self.id(), vn_obj)
        self._vnc_lib.floating_ip_pool_create(fip_pool_obj)
        objects['floating_ip'] = [vnc_api.FloatingIp('%s-%s' %(self.id(), i),
            fip_pool_obj)
            for i in range(3)]
        for obj in objects['floating_ip']:
            obj.add_project(proj_obj)
            self._vnc_lib.floating_ip_create(obj)

        collection_types = [
            (objects['virtual_network'], 'network',
             '_network_vnc_to_neutron'),
            (objects['virtual_machine_interface'], 'port',
             '_port_vnc_to_neutron'),
            (objects['network_ipam'], 'ipam',
             '_ipam_vnc_to_neutron'),
            (objects['network_policy'], 'policy',
             '_policy_vnc_to_neutron'),
            (objects['logical_router'], 'router',
             '_router_vnc_to_neutron'),
            (objects['floating_ip'], 'floatingip',
             '_floatingip_vnc_to_neutron'),
            (objects['security_group'], 'security_group',
             '_security_group_vnc_to_neutron'),
            (objects['route_table'], 'route_table',
             '_route_table_vnc_to_neutron'),
            (objects['service_instance'], 'nat_instance',
             '_svc_instance_vnc_to_neutron'),
        ]

        def list_resource(url_pfx):
            context = {'operation': 'READALL',
                       'user_id': '',
                       'tenant_id': proj_obj.uuid,
                       'roles': '',
                       'is_admin': 'False'}
            data = {'fields': None, 'filters': {}}
            body = {'context': context, 'data': data}
            resp = self._api_svr_app.post_json(
                '/neutron/%s' %(url_pfx), body)
            return json.loads(resp.text)

        # for collections that are objects in contrail model
        for (objects, res_url_pfx, res_xlate_name) in collection_types:
            res_dicts = list_resource(res_url_pfx)
            present_ids = [r['id'] for r in res_dicts] 
            for obj in objects:
                self.assertIn(obj.uuid, present_ids)

            neutron_api_obj = FakeExtensionManager.get_extension_objects(
                'vnc_cfg_api.neutronApi')[0]
            neutron_db_obj = neutron_api_obj._npi._cfgdb

            def err_on_object_2(orig_method, res_obj, *args, **kwargs):
                if res_obj.uuid == objects[2].uuid:
                    raise Exception('faking inconsistent element')
                return orig_method(res_obj, *args, **kwargs)

            with test_common.patch(
                neutron_db_obj, res_xlate_name, err_on_object_2):
                res_dicts = list_resource(res_url_pfx)
                present_ids = [r['id'] for r in res_dicts] 
                self.assertNotIn(objects[2].uuid, present_ids)

            res_dicts = list_resource(res_url_pfx)
            present_ids = [r['id'] for r in res_dicts] 
            for obj in objects:
                self.assertIn(obj.uuid, present_ids)
        # end for collections that are objects in contrail model

        # subnets, sg-rules etc.
        res_dicts = list_resource('subnet')
        present_ids = [r['id'] for r in res_dicts]
        for sn_id in [sn0_id, sn1_id, sn2_id]:
            self.assertIn(sn_id, present_ids)

        def err_on_sn2(orig_method, subnet_vnc, *args, **kwargs):
            if subnet_vnc.subnet_uuid == sn2_id:
                raise Exception('faking inconsistent element')
            return orig_method(subnet_vnc, *args, **kwargs)

        with test_common.patch(
            neutron_db_obj, '_subnet_vnc_to_neutron', err_on_sn2):
            res_dicts = list_resource('subnet')
            present_ids = [r['id'] for r in res_dicts] 
            self.assertNotIn(sn2_id, present_ids)
    # end test_list_with_inconsistent_members

    def test_subnet_uuid_heal(self):
        # 1. create 2 subnets thru vnc_api
        # 2. mess with useragent-kv index for one
        # 3. neutron subnet list should report fine
        # 4. neutron subnet list with uuids where
        #    one is a fake uuid shouldnt cause error in list
        ipam_obj = vnc_api.NetworkIpam('ipam-%s' %(self.id()))
        self._vnc_lib.network_ipam_create(ipam_obj)
        vn1_obj = vnc_api.VirtualNetwork('vn1-%s' %(self.id()))
        sn1_uuid = str(uuid.uuid4())
        vn1_obj.add_network_ipam(ipam_obj,
            vnc_api.VnSubnetsType(
                [vnc_api.IpamSubnetType(vnc_api.SubnetType('1.1.1.0', 28),
                                        subnet_uuid=sn1_uuid)]))
        self._vnc_lib.virtual_network_create(vn1_obj)
        vn2_obj = vnc_api.VirtualNetwork('vn2-%s' %(self.id()))
        sn2_uuid = str(uuid.uuid4())
        vn2_obj.add_network_ipam(ipam_obj,
            vnc_api.VnSubnetsType(
                [vnc_api.IpamSubnetType(vnc_api.SubnetType('2.2.2.0', 28),
                                        subnet_uuid=sn2_uuid)]))
        self._vnc_lib.virtual_network_create(vn2_obj)

        # the list primes cfgdb handle(conn to api server)
        self.list_resource('subnet')
        neutron_api_obj = FakeExtensionManager.get_extension_objects(
            'vnc_cfg_api.neutronApi')[0]
        neutron_db_obj = neutron_api_obj._npi._cfgdb

        heal_invoked = [False]
        def verify_heal_invoked(orig_method, *args, **kwargs):
            heal_invoked[0] = True
            return orig_method(*args, **kwargs)
        with test_common.patch(
            neutron_db_obj, 'subnet_id_heal', verify_heal_invoked):
            with CassandraCFs.get_cf(
                'useragent_keyval_table').patch_row(sn2_uuid, None):
                # verify heal
                rsp = self.list_resource('subnet',
                    req_filters={'id': [sn1_uuid, sn2_uuid]})
                self.assertEqual(heal_invoked[0], True)
                self.assertEqual(len(rsp), 2)
                self.assertEqual(set([r['id'] for r in rsp]),
                                 set([sn1_uuid, sn2_uuid]))

                # verify wrong/non-existent id doesn't cause
                # list to have error
                heal_invoked[0] = False
                fake_uuid = str(uuid.uuid4())
                rsp = self.list_resource('subnet',
                    req_filters={'id': [sn1_uuid, sn2_uuid, fake_uuid]})
                self.assertEqual(heal_invoked[0], True)
                self.assertEqual(len(rsp), 2)
                self.assertEqual(set([r['id'] for r in rsp]),
                                 set([sn1_uuid, sn2_uuid]))

                # verify read of non-existent id throws exception
                # and valid one doesn't
                with ExpectedException(webtest.app.AppError):
                    self.read_resource('subnet', fake_uuid)
                self.read_resource('subnet', sn1_uuid)
    # end test_subnet_uuid_heal

    def test_extra_fields_on_network(self):
        test_obj = self._create_test_object()
        context = {'operation': 'READ',
                   'user_id': '',
                   'roles': ''}
        data = {'fields': None,
                'id': test_obj.uuid}
        body = {'context': context, 'data': data}
        resp = self._api_svr_app.post_json('/neutron/network', body)
        net_dict = json.loads(resp.text)
        self.assertIn('contrail:fq_name', net_dict)
    # end test_extra_fields_on_network

    def test_port_bindings(self):
        vn_obj = vnc_api.VirtualNetwork(self.id())
        vn_obj.add_network_ipam(vnc_api.NetworkIpam(),
            vnc_api.VnSubnetsType(
                [vnc_api.IpamSubnetType(
                         vnc_api.SubnetType('1.1.1.0', 24))]))
        self._vnc_lib.virtual_network_create(vn_obj)

        sg_obj = vnc_api.SecurityGroup('default')
        self._vnc_lib.security_group_create(sg_obj)

        proj_uuid = self._vnc_lib.fq_name_to_id('project',
            fq_name=['default-domain', 'default-project'])

        context = {'operation': 'CREATE',
                   'user_id': '',
                   'is_admin': True,
                   'roles': ''}
        data = {'resource':{'network_id': vn_obj.uuid,
                            'tenant_id': proj_uuid,
                            'binding:profile': {'foo': 'bar'},
                            'binding:host_id': 'somehost'}}
        body = {'context': context, 'data': data}
        resp = self._api_svr_app.post_json('/neutron/port', body)
        port_dict = json.loads(resp.text)
        self.assertTrue(isinstance(port_dict['binding:profile'], dict))
        self.assertTrue(isinstance(port_dict['binding:host_id'], basestring))
    # end test_port_bindings
# end class TestBasic

class TestExtraFieldsPresenceByKnob(test_case.NeutronBackendTestCase):
    @classmethod
    def setUpClass(cls):
        super(TestExtraFieldsPresenceByKnob, cls).setUpClass(
            extra_config_knobs=[('NEUTRON', 'contrail_extensions_enabled', True)])

    def test_extra_fields_on_network(self):
        test_obj = self._create_test_object()
        context = {'operation': 'READ',
                   'user_id': '',
                   'roles': ''}
        data = {'fields': None,
                'id': test_obj.uuid}
        body = {'context': context, 'data': data}
        resp = self._api_svr_app.post_json('/neutron/network', body)
        net_dict = json.loads(resp.text)
        self.assertIn('contrail:fq_name', net_dict)
    # end test_extra_fields_on_network
# end class TestExtraFieldsPresenceByKnob

class TestExtraFieldsAbsenceByKnob(test_case.NeutronBackendTestCase):
    @classmethod
    def setUpClass(cls):
        super(TestExtraFieldsAbsenceByKnob, cls).setUpClass(
            extra_config_knobs=[('NEUTRON', 'contrail_extensions_enabled', False)])

    def test_no_extra_fields_on_network(self):
        test_obj = self._create_test_object()
        context = {'operation': 'READ',
                   'user_id': '',
                   'roles': ''}
        data = {'fields': None,
                'id': test_obj.uuid}
        body = {'context': context, 'data': data}
        resp = self._api_svr_app.post_json('/neutron/network', body)
        net_dict = json.loads(resp.text)
        self.assertNotIn('contrail:fq_name', net_dict)
    # end test_extra_fields_on_network
# end class TestExtraFieldsAbsenceByKnob


class TestListWithFilters(test_case.NeutronBackendTestCase):
    def test_filters_with_id(self):
        neutron_api_obj = FakeExtensionManager.get_extension_objects(
            'vnc_cfg_api.neutronApi')[0]
        neutron_api_obj._npi._connect_to_db()
        neutron_db_obj = neutron_api_obj._npi._cfgdb

        # sg setup
        proj_obj = self._vnc_lib.project_read(
            fq_name=['default-domain', 'default-project'])
        sg_obj = vnc_api.SecurityGroup('sg-%s' %(self.id()), proj_obj)
        self._vnc_lib.security_group_create(sg_obj)

        # fip setup
        vn_obj = vnc_api.VirtualNetwork('vn1-%s' %(self.id()), proj_obj)
        vn_obj.add_network_ipam(vnc_api.NetworkIpam(),
            vnc_api.VnSubnetsType(
                [vnc_api.IpamSubnetType(vnc_api.SubnetType('1.1.1.0', 28))]))
        self._vnc_lib.virtual_network_create(vn_obj)
        fip_pool_obj = vnc_api.FloatingIpPool('fip-pool-%s' %(self.id()),
                                               vn_obj)
        self._vnc_lib.floating_ip_pool_create(fip_pool_obj)
        fip1_obj = vnc_api.FloatingIp('fip1-%s' %(self.id()), fip_pool_obj)
        fip1_obj.add_project(proj_obj)
        self._vnc_lib.floating_ip_create(fip1_obj)

        proj2_obj = vnc_api.Project('proj2-%s' %(self.id()), vnc_api.Domain())
        self._vnc_lib.project_create(proj2_obj)
        fip2_obj = vnc_api.FloatingIp('fip2-%s' %(self.id()), fip_pool_obj)
        fip2_obj.add_project(proj2_obj)
        self._vnc_lib.floating_ip_create(fip2_obj)

        vmi_obj = vnc_api.VirtualMachineInterface(
                      'vmi-%s' %(self.id()), proj_obj)
        vmi_obj.add_virtual_network(vn_obj)
        self._vnc_lib.virtual_machine_interface_create(vmi_obj)
        fip3_obj = vnc_api.FloatingIp('fip3-%s' %(self.id()), fip_pool_obj)
        fip3_obj.add_virtual_machine_interface(vmi_obj)
        fip3_obj.add_project(proj_obj)
        self._vnc_lib.floating_ip_create(fip3_obj)

        def spy_list(orig_method, *args, **kwargs):
            self.assertIn(sg_obj.uuid, kwargs['obj_uuids'])
            return orig_method(*args, **kwargs)
        with test_common.patch(
            neutron_db_obj._vnc_lib, 'security_groups_list', spy_list):
            context = {'operation': 'READALL',
                       'user_id': '',
                       'tenant_id': proj_obj.uuid,
                       'roles': '',
                       'is_admin': 'False'}
            data = {'filters': {'id':[sg_obj.uuid]}}
            body = {'context': context, 'data': data}
            resp = self._api_svr_app.post_json(
                '/neutron/security_group', body)

        sg_neutron_list = json.loads(resp.text)
        self.assertEqual(len(sg_neutron_list), 1)
        self.assertEqual(sg_neutron_list[0]['id'], sg_obj.uuid)

        def spy_list(orig_method, *args, **kwargs):
            self.assertIn(fip1_obj.uuid, kwargs['obj_uuids'])
            return orig_method(*args, **kwargs)
        with test_common.patch(
            neutron_db_obj._vnc_lib, 'floating_ips_list', spy_list):
            context = {'operation': 'READALL',
                       'user_id': '',
                       'tenant_id': '',
                       'roles': '',
                       'is_admin': 'False'}
            # ask for one explicit id
            data = {'filters': {'id':[fip1_obj.uuid]}}
            body = {'context': context, 'data': data}
            resp = self._api_svr_app.post_json(
                '/neutron/floatingip', body)
            fip_neutron_list = json.loads(resp.text)
            self.assertEqual(len(fip_neutron_list), 1)
            self.assertEqual(fip_neutron_list[0]['id'], fip1_obj.uuid)

            # ask for list of ids AND in a project, should return 1
            data = {'filters': {'id':[fip1_obj.uuid, fip2_obj.uuid],
                                'tenant_id': [proj2_obj.uuid.replace('-', '')]}}
            body = {'context': context, 'data': data}
            resp = self._api_svr_app.post_json(
                '/neutron/floatingip', body)
            fip_neutron_list = json.loads(resp.text)
            self.assertEqual(len(fip_neutron_list), 1)
            self.assertEqual(fip_neutron_list[0]['id'], fip2_obj.uuid)

            # ask for list of ids AND on a VMI, should return 1
            data = {'filters': {'id':[fip1_obj.uuid, fip2_obj.uuid,
                                      fip3_obj.uuid],
                                'tenant_id': [proj2_obj.uuid.replace('-', '')]}}
            body = {'context': context, 'data': data}
            resp = self._api_svr_app.post_json(
                '/neutron/floatingip', body)
            fip_neutron_list = json.loads(resp.text)
            self.assertEqual(len(fip_neutron_list), 1)
            self.assertEqual(fip_neutron_list[0]['id'], fip2_obj.uuid)

        self._vnc_lib.security_group_delete(id=sg_obj.uuid)
        self._vnc_lib.floating_ip_delete(id=fip1_obj.uuid)
        self._vnc_lib.floating_ip_delete(id=fip2_obj.uuid)
        self._vnc_lib.floating_ip_delete(id=fip3_obj.uuid)
        self._vnc_lib.floating_ip_pool_delete(id=fip_pool_obj.uuid)
        self._vnc_lib.virtual_machine_interface_delete(id=vmi_obj.uuid)
        self._vnc_lib.virtual_network_delete(id=vn_obj.uuid)
        self._vnc_lib.project_delete(id=proj2_obj.uuid)
    # end test_filters_with_id
# end class TestListWithFilters
