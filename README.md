# Tailarr

Self-hosted media stack on your own tailnet. One command installs a controller that
deploys and wires the services for you, reachable privately over Tailscale.

## Install

```bash
sudo bash -c "$(curl -fsSL https://raw.githubusercontent.com/scs32/tailarr-server/main/install.sh)"
```

macOS:

```bash
bash -c "$(curl -fsSL https://raw.githubusercontent.com/scs32/tailarr-server/main/install-mac.sh)"
```

## Uninstall

```bash
sudo bash -c "$(curl -fsSL https://raw.githubusercontent.com/scs32/tailarr-server/main/uninstall.sh)"
```

## What is in this repository

This repository is the **release surface**: the install and deploy scripts fetched at
install time, and the tailnet policy template. It is generated from the development
repository on each release — please do not send pull requests here, as they will be
overwritten by the next publish.

The controller itself ships as a container image:

- `ghcr.io/scs32/tailarr`
- `ghcr.io/scs32/tailarr-storage`

## Licence

See [LICENSE](LICENSE).
