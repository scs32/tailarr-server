[
  {
    "name": "sonarr",
    "image": "linuxserver/sonarr:latest",
    "default_port": 8989,
    "volumes": {
      "/path/to/config": "/config",
      "/path/to/tv": "/tv",
      "/path/to/downloads": "/downloads"
    },
    "environment": {
      "PUID": "1000",
      "PGID": "1000",
      "TZ": "America/Los_Angeles"
    },
    "network_mode": "bridge",
    "ports": {
      "8989": "8989"
    },
    "restart_policy": "unless-stopped"
  },
  {
    "name": "radarr",
    "image": "linuxserver/radarr:latest",
    "default_port": 7878,
    "volumes": {
      "/path/to/config": "/config",
      "/path/to/movies": "/movies",
      "/path/to/downloads": "/downloads"
    },
    "environment": {
      "PUID": "1000",
      "PGID": "1000",
      "TZ": "America/Los_Angeles"
    },
    "network_mode": "bridge",
    "ports": {
      "7878": "7878"
    },
    "restart_policy": "unless-stopped"
  },
  {
    "name": "lidarr",
    "image": "linuxserver/lidarr:latest",
    "default_port": 8686,
    "volumes": {
      "/path/to/config": "/config",
      "/path/to/music": "/music",
      "/path/to/downloads": "/downloads"
    },
    "environment": {
      "PUID": "1000",
      "PGID": "1000",
      "TZ": "America/Los_Angeles"
    },
    "network_mode": "bridge",
    "ports": {
      "8686": "8686"
    },
    "restart_policy": "unless-stopped"
  },
  {
    "name": "prowlarr",
    "image": "linuxserver/prowlarr:latest",
    "default_port": 9696,
    "volumes": {
      "/path/to/config": "/config"
    },
    "environment": {
      "PUID": "1000",
      "PGID": "1000",
      "TZ": "America/Los_Angeles"
    },
    "network_mode": "bridge",
    "ports": {
      "9696": "9696"
    },
    "restart_policy": "unless-stopped"
  },
  {
    "name": "bazarr",
    "image": "linuxserver/bazarr:latest",
    "default_port": 6767,
    "volumes": {
      "/path/to/config": "/config",
      "/path/to/movies": "/movies",
      "/path/to/tv": "/tv"
    },
    "environment": {
      "PUID": "1000",
      "PGID": "1000",
      "TZ": "America/Los_Angeles"
    },
    "network_mode": "bridge",
    "ports": {
      "6767": "6767"
    },
    "restart_policy": "unless-stopped"
  },
  {
    "name": "qbittorrent",
    "image": "linuxserver/qbittorrent:latest",
    "default_port": 8080,
    "volumes": {
      "/path/to/config": "/config",
      "/path/to/downloads": "/downloads"
    },
    "environment": {
      "PUID": "1000",
      "PGID": "1000",
      "TZ": "America/Los_Angeles"
    },
    "network_mode": "bridge",
    "ports": {
      "8080": "8080"
    },
    "restart_policy": "unless-stopped"
  },
  {
    "name": "jellyfin",
    "image": "linuxserver/jellyfin:latest",
    "default_port": 8096,
    "volumes": {
      "/path/to/config": "/config",
      "/path/to/media": "/media"
    },
    "environment": {
      "PUID": "1000",
      "PGID": "1000",
      "TZ": "America/Los_Angeles"
    },
    "network_mode": "bridge",
    "ports": {
      "8096": "8096"
    },
    "restart_policy": "unless-stopped"
  },
  {
    "name": "plex",
    "image": "linuxserver/plex:latest",
    "default_port": 32400,
    "volumes": {
      "/path/to/config": "/config",
      "/path/to/media": "/media"
    },
    "environment": {
      "PUID": "1000",
      "PGID": "1000",
      "TZ": "America/Los_Angeles"
    },
    "network_mode": "bridge",
    "ports": {
      "32400": "32400"
    },
    "restart_policy": "unless-stopped"
  },
  {
    "name": "nextcloud",
    "image": "linuxserver/nextcloud:latest",
    "default_port": 443,
    "volumes": {
      "/path/to/config": "/config",
      "/path/to/data": "/data"
    },
    "environment": {
      "PUID": "1000",
      "PGID": "1000",
      "TZ": "America/Los_Angeles"
    },
    "network_mode": "bridge",
    "ports": {
      "443": "443"
    },
    "restart_policy": "unless-stopped"
  },
  {
    "name": "homeassistant",
    "image": "ghcr.io/home-assistant/home-assistant:stable",
    "default_port": 8123,
    "volumes": {
      "/path/to/config": "/config"
    },
    "environment": {
      "PUID": "1000",
      "PGID": "1000",
      "TZ": "America/Los_Angeles"
    },
    "network_mode": "bridge",
    "ports": {
      "8123": "8123"
    },
    "restart_policy": "unless-stopped"
  },
  {
    "name": "portainer",
    "image": "portainer/portainer-ce:latest",
    "default_port": 9000,
    "volumes": {
      "/var/run/docker.sock": "/var/run/docker.sock",
      "/path/to/data": "/data"
    },
    "environment": {
      "PUID": "1000",
      "PGID": "1000",
      "TZ": "America/Los_Angeles"
    },
    "network_mode": "bridge",
    "ports": {
      "9000": "9000"
    },
    "restart_policy": "unless-stopped"
  },
  {
    "name": "vaultwarden",
    "image": "vaultwarden/server:latest",
    "default_port": 80,
    "volumes": {
      "/path/to/data": "/data"
    },
    "environment": {
      "PUID": "1000",
      "PGID": "1000",
      "TZ": "America/Los_Angeles"
    },
    "network_mode": "bridge",
    "ports": {
      "80": "80"
    },
    "restart_policy": "unless-stopped"
  },
  {
    "name": "uptime-kuma",
    "image": "louislam/uptime-kuma:latest",
    "default_port": 3001,
    "volumes": {
      "/path/to/data": "/app/data"
    },
    "environment": {
      "PUID": "1000",
      "PGID": "1000",
      "TZ": "America/Los_Angeles"
    },
    "network_mode": "bridge",
    "ports": {
      "3001": "3001"
    },
    "restart_policy": "unless-stopped"
  },
  {
    "name": "tautulli",
    "image": "linuxserver/tautulli:latest",
    "default_port": 8181,
    "volumes": {
      "/path/to/config": "/config"
    },
    "environment": {
      "PUID": "1000",
      "PGID": "1000",
      "TZ": "America/Los_Angeles"
    },
    "network_mode": "bridge",
    "ports": {
      "8181": "8181"
    },
    "restart_policy": "unless-stopped"
  },
  {
    "name": "pi-hole",
    "image": "pihole/pihole:latest",
    "default_port": 80,
    "volumes": {
      "/path/to/etc-pihole": "/etc/pihole",
      "/path/to/etc-dnsmasq.d": "/etc/dnsmasq.d"
    },
    "environment": {
      "PUID": "1000",
      "PGID": "1000",
      "TZ": "America/Los_Angeles"
    },
    "network_mode": "bridge",
    "ports": {
      "80": "80"
    },
    "restart_policy": "unless-stopped"
  },
  {
    "name": "unifi-controller",
    "image": "linuxserver/unifi-controller:latest",
    "default_port": 8443,
    "volumes": {
      "/path/to/config": "/config"
    },
    "environment": {
      "PUID": "1000",
      "PGID": "1000",
      "TZ": "America/Los_Angeles"
    },
    "network_mode": "bridge",
    "ports": {
      "8443": "8443"
    },
    "restart_policy": "unless-stopped"
  },
  {
    "name": "wireguard",
    "image": "linuxserver/wireguard:latest",
    "default_port": 51820,
    "volumes": {
      "/path/to/config": "/config"
    },
    "environment": {
      "PUID": "1000",
      "PGID": "1000",
      "TZ": "America/Los_Angeles"
    },
    "network_mode": "bridge",
    "ports": {
      "51820": "51820"
    },
    "restart_policy": "unless-stopped"
  },
  {
    "name": "bookstack",
    "image": "linuxserver/bookstack:latest",
    "default_port": 6875,
    "volumes": {
      "/path/to/config": "/config"
    },
    "environment": {
      "PUID": "1000",
      "PGID": "1000",
      "TZ": "America/Los_Angeles"
    },
    "network_mode": "bridge",
    "ports": {
      "6875": "6875"
    },
    "restart_policy": "unless-stopped"
  },
  {
    "name": "gitea",
    "image": "gitea/gitea:latest",
    "default_port": 3000,
    "volumes": {
      "/path/to/data": "/data"
    },
    "environment": {
      "PUID": "1000",
      "PGID": "1000",
      "TZ": "America/Los_Angeles"
    },
    "network_mode": "bridge",
    "ports": {
      "3000": "3000"
    },
    "restart_policy": "unless-stopped"
  }
]
