# EKS cluster for GPU CI (migration plan: docs/gpu-queues-eks-migration.md)
#
# Replaces the gpu_1_queue / gpu_4_queue Elastic CI Stack ASGs with one
# autoscaling GPU node pool (g6.12xlarge, 4x L4) behind a single Buildkite
# agent-stack-k8s controller queue (l4-k8s).
#
# FSx note: there are four Lustre filesystems, one per AZ (see Phase 0 audit).
# Nodes mount their AZ-local filesystem at /fsx via user-data (same behavior as
# s3://vllm-ci/bootstrap.sh on the ASGs) and pods consume it through a hostPath
# volume. The FSx CSI driver / static PV design was dropped because a static PV
# cannot select among the four per-AZ filesystems by node AZ.

locals {
  eks_cluster_name = "l4-ci"

  # From the Phase 0 audit (aws fsx describe-file-systems, us-west-2).
  # us-west-2a is the odd one out: 500 MB/s/TiB (others 1000) and missing the
  # ci-model-weights security group — fix in console (filesystems are not
  # managed by this terraform).
  fsx_lustre_by_az = {
    "us-west-2a" = { dns = "fs-0c88a6d0c07e7579b.fsx.us-west-2.amazonaws.com", mount = "7wycbb4v" }
    "us-west-2b" = { dns = "fs-0e20a97e78295dc40.fsx.us-west-2.amazonaws.com", mount = "7szcbb4v" }
    "us-west-2c" = { dns = "fs-0ad82609009496582.fsx.us-west-2.amazonaws.com", mount = "zgzcbb4v" }
    "us-west-2d" = { dns = "fs-0337adaa59ddb88f5.fsx.us-west-2.amazonaws.com", mount = "ygzcbb4v" }
  }
}

module "eks" {
  source  = "terraform-aws-modules/eks/aws"
  # AL2023_x86_64_NVIDIA support (nodeadm user-data) requires module >= 20.23;
  # 20.37.2 is the last 20.x and needs aws provider >= 5.95 (lock is at 5.100.0).
  version = "20.37.2"

  cluster_name    = local.eks_cluster_name
  cluster_version = "1.32"

  cluster_endpoint_public_access           = true
  enable_cluster_creator_admin_permissions = true

  # The CI VPC has public subnets only (same ones the ASGs and FSx ENIs use).
  vpc_id     = module.vpc.vpc_id
  subnet_ids = module.vpc.public_subnets

  cluster_addons = {
    eks-pod-identity-agent = {}
  }

