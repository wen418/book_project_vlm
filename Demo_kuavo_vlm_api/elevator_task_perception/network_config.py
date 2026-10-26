"""Use an internal SSH alias and derive ROS addresses from the actual route."""
import argparse
import ipaddress
import os
import shlex
import socket
import sys

DEFAULT_LOWER_HOST = 'kuavo_master'


def lower_host(arguments, environment=None):
    env = os.environ if environment is None else environment
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument('--lower-host', default=env.get('ELEVATOR_LOWER_HOST') or DEFAULT_LOWER_HOST)
    args, _ = parser.parse_known_args(arguments)
    if not args.lower_host or args.lower_host.startswith('-') or any(c.isspace() for c in args.lower_host):
        raise ValueError('下位机主机名无效')
    return args.lower_host


def route_ip(host):
    addresses = socket.getaddrinfo(host, 22, socket.AF_INET, socket.SOCK_DGRAM)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as connection:
        # UDP connect chooses a route without sending data or contacting SSH.
        connection.connect(addresses[0][4])
        return str(ipaddress.IPv4Address(connection.getsockname()[0]))


def upper_environment(arguments, environment=None):
    env = os.environ if environment is None else environment
    host = lower_host(arguments, env)
    # An explicit ROS_IP is respected; the default is derived afresh on each launch.
    source = str(ipaddress.IPv4Address(env['ROS_IP'])) if env.get('ROS_IP') else route_ip(host)
    return {'ELEVATOR_LOWER_HOST': host, 'ROS_IP': source,
            'ROS_MASTER_URI': env.get('ROS_MASTER_URI') or 'http://'+host+':11311'}


def configure_lower(environment=None):
    env = os.environ if environment is None else environment
    connection = env.get('SSH_CONNECTION', '').split()
    source = env.get('ELEVATOR_LOWER_ROS_IP')
    if not source:
        if len(connection) != 4:
            raise ValueError('无法从 SSH_CONNECTION 确定下位机 ROS_IP')
        source = connection[2]
    env['ROS_IP'] = str(ipaddress.IPv4Address(source))
    env.pop('ROS_HOSTNAME', None)
    return env['ROS_IP']


def main():
    if '--help' in sys.argv[1:] or '-h' in sys.argv[1:]: return
    for key, val in upper_environment(sys.argv[1:]).items():
        print('export '+key+'='+shlex.quote(val))


if __name__ == '__main__': main()
