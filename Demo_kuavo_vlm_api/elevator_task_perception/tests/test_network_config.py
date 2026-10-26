import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from network_config import lower_host, upper_environment, configure_lower


class NetworkTests(unittest.TestCase):
    def test_default_is_internal_alias(self):
        self.assertEqual(lower_host([], {}), 'kuavo_master')

    def test_cli_and_environment(self):
        self.assertEqual(lower_host([], {'ELEVATOR_LOWER_HOST': 'robot_internal'}), 'robot_internal')
        self.assertEqual(lower_host(['--lower-host=robot_new'], {'ELEVATOR_LOWER_HOST': 'robot_internal'}), 'robot_new')

    def test_upper_uses_route_not_wifi_constant(self):
        with patch('network_config.route_ip', return_value='192.168.12.12') as route:
            env = upper_environment(['--current-floor', '21', '--target-floor', '20'], {})
        route.assert_called_once_with('kuavo_master')
        self.assertEqual(env['ROS_IP'], '192.168.12.12')
        self.assertEqual(env['ROS_MASTER_URI'], 'http://kuavo_master:11311')

    def test_explicit_ros_override_preserved(self):
        env = upper_environment([], {'ROS_IP': '10.0.0.8', 'ROS_MASTER_URI': 'http://robot_master:11311'})
        self.assertEqual(env['ROS_IP'], '10.0.0.8')
        self.assertEqual(env['ROS_MASTER_URI'], 'http://robot_master:11311')

    def test_lower_uses_actual_ssh_server_address(self):
        env = {'SSH_CONNECTION': '192.168.12.12 56780 192.168.12.1 22', 'ROS_IP': '192.168.5.133', 'ROS_HOSTNAME': 'stale'}
        self.assertEqual(configure_lower(env), '192.168.12.1')
        self.assertNotIn('ROS_HOSTNAME', env)

    def test_missing_or_invalid_connection_fails(self):
        for env in ({}, {'SSH_CONNECTION': 'bad value'}, {'SSH_CONNECTION': '10.0.0.2 1 bad 22'}):
            with self.assertRaises(ValueError): configure_lower(env)

    def test_lower_override(self):
        self.assertEqual(configure_lower({'ELEVATOR_LOWER_ROS_IP': '10.0.0.1'}), '10.0.0.1')


if __name__ == '__main__': unittest.main()
