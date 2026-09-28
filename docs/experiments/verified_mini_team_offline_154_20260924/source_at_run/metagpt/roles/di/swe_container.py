"""Checks shared by generation and evaluation before executing task code."""


def verify_network_isolation(attrs):
    mode = attrs.get('HostConfig', {}).get('NetworkMode')
    networks = sorted(attrs.get('NetworkSettings', {}).get('Networks', {}))
    if mode != 'none' or any(name != 'none' for name in networks):
        raise RuntimeError(f'SWE container must be offline: network_mode={mode!r}, networks={networks!r}')
    return {'network_mode': mode, 'networks': networks}
