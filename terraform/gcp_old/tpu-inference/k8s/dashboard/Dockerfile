# The health dashboard: a Python program that imports nothing outside the
# standard library, its page templates and its static files, so the interpreter
# is the whole image.
FROM python:3.13-slim

COPY *.py /app/
COPY templates /app/templates
COPY static /app/static
# The Compare page's bare-metal baseline: built once from the pre-migration
# snapshot by baseline/build_baseline.py, which the image does not need.
COPY baseline/premigration-2026-09.json /app/baseline/

# Any unprivileged UID: the program writes nothing to disk.
USER 65532

# Cloud Run supplies PORT.
ENTRYPOINT ["python3", "/app/app.py"]
