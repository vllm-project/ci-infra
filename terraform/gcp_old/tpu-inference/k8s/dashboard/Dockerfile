# The health dashboard: a Python program that imports nothing outside the
# standard library, its page templates and its static files, so the interpreter
# is the whole image.
FROM python:3.13-slim

COPY *.py /app/
COPY templates /app/templates
COPY static /app/static

# Any unprivileged UID: the program writes nothing to disk.
USER 65532

# Cloud Run supplies PORT.
ENTRYPOINT ["python3", "/app/app.py"]
