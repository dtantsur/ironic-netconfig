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
import logging
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

LOG = logging.getLogger(__name__)


def find_device_by_mac(mac):
    for dev in netifaces.interfaces():
        ifaddresses = netifaces.ifaddresses(dev)
        LOG.debug('Inspecting device %s with addresses %s', dev, ifaddresses)
        if mac.lower() in (x['addr'].lower() for x in
                           ifaddresses.get(netifaces.AF_LINK, ())):
            return dev
    raise RuntimeError("Device with MAC %s was not found" % mac)


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
            LOG.debug('Skipping partition %s', part)
            continue

        part_path = partition_index_to_name(root_dev, part['number'])
        try:
            with utils.mounted(part_path) as local_path:
                conf_path = os.path.join(local_path, path)
                LOG.debug('Checking for path %s on %s', conf_path, part_path)
                if not os.path.isdir(conf_path):
                    continue

                LOG.info('Path found: %s on %s', conf_path, part_path)
                yield conf_path
                return
        except processutils.ProcessExecutionError as exc:
            LOG.warning('Failure when inspecting partition %s: %s', part, exc)

    raise RuntimeError("No partition found with path %s, scanned: %s"
                       % (path, partitions))


PATH = 'etc/sysconfig/network-scripts'


class NetConfigHardwareManager(hardware.HardwareManager):

    HARDWARE_MANAGER_NAME = 'NetConfigHardwareManager'
    HARDWARE_MANAGER_VERSION = '1'

    def evaluate_hardware_support(self):
        return hardware.HardwareSupport.SERVICE_PROVIDER

    def get_deploy_steps(self, node, ports):
        return [
            {
                'interface': 'deploy',
                'step': 'write_netconfig',
                'priority': 0,
                'reboot_requested': False,
                'abortable': True,
            }
        ]

    def write_netconfig(self, node, ports):
        # Run this first to validate the request
        configs = [(port, port_to_config(port)) for port in ports]

        with partition_with_path(PATH) as path:
            # Purge any existing configuration
            for current in os.listdir(path):
                if current.startswith('ifcfg-'):
                    LOG.debug('Removing %s', current)
                    utils.unlink_without_raise(os.path.join(path, current))

            for port, config in configs:
                # Write a new configuration
                fname = "ifcfg-%s" % find_device_by_mac(port['address'])
                fname = os.path.join(path, fname)
                LOG.info("Writing config to %s: %s", fname, config)
                with open(fname, "wt") as fp:
                    fp.write(config)
