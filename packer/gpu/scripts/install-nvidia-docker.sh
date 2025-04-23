#!/bin/bash
set -eu -o pipefail

# This script is running as ec2-user so need to sudo
# Install Nvidia driver
sudo yum install -y kernel-devel-$(uname -r) kernel-headers-$(uname -r) dkms 

sudo yum install -y pkgconfig libglvnd-devel

readonly NVIDIA_DRIVER_VERSION="570.133.20" # Driver version for CUDA 12.8, Linux 64-bit, and L4 GPU
NVIDIA_DRIVER="https://us.download.nvidia.com/tesla/${NVIDIA_DRIVER_VERSION}/NVIDIA-Linux-x86_64-${NVIDIA_DRIVER_VERSION}.run"

curl -sfL -o cuda_toolkit.run "${NVIDIA_DRIVER}" # Load runfile into cuda_toolkit.run
sudo dnf install -y kernel-modules-extra # https://github.com/amazonlinux/amazon-linux-2023/issues/538
sudo sh cuda_toolkit.run --silent --dkms # Install the driver
rm cuda_toolkit.run

# Install nvidia container toolkit
curl -sfL https://nvidia.github.io/libnvidia-container/stable/rpm/nvidia-container-toolkit.repo | sudo tee /etc/yum.repos.d/nvidia-container-toolkit.repo
sudo yum install -y nvidia-container-toolkit nvidia-container-runtime

# Update Docker configuration
sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker


sudo cp /tmp/conf/docker-daemon.json /etc/docker/daemon.json
