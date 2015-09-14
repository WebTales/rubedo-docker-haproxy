FROM ubuntu:trusty

# Install pip and haproxy
RUN echo 'deb http://ppa.launchpad.net/vbernat/haproxy-1.5/ubuntu trusty main' >> /etc/apt/sources.list && \
    echo 'deb-src http://ppa.launchpad.net/vbernat/haproxy-1.5/ubuntu trusty main' >> /etc/apt/sources.list && \
    apt-key adv --keyserver keyserver.ubuntu.com --recv-keys 505D97A41C61B9CD && \
    apt-get update && \
    apt-get install -y --no-install-recommends haproxy python-pip && \
    apt-get clean && \
    pip install requests==2.2.1 && \
    rm -rf /var/lib/apt/lists/*

# BACKEND_PORT is the port of the app server which is load balanced
ENV BACKEND_PORT 80

# BACKEND_PORTS is the list of ports of the app server which is load balanced
ENV BACKEND_PORTS **None**

# FRONTEND_PORT is the port on which the load balancer is accessible (also update the EXPOSE directive below)
ENV FRONTEND_PORT 80

# MODE of operation (http, tcp)
ENV MODE http

# HDR is the "hdr" criteria used in "acl" for virtualhost
ENV HDR hdr

# algorithm for load balancing (roundrobin, source, leastconn, ...)
ENV BALANCE roundrobin

# define session stickiness on an existing application cookie
ENV SESSION_COOKIE **None**

# maximum number of connections
ENV MAXCONN 4096

# list of options separated by commas
ENV OPTION redispatch, httplog, dontlognull, forwardfor

# list of timeout entries separated by commas
ENV TIMEOUT connect 5000, client 50000, server 50000

# Virtual host
ENV VIRTUAL_HOST **None**

# Ip_probe
ENV IP_PROBE **None**

# SSL certificate to use (optional)
ENV SSL_CERT **None**

# SSL bind options to use (optional)
ENV SSL_BIND_OPTIONS no-sslv3

# Add scripts
ADD haproxy.py /haproxy.py
ADD run.sh /run.sh
RUN chmod +x /*.sh

# Copy 503
COPY 503 /etc/haproxy/503

EXPOSE 80 443
CMD ["/run.sh"]
