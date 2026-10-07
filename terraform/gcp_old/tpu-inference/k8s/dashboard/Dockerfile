# The queue dashboard: one Python program that imports nothing outside the
# standard library, so the interpreter is the whole image.
FROM python:3.13-slim

COPY app.py /app/app.py

# Any unprivileged UID: the program writes nothing to disk.
USER 65532

# Cloud Run supplies PORT.
ENTRYPOINT ["python3", "/app/app.py"]
