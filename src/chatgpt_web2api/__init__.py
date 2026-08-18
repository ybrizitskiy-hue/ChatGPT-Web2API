"""ChatGPT-Web2API — OpenAI-compatible proxy through ChatGPT web via CDP."""

__version__ = "0.2.0"

# Install narrow production recoveries before callers import the service
# classes.  The installer is idempotent and only changes behavior for the
# ambiguous Page.navigate timeout and a stalled model-catalog probe.
from .runtime_hotfixes import install_runtime_hotfixes as _install_runtime_hotfixes

_install_runtime_hotfixes()
del _install_runtime_hotfixes
