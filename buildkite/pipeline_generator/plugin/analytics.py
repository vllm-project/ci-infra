BUILDKITE_ANALYTICS_TOKEN = "BUILDKITE_ANALYTICS_TOKEN"
BUILDKITE_ANALYTICS_SECRET = "buildkite-analytics-token-secret"


def get_buildkite_analytics_token_env():
    """Return a Kubernetes env entry backed by the CI analytics secret."""
    return {
        "name": BUILDKITE_ANALYTICS_TOKEN,
        "valueFrom": {
            "secretKeyRef": {
                "name": BUILDKITE_ANALYTICS_SECRET,
                "key": "token",
            }
        },
    }
