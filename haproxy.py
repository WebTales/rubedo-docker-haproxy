#!/usr/bin/env python
import logging
import os
import time
import string
import subprocess
import sys
import re
import socket
from collections import OrderedDict

import requests


logger = logging.getLogger(__name__)

# Config ENV
BACKEND_PORT = os.getenv("PORT", os.getenv("BACKEND_PORT", "80"))
BACKEND_PORTS = [x.strip() for x in os.getenv("BACKEND_PORTS", BACKEND_PORT).split(",")]
FRONTEND_PORT = os.getenv("FRONTEND_PORT", "80")
MODE = os.getenv("MODE", "http")
HDR = os.getenv("HDR", "hdr")
BALANCE = os.getenv("BALANCE", "roundrobin")
MAXCONN = os.getenv("MAXCONN", "4096")
SSL = os.getenv("SSL", "")
SSL_BIND_OPTIONS = os.getenv("SSL_BIND_OPTIONS", None)
SSL_BIND_CIPHERS = os.getenv("SSL_BIND_CIPHERS", None)
SESSION_COOKIE = os.getenv("SESSION_COOKIE")
OPTION = os.getenv("OPTION", "redispatch, httplog, dontlognull, forwardfor").split(",")
TIMEOUT = os.getenv("TIMEOUT", "connect 5000, client 50000, server 50000").split(",")
VIRTUAL_HOST = os.getenv("VIRTUAL_HOST", None)
IP_PROBE = os.getenv("IP_PROBE", None)
TUTUM_CONTAINER_API_URL = os.getenv("TUTUM_CONTAINER_API_URL", None)
POLLING_PERIOD = max(int(os.getenv("POLLING_PERIOD", 30)), 5)

TUTUM_AUTH = os.getenv("TUTUM_AUTH")
DEBUG = os.getenv("DEBUG", False)

# Const var
CONFIG_FILE = '/etc/haproxy/haproxy.cfg'
HAPROXY_CMD = ['/usr/sbin/haproxy', '-f', CONFIG_FILE, '-db']
LINK_ENV_PATTERN = "_PORT_%s_TCP" % BACKEND_PORT
LINK_ADDR_SUFFIX = LINK_ENV_PATTERN + "_ADDR"
LINK_PORT_SUFFIX = LINK_ENV_PATTERN + "_PORT"
TUTUM_URL_SUFFIX = "_TUTUM_API_URL"
VIRTUAL_HOST_SUFFIX = "_ENV_VIRTUAL_HOST"
TUTUM_API_URL = "https://dashboard.tutum.co"
TUTUM_SERVICES_URL = TUTUM_API_URL + "/api/v1/service/"

# Global Var
HAPROXY_CURRENT_SUBPROCESS = None

endpoint_match = re.compile(r"(?P<proto>tcp|udp):\/\/(?P<addr>[^:]*):(?P<port>.*)")


def get_cfg_text(cfg):
    text = ""
    for section, contents in cfg.items():
        text += "%s\n" % section
        for content in contents:
            text += "  %s\n" % content
    return text.strip()


def create_default_cfg(maxconn, mode):
    cfg = OrderedDict({
        "global": ["log 127.0.0.1 local0",
                   "log 127.0.0.1 local1 notice",
                   "maxconn %s" % maxconn,
                   "tune.ssl.default-dh-param 2048",
                   "pidfile /var/run/haproxy.pid",
                   "user haproxy",
                   "group haproxy",
                   "daemon",
                   "stats socket /var/run/haproxy.stats level admin"],
        "defaults": ["log global",
                     "mode %s" % mode]})
    for option in OPTION:
        if option:
            cfg["defaults"].append("option %s" % option.strip())
    for timeout in TIMEOUT:
        if timeout:
            cfg["defaults"].append("timeout %s" % timeout.strip())
    if SSL_BIND_OPTIONS:
        cfg["global"].append("ssl-default-bind-options %s" % SSL_BIND_OPTIONS)
    if SSL_BIND_CIPHERS:
        cfg["global"].append("ssl-default-bind-ciphers %s" % SSL_BIND_CIPHERS)

    return cfg

