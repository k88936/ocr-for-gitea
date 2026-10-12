"""gitea-ocr-review: bridge Gitea pull requests with alibaba/open-code-review.

A small, dependency-free command line application that:

1. reads a Gitea "review request" (pull request) through the Gitea API,
2. runs ``ocr review --from <base> --to <head> --format json`` on a local clone,
3. turns the JSON findings into a review verdict, and
4. posts that verdict back to Gitea as an approve / request-changes review.

Authentication is done with a Gitea access token, supplied via CLI flag,
token file, stdin, environment variable or config file (in that order).
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
