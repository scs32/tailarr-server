[
  {
    "name": "sonarr",
    "image": "linuxserver/sonarr:latest",
    "default_port": 8989,
    "volumes": {
      "/path/to/config": "/config",
      "/path/to/tv": "/tv",
      "/path/to/data": "/data"
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
      "/path/to/data": "/data"
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
      "/path/to/data": "/data"
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
      "/path/to/data": "/data"
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
    "name": "sabnzbd",
    "image": "linuxserver/sabnzbd:latest",
    "default_port": 8080,
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
      "8080": "8080"
    },
    "restart_policy": "unless-stopped"
  },
  {
    "name": "nzbget",
    "image": "linuxserver/nzbget:latest",
    "default_port": 6789,
    "volumes": {
      "/path/to/config": "/config",
      "/path/to/data": "/data"
    },
    "config_file": "/config/nzbget.conf",
    "config_set": {
      "DestDir": "/data/downloads/completed",
      "InterDir": "/data/downloads/intermediate"
    },
    "environment": {
      "PUID": "1000",
      "PGID": "1000",
      "TZ": "America/Los_Angeles"
    },
    "network_mode": "bridge",
    "ports": {
      "6789": "6789"
    },
    "restart_policy": "unless-stopped"
  },
  {
    "name": "nzbhydra2",
    "image": "linuxserver/nzbhydra2:latest",
    "default_port": 5076,
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
      "5076": "5076"
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
  }
]