def list_services_url(services, servicesUrl, auth):
    session = requests.Session()
    headers = {"Authorization": auth}
    for service in services.get("objects", []):
        if (service["state"] == "Running") or (service["state"] == "Partly running"):
            prefixName = service["name"].split("-")
            if prefixName[0] == "APACHE":
                servicesUrl.append(service["resource_uri"])
    meta = services.get("meta")
    next = meta["next"]
    if next:
        r = session.get(TUTUM_API_URL + next, headers=headers)
        r.raise_for_status()
        nextServices = r.json()
        list_services_url(nextServices, servicesUrl, auth)

    return servicesUrl

def get_backend_routes_tutum(api_url, auth):
    # Return sth like: {'HELLO_WORLD_1': {'proto': 'tcp', 'addr': '172.17.0.103', 'port': '80'},
    # 'HELLO_WORLD_2': {'proto': 'tcp', 'addr': '172.17.0.95', 'port': '80'}}
    global VIRTUAL_HOST
    session = requests.Session()
    headers = {"Authorization": auth}
    r = session.get(api_url, headers=headers)
    r.raise_for_status()
    container_details = r.json()

    addr_port_dict = {}
    for link in container_details.get("linked_to_container", []):
        for port, endpoint in link.get("endpoints", {}).iteritems():
            if port in ["%s/tcp" % x for x in BACKEND_PORTS]:
                addr_port_dict[link["name"].upper().replace("-", "_")] = endpoint_match.match(endpoint).groupdict()

    r = session.get(TUTUM_SERVICES_URL, headers=headers)
    r.raise_for_status()
    services = r.json()

    servicesUrl = list_services_url(services, [], auth)

    for serviceUrl in servicesUrl:
        serviceUrl = TUTUM_API_URL + serviceUrl
        r = session.get(serviceUrl, headers=headers)
        r.raise_for_status()
        service = r.json()
        if service["state"] == "Running":
            serviceName = service["name"].upper().replace("-", "_")
            for nbr in range(1, service["target_num_containers"] + 1):
                containerName = serviceName + "_" + str(nbr)
                linkVars = service.get("link_variables")
                container = {}
                container["port"] = "80"
                container["proto"] = "tcp"
                container["addr"] = linkVars[containerName+"_PORT_80_TCP_ADDR"]
                addr_port_dict[containerName] = container

            for env in service.get("container_envvars", []):
                if env["key"] == "VIRTUAL_HOST":
                    if VIRTUAL_HOST:
                        VIRTUAL_HOST = VIRTUAL_HOST + "," + serviceName + "=" + env["value"]
                    else:
                        VIRTUAL_HOST = serviceName + "=" + env["value"]


    return addr_port_dict


def get_backend_routes(dict_var):
    # Return sth like: {'HELLO_WORLD_1': {'addr': '172.17.0.103', 'port': '80'},
    # 'HELLO_WORLD_2': {'addr': '172.17.0.95', 'port': '80'}}
    addr_port_dict = {}
    for name, value in dict_var.iteritems():
        position = string.find(name, LINK_ENV_PATTERN)
        if position != -1:
            container_name = name[:position]
            add_port = addr_port_dict.get(container_name, {'addr': "", 'port': ""})
            try:
                add_port['addr'] = socket.gethostbyname(container_name.lower())
            except socket.gaierror:
                add_port['addr'] = socket.gethostbyname(container_name.lower().replace("_", "-"))
            if name.endswith(LINK_PORT_SUFFIX):
                add_port['port'] = value
            addr_port_dict[container_name] = add_port

    return addr_port_dict


