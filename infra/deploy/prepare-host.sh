#!/bin/bash
# Prepare a fresh Ubuntu host to run NovaBrief.
#
#     sudo bash infra/deploy/prepare-host.sh
#
# Idempotent: every step checks before acting, so running it twice does
# nothing the second time. That matters more than elegance — this script gets
# re-run when something went wrong halfway, which is exactly when a script
# that assumes a clean slate does damage.
#
# It installs Docker, gives the machine some swap, and closes the firewall. It
# deploys nothing: the application comes up with docker compose afterwards.
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
    echo "run this with sudo" >&2
    exit 1
fi

SWAP_FILE=/swapfile
SWAP_SIZE_MB=2048
TARGET_USER=${SUDO_USER:-ubuntu}

say() { printf '\n=== %s ===\n' "$1"; }

say "system packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get upgrade -y -qq
apt-get install -y -qq ca-certificates curl gnupg ufw fail2ban

say "swap"
# The host has 3.7 GiB and no swap. Without it an over-committed moment does
# not slow the machine down: the kernel kills a process outright, and if that
# process is PostgreSQL the service is gone. Two gigabytes turn an incident
# into a slowdown.
if swapon --show | grep -q "$SWAP_FILE"; then
    echo "swap already active"
else
    if [[ ! -f $SWAP_FILE ]]; then
        fallocate -l "${SWAP_SIZE_MB}M" "$SWAP_FILE"
        chmod 600 "$SWAP_FILE"
        mkswap "$SWAP_FILE" > /dev/null
    fi
    swapon "$SWAP_FILE"
    grep -q "^$SWAP_FILE" /etc/fstab || echo "$SWAP_FILE none swap sw 0 0" >> /etc/fstab
    # Swap as an emergency brake, not as routine memory: at 60 the kernel
    # starts paging while there is still RAM free, and a database that gets
    # paged out is a database that crawls.
    sysctl -q vm.swappiness=10
    grep -q "^vm.swappiness" /etc/sysctl.conf || echo "vm.swappiness=10" >> /etc/sysctl.conf
    echo "swap enabled (${SWAP_SIZE_MB} MB)"
fi

say "docker"
if command -v docker > /dev/null; then
    echo "docker already installed: $(docker --version)"
else
    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
        | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
    chmod a+r /etc/apt/keyrings/docker.gpg

    # Ubuntu 26.04 is newer than Docker's repository: its codename has no
    # packages yet. Falling back to the previous LTS is deliberate and worth
    # knowing about — those packages are built for the older glibc and run
    # fine, but this line is the first thing to revisit when Docker publishes
    # for this release.
    codename=$(. /etc/os-release && echo "$VERSION_CODENAME")
    if ! curl -fsI "https://download.docker.com/linux/ubuntu/dists/${codename}/Release" \
        > /dev/null 2>&1; then
        echo "no Docker repository for ${codename}; falling back to noble"
        codename=noble
    fi

    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu ${codename} stable" \
        > /etc/apt/sources.list.d/docker.list
    apt-get update -qq
    apt-get install -y -qq docker-ce docker-ce-cli containerd.io \
        docker-buildx-plugin docker-compose-plugin
    systemctl enable --now docker
    echo "installed: $(docker --version)"
fi

# So the deploy user can run docker without sudo. Takes effect on their next
# login, which is why the deployment reconnects rather than continuing.
if ! id -nG "$TARGET_USER" | grep -qw docker; then
    usermod -aG docker "$TARGET_USER"
    echo "added $TARGET_USER to the docker group (effective on next login)"
fi

say "firewall"
# Order matters: allow SSH *before* enabling, or enabling the firewall ends the
# session that is running this script and the machine is only reachable from
# the provider's console.
ufw allow 22/tcp > /dev/null
ufw allow 80/tcp > /dev/null
ufw allow 443/tcp > /dev/null
ufw allow 443/udp > /dev/null
if ufw status | grep -q "Status: active"; then
    echo "firewall already active"
else
    ufw --force enable > /dev/null
    echo "firewall enabled: 22, 80, 443 only"
fi

say "fail2ban"
systemctl enable --now fail2ban > /dev/null 2>&1 || true
systemctl is-active fail2ban

say "done"
echo "Docker:   $(docker --version)"
echo "Compose:  $(docker compose version --short 2>/dev/null || echo 'plugin missing')"
free -h | head -3
if [[ -f /var/run/reboot-required ]]; then
    echo
    echo "A reboot is required to finish applying the kernel updates."
fi
