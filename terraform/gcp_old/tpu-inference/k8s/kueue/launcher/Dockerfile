# The launcher image: the Google Cloud CLI plus PyYAML.
#
# The CLI image for the three things only it ships together - kubectl, gcloud,
# and gke-gcloud-auth-plugin, without which Connect Gateway credentials are
# useless. PyYAML because it is the one thing it does not: the only YAML under
# the SDK root is a Python 2 PyYAML whose imports fail on the image's 3.13.
# Installed here rather than at job time, so PyPI is not in the path of every
# TPU step.
ARG CLOUD_SDK_VERSION
FROM gcr.io/google.com/cloudsdktool/google-cloud-cli:${CLOUD_SDK_VERSION}

# --break-system-packages because Debian marks the interpreter externally
# managed. This is one program's runtime rather than a system to keep
# consistent, and pyyaml is a leaf.
RUN python3 -m pip install --no-cache-dir --break-system-packages pyyaml==6.0.3

# Fails the build, rather than the first TPU step of the day, if the base image
# ever moves the interpreter this installed against.
RUN python3 -c "import yaml; print('PyYAML', yaml.__version__)"