def update_cfg(cfg, backend_routes, vhost):
    logger.debug("Updating cfg: \n old cfg: %s\n backend_routes: %s\n vhost: %s", cfg, backend_routes, vhost)
    # Set frontend
    frontend = []
    frontend.append("bind 0.0.0.0:%s" % FRONTEND_PORT)
    if SSL:
        frontend.append("reqadd X-Forwarded-Proto:\ https")
        frontend.append("redirect scheme https code 301 if !{ ssl_fc }"),
        frontend.append("bind 0.0.0.0:443 %s" % SSL)
    if vhost:
        added_vhost = {}
        for _, domains_names in vhost.iteritems():
            domains = domains_names.split(":")
            for domain_name in domains:
                if not added_vhost.has_key(domain_name):
                    domain_str = domain_name.upper().replace(".", "_")
                    frontend.append("acl host_%s %s(host) -i %s" % (domain_str, HDR, domain_name))
                    frontend.append("use_backend %s_cluster if host_%s" % (domains[-1].upper().replace(".", "_"), domain_str))
                    added_vhost[domain_name] = domain_str
    else:
        frontend.append("default_backend default_service")
    if IP_PROBE and IP_PROBE != "**None**":
        ips = IP_PROBE.split(',')
        for ip in ips:
            idx = ips.index(ip)
            frontend.append("acl ip_probe_%s src %s" % (idx, ip.strip()))
            frontend.append("use_backend IPPROBE if ip_probe_%s" % (idx))
    if TUTUM_AUTH:
        frontend.append("acl is_websocket hdr(Upgrade) -i websocket")
        frontend.append("acl is_events path_beg -i /v1/events")
        frontend.append("use_backend websocket_backend if is_events is_websocket")
    cfg["frontend default_frontend"] = frontend

    # Set backend
    if vhost:
        added_vhost = {}
        for service_name, domains_names in vhost.iteritems():
            for domain_name in domains_names.split(":"):
                domain_str = domain_name.upper().replace(".", "_")
                service_name = service_name.upper()
                if added_vhost.has_key(domain_name):
                    backend = cfg.get("backend %s_cluster" % domain_str, [])
                    for container_name, addr_port in backend_routes.iteritems():
                        if container_name.startswith(service_name):
                            server_string = "server %s %s:%s" % (container_name, addr_port["addr"], addr_port["port"])
                            if SESSION_COOKIE:
                                server_string += " cookie check"

                            # Do not add duplicate backend routes
                            duplicated = False
                            for server_str in backend:
                                if "%s:%s" % (addr_port["addr"], addr_port["port"]) in server_str:
                                    duplicated = True
                                    break
                            if not duplicated:
                                backend.append(server_string)
                    cfg["backend %s_cluster" % domain_str] = sorted(backend)
                else:
                    backend = []
                    if SESSION_COOKIE:
                        backend.append("appsession %s len 64 timeout 3h request-learn prefix" % (SESSION_COOKIE, ))

                    backend.append("balance %s" % BALANCE)
                    for container_name, addr_port in backend_routes.iteritems():
                        if container_name.startswith(service_name):
                            server_string = "server %s %s:%s" % (container_name, addr_port["addr"], addr_port["port"])
                            if SESSION_COOKIE:
                                server_string += " cookie check"

                            # Do not add duplicate backend routes
                            duplicated = False
                            for server_str in backend:
                                if "%s:%s" % (addr_port["addr"], addr_port["port"]) in server_str:
                                    duplicated = True
                                    break
                            if not duplicated:
                                backend.append(server_string)
            if backend:
                cfg["backend %s_cluster" % domain_name.upper().replace(".", "_")] = sorted(backend)
                added_vhost[domain_name] = domain_name.upper().replace(".", "_")

    else:
        backend = []
        if SESSION_COOKIE:
            backend.append("appsession %s len 64 timeout 3h request-learn prefix" % (SESSION_COOKIE, ))

        backend.append("balance %s" % BALANCE)
        for container_name, addr_port in backend_routes.iteritems():
            server_string = "server %s %s:%s" % (container_name, addr_port["addr"], addr_port["port"])
            if SESSION_COOKIE:
                server_string += " cookie check"

            # Do not add duplicate backend routes
            duplicated = False
            for server_str in backend:
                if "%s:%s" % (addr_port["addr"], addr_port["port"]) in server_str:
                    duplicated = True
                    break
            if not duplicated:
                backend.append(server_string)

        cfg["backend default_service"] = sorted(backend)

    if IP_PROBE and IP_PROBE != "**None**":
        cfg["backend IPPROBE"] = ["errorfile 503 /etc/haproxy/503"]
    if TUTUM_AUTH:
        cfgwebsocket = []
        cfgwebsocket.append("timeout server 600s")
        auth_split = TUTUM_AUTH.split()
        tutum_auth_string = "%s%%20%s" % (auth_split[0], auth_split[1])
        cfgwebsocket.append("reqrep ^([^\ :]*)\ /v1/events(.*)     \\1\ /v1/events?auth=%s\\2" % tutum_auth_string)
        cfgwebsocket.append("server tutum stream.tutum.co:443 ssl verify none")
        cfg["backend websocket_backend"] = cfgwebsocket

    logger.debug("New cfg: %s", cfg)



