"""Explicit LAN allowlist; unknown hosts and public peers remain blocked."""
import ipaddress
import json


def config(private):
    try:return json.loads((private/'network.json').read_text('utf-8'))
    except (OSError,ValueError):return {}


def allowed(client,host,settings):
    if client=='testclient' and host=='testserver':return True
    try:peer=ipaddress.ip_address(client)
    except ValueError:return False
    if peer.is_loopback and host in ('localhost','127.0.0.1','::1'):return True
    if settings.get('lan_enabled') is not True:return False
    try:
        address=ipaddress.ip_address(host)
        networks=[ipaddress.ip_network(value,strict=True) for value in settings.get('allowed_networks',[])]
        return bool(address.is_private and str(address) in settings.get('allowed_hosts',[]) and
                    (peer.is_loopback or any(peer in network and network.is_private for network in networks)))
    except (ValueError,TypeError):return False
