# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import contextlib
import ipaddress
import os
import tempfile

from ironic_lib import disk_utils
from ironic_lib import utils
import netifaces
from oslo_concurrency import processutils

from ironic_python_agent import hardware


TEMPLATE = """
DEVICE={device}
BOOTPROTO=
HWADDR={mac}
IPADDR={ip}
NETMASK={netmask}
ONBOOT=yes
NM_CONTROLLED=yes
"""


def find_device_by_mac(mac):
    for dev in netifaces.interfaces():
        if mac.lower() in (x['addr'].lower() for x in
                           netifaces.ifaddresses(dev)[netifaces.AF_LINK]):
            return dev


def port_to_config(port):
    addr = ipaddress.ip_interface(port['extra']['netconfig'])
    return TEMPLATE.format(
        device=find_device_by_mac(port['address']),
        mac=port['address'],
        ip=addr.ip,
        netmask=addr.network.netmask,
    )


def partition_index_to_name(device, index):
    # The partition delimiter for all common harddrives (sd[a-z]+)
    part_delimiter = ''
    if 'nvme' in device:
        part_delimiter = 'p'
    return device + part_delimiter + str(index)


@contextlib.contextmanager
def partition_with_path(path):
    root_dev = hardware.dispatch_to_managers('get_os_install_device')
    partitions = disk_utils.list_partitions(root_dev)
    local_path = tempfile.mkdtemp()

    for part in partitions:
        if 'esp' in part['flags'] or 'lvm' in part['flags']:
            continue

        path = partition_index_to_name(root_dev, part['number'])
        try:
            with utils.mounted(path) as local_path:
                conf_path = os.path.join(local_path, path)
                if not os.path.isdir(conf_path):
                    continue

                yield conf_path
        except processutils.ProcessExecutionError:
            continue


PATH = '/etc/sysconfig/network-scripts'


class NetConfigHardwareManager(hardware.HardwareManager):

    HARDWARE_MANAGER_NAME = 'NetConfigHardwareManager'
    HARDWARE_MANAGER_VERSION = '1'

    def evaluate_hardware_support(self):
        return hardware.HardwareSupport.SERVICE_PROVIDER

    def get_deploy_steps(self, node, ports):
        return [
            {
                'step': 'write_netconfig',
                'priority': 0,
                'reboot_requested': False,
                'abortable': True,
            }
        ]

    def write_netconfig(self, node, ports):
        for port in ports:
            config = port_to_config(port)
            with partition_with_path(PATH) as path:
                fname = "ifcfg-%s" % find_device_by_mac(port['address'])
                fname = os.path.join(path, fname)
                with open(fname, "wt") as fp:
                    fp.write(config)
