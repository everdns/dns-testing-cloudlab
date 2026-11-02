#!/bin/sh
SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
sudo apt update -y
#Install dnsperf
sudo apt install -y autoconf automake libtool  libssl-dev libldns-dev libck-dev libnghttp2-dev
sudo git clone https://codeberg.org/DNS-OARC/dnsperf.git /opt/dnsperf
cd /opt/dnsperf && sudo ./autogen.sh && sudo ./configure
cd /opt/dnsperf && sudo make && sudo make install
#Install kxdpgun
sudo apt-get install -y \
  libtool autoconf automake make pkg-config liburcu-dev libgnutls28-dev libedit-dev liblmdb-dev libbpf-dev libmnl-dev
sudo git clone --branch 3.5 --depth 1 https://gitlab.nic.cz/knot/knot-dns.git /opt/knot-dns
# kxdpgun runs one thread per 'combined' NIC channel, which sfc, mlx4 and bnx2 do not report, so it ran a
# single thread on queue 0. This patch uses one thread per CPU (nproc), capped at the NIC RX queue count (or KXDPGUN_QUEUES=<n>).
sudo patch -p1 -N -d /opt/knot-dns < "$SCRIPT_DIR/kxdpgun-queue-fallback.patch" || true
cd /opt/knot-dns && sudo autoreconf -if && sudo ./configure --enable-xdp=yes && sudo make -j$(nproc) && sudo make install
echo "/usr/local/lib" | sudo tee /etc/ld.so.conf.d/knot-dns.conf && sudo ldconfig