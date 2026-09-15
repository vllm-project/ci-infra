---
# Puts a Kubernetes credential plugin on the manager controller's filesystem.
#
# MultiKueue reaches the workers through ClusterProfiles, whose access provider
# is "google": the endpoint is a Connect Gateway URL and the credential is a
# Google OAuth token. Kueue mints that token by executing the command named in
# multiKueue.clusterProfile.accessProviders, and the upstream Kueue image
# contains no such binary. This mounts one.
#
# A partial object, applied server-side after the upstream release manifests.
# Field ownership is per list item - by container name, by volume name, by
# mount path - so none of this collides with what upstream sets on the same
# Deployment, and neither apply undoes the other.
apiVersion: apps/v1
kind: Deployment
metadata:
  name: kueue-controller-manager
  namespace: kueue-system
spec:
  template:
    spec:
      initContainers:
        - name: add-gcp-auth-plugin
          image: ${AUTH_PLUGIN_IMAGE}
          command: ["cp", "${AUTH_PLUGIN_SRC}", "/plugins/gcp-auth-plugin"]
          volumeMounts:
            - name: clusterprofile-plugins
              mountPath: /plugins
          securityContext:
            # The pod runs with runAsNonRoot and the gcloud image's default user
            # is root, so a uid has to be named here or the kubelet refuses to
            # start the container. Any uid works: the binary is world readable
            # and an emptyDir is world writable.
            runAsNonRoot: true
            runAsUser: 65532
            allowPrivilegeEscalation: false
            readOnlyRootFilesystem: true
            capabilities:
              drop: ["ALL"]
          resources:
            requests:
              cpu: 100m
              memory: 128Mi
      containers:
        # Named to match the container in the upstream Deployment; this adds a
        # mount to it and leaves every other field alone.
        - name: manager
          volumeMounts:
            - name: clusterprofile-plugins
              mountPath: /plugins
      volumes:
        - name: clusterprofile-plugins
          emptyDir: {}
