# The health dashboard: one Python program that imports nothing outside the
# standard library, and the page template it fills, so the interpreter is the
# whole image.
FROM python:3.13-slim

COPY app.py page.html /app/

# Any unprivileged UID: the program writes nothing to disk.
USER 65532

# Cloud Run supplies PORT.
ENTRYPOINT ["python3", "/app/app.py"]