  eks_managed_node_groups = {
    # Controllers, CoreDNS, CSI, cluster-autoscaler, device plugin, Datadog.
    system = {
      instance_types = ["m6i.large"]
      capacity_type  = "ON_DEMAND"
      min_size       = 2
      max_size       = 3
      desired_size   = 2
    }

    # Single GPU pool: 4x L4 per node, serves 1/2/4-GPU pods (bin-packed by
    # resource requests).
    l4x4 = {
      ami_type       = "AL2023_x86_64_NVIDIA"
      instance_types = ["g6.12xlarge"]
      capacity_type  = "ON_DEMAND"
      # 5 warm nodes at all times (fast job pickup, warm FSx clients), CA scales
      # to 30 under load. Note: 5x g6.12xlarge on-demand 24/7 is a real standing
      # cost (~$9.7k/mo) — revisit min_size once Phase 5 has wait-time data.
      min_size     = 5
      max_size     = 30
      desired_size = 5

      labels = {
        "vllm.ci/gpu-pool"              = "l4x4"
        "k8s.amazonaws.com/accelerator" = "nvidia-l4"
      }

      taints = {
        gpu = {
          key    = "nvidia.com/gpu"
          value  = "true"
          effect = "NO_SCHEDULE"
        }
      }

      # 512 GB root, parity with the ASGs (RootVolumeSize=512).
      # Fast-follow (plan Phase 5): move containerd/kubelet onto the 2x940GB
      # instance-store NVMe (nodeadm localStorage RAID0) and shrink this.
      block_device_mappings = {
        xvda = {
          device_name = "/dev/xvda"
          ebs = {
            volume_size           = 512
            volume_type           = "gp3"
            delete_on_termination = true
          }
        }
      }

      # Kubelet pulls private ECR images using the node role (no
      # imagePullSecrets needed).
      iam_role_additional_policies = {
        ecr_readonly = "arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly"
      }

      # Node bootstrap, adapted from s3://vllm-ci/bootstrap.sh:
      # disable algif_aead, install the Lustre client, mount the AZ-local FSx
      # filesystem at /fsx (relatime,flock — flock is required by
      # huggingface_hub's cache locking).
      enable_bootstrap_user_data = true
      cloudinit_pre_nodeadm = [
        {
          content_type = "text/x-shellscript"
          content      = <<-EOT
            #!/bin/bash
            set -euo pipefail

            echo "install algif_aead /bin/false" > /etc/modprobe.d/disable-algif-aead.conf
            rmmod algif_aead 2>/dev/null || true

            dnf install -y lustre-client

            TOKEN=$(curl -sX PUT "http://169.254.169.254/latest/api/token" -H "X-aws-ec2-metadata-token-ttl-seconds: 21600")
            AZ=$(curl -s -H "X-aws-ec2-metadata-token: $TOKEN" http://169.254.169.254/latest/meta-data/placement/availability-zone)

            case "$AZ" in
            %{ for az, fsx in local.fsx_lustre_by_az ~}
              ${az}) FSX_DNS_NAME="${fsx.dns}"; MOUNT_NAME="${fsx.mount}" ;;
            %{ endfor ~}
              *) echo "unknown AZ $AZ" >&2; exit 1 ;;
            esac

            mkdir -p /fsx
            # fstab entry so the mount survives a node reboot.
            echo "$${FSX_DNS_NAME}@tcp:/$${MOUNT_NAME} /fsx lustre relatime,flock,_netdev 0 0" >> /etc/fstab
            mount -t lustre -o relatime,flock $${FSX_DNS_NAME}@tcp:/$${MOUNT_NAME} /fsx
            mkdir -p /fsx/hf_cache
          EOT
        }
      ]

      # Cluster Autoscaler scale-from-zero: managed-node-group tags do NOT
      # propagate to the ASG, so these must be set explicitly. The
      # resources/nvidia.com/gpu tag works around CA modeling a zero-size GPU
      # group as having no GPU capacity.
      autoscaling_group_tags = {
        "k8s.io/cluster-autoscaler/enabled"                                            = "true"
        "k8s.io/cluster-autoscaler/${local.eks_cluster_name}"                          = "owned"
        "k8s.io/cluster-autoscaler/node-template/label/vllm.ci/gpu-pool"               = "l4x4"
        "k8s.io/cluster-autoscaler/node-template/label/k8s.amazonaws.com/accelerator"  = "nvidia-l4"
        "k8s.io/cluster-autoscaler/node-template/taint/nvidia.com/gpu"                 = "true:NoSchedule"
        "k8s.io/cluster-autoscaler/node-template/resources/nvidia.com/gpu"             = "4"
      }
    }
  }
}

# --- Cluster Autoscaler IAM (helm install of CA is a separate step; this gives
# its kube-system/cluster-autoscaler service account AWS permissions via EKS Pod
# Identity) ---

data "aws_iam_policy_document" "cluster_autoscaler_assume" {
  statement {
    actions = ["sts:AssumeRole", "sts:TagSession"]
    principals {
      type        = "Service"
      identifiers = ["pods.eks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "cluster_autoscaler" {
  name               = "${local.eks_cluster_name}-cluster-autoscaler"
  assume_role_policy = data.aws_iam_policy_document.cluster_autoscaler_assume.json
}

data "aws_iam_policy_document" "cluster_autoscaler" {
  statement {
    actions = [
      "autoscaling:DescribeAutoScalingGroups",
      "autoscaling:DescribeAutoScalingInstances",
      "autoscaling:DescribeLaunchConfigurations",
      "autoscaling:DescribeScalingActivities",
      "autoscaling:DescribeTags",
      "ec2:DescribeImages",
      "ec2:DescribeInstanceTypes",
      "ec2:DescribeLaunchTemplateVersions",
      "ec2:GetInstanceTypesFromInstanceRequirements",
      "eks:DescribeNodegroup",
    ]
    resources = ["*"]
  }

  statement {
    actions = [
      "autoscaling:SetDesiredCapacity",
      "autoscaling:TerminateInstanceInAutoScalingGroup",
    ]
    resources = ["*"]
    condition {
      test     = "StringEquals"
      variable = "aws:ResourceTag/k8s.io/cluster-autoscaler/${local.eks_cluster_name}"
      values   = ["owned"]
    }
  }
}

resource "aws_iam_role_policy" "cluster_autoscaler" {
  name   = "cluster-autoscaler"
  role   = aws_iam_role.cluster_autoscaler.id
  policy = data.aws_iam_policy_document.cluster_autoscaler.json
}

resource "aws_eks_pod_identity_association" "cluster_autoscaler" {
  cluster_name    = module.eks.cluster_name
  namespace       = "kube-system"
  service_account = "cluster-autoscaler"
  role_arn        = aws_iam_role.cluster_autoscaler.arn
}