def save_config_file(cfg_text, config_file):
    try:
        directory = os.path.dirname(config_file)
        if not os.path.exists(directory):
            os.makedirs(directory)
        f = open(config_file, 'w')
    except Exception as e:
        logger.error(e)
    else:
        f.write(cfg_text)
        logger.info("Config file is updated")
        f.close()


def reload_haproxy():
    global HAPROXY_CURRENT_SUBPROCESS
    if HAPROXY_CURRENT_SUBPROCESS:
        # Reload haproxy
        logger.info("Reloading haproxy")
        process = subprocess.Popen(HAPROXY_CMD + ["-sf", str(HAPROXY_CURRENT_SUBPROCESS.pid)])
        HAPROXY_CURRENT_SUBPROCESS.wait()
        HAPROXY_CURRENT_SUBPROCESS = process
    else:
        # Launch haproxy
        logger.info("Launching haproxy")
        HAPROXY_CURRENT_SUBPROCESS = subprocess.Popen(HAPROXY_CMD)


def update_virtualhost(vhost):
    if VIRTUAL_HOST:
        # vhost specified using environment variables
        for host in VIRTUAL_HOST.split(","):
            tmp = host.split("=", 2)
            if len(tmp) == 2:
                vhost[tmp[0].strip()] = tmp[1].strip()
    # vhost specified in the linked containers
    for name, value in os.environ.iteritems():
        position = string.find(name, VIRTUAL_HOST_SUFFIX)
        if position != -1 and value != "**None**":
            hostname = name[:position]
            vhost[hostname] = value

if __name__ == "__main__":
    logging.basicConfig(stream=sys.stdout)
    logging.getLogger(__name__).setLevel(logging.DEBUG if DEBUG else logging.INFO)

    cfg = create_default_cfg(MAXCONN, MODE)
    vhost = {}

    # Tell the user the mode of autoupdate we are using, if any
    if TUTUM_CONTAINER_API_URL:
        if TUTUM_AUTH:
            logger.info("HAproxy has access to Tutum API - will reload list of backends every %d seconds",
                        POLLING_PERIOD)
        else:
            logger.warning(
                "HAproxy doesn't have access to Tutum API and it's running in Tutum - you might want to give "
                "an API role to this service for automatic backend reconfiguration")
    else:
        logger.info("HAproxy is not running in Tutum")

    # Main loop
    old_text = ""
    while True:
        try:
            if TUTUM_CONTAINER_API_URL and TUTUM_AUTH:
                # Running on Tutum with API access - fetch updated list of environment variables
                backend_routes = get_backend_routes_tutum(TUTUM_CONTAINER_API_URL, TUTUM_AUTH)
            else:
                # No Tutum API access - configuring backends based on static environment variables
                backend_routes = get_backend_routes(os.environ)

            # Update backend routes
            update_virtualhost(vhost)
            update_cfg(cfg, backend_routes, vhost)
            cfg_text = get_cfg_text(cfg)

            # If cfg changes, write to file
            if old_text != cfg_text:
                logger.info("HAProxy configuration has been changed:\n%s" % cfg_text)
                save_config_file(cfg_text, CONFIG_FILE)
                reload_haproxy()
                old_text = cfg_text
        except Exception as e:
            logger.exception("Error: %s" % e)

        time.sleep(POLLING_PERIOD)
